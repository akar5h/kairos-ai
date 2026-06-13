"""backfill.py — replay the last N days of live Phoenix traces through
the full analysis + persist path, bucketed by night (UTC date).

Idempotent: safe to re-run.  Existing rows are upserted, never duplicated.

Resilient: a per-trace try/except catches transient Phoenix timeouts; skipped
traces are counted and logged but do NOT abort the whole run.

Usage:
    uv run scripts/backfill.py [--days N] [--endpoint URL] [--project NAME]
        [--context PATH] [--endpoint-timeout S]

Default:
    --days 7      (last 7 × 24h)
    --endpoint    http://localhost:6006
    --project     default
    --context     <repo_root>/config/context.yaml

Environment:
    KAIROS_PG_DSN          required — postgres DSN for kairos-pg
    KAIROS_PHOENIX_ENDPOINT optional override for --endpoint
    KAIROS_PHOENIX_PROJECT  optional override for --project
    KAIROS_CONTEXT_PATH     optional override for --context

Agent derivation:
    Root span meta carries ``paperclip.agent_id`` (preferred) then
    ``service.name``.  _root_span_meta() from export_spotcheck.py returns
    {"session_id", "issue", "agent"} where "agent" is the service.name value.
    For Paperclip traces ``service.name`` is the agent identity
    (e.g. "claudecoder", "cto", "qaengineer").
"""

from __future__ import annotations

import argparse
import json
import os
import sys
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

from kairos.detection.session_quality import (
    CURL_T,
    RECOVERY_WINDOW,
    REPEAT_T,
    STRUGGLE_T,
    WTT_T,
    detect_session_quality,
)
from kairos.engine.pipeline import run_pipeline
from kairos.loop.db import apply_migrations
from kairos.loop.persist import compute_config_hash, persist_night
from kairos.readers.phoenix import PhoenixReader
from kairos.taxonomy.business_context import BusinessContext

DEFAULT_ENDPOINT = os.environ.get("KAIROS_PHOENIX_ENDPOINT", "http://localhost:6006")
DEFAULT_PROJECT = os.environ.get("KAIROS_PHOENIX_PROJECT", "default")
DEFAULT_CONTEXT = os.environ.get(
    "KAIROS_CONTEXT_PATH", str(_REPO / "config" / "context.yaml")
)
DEFAULT_DAYS = 7
DEFAULT_TIMEOUT = 120  # seconds per Phoenix request


# ── Phoenix GraphQL helpers (reuse pattern from export_spotcheck.py) ──────────


def _gql(endpoint: str, query: str, timeout: int = DEFAULT_TIMEOUT) -> Any:
    url = endpoint.rstrip("/") + "/graphql"
    body = json.dumps({"query": query}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    parsed = json.loads(raw)
    if "errors" in parsed:
        raise RuntimeError(f"GraphQL errors: {parsed['errors']}")
    return parsed.get("data", {})


def _resolve_project_id(endpoint: str, project_name: str, timeout: int) -> str:
    data = _gql(
        endpoint,
        "{ projects(first: 100) { edges { node { id name } } } }",
        timeout=timeout,
    )
    edges = data.get("projects", {}).get("edges", [])
    for edge in edges:
        node = edge.get("node", {})
        if node.get("name") == project_name:
            return node["id"]
    available = [e["node"]["name"] for e in edges]
    raise RuntimeError(
        f"project '{project_name}' not found in Phoenix. "
        f"Available: {', '.join(available)}"
    )


def _root_span_meta(raw_attributes: str | None) -> dict[str, str | None]:
    """Extract session_id / paperclip issue / agent from root span attributes.

    Agent derivation (priority order):
      1. ``service.name``  (e.g. "paperclip-claude-cto", "paperclip-claude-coder") —
         the human-readable agent class name matching the spec's claudecoder/cto/qaengineer
         examples.  Present on all Paperclip OTel spans.
      2. ``paperclip.agent_id`` — UUID instance identifier; used as fallback when
         service.name is absent (non-Paperclip traces).

    Live-verified 2026-06-13: service.name = "paperclip-claude-cto" is the correct
    agent class signal.  paperclip.agent_id is a UUID that identifies the agent
    *instance*, not the class, so it cannot be used for grouping by agent type.
    """
    meta: dict[str, str | None] = {
        "session_id": None,
        "issue": None,
        "agent": None,
    }
    if not raw_attributes:
        return meta
    try:
        attrs = json.loads(raw_attributes)
    except (json.JSONDecodeError, TypeError):
        return meta
    if not isinstance(attrs, dict):
        return meta

    # session.id
    session = attrs.get("session")
    if isinstance(session, dict) and session.get("id"):
        meta["session_id"] = str(session["id"])

    # service.name → agent class (preferred: human-readable, matchable to agent types).
    service = attrs.get("service")
    if isinstance(service, dict) and service.get("name"):
        meta["agent"] = str(service["name"])

    # paperclip.issue + paperclip.agent_id as fallback when service.name absent.
    paperclip = attrs.get("paperclip")
    if isinstance(paperclip, dict):
        if paperclip.get("issue"):
            meta["issue"] = str(paperclip["issue"])
        if meta["agent"] is None and paperclip.get("agent_id"):
            meta["agent"] = str(paperclip["agent_id"])

    return meta


def _fetch_root_trace_ids(
    endpoint: str,
    project_id: str,
    start_iso: str,
    end_iso: str,
    timeout: int,
) -> tuple[list[str], dict[str, dict[str, str | None]]]:
    """Paginate root spans for [start_iso, end_iso]; return (trace_ids, meta_by_trace)."""
    trace_ids: list[str] = []
    meta_by_trace: dict[str, dict[str, str | None]] = {}
    seen: set[str] = set()
    cursor: str | None = None

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
        data = _gql(endpoint, query, timeout=timeout)
        spans_data = data.get("node", {}).get("spans")
        if not spans_data:
            break

        for edge in spans_data.get("edges", []):
            node = edge.get("node", {})
            tid = node.get("context", {}).get("traceId", "")
            if tid and tid not in seen:
                seen.add(tid)
                trace_ids.append(tid)
                meta_by_trace[tid] = _root_span_meta(node.get("attributes"))

        page_info = spans_data.get("pageInfo", {})
        if not page_info.get("hasNextPage") or not page_info.get("endCursor"):
            break
        cursor = page_info["endCursor"]

    return trace_ids, meta_by_trace


# ── Night bucketing ───────────────────────────────────────────────────────────


def _night_for_trace(envelope: Any) -> date:
    """Return the UTC date (night) for a TraceEnvelope based on started_at.

    Falls back to today when started_at is None (shouldn't happen on live data).
    """
    if envelope.started_at is not None:
        return envelope.started_at.astimezone(UTC).date()
    return datetime.now(tz=UTC).date()


# ── Session-quality detector thresholds (for config_hash) ─────────────────────

_DETECTOR_THRESHOLDS: dict[str, Any] = {
    "RECOVERY_WINDOW": RECOVERY_WINDOW,
    "STRUGGLE_T": STRUGGLE_T,
    "REPEAT_T": REPEAT_T,
    "CURL_T": CURL_T,
    "WTT_T": WTT_T,
}


# ── Backfill main ─────────────────────────────────────────────────────────────


def run_backfill(
    *,
    days: int,
    endpoint: str,
    project: str,
    context_path: str,
    timeout: int,
) -> dict[str, int]:
    """Execute the backfill.  Returns summary counts."""
    print(f"[backfill] Loading context from {context_path} ...", flush=True)
    context = BusinessContext.from_yaml(context_path)

    print(f"[backfill] Applying migrations ...", flush=True)
    apply_migrations()

    print(f"[backfill] Resolving Phoenix project '{project}' at {endpoint} ...", flush=True)
    project_id = _resolve_project_id(endpoint, project, timeout)

    now = datetime.now(tz=UTC)
    start = now - timedelta(hours=days * 24)
    start_iso = start.isoformat().replace("+00:00", "Z")
    end_iso = now.isoformat().replace("+00:00", "Z")

    print(f"[backfill] Fetching root trace IDs: last {days}×24h ({start_iso} → {end_iso}) ...", flush=True)
    trace_ids, meta_by_trace = _fetch_root_trace_ids(
        endpoint, project_id, start_iso, end_iso, timeout
    )
    print(f"[backfill] {len(trace_ids)} root trace IDs found.", flush=True)

    if not trace_ids:
        print("[backfill] No traces found. Nothing to backfill.", flush=True)
        return {
            "nights": 0,
            "traces_attempted": 0,
            "traces_skipped": 0,
            "traces_persisted": 0,
            "findings_rows": 0,
            "rollup_rows": 0,
            "units": 0,
        }

    # Fetch envelopes — resilient per-trace.
    reader = PhoenixReader(
        endpoint=endpoint,
        project=project,
    )

    print(f"[backfill] Fetching {len(trace_ids)} envelopes from Phoenix ...", flush=True)
    envelopes_by_night: dict[date, list[Any]] = defaultdict(list)
    meta_by_night_trace: dict[date, dict[str, dict[str, str | None]]] = defaultdict(dict)
    skipped = 0

    for i, tid in enumerate(trace_ids):
        if i % 20 == 0 and i > 0:
            print(f"[backfill]   {i}/{len(trace_ids)} envelopes fetched ({skipped} skipped) ...", flush=True)
        try:
            env = reader.fetch_envelope(
                tid,
                correlation_key_attr=context.correlation_key,
            )
        except Exception as exc:  # noqa: BLE001 — transient timeout / network errors
            print(f"[backfill]   SKIP {tid[:16]}: {exc}", flush=True)
            skipped += 1
            continue

        if not env.is_valid:
            skipped += 1
            continue

        night = _night_for_trace(env)
        envelopes_by_night[night].append(env)
        meta_by_night_trace[night][tid] = meta_by_trace.get(tid, {})

    traces_fetched = sum(len(v) for v in envelopes_by_night.values())
    nights_list = sorted(envelopes_by_night.keys())
    print(
        f"[backfill] {traces_fetched} envelopes across {len(nights_list)} nights "
        f"({skipped} skipped).",
        flush=True,
    )

    # ── Per-night: run_pipeline → detect_session_quality → persist ──────────────
    total_findings = 0
    total_rollup = 0
    total_units = 0

    cfg_hash = compute_config_hash(context, _DETECTOR_THRESHOLDS)

    for night in nights_list:
        night_envs = envelopes_by_night[night]
        night_meta = meta_by_night_trace[night]

        print(f"[backfill]   Night {night}: {len(night_envs)} traces ...", flush=True)

        # Build agent lookup for this night's traces.
        agent_by_trace: dict[str, str] = {
            tid: (meta.get("agent") or "unknown")
            for tid, meta in night_meta.items()
        }

        try:
            # Run the full pipeline.
            result = run_pipeline(night_envs, context)
        except Exception as exc:  # noqa: BLE001
            print(f"[backfill]     pipeline error for night {night}: {exc}", flush=True)
            continue

        # Run session-quality detectors (tier 1.5) per workflow cohort and
        # merge findings back into the pipeline result's unit_summaries.
        # detect_session_quality returns a flat list; attach findings to traces
        # via their unit_summaries.unit_findings union.
        for ws in result.workflows:
            op = next(
                (o for o in context.operations if o.name == ws.operation_name),
                None,
            )
            sq_findings = detect_session_quality(
                ws.member_envelopes,
                operation=op,
            )
            # Append session-quality findings to each matching unit_summary.
            # Build a set of trace_ids for this workflow's members.
            workflow_trace_ids = {e.trace_id for e in ws.member_envelopes}
            for us in result.unit_summaries:
                unit_trace_ids = set(us.trace_ids)
                if unit_trace_ids & workflow_trace_ids:
                    for f in sq_findings:
                        if f.trace_id in unit_trace_ids:
                            us.unit_findings.append(f)

        try:
            counts = persist_night(
                night_id=night,
                result=result,
                envelopes=night_envs,
                agent_by_trace=agent_by_trace,
                context=context,
                detector_thresholds=_DETECTOR_THRESHOLDS,
                conn=None,  # open from env
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[backfill]     persist error for night {night}: {exc}", flush=True)
            continue

        total_findings += counts["findings_rows"]
        total_rollup += counts["rollup_rows"]
        total_units += len(result.unit_summaries)

        print(
            f"[backfill]     persisted: {counts['findings_rows']} findings rows, "
            f"{counts['rollup_rows']} rollup rows, "
            f"{len(result.unit_summaries)} units.",
            flush=True,
        )

    summary = {
        "nights": len(nights_list),
        "traces_attempted": len(trace_ids),
        "traces_skipped": skipped,
        "traces_persisted": traces_fetched,
        "findings_rows": total_findings,
        "rollup_rows": total_rollup,
        "units": total_units,
    }

    print(
        f"\n[backfill] DONE — "
        f"nights={summary['nights']}, "
        f"traces_persisted={summary['traces_persisted']}, "
        f"traces_skipped={summary['traces_skipped']}, "
        f"findings_rows={summary['findings_rows']}, "
        f"rollup_rows={summary['rollup_rows']}, "
        f"units={summary['units']}",
        flush=True,
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS,
        help="Number of days to backfill (default: %(default)s)",
    )
    parser.add_argument(
        "--endpoint",
        default=DEFAULT_ENDPOINT,
        help="Phoenix base URL (default: %(default)s)",
    )
    parser.add_argument(
        "--project",
        default=DEFAULT_PROJECT,
        help="Phoenix project name (default: %(default)s)",
    )
    parser.add_argument(
        "--context",
        default=DEFAULT_CONTEXT,
        help="Path to context.yaml (default: %(default)s)",
    )
    parser.add_argument(
        "--endpoint-timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        dest="timeout",
        help="Per-request timeout in seconds for Phoenix calls (default: %(default)s)",
    )
    args = parser.parse_args()

    context_path = Path(args.context)
    if not context_path.exists():
        print(f"ERROR: context file not found: {context_path}", file=sys.stderr)
        sys.exit(1)

    try:
        run_backfill(
            days=args.days,
            endpoint=args.endpoint,
            project=args.project,
            context_path=str(context_path),
            timeout=args.timeout,
        )
    except Exception as exc:
        print(f"ERROR: backfill failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
