"""Tests for P4.0 outcome_only clustering in discover.py + outcomes.py.

Coverage:
  - outcome_only candidate emitted for known-fail non-outlier trace
  - no outcome_only for known-pass or unlabeled trace
  - no duplicate: anomaly trace with outcome_truth="fail" keeps anomaly only
  - cluster_key ends with ::outcome_only
  - result.outcome_only_count matches actual outcome_only candidates
  - labeled_outcomes=None → no outcome_only (backward compat)
  - load_outcome_labels merges held_in + held_out, skips "unknown" (DB test)
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import date
from typing import Any

import pytest

from kairos.loop.discover import run_discovery
from kairos.models.enums import StepStatus, StepType
from kairos.models.trace import Step, TraceEnvelope

# ── DB guard ──────────────────────────────────────────────────────────────────

_DSN = os.environ.get("KAIROS_PG_DSN", "").strip()
_DB_AVAILABLE = bool(_DSN)

_skip_no_db = pytest.mark.skipif(
    not _DB_AVAILABLE,
    reason="KAIROS_PG_DSN not set — kairos-pg not reachable",
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_step(
    index: int,
    tool_name: str = "Bash",
    status: StepStatus = StepStatus.OK,
    args: dict[str, Any] | None = None,
    step_type: StepType = StepType.TOOL_CALL,
) -> Step:
    return Step(
        step_index=index,
        step_type=step_type,
        tool_name=tool_name,
        tool_args_normalized=args,
        status=status,
    )


def _make_normal_trace(trace_id: str | None = None) -> TraceEnvelope:
    """Trace that is NOT a structural outlier (low tokens, no restarts, no rare ngrams)."""
    tid = trace_id or str(uuid.uuid4()).replace("-", "")
    return TraceEnvelope(
        trace_id=tid,
        steps=[
            _make_step(0, "Read"),
            _make_step(1, "Bash"),
        ],
        total_tokens=100,
        total_latency_ms=500,
        step_count=2,
    )


def _make_outlier_trace(trace_id: str | None = None) -> TraceEnvelope:
    """Trace that IS a structural outlier (very high token count triggers token_z).

    Robust z-score needs non-zero MAD, so callers must include varied normal
    traces alongside this one (see _make_varied_normal_traces).
    """
    tid = trace_id or str(uuid.uuid4()).replace("-", "")
    return TraceEnvelope(
        trace_id=tid,
        steps=[
            _make_step(0, "Read"),
            _make_step(1, "Bash"),
        ],
        total_tokens=999_999,  # extreme outlier token count
        total_latency_ms=500,
        step_count=2,
    )


def _make_varied_normal_traces(n: int = 10, base_trace_id: str = "normal") -> list[TraceEnvelope]:
    """Return n traces with varied token counts so MAD > 0 (needed for robust z)."""
    return [
        TraceEnvelope(
            trace_id=f"{base_trace_id}-{i}",
            steps=[_make_step(0, "Read"), _make_step(1, "Bash")],
            total_tokens=50 + i * 20,  # 50, 70, 90, ... — varied so MAD != 0
            total_latency_ms=500,
            step_count=2,
        )
        for i in range(n)
    ]


NIGHT = date(2026, 6, 18)


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_outcome_only_emitted_for_fail_non_outlier():
    """Known-fail non-outlier trace → outcome_only candidate emitted."""
    fail_trace = _make_normal_trace("fail-trace-1")
    pass_trace = _make_normal_trace("pass-trace-1")
    unlabeled_trace = _make_normal_trace("unlabeled-trace-1")

    labeled_outcomes = {
        "fail-trace-1": "fail",
        "pass-trace-1": "pass",
        # unlabeled-trace-1 absent
    }

    result = run_discovery(
        traces=[fail_trace, pass_trace, unlabeled_trace],
        miss_candidates=[],
        night_id=NIGHT,
        labeled_outcomes=labeled_outcomes,
    )

    oo = [c for c in result.candidates if c.kind == "outcome_only"]
    assert len(oo) == 1
    assert oo[0].trace_id == "fail-trace-1"
    assert result.outcome_only_count == 1


def test_outcome_only_not_emitted_for_pass_or_unlabeled():
    """Pass and unlabeled traces do NOT become outcome_only candidates."""
    pass_trace = _make_normal_trace("pass-1")
    unlabeled_trace = _make_normal_trace("unlabeled-1")

    result = run_discovery(
        traces=[pass_trace, unlabeled_trace],
        miss_candidates=[],
        night_id=NIGHT,
        labeled_outcomes={"pass-1": "pass"},
    )

    oo = [c for c in result.candidates if c.kind == "outcome_only"]
    assert len(oo) == 0
    assert result.outcome_only_count == 0


def test_no_duplicate_for_anomaly_and_fail():
    """Trace that fires anomaly AND has outcome_truth=fail → anomaly only, no outcome_only."""
    # Varied normal traces give non-zero MAD so robust z fires on the outlier
    normal_traces = _make_varied_normal_traces(n=10, base_trace_id="nd-normal")
    outlier_trace = _make_outlier_trace("outlier-fail-1")
    all_traces = normal_traces + [outlier_trace]

    labeled_outcomes = {"outlier-fail-1": "fail"}

    result = run_discovery(
        traces=all_traces,
        miss_candidates=[],
        night_id=NIGHT,
        labeled_outcomes=labeled_outcomes,
    )

    candidates_for_trace = [c for c in result.candidates if c.trace_id == "outlier-fail-1"]
    kinds = {c.kind for c in candidates_for_trace}
    # Must have anomaly, must NOT have outcome_only (no duplicate)
    assert "anomaly" in kinds
    assert "outcome_only" not in kinds
    assert result.outcome_only_count == 0


def test_outcome_only_cluster_key_format():
    """outcome_only cluster_key ends with ::outcome_only."""
    fail_trace = _make_normal_trace("ck-fail-1")

    result = run_discovery(
        traces=[fail_trace],
        miss_candidates=[],
        night_id=NIGHT,
        labeled_outcomes={"ck-fail-1": "fail"},
    )

    oo = [c for c in result.candidates if c.kind == "outcome_only"]
    assert len(oo) == 1
    assert oo[0].cluster_key.endswith("::outcome_only")


def test_outcome_only_count_matches_candidates():
    """result.outcome_only_count == count of kind=='outcome_only' in candidates."""
    traces = [_make_normal_trace(f"t{i}") for i in range(5)]
    labeled_outcomes = {f"t{i}": "fail" for i in range(3)}

    result = run_discovery(
        traces=traces,
        miss_candidates=[],
        night_id=NIGHT,
        labeled_outcomes=labeled_outcomes,
    )

    actual_oo = sum(1 for c in result.candidates if c.kind == "outcome_only")
    assert result.outcome_only_count == actual_oo


def test_labeled_outcomes_none_no_outcome_only():
    """labeled_outcomes=None (default) → no outcome_only candidates emitted."""
    fail_trace = _make_normal_trace("would-be-fail-1")

    result = run_discovery(
        traces=[fail_trace],
        miss_candidates=[],
        night_id=NIGHT,
        labeled_outcomes=None,
    )

    oo = [c for c in result.candidates if c.kind == "outcome_only"]
    assert len(oo) == 0
    assert result.outcome_only_count == 0


def test_outcome_only_features_no_raw_args():
    """outcome_only candidate features contain only numeric/safe-string fields."""
    fail_trace = _make_normal_trace("safe-fail-1")

    result = run_discovery(
        traces=[fail_trace],
        miss_candidates=[],
        night_id=NIGHT,
        labeled_outcomes={"safe-fail-1": "fail"},
    )

    oo = [c for c in result.candidates if c.kind == "outcome_only"]
    assert len(oo) == 1
    features = oo[0].features
    allowed_keys = {
        "outcome_truth",
        "tool_signature",
        "dominant_feature",
        "restart_count",
        "struggle",
        "token_z",
        "latency_z",
        "rare_ngram_count",
    }
    assert set(features.keys()) == allowed_keys
    # All values are scalar (str, int, float, bool) — no nested objects
    for v in features.values():
        assert isinstance(v, (str, int, float, bool))


# ── DB tests: load_outcome_labels ─────────────────────────────────────────────


@_skip_no_db
def test_load_outcome_labels_merges_held_in_and_held_out():
    """load_outcome_labels: held_in → fail, held_out pass → pass, unknown skipped."""
    from datetime import UTC, datetime

    import psycopg

    from kairos.loop.outcomes import load_outcome_labels

    eval_set_id = "test-ool-" + str(uuid.uuid4())[:8]
    # held_in entries have no outcome_truth (they ARE the cluster's bad members)
    held_in = json.dumps(
        [
            {"trace_id": "tid-fail-1", "features": {}},
            {"trace_id": "tid-fail-2", "features": {}},
        ]
    )
    # held_out entries carry outcome_truth
    held_out = json.dumps(
        [
            {"trace_id": "tid-pass-1", "outcome_truth": "pass", "source": "labeled"},
            {"trace_id": "tid-unknown-1", "outcome_truth": "unknown", "source": "other_cluster"},
        ]
    )

    with psycopg.connect(_DSN) as conn:
        conn.execute(
            """
            INSERT INTO eval_sets
              (eval_set_id, cluster_key, detector_version, frozen_at,
               held_in, held_out, discriminator_type, discriminator_config)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s::jsonb)
            ON CONFLICT (eval_set_id) DO NOTHING
            """,
            (
                eval_set_id,
                "test-cluster::outcome_only",
                "v0-test",
                datetime.now(tz=UTC),
                held_in,
                held_out,
                "feature",
                "{}",
            ),
        )
        conn.commit()

    try:
        labels = load_outcome_labels(_DSN)
        # held_in traces → fail
        assert labels.get("tid-fail-1") == "fail"
        assert labels.get("tid-fail-2") == "fail"
        # held_out pass → pass
        assert labels.get("tid-pass-1") == "pass"
        # held_out unknown → skipped
        assert "tid-unknown-1" not in labels
    finally:
        with psycopg.connect(_DSN) as conn:
            conn.execute("DELETE FROM eval_sets WHERE eval_set_id = %s", (eval_set_id,))
            conn.commit()
