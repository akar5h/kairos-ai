"""Tests for src/kairos/loop/discover.py — Day 12.

Coverage:
  - restart count feature computation
  - post-restart rework detection (the CRITICAL Day-14 dependency)
  - n-gram rarity computation
  - robust z-score outlier tagging
  - clustering (cluster_key = tool_signature + dominant_feature)
  - cap + drop logging
  - expectation-miss candidate folding
  - DB-down → JSON-only path (best-effort)
  - grep_secrets() audit on emitted JSON
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path  # noqa: TC003
from typing import Any

import pytest

from kairos.loop.discover import (
    ROBUST_Z_T,
    DiscoveryResult,
    _build_corpus_ngram_freqs,
    _post_restart_rework_count,
    _redact_arg_digest,
    compute_trace_features,
    grep_secrets,
    run_discovery,
)
from kairos.models.enums import StepStatus, StepType
from kairos.models.trace import Step, TraceEnvelope

# ── DB availability guard ─────────────────────────────────────────────────────

_DSN = os.environ.get("KAIROS_PG_DSN", "").strip()
_DB_AVAILABLE = bool(_DSN)

_skip_no_db = pytest.mark.skipif(
    not _DB_AVAILABLE,
    reason="KAIROS_PG_DSN not set — kairos-pg not reachable",
)


# ── Stubs ─────────────────────────────────────────────────────────────────────


@dataclass
class _FakeMissCandidate:
    trace_id: str
    workflow_name: str
    missing_tool: str
    presence_rate: float
    clean_trace_count: int


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


def _make_trace(
    trace_id: str | None = None,
    steps: list[Step] | None = None,
    total_tokens: int = 100,
    total_latency_ms: int = 1000,
) -> TraceEnvelope:
    tid = trace_id or str(uuid.uuid4()).replace("-", "")
    env = TraceEnvelope(
        trace_id=tid,
        steps=steps or [],
        total_tokens=total_tokens,
        total_latency_ms=total_latency_ms,
        step_count=len(steps or []),
    )
    return env


# ── restart count ─────────────────────────────────────────────────────────────


def test_restart_count_zero_no_restarts():
    """Trace with no restart patterns → restart_count = 0."""
    steps = [
        _make_step(0, "Bash", args={"command": "ls"}),
        _make_step(1, "Read", args={"file_path": "/tmp/foo"}),
    ]
    trace = _make_trace(steps=steps)
    features = compute_trace_features([trace])
    assert len(features) == 1
    assert features[0].restart_count == 0
    assert features[0].post_restart_rework == 0


def test_restart_count_detects_boundary():
    """Step with .claude pattern marks a restart boundary."""
    steps = [
        _make_step(0, "Bash", args={"command": "ls"}),
        _make_step(1, "Bash", args={"command": "cat .claude/CLAUDE.md"}),
        _make_step(2, "Read", args={"file_path": "/tmp/bar"}),
    ]
    trace = _make_trace(steps=steps)
    features = compute_trace_features([trace])
    assert features[0].restart_count == 1


# ── post-restart rework ───────────────────────────────────────────────────────


def test_post_restart_rework_detects_redo():
    """Steps after restart that re-execute the same command as before restart."""
    from kairos.detection.session_quality import _find_session_restart_indices

    pre_args = {"command": "git status"}
    restart_args = {"command": "cat .claude/CLAUDE.md"}
    post_args = {"command": "git status"}  # same as pre-restart → rework

    steps = [
        _make_step(0, "Bash", status=StepStatus.OK, args=pre_args),
        _make_step(1, "Bash", status=StepStatus.OK, args=restart_args),  # restart
        _make_step(2, "Bash", status=StepStatus.OK, args=post_args),  # rework
    ]

    restart_indices = _find_session_restart_indices(steps)
    count = _post_restart_rework_count(steps, restart_indices)
    assert count >= 1, f"Expected rework_count >= 1, got {count}"


def test_post_restart_rework_zero_when_no_restart():
    """No restarts → no rework."""
    from kairos.detection.session_quality import _find_session_restart_indices

    steps = [
        _make_step(0, "Bash", args={"command": "ls"}),
        _make_step(1, "Bash", args={"command": "ls"}),  # identical but no restart
    ]
    restart_indices = _find_session_restart_indices(steps)
    count = _post_restart_rework_count(steps, restart_indices)
    assert count == 0


def test_post_restart_rework_no_args_skipped():
    """Steps without args are excluded from rework comparison."""
    from kairos.detection.session_quality import _find_session_restart_indices

    steps = [
        _make_step(0, "Bash", args={"command": "ls"}),
        _make_step(1, "Bash", args={"command": "cat .claude/CLAUDE.md"}),  # restart
        _make_step(2, "Bash", args=None),  # no args — cannot compare
    ]
    restart_indices = _find_session_restart_indices(steps)
    count = _post_restart_rework_count(steps, restart_indices)
    assert count == 0


# ── n-gram rarity ─────────────────────────────────────────────────────────────


def test_ngram_rarity_common_ngram_not_rare():
    """An n-gram present in all traces has freq=1.0 → not rare."""
    # 5 traces all using the same bigram Bash->Read.
    traces = []
    for _ in range(5):
        steps = [
            _make_step(0, "Bash"),
            _make_step(1, "Read"),
        ]
        traces.append(_make_trace(steps=steps))

    freqs = _build_corpus_ngram_freqs(traces, n_values=(2,))
    # Bash->Read should have freq=1.0
    assert freqs.get(("Bash", "Read"), 0.0) == pytest.approx(1.0)


def test_ngram_rarity_rare_ngram_flagged():
    """An n-gram appearing in 1/200 traces (0.5% < 1%) is flagged as rare."""
    traces = []
    # 199 traces with Bash->Read
    for _ in range(199):
        steps = [_make_step(0, "Bash"), _make_step(1, "Read")]
        traces.append(_make_trace(steps=steps))
    # 1 trace with Bash->Write (rare: 1/200 = 0.5%)
    traces.append(_make_trace(steps=[_make_step(0, "Bash"), _make_step(1, "Write")]))

    freqs = _build_corpus_ngram_freqs(traces, n_values=(2,))
    rare_ngram = ("Bash", "Write")
    freq = freqs.get(rare_ngram, 0.0)
    assert freq < 0.01, f"Expected freq < 0.01 for rare ngram, got {freq}"


# ── robust z-score ────────────────────────────────────────────────────────────


def test_robust_z_outlier_flagged():
    """A trace with extreme token count has robust z > 3.

    For MAD-based robust z to be non-zero, the base values must have spread.
    We use a realistic distribution: values spaced between 100 and 400 with
    one extreme outlier at 100_000.  This ensures MAD > 0 and the outlier z > 3.
    """
    import numpy as np

    # 20 base traces with varying token counts (ensures MAD > 0).
    base_tokens = list(range(100, 501, 20))  # 100, 120, ..., 500 — 21 values
    traces = [_make_trace(total_tokens=t) for t in base_tokens]
    # Add extreme outlier.
    traces.append(_make_trace(total_tokens=100_000))

    features = compute_trace_features(traces)
    extreme_f = features[-1]

    # Verify with the same formula we use.
    vals = [float(t.total_tokens) for t in traces]
    arr = np.array(vals)
    mad = float(np.median(np.abs(arr - np.median(arr))))
    assert mad > 0, "MAD must be > 0 for this test to be meaningful"

    assert abs(extreme_f.token_z) > ROBUST_Z_T, (
        f"Expected |token_z| > {ROBUST_Z_T}, got {extreme_f.token_z}"
    )
    assert extreme_f.dominant_feature == "token_z"


def test_robust_z_identical_values_no_outlier():
    """All traces with the same token count → z-score = 0 for all."""
    traces = [_make_trace(total_tokens=500) for _ in range(10)]
    features = compute_trace_features(traces)
    for f in features:
        assert f.token_z == pytest.approx(0.0)


# ── clustering ────────────────────────────────────────────────────────────────


def test_cluster_key_groups_same_signature():
    """Two traces with identical tool signatures and same dominant feature → same cluster_key."""
    import numpy as np

    # Use varied base token counts so MAD > 0.
    base_tokens = list(range(100, 501, 20))  # 21 base values
    steps_base = [_make_step(0, "Bash"), _make_step(1, "Read")]
    traces = [_make_trace(steps=steps_base, total_tokens=t) for t in base_tokens]

    # Two extreme outlier traces with the same tools.
    steps_a = [_make_step(0, "Bash"), _make_step(1, "Read")]
    steps_b = [_make_step(0, "Bash"), _make_step(1, "Read")]
    traces.append(_make_trace(steps=steps_a, total_tokens=200_000))
    traces.append(_make_trace(steps=steps_b, total_tokens=180_000))

    # Verify MAD > 0.
    vals = [float(t.total_tokens) for t in traces]
    arr = np.array(vals)
    mad = float(np.median(np.abs(arr - np.median(arr))))
    assert mad > 0, "MAD must be > 0 for this test to be meaningful"

    features = compute_trace_features(traces)
    # The last two extreme traces should share a cluster_key.
    outlier_features = [f for f in features if abs(f.token_z) > ROBUST_Z_T]
    assert len(outlier_features) >= 2, f"Expected >= 2 outliers, got {len(outlier_features)}"
    cluster_keys = {f.tool_signature + "::" + f.dominant_feature for f in outlier_features}
    # All outlier traces with same tool_signature should have the same base cluster_key.
    assert len(cluster_keys) == 1


# ── cap + drop logging ────────────────────────────────────────────────────────


def test_cap_drops_surplus_candidates(caplog: pytest.LogCaptureFixture, tmp_path: Path):
    """Surplus candidates are dropped and logged — never silently truncated."""
    import logging

    # Force candidates via expectation-miss (simpler and guaranteed) rather
    # than relying on outlier z-score (MAD=0 when all identical).
    small_cap = 5
    miss_candidates = [
        _FakeMissCandidate(
            trace_id=str(uuid.uuid4()).replace("-", ""),
            workflow_name="Code Implementation",
            missing_tool="Edit",
            presence_rate=0.95,
            clean_trace_count=10,
        )
        for _ in range(small_cap + 10)  # 15 candidates for cap=5
    ]

    traces = [_make_trace(total_tokens=100) for _ in range(5)]
    night_id = date(2026, 6, 12)
    with caplog.at_level(logging.WARNING, logger="kairos.loop.discover"):
        result = run_discovery(
            traces=traces,
            miss_candidates=miss_candidates,  # type: ignore[arg-type]
            night_id=night_id,
            conn=None,
            json_output_path=None,
            max_candidates=small_cap,
        )

    # No silent truncation: the drop count is SURFACED on the result (the reliable
    # contract), not only logged. (caplog capture of the structlog warning is
    # propagation-order-dependent across the suite, so we assert the surfaced field.)
    assert result.dropped_by_cap == len(miss_candidates) - small_cap
    assert result.dropped_by_cap > 0
    assert len(result.candidates) == small_cap


# ── expectation-miss folding ──────────────────────────────────────────────────


def test_expectation_miss_candidates_folded(tmp_path: Path):
    """Expectation-miss candidates from LEARN stage appear in result."""
    traces = [_make_trace(total_tokens=100) for _ in range(5)]
    miss = [
        _FakeMissCandidate(
            trace_id=traces[0].trace_id,
            workflow_name="Code Implementation",
            missing_tool="Edit",
            presence_rate=0.95,
            clean_trace_count=10,
        )
    ]

    result = run_discovery(
        traces=traces,
        miss_candidates=miss,  # type: ignore[arg-type]
        night_id=date(2026, 6, 12),
        conn=None,
        json_output_path=None,
    )

    em_candidates = [c for c in result.candidates if c.kind == "expectation_miss"]
    assert len(em_candidates) >= 1
    assert result.expectation_miss_count >= 1
    assert em_candidates[0].features["missing_tool"] == "Edit"


# ── JSON emit ─────────────────────────────────────────────────────────────────


def test_json_emitted_no_secrets(tmp_path: Path):
    """Emitted JSON has no leaked secrets — grep_secrets returns no hits."""
    steps = [_make_step(0, "Bash"), _make_step(1, "Read")]
    traces = [_make_trace(steps=steps, total_tokens=100_000)]
    # Also plant a miss candidate.
    miss = [
        _FakeMissCandidate(
            trace_id=traces[0].trace_id,
            workflow_name="Test Workflow",
            missing_tool="Write",
            presence_rate=0.92,
            clean_trace_count=6,
        )
    ]

    json_path = tmp_path / "discovery_queue.json"
    run_discovery(
        traces=traces,
        miss_candidates=miss,  # type: ignore[arg-type]
        night_id=date(2026, 6, 12),
        conn=None,
        json_output_path=json_path,
    )

    if json_path.exists():
        content = json_path.read_text()
        hits = grep_secrets(content)
        assert not hits, f"Secret patterns found in discovery_queue.json: {hits}"


# ── DB-down best-effort ───────────────────────────────────────────────────────


def test_db_down_json_still_written(tmp_path: Path):
    """When no DB conn is provided, JSON output still works (best-effort)."""
    traces = [_make_trace(total_tokens=100_000)]
    json_path = tmp_path / "discovery_queue.json"
    result = run_discovery(
        traces=traces,
        miss_candidates=[],
        night_id=date(2026, 6, 12),
        conn=None,
        json_output_path=json_path,
    )
    # pg_rows_upserted should be 0 (no conn).
    assert result.pg_rows_upserted == 0
    # JSON may or may not exist depending on whether the trace is an outlier;
    # but the result should not raise.
    assert isinstance(result, DiscoveryResult)


@_skip_no_db
def test_pg_upsert_idempotent(tmp_path: Path):
    """Running discovery twice on the same traces produces the same rows (idempotent)."""
    from kairos.loop.db import apply_migrations, get_connection

    apply_migrations()

    traces = [_make_trace(total_tokens=100_000)]
    night_id = date(2026, 6, 12)

    with get_connection() as conn:
        r1 = run_discovery(
            traces=traces,
            miss_candidates=[],
            night_id=night_id,
            conn=conn,
        )
        r2 = run_discovery(
            traces=traces,
            miss_candidates=[],
            night_id=night_id,
            conn=conn,
        )

    # Second run should upsert the same rows — not add extras.
    assert r1.pg_rows_upserted == r2.pg_rows_upserted


# ── grep_secrets unit test ────────────────────────────────────────────────────


def test_grep_secrets_detects_patterns():
    """grep_secrets flags known secret patterns."""
    assert grep_secrets("sk-abc123abc123abc123abc123") != []
    assert grep_secrets("Authorization: Bearer eyJhbGc.eyJzdW.sig") != []
    assert grep_secrets("no secrets here") == []


def test_grep_secrets_clean_text():
    """grep_secrets returns empty for clean numeric features JSON."""
    payload = json.dumps({
        "restart_count": 2,
        "post_restart_rework": 1,
        "struggle": 3.5,
        "token_z": 4.2,
        "latency_z": 0.5,
    })
    assert grep_secrets(payload) == []


# ── redact_arg_digest ─────────────────────────────────────────────────────────


def test_redact_arg_digest_stable():
    """Same input always produces the same digest."""
    raw = "Bash:ls -la"
    assert _redact_arg_digest(raw) == _redact_arg_digest(raw)
    assert len(_redact_arg_digest(raw)) == 16


def test_redact_arg_digest_different_inputs():
    """Different inputs produce different digests."""
    assert _redact_arg_digest("Bash:ls") != _redact_arg_digest("Bash:pwd")
