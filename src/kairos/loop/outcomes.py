"""outcomes.py — Load per-trace outcome labels from the eval_sets table.

Used by discover.py (P4.0) to surface known-fail traces that don't trigger
any structural anomaly detector — the semantic-miss gap.

Schema note: eval_sets.held_in entries are {"trace_id": ..., "features": {...}}
(no outcome_truth field — held_in members ARE the cluster's bad examples).
eval_sets.held_out entries carry {"trace_id": ..., "outcome_truth": "pass"|"unknown", ...}.
"""

from __future__ import annotations

import psycopg
from psycopg.rows import tuple_row

from kairos.log import get_logger

logger = get_logger(__name__)


def load_outcome_labels(dsn: str) -> dict[str, str]:
    """Return trace_id → outcome_truth for all labeled traces in eval_sets.

    Derivation:
    - held_in trace_ids → "fail" (they are the cluster's confirmed-bad members)
    - held_out entries with outcome_truth="pass" → "pass"
    - held_out entries with outcome_truth="unknown" → skipped

    Later eval_set rows win on conflict (ORDER BY frozen_at ASC means newest
    row is processed last and wins — deterministic for v0).
    """
    outcomes: dict[str, str] = {}
    try:
        with psycopg.connect(dsn, row_factory=tuple_row) as conn:
            rows = conn.execute("SELECT held_in, held_out FROM eval_sets ORDER BY frozen_at ASC").fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning("outcomes.load_failed", error=str(exc))
        return {}

    for held_in_raw, held_out_raw in rows:
        held_in: list[dict[str, object]] = held_in_raw if isinstance(held_in_raw, list) else []
        held_out: list[dict[str, object]] = held_out_raw if isinstance(held_out_raw, list) else []

        for entry in held_in:
            tid = str(entry["trace_id"]) if "trace_id" in entry else None
            if tid:
                outcomes[tid] = "fail"

        for entry in held_out:
            tid = str(entry["trace_id"]) if "trace_id" in entry else None
            truth = str(entry.get("outcome_truth", "")) if "outcome_truth" in entry else None
            if tid and truth == "pass":
                outcomes[tid] = "pass"

    logger.info("outcomes.loaded", labeled_count=len(outcomes))
    return outcomes
