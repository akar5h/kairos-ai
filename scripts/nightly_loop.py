"""nightly_loop.py — Deterministic nightly runner for Kairos.

State machine (each transition logs a timestamped line):
  FETCH    — pull Phoenix traces for the last 26h, dedupe vs seen;
             3× retry / 30-minute back-off → skip-marker report + EXIT 0
  ANALYZE  — run_pipeline (outcome + tier-1 + tier-1.5 session-quality);
             0 traces → "quiet night" report (valid)
  ROLLUP   — correlation_key grouping;
             key absent → per-trace mode + note (degrade, don't die)
  LEARN    — per-workflow tool-presence rates → expectation deltas
  PERSIST  — Postgres upserts (findings + rollup);
             DB down → local parquet fallback + WARN, never lose the night
  DISCOVER — anomaly + expectation-miss → discovery_queue (best-effort)
  EMIT     — report file + decision_ledger improvement.suggested rows

Kill switch: KAIROS_LOOP_DISABLED=1 checked FIRST — clean no-op + exit 0.
ANY unexpected exception → traceback to log + skip-marker report.
The night is NEVER silent.

NO LLM calls anywhere.  Loop's own traces excluded via actor_id filter.

Env (names → .env.example):
  KAIROS_LOOP_DISABLED        — kill switch; any non-empty value disables
  KAIROS_CONTEXT_PATH         — path to context.yaml
  KAIROS_PHOENIX_ENDPOINT     — Phoenix base URL (default: http://localhost:6006)
  KAIROS_PHOENIX_PROJECT      — Phoenix project name (default: default)
  KAIROS_PG_DSN               — Postgres DSN for kairos-pg
  LEDGER_API_URL              — Decision Ledger API for EMIT stage (optional)
  KAIROS_LOOP_DATA_DIR        — directory for parquet fallback + report output
                                 (default: <repo_root>/output/loop_data)

Spec ref: docs/sprint-exec-3-loop.md §"Day 12 — Runner"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

# ── path bootstrap ────────────────────────────────────────────────────────────
_HERE = Path(__file__).parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO / "src"))

from kairos.detection.session_quality import (  # noqa: E402
    CURL_T,
    RECOVERY_WINDOW,
    REPEAT_T,
    STRUGGLE_T,
    WTT_T,
    detect_session_quality,
    learn_tool_expectations,
)
from kairos.engine.pipeline import run_pipeline  # noqa: E402
from kairos.log import get_logger, setup_logging  # noqa: E402
from kairos.loop.discover import run_discovery  # noqa: E402
from kairos.loop.persist import persist_night  # noqa: E402
from kairos.readers.phoenix import PhoenixReader  # noqa: E402
from kairos.taxonomy.business_context import BusinessContext  # noqa: E402

# ── Constants ─────────────────────────────────────────────────────────────────

DEFAULT_ENDPOINT = os.environ.get("KAIROS_PHOENIX_ENDPOINT", "http://localhost:6006")
DEFAULT_PROJECT = os.environ.get("KAIROS_PHOENIX_PROJECT", "default")
DEFAULT_CONTEXT = os.environ.get(
    "KAIROS_CONTEXT_PATH", str(_REPO / "config" / "context.yaml")
)
DEFAULT_DATA_DIR = os.environ.get(
    "KAIROS_LOOP_DATA_DIR", str(_REPO / "output" / "loop_data")
)

FETCH_WINDOW_HOURS: int = 26
FETCH_RETRY_COUNT: int = 3
FETCH_RETRY_WAIT_S: float = 5.0   # short wait in tests; launchd nights use 30min
PHOENIX_TIMEOUT: int = 120

# actor_id tag used by loop traces — excluded from analysis.
LOOP_ACTOR_TAG: str = "kairos-loop"

_DETECTOR_THRESHOLDS: dict[str, Any] = {
    "RECOVERY_WINDOW": RECOVERY_WINDOW,
    "STRUGGLE_T": STRUGGLE_T,
    "REPEAT_T": REPEAT_T,
    "CURL_T": CURL_T,
    "WTT_T": WTT_T,
}

setup_logging(
    level=os.environ.get("KAIROS_LOG_LEVEL", "INFO"),
    json_output=os.environ.get("KAIROS_LOG_FORMAT", "json") == "json",
)
logger = get_logger(__name__)


# ── Report / skip-marker helpers ──────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def _write_report(data_dir: Path, report: dict[str, Any]) -> Path:
    """Write a JSON report to data_dir/reports/YYYY-MM-DD_HH-MM-SS.json."""
    reports_dir = data_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H-%M-%S")
    path = reports_dir / f"{ts}.json"
    path.write_text(json.dumps(report, indent=2, default=str))
    return path


def _skip_marker(
    data_dir: Path,
    reason: str,
    exc_tb: str | None = None,
) -> Path:
    """Write a skip-marker report (night was skipped but not silent)."""
    report = {
        "type": "skip_marker",
        "timestamp": _now_iso(),
        "reason": reason,
        "traceback": exc_tb,
    }
    logger.warning("nightly_loop.skip_marker", reason=reason, has_traceback=bool(exc_tb))
    return _write_report(data_dir, report)


def _quiet_night_report(data_dir: Path, night_id: date) -> Path:
    """Write a 'quiet night' report (0 traces is valid)."""
    report = {
        "type": "quiet_night",
        "night_id": str(night_id),
        "timestamp": _now_iso(),
        "message": "0 traces in the fetch window. Valid quiet night.",
    }
    logger.info("nightly_loop.quiet_night", night=str(night_id))
    return _write_report(data_dir, report)


# ── GraphQL helpers (reuse pattern from backfill.py) ─────────────────────────


def _gql(endpoint: str, query: str, timeout: int = PHOENIX_TIMEOUT) -> Any:
    url = endpoint.rstrip("/") + "/graphql"
    body = json.dumps({"query": query}).encode()
    req = urllib.request.Request(  # noqa: S310
        url,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        raw = resp.read()
    parsed = json.loads(raw)
    if "errors" in parsed:
        raise RuntimeError(f"GraphQL errors: {parsed['errors']}")
    return parsed.get("data", {})


def _resolve_project_id(endpoint: str, project_name: str) -> str:
    data = _gql(
        endpoint,
        "{ projects(first: 100) { edges { node { id name } } } }",
    )
    edges = data.get("projects", {}).get("edges", [])
    for edge in edges:
        node = edge.get("node", {})
        if node.get("name") == project_name:
            return node["id"]
    available = [e["node"]["name"] for e in edges]
    raise RuntimeError(
        f"project '{project_name}' not found. Available: {', '.join(available)}"
    )


def _root_span_meta(raw_attributes: str | None) -> dict[str, str | None]:
    """Extract session_id / agent from root span attributes."""
    meta: dict[str, str | None] = {"session_id": None, "agent": None, "actor_id": None}
    if not raw_attributes:
        return meta
    try:
        attrs = json.loads(raw_attributes)
    except (json.JSONDecodeError, TypeError):
        return meta
    if not isinstance(attrs, dict):
        return meta
    session = attrs.get("session")
    if isinstance(session, dict) and session.get("id"):
        meta["session_id"] = str(session["id"])
    service = attrs.get("service")
    if isinstance(service, dict) and service.get("name"):
        meta["agent"] = str(service["name"])
    paperclip = attrs.get("paperclip")
    if isinstance(paperclip, dict) and meta["agent"] is None and paperclip.get("agent_id"):
        meta["agent"] = str(paperclip["agent_id"])
    # actor_id: used to filter loop's own traces
    actor_id = attrs.get("actor_id")
    if isinstance(actor_id, str):
        meta["actor_id"] = actor_id
    return meta


def _fetch_root_trace_ids(
    endpoint: str,
    project_id: str,
    start_iso: str,
    end_iso: str,
) -> tuple[list[str], dict[str, dict[str, str | None]]]:
    """Paginate root spans; return (trace_ids, meta_by_trace).

    Filters out traces tagged actor_id=kairos-loop (loop's own activity).
    """
    trace_ids: list[str] = []
    meta_by_trace: dict[str, dict[str, str | None]] = {}
    seen: set[str] = set()
    cursor: str | None = None
    loop_excluded = 0

    while True:
        after_clause = f', after: "{cursor}"' if cursor is not None else ""
        query = (
            f'{{ node(id: "{project_id}") {{ ... on Project {{ '
            f'spans(first: 100{after_clause}, rootSpansOnly: true, '
            f'timeRange: {{start: "{start_iso}", end: "{end_iso}"}}) {{ '
            f'pageInfo {{ hasNextPage endCursor }} '
            f'edges {{ node {{ context {{ traceId }} attributes }} }} '
            f'}} }} }} }}'
        )
        data = _gql(endpoint, query)
        spans_data = data.get("node", {}).get("spans")
        if not spans_data:
            break

        for edge in spans_data.get("edges", []):
            node = edge.get("node", {})
            tid = node.get("context", {}).get("traceId", "")
            if not tid or tid in seen:
                continue
            meta = _root_span_meta(node.get("attributes"))
            # Exclude loop's own traces.
            if meta.get("actor_id") == LOOP_ACTOR_TAG:
                loop_excluded += 1
                continue
            seen.add(tid)
            trace_ids.append(tid)
            meta_by_trace[tid] = meta

        page_info = spans_data.get("pageInfo", {})
        if not page_info.get("hasNextPage") or not page_info.get("endCursor"):
            break
        cursor = page_info["endCursor"]

    if loop_excluded:
        logger.info("fetch.loop_traces_excluded", count=loop_excluded)

    return trace_ids, meta_by_trace


def _load_seen_trace_ids(data_dir: Path) -> set[str]:
    """Load the set of trace IDs already processed in prior runs."""
    seen_path = data_dir / "seen_trace_ids.json"
    if not seen_path.exists():
        return set()
    try:
        return set(json.loads(seen_path.read_text()))
    except Exception:  # noqa: BLE001
        logger.warning("seen_ids.load_failed", path=str(seen_path))
        return set()


def _save_seen_trace_ids(data_dir: Path, ids: set[str]) -> None:
    """Persist the seen trace IDs set."""
    seen_path = data_dir / "seen_trace_ids.json"
    seen_path.parent.mkdir(parents=True, exist_ok=True)
    seen_path.write_text(json.dumps(sorted(ids)))


def _night_for_trace(envelope: Any) -> date:
    if envelope.started_at is not None:
        return envelope.started_at.astimezone(UTC).date()
    return datetime.now(tz=UTC).date()


# ── Parquet fallback ──────────────────────────────────────────────────────────


def _fallback_parquet(data_dir: Path, night_id: date, result: Any, envelopes: list[Any]) -> Path:
    """Write a parquet fallback when Postgres is unavailable.

    Stores a minimal row per trace: trace_id, night_id, step_count, total_tokens.
    The full analysis result is serialised to JSON alongside it.
    """
    fallback_dir = data_dir / "parquet_fallback"
    fallback_dir.mkdir(parents=True, exist_ok=True)

    # JSON full dump (result metadata).
    meta_path = fallback_dir / f"{night_id}.json"
    meta = {
        "night_id": str(night_id),
        "trace_count": len(envelopes),
        "unit_count": len(result.unit_summaries),
        "workflow_count": len(result.workflows),
    }
    meta_path.write_text(json.dumps(meta, indent=2))

    # Parquet: per-trace minimal rows.
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq

        rows = [
            {
                "night_id": str(night_id),
                "trace_id": e.trace_id,
                "step_count": e.step_count,
                "total_tokens": e.total_tokens,
                "total_latency_ms": e.total_latency_ms,
            }
            for e in envelopes
        ]
        if rows:
            table = pa.table({k: [r[k] for r in rows] for k in rows[0]})
            pq_path = fallback_dir / f"{night_id}.parquet"
            pq.write_table(table, str(pq_path))
            logger.warning(
                "persist.db_down_parquet_fallback",
                night=str(night_id),
                parquet_path=str(pq_path),
                meta_path=str(meta_path),
                trace_count=len(rows),
            )
            return pq_path
    except ImportError:
        logger.warning(
            "persist.parquet_unavailable",
            night=str(night_id),
            meta_path=str(meta_path),
        )
    return meta_path


# ── Decision ledger emit ──────────────────────────────────────────────────────


def _emit_ledger_rows(
    ledger_url: str | None,
    night_id: date,
    report: dict[str, Any],
) -> None:
    """POST improvement.suggested rows to the Decision Ledger (best-effort)."""
    if not ledger_url:
        return
    try:
        rows = []
        for ws in report.get("workflows", []):
            if ws.get("finding_count", 0) > 0:
                rows.append({
                    "kind": "improvement.suggested",
                    "night_id": str(night_id),
                    "workflow": ws.get("workflow"),
                    "finding_count": ws.get("finding_count"),
                    "outcome_rate": ws.get("outcome_rate"),
                    "source": "kairos-loop",
                })
        if not rows:
            return
        body = json.dumps({"rows": rows}).encode()
        req = urllib.request.Request(  # noqa: S310
            ledger_url.rstrip("/") + "/api/ledger/entries/batch",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)  # noqa: S310
        logger.info("emit.ledger_rows", count=len(rows), night=str(night_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("emit.ledger_failed", error=str(exc), night=str(night_id))


# ── State machine ─────────────────────────────────────────────────────────────


class LoopState:
    FETCH = "FETCH"
    ANALYZE = "ANALYZE"
    ROLLUP = "ROLLUP"
    LEARN = "LEARN"
    PERSIST = "PERSIST"
    DISCOVER = "DISCOVER"
    EMIT = "EMIT"
    DONE = "DONE"


def _log_transition(state: str) -> None:
    logger.info("nightly_loop.transition", state=state, ts=_now_iso())


def run_nightly_loop(
    *,
    endpoint: str = DEFAULT_ENDPOINT,
    project: str = DEFAULT_PROJECT,
    context_path: str = DEFAULT_CONTEXT,
    data_dir_path: str = DEFAULT_DATA_DIR,
    ledger_url: str | None = None,
    retry_wait_s: float = FETCH_RETRY_WAIT_S,
    # Injected dependencies for testing.
    _force_exception: Exception | None = None,
    _pg_conn: Any | None = None,
) -> dict[str, Any]:
    """Execute the nightly loop state machine.

    Returns a summary dict.  Always exits cleanly (skip-marker on any error).

    Kill switch: KAIROS_LOOP_DISABLED=1 → log + return immediately.
    """
    data_dir = Path(data_dir_path)
    data_dir.mkdir(parents=True, exist_ok=True)

    # ── Kill switch check (FIRST) ─────────────────────────────────────────────
    if os.environ.get("KAIROS_LOOP_DISABLED", "").strip():
        logger.info("nightly_loop.kill_switch_active", reason="KAIROS_LOOP_DISABLED is set")
        return {"status": "disabled", "reason": "KAIROS_LOOP_DISABLED is set"}

    night_id = datetime.now(tz=UTC).date()

    try:
        # Inject forced exception for testing (inside try so skip-marker fires).
        if _force_exception is not None:
            raise _force_exception

        # ── FETCH ─────────────────────────────────────────────────────────────
        _log_transition(LoopState.FETCH)

        context = BusinessContext.from_yaml(context_path)
        seen_ids = _load_seen_trace_ids(data_dir)

        # Retry loop.
        trace_ids: list[str] = []
        meta_by_trace: dict[str, dict[str, str | None]] = {}
        fetch_error: str | None = None

        for attempt in range(1, FETCH_RETRY_COUNT + 1):
            try:
                now = datetime.now(tz=UTC)
                start = now - timedelta(hours=FETCH_WINDOW_HOURS)
                start_iso = start.isoformat().replace("+00:00", "Z")
                end_iso = now.isoformat().replace("+00:00", "Z")

                project_id = _resolve_project_id(endpoint, project)
                raw_ids, meta_by_trace = _fetch_root_trace_ids(
                    endpoint, project_id, start_iso, end_iso
                )
                # Dedupe vs seen.
                new_ids = [tid for tid in raw_ids if tid not in seen_ids]
                logger.info(
                    "fetch.deduped",
                    raw=len(raw_ids),
                    new=len(new_ids),
                    seen=len(seen_ids),
                )
                trace_ids = new_ids
                fetch_error = None
                break
            except Exception as exc:  # noqa: BLE001
                fetch_error = str(exc)
                logger.warning(
                    "fetch.retry",
                    attempt=attempt,
                    max_attempts=FETCH_RETRY_COUNT,
                    error=fetch_error,
                )
                if attempt < FETCH_RETRY_COUNT:
                    time.sleep(retry_wait_s)

        if fetch_error is not None:
            path = _skip_marker(
                data_dir,
                reason=f"FETCH failed after {FETCH_RETRY_COUNT} attempts: {fetch_error}",
            )
            logger.error("nightly_loop.fetch_failed", skip_marker=str(path))
            return {"status": "skip", "stage": LoopState.FETCH, "report_path": str(path)}

        # Quiet night: 0 traces is valid.
        if not trace_ids:
            path = _quiet_night_report(data_dir, night_id)
            return {"status": "quiet_night", "night_id": str(night_id), "report_path": str(path)}

        # Fetch envelopes.
        reader = PhoenixReader(endpoint=endpoint, project=project)
        envelopes_by_night: dict[date, list[Any]] = defaultdict(list)
        meta_by_night: dict[date, dict[str, dict[str, str | None]]] = defaultdict(dict)
        fetch_skipped = 0

        for tid in trace_ids:
            try:
                env = reader.fetch_envelope(
                    tid,
                    correlation_key_attr=context.correlation_key,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("fetch.envelope_skip", trace_id=tid[:16], error=str(exc))
                fetch_skipped += 1
                continue
            if not env.is_valid:
                fetch_skipped += 1
                continue
            night = _night_for_trace(env)
            envelopes_by_night[night].append(env)
            meta_by_night[night][tid] = meta_by_trace.get(tid, {})

        fetched_total = sum(len(v) for v in envelopes_by_night.values())
        logger.info(
            "fetch.envelopes_ready",
            fetched=fetched_total,
            skipped=fetch_skipped,
            nights=len(envelopes_by_night),
        )

        # ── ANALYZE ───────────────────────────────────────────────────────────
        _log_transition(LoopState.ANALYZE)

        all_results: dict[date, Any] = {}
        all_envelopes: dict[date, list[Any]] = dict(envelopes_by_night)

        for night, night_envs in sorted(all_envelopes.items()):
            try:
                result = run_pipeline(night_envs, context)

                # Tier-1.5: session-quality detectors per workflow cohort.
                for ws in result.workflows:
                    op = next(
                        (o for o in context.operations if o.name == ws.operation_name),
                        None,
                    )
                    sq_findings = detect_session_quality(ws.member_envelopes, operation=op)
                    workflow_trace_ids = {e.trace_id for e in ws.member_envelopes}
                    for us in result.unit_summaries:
                        unit_trace_ids = set(us.trace_ids)
                        if unit_trace_ids & workflow_trace_ids:
                            for f in sq_findings:
                                if f.trace_id in unit_trace_ids:
                                    us.unit_findings.append(f)

                all_results[night] = result
                logger.info(
                    "analyze.done",
                    night=str(night),
                    traces=len(night_envs),
                    units=len(result.unit_summaries),
                    workflows=len(result.workflows),
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("analyze.night_failed", night=str(night), error=str(exc))
                # Continue with other nights.

        # ── ROLLUP ────────────────────────────────────────────────────────────
        _log_transition(LoopState.ROLLUP)

        correlation_key_used = context.correlation_key
        if correlation_key_used is None:
            logger.info(
                "rollup.per_trace_mode",
                note="correlation_key not configured; units == traces (backward-compat)",
            )

        # (Rollup is already embedded in run_pipeline via rollup_units.)

        # ── LEARN ─────────────────────────────────────────────────────────────
        _log_transition(LoopState.LEARN)

        all_miss_candidates: list[Any] = []
        for night, result in all_results.items():
            night_envs = all_envelopes.get(night, [])
            for ws in result.workflows:
                op = next(
                    (o for o in context.operations if o.name == ws.operation_name),
                    None,
                )
                if op is None:
                    continue
                learn_result = learn_tool_expectations(ws.member_envelopes, op)
                if learn_result.abstained:
                    logger.info(
                        "learn.abstained",
                        workflow=ws.operation_name,
                        reason=learn_result.abstain_reason,
                    )
                else:
                    all_miss_candidates.extend(learn_result.candidates)
                    logger.info(
                        "learn.candidates",
                        workflow=ws.operation_name,
                        clean_n=learn_result.clean_trace_count,
                        candidates=len(learn_result.candidates),
                    )

        # ── PERSIST ───────────────────────────────────────────────────────────
        _log_transition(LoopState.PERSIST)

        persist_summary: dict[str, int] = {"findings_rows": 0, "rollup_rows": 0}
        db_down = False

        for night, result in all_results.items():
            night_envs = all_envelopes.get(night, [])
            night_meta = meta_by_night.get(night, {})
            agent_by_trace = {
                tid: (m.get("agent") or "unknown")
                for tid, m in night_meta.items()
            }

            try:
                conn = _pg_conn  # None → persist_night opens its own
                counts = persist_night(
                    night_id=night,
                    result=result,
                    envelopes=night_envs,
                    agent_by_trace=agent_by_trace,
                    context=context,
                    detector_thresholds=_DETECTOR_THRESHOLDS,
                    conn=conn,
                )
                persist_summary["findings_rows"] += counts["findings_rows"]
                persist_summary["rollup_rows"] += counts["rollup_rows"]
                logger.info("persist.done", night=str(night), counts=counts)
            except Exception as exc:  # noqa: BLE001
                db_down = True
                logger.warning(
                    "persist.db_error",
                    night=str(night),
                    error=str(exc),
                    action="parquet_fallback",
                )
                _fallback_parquet(data_dir, night, result, night_envs)

        # ── DISCOVER ──────────────────────────────────────────────────────────
        _log_transition(LoopState.DISCOVER)

        discovery_result = None
        try:
            # Flatten all envelopes for discovery.
            all_envs_flat = [e for evs in all_envelopes.values() for e in evs]
            json_path = data_dir / "discovery_queue.json"

            # Open PG connection for discovery (best-effort).
            disc_conn: Any | None = _pg_conn
            if disc_conn is None and not db_down:
                try:
                    from kairos.loop.db import get_connection  # noqa: PLC0415
                    disc_conn = get_connection()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("discover.pg_connect_failed", error=str(exc))

            discovery_result = run_discovery(
                traces=all_envs_flat,
                miss_candidates=all_miss_candidates,
                night_id=night_id,
                conn=disc_conn,
                json_output_path=json_path,
            )
            logger.info(
                "discover.done",
                anomaly_count=discovery_result.anomaly_count,
                em_count=discovery_result.expectation_miss_count,
                dropped=discovery_result.dropped_by_cap,
                pg_rows=discovery_result.pg_rows_upserted,
            )

            if disc_conn is not None and _pg_conn is None:
                import contextlib  # noqa: PLC0415
                with contextlib.suppress(Exception):
                    disc_conn.close()
        except Exception as exc:  # noqa: BLE001
            # DISCOVER is best-effort — don't abort the night.
            logger.warning("discover.failed", error=str(exc))

        # ── EMIT ──────────────────────────────────────────────────────────────
        _log_transition(LoopState.EMIT)

        # Build the nightly report.
        workflow_summaries = []
        for night, result in all_results.items():
            for ws in result.workflows:
                # Finding count for this workflow from unit_summaries.
                finding_count = sum(
                    len(us.unit_findings)
                    for us in result.unit_summaries
                    if any(
                        tid in {e.trace_id for e in ws.member_envelopes}
                        for tid in us.trace_ids
                    )
                )
                workflow_summaries.append({
                    "night": str(night),
                    "workflow": ws.operation_name,
                    "full_traces": ws.full_trace_count,
                    "attempted_traces": ws.attempted_trace_count,
                    "outcome_rate": getattr(ws.outcome, "outcome_rate", None),
                    "finding_count": finding_count,
                })

        report: dict[str, Any] = {
            "type": "nightly_report",
            "night_id": str(night_id),
            "timestamp": _now_iso(),
            "fetch": {
                "trace_ids_new": len(trace_ids),
                "envelopes_fetched": fetched_total,
                "envelopes_skipped": fetch_skipped,
            },
            "analyze": {"nights_processed": len(all_results)},
            "persist": {
                **persist_summary,
                "db_down": db_down,
            },
            "discover": {
                "anomaly_count": discovery_result.anomaly_count if discovery_result else 0,
                "expectation_miss_count": discovery_result.expectation_miss_count if discovery_result else 0,
                "dropped_by_cap": discovery_result.dropped_by_cap if discovery_result else 0,
            },
            "learn": {"miss_candidates": len(all_miss_candidates)},
            "workflows": workflow_summaries,
            "correlation_key": correlation_key_used,
        }

        report_path = _write_report(data_dir, report)
        logger.info("emit.report_written", path=str(report_path), night=str(night_id))

        # Decision ledger rows.
        ledger_api = ledger_url or os.environ.get("LEDGER_API_URL", "")
        _emit_ledger_rows(ledger_api or None, night_id, report)

        # Update seen IDs.
        new_seen = seen_ids | set(trace_ids)
        _save_seen_trace_ids(data_dir, new_seen)

        _log_transition(LoopState.DONE)
        return {
            "status": "ok",
            "night_id": str(night_id),
            "report_path": str(report_path),
            **persist_summary,
            "discovery_anomalies": discovery_result.anomaly_count if discovery_result else 0,
        }

    except Exception as exc:  # noqa: BLE001
        tb = traceback.format_exc()
        logger.error("nightly_loop.exception", error=str(exc), traceback=tb)
        path = _skip_marker(data_dir, reason=f"Unhandled exception: {exc}", exc_tb=tb)
        return {"status": "skip", "stage": "unknown", "report_path": str(path)}


# ── CLI entry point ───────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="Phoenix base URL")
    parser.add_argument("--project", default=DEFAULT_PROJECT, help="Phoenix project name")
    parser.add_argument("--context", default=DEFAULT_CONTEXT, help="Path to context.yaml")
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help="Loop data directory")
    parser.add_argument("--ledger-url", default=None, help="Decision Ledger API URL")
    parser.add_argument(
        "--retry-wait",
        type=float,
        default=FETCH_RETRY_WAIT_S,
        help="Seconds between fetch retries (default: 5)",
    )
    args = parser.parse_args()

    context_path = Path(args.context)
    if not context_path.exists():
        print(f"ERROR: context file not found: {context_path}", file=sys.stderr)  # noqa: T201
        sys.exit(1)

    result = run_nightly_loop(
        endpoint=args.endpoint,
        project=args.project,
        context_path=str(context_path),
        data_dir_path=args.data_dir,
        ledger_url=args.ledger_url,
        retry_wait_s=args.retry_wait,
    )

    status = result.get("status", "unknown")
    print(json.dumps(result, indent=2, default=str))  # noqa: T201

    if status in ("ok", "quiet_night", "disabled"):
        sys.exit(0)
    else:
        # skip_marker: still exit 0 (don't crash launchd).
        sys.exit(0)


if __name__ == "__main__":
    main()
