"""backfill.py — replay the last N days of DB spans through
the full analysis + persist path, bucketed by night (UTC date).

F1.5: reads spans from kairos-pg (spans table) via list_trace_ids +
fetch_envelope_from_db instead of Phoenix GraphQL.

Idempotent: safe to re-run.  Existing rows are upserted, never duplicated.

Resilient: a per-trace try/except catches transient DB errors; skipped
traces are counted and logged but do NOT abort the whole run.

Usage:
    uv run scripts/backfill.py [--days N] [--dsn DSN] [--context PATH]

Default:
    --days 7
    --dsn   $KAIROS_PG_DSN
    --context     <repo_root>/config/context.yaml

Environment:
    KAIROS_PG_DSN          required — postgres DSN for kairos-pg
    KAIROS_CONTEXT_PATH     optional override for --context
"""

from __future__ import annotations

import argparse
import os
import sys
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
from kairos.loop.persist import persist_night
from kairos.readers.db import fetch_envelope_from_db, list_trace_ids
from kairos.taxonomy.business_context import BusinessContext

DEFAULT_DSN = os.environ.get("KAIROS_PG_DSN", "")
DEFAULT_CONTEXT = os.environ.get(
    "KAIROS_CONTEXT_PATH", str(_REPO / "config" / "context.yaml")
)
DEFAULT_DAYS = 7


# PhoenixReader + GraphQL helpers removed in F1.5.
# Use list_trace_ids + fetch_envelope_from_db (kairos.readers.db) instead.


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
    dsn: str,
    context_path: str,
) -> dict[str, int]:
    """Execute the backfill.  Returns summary counts."""
    print(f"[backfill] Loading context from {context_path} ...", flush=True)
    context = BusinessContext.from_yaml(context_path)

    print("[backfill] Applying migrations ...", flush=True)
    apply_migrations()

    now = datetime.now(tz=UTC)
    start = now - timedelta(hours=days * 24)
    start_iso = start.isoformat().replace("+00:00", "Z")

    print(f"[backfill] Listing trace IDs from DB: last {days}×24h (since {start_iso}) ...", flush=True)
    trace_ids = list_trace_ids(dsn, since=start_iso)
    print(f"[backfill] {len(trace_ids)} trace IDs found in DB.", flush=True)

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

    print(f"[backfill] Fetching {len(trace_ids)} envelopes from DB ...", flush=True)
    envelopes_by_night: dict[date, list[Any]] = defaultdict(list)
    skipped = 0

    for i, tid in enumerate(trace_ids):
        if i % 20 == 0 and i > 0:
            print(f"[backfill]   {i}/{len(trace_ids)} envelopes fetched ({skipped} skipped) ...", flush=True)
        try:
            env = fetch_envelope_from_db(
                tid,
                dsn,
                correlation_key_attr=context.correlation_key,
                enrich_hooks=False,
            )
        except Exception as exc:  # noqa: BLE001 — transient DB errors
            print(f"[backfill]   SKIP {tid[:16]}: {exc}", flush=True)
            skipped += 1
            continue

        if not env.is_valid:
            skipped += 1
            continue

        night = _night_for_trace(env)
        envelopes_by_night[night].append(env)

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

    for night in nights_list:
        night_envs = envelopes_by_night[night]

        print(f"[backfill]   Night {night}: {len(night_envs)} traces ...", flush=True)

        # Agent metadata no longer available from Phoenix root spans (F1.5).
        # agent_by_trace defaults to "unknown"; extracted from envelope attrs where possible.
        agent_by_trace: dict[str, str] = {env.trace_id: "unknown" for env in night_envs}

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
        "--dsn",
        default=DEFAULT_DSN,
        help="Postgres DSN for kairos-pg (default: $KAIROS_PG_DSN)",
    )
    parser.add_argument(
        "--context",
        default=DEFAULT_CONTEXT,
        help="Path to context.yaml (default: %(default)s)",
    )
    args = parser.parse_args()

    if not args.dsn:
        print("ERROR: --dsn or KAIROS_PG_DSN required", file=sys.stderr)
        sys.exit(1)

    context_path = Path(args.context)
    if not context_path.exists():
        print(f"ERROR: context file not found: {context_path}", file=sys.stderr)
        sys.exit(1)

    try:
        run_backfill(
            days=args.days,
            dsn=args.dsn,
            context_path=str(context_path),
        )
    except Exception as exc:
        print(f"ERROR: backfill failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
