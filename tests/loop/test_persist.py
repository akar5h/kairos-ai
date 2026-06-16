"""Tests for src/kairos/loop/persist.py — Day 10.

Tests that require a live kairos-pg are guarded by _skip_no_db.
Tests that are pure-unit (no DB) run unconditionally.

Coverage:
  - persist_findings: idempotency (run twice → identical row counts)
  - persist_findings: redaction check (no raw text fields written)
  - persist_nightly_rollup: aggregation math (p50/p90, outcome_rate, finding_counts)
  - config_hash change → baseline_break row written to nightly_rollup
  - backfill night-bucketing (_night_for_trace)
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import pytest

# ── DB availability guard ─────────────────────────────────────────────────────

_DSN = os.environ.get("KAIROS_PG_DSN", "").strip()
_DB_AVAILABLE = bool(_DSN)

_skip_no_db = pytest.mark.skipif(
    not _DB_AVAILABLE,
    reason="KAIROS_PG_DSN not set — kairos-pg not reachable in this environment",
)


# ── Minimal stubs for pipeline types ─────────────────────────────────────────


@dataclass
class _FakeOp:
    name: str
    expected_tools: list[str] = field(default_factory=list)
    required_side_effect_tools: list[str] = field(default_factory=list)
    side_effect_match: str = "all"
    excluded_tools: list[str] = field(default_factory=list)


@dataclass
class _FakeContext:
    agent_name: str = "TestAgent"
    agent_description: str = ""
    correlation_key: str | None = None
    operations: list[_FakeOp] = field(default_factory=list)


@dataclass
class _FakeOutcomeResult:
    trace_id: str
    outcome_pass: bool
    computable: bool


@dataclass
class _FakeOutcomeSummary:
    workflow_name: str
    total_traces: int = 0
    computable_count: int = 0
    passed_count: int = 0
    outcome_rate: float | None = None
    pending_reason: str | None = None
    human_escalation_rate: float | None = None
    per_trace_results: list[_FakeOutcomeResult] = field(default_factory=list)


@dataclass
class _FakeFinding:
    pattern_name: str
    tier: int = 1
    trace_id: str = ""
    confidence: float = 1.0
    severity: str = "warning"
    evidence: dict[str, Any] = field(default_factory=dict)
    affected_step_indices: list[int] = field(default_factory=list)
    estimated_token_waste: int = 0


@dataclass
class _FakeUnitSummary:
    unit_id: str
    correlation_key_value: str | None
    trace_ids: list[str]
    unit_outcome_pass: bool | None
    unit_computable: bool
    unit_findings: list[_FakeFinding] = field(default_factory=list)
    unit_total_tokens: int = 0
    unit_struggle: int = 0
    unit_started_at: datetime | None = None
    unit_ended_at: datetime | None = None


@dataclass
class _FakeWorkflowSummary:
    operation_name: str
    full_trace_count: int = 0
    attempted_trace_count: int = 0
    outcome: _FakeOutcomeSummary = field(default_factory=_FakeOutcomeSummary)
    reference: Any = None
    deterministic_findings: list[_FakeFinding] = field(default_factory=list)
    divergences: list[Any] = field(default_factory=list)
    top_pattern_names: list[str] = field(default_factory=list)
    member_envelopes: list[Any] = field(default_factory=list)
    secondary_membership_count: int = 0
    primary_trace_ids: set[str] = field(default_factory=set)


@dataclass
class _FakeAnalysisResult:
    workflows: list[_FakeWorkflowSummary]
    unmapped: Any = None
    reliability: dict[str, Any] = field(default_factory=dict)
    unit_summaries: list[_FakeUnitSummary] = field(default_factory=list)


@dataclass
class _FakeEnvelope:
    trace_id: str
    total_tokens: int = 0
    step_count: int = 10
    error_count: int = 0
    started_at: datetime | None = None
    ended_at: datetime | None = None
    is_valid: bool = True


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_test_night() -> date:
    """Return a stable test date (not today, to avoid conflicts with real runs)."""
    return date(2026, 1, 1)


def _make_result_with_findings(
    trace_id: str,
    night: date,
    findings: list[_FakeFinding],
) -> tuple[_FakeAnalysisResult, list[_FakeEnvelope]]:
    """Build a minimal AnalysisResult + envelopes with the given findings."""
    env = _FakeEnvelope(
        trace_id=trace_id,
        total_tokens=1000,
        step_count=20,
        error_count=2,
        started_at=datetime(2026, 1, 1, 3, 0, 0, tzinfo=UTC),
    )
    outcome_res = _FakeOutcomeResult(
        trace_id=trace_id,
        outcome_pass=True,
        computable=True,
    )
    workflow_summary = _FakeWorkflowSummary(
        operation_name="Code Implementation",
        outcome=_FakeOutcomeSummary(
            workflow_name="Code Implementation",
            per_trace_results=[outcome_res],
        ),
        deterministic_findings=findings,
        primary_trace_ids={trace_id},
        member_envelopes=[env],
    )
    unit_summary = _FakeUnitSummary(
        unit_id=trace_id,
        correlation_key_value=None,
        trace_ids=[trace_id],
        unit_outcome_pass=True,
        unit_computable=True,
        unit_findings=list(findings),
        unit_total_tokens=1000,
    )
    result = _FakeAnalysisResult(
        workflows=[workflow_summary],
        unit_summaries=[unit_summary],
    )
    return result, [env]


# ── Pure-unit tests (no DB) ───────────────────────────────────────────────────


class TestComputeConfigHash:
    """compute_config_hash() is stable and changes when inputs change."""

    def _make_ctx(self) -> _FakeContext:
        return _FakeContext(
            agent_name="A",
            correlation_key="paperclip.issue",
            operations=[_FakeOp(name="Code Implementation")],
        )

    def test_stable_on_repeated_calls(self) -> None:
        from kairos.loop.persist import compute_config_hash

        ctx = self._make_ctx()
        h1 = compute_config_hash(ctx)
        h2 = compute_config_hash(ctx)
        assert h1 == h2

    def test_changes_on_op_name_change(self) -> None:
        from kairos.loop.persist import compute_config_hash

        ctx1 = _FakeContext(
            agent_name="A",
            operations=[_FakeOp(name="Code Implementation")],
        )
        ctx2 = _FakeContext(
            agent_name="A",
            operations=[_FakeOp(name="Code Implementation CHANGED")],
        )
        assert compute_config_hash(ctx1) != compute_config_hash(ctx2)

    def test_changes_on_threshold_change(self) -> None:
        from kairos.loop.persist import compute_config_hash

        ctx = self._make_ctx()
        h1 = compute_config_hash(ctx, detector_thresholds={"STRUGGLE_T": 2.0})
        h2 = compute_config_hash(ctx, detector_thresholds={"STRUGGLE_T": 3.0})
        assert h1 != h2

    def test_16_char_hex(self) -> None:
        from kairos.loop.persist import compute_config_hash

        ctx = self._make_ctx()
        h = compute_config_hash(ctx)
        assert len(h) == 16
        assert all(c in "0123456789abcdef" for c in h)


class TestSafeEvidenceSteps:
    """_safe_evidence_steps() returns only integer indices."""

    def test_only_ints_returned(self) -> None:
        from kairos.loop.persist import _safe_evidence_steps

        finding = _FakeFinding(
            pattern_name="test",
            trace_id="abc",
            affected_step_indices=[0, 1, 2],
        )
        assert _safe_evidence_steps(finding) == [0, 1, 2]  # type: ignore[arg-type]

    def test_non_ints_dropped(self) -> None:
        from kairos.loop.persist import _safe_evidence_steps

        finding = _FakeFinding(
            pattern_name="test",
            trace_id="abc",
            affected_step_indices=[0, "leak_secret", 2, None],  # type: ignore[list-item]
        )
        result = _safe_evidence_steps(finding)  # type: ignore[arg-type]
        assert result == [0, 2]


class TestPercentile:
    """_percentile() returns correct values."""

    def test_median(self) -> None:
        from kairos.loop.persist import _percentile

        assert _percentile([1.0, 2.0, 3.0, 4.0, 5.0], 50) == pytest.approx(3.0)

    def test_p90_simple(self) -> None:
        from kairos.loop.persist import _percentile

        # 10 values: p90 should return the 9th (index 8 in 0-based = 9th of 10)
        vals = [float(i) for i in range(1, 11)]  # 1..10
        # linear interp: idx = 0.9 * 9 = 8.1 → 9.0 + 0.1*(10.0-9.0) = 9.1
        result = _percentile(vals, 90)
        assert result == pytest.approx(9.1)

    def test_empty_returns_zero(self) -> None:
        from kairos.loop.persist import _percentile

        assert _percentile([], 50) == 0.0

    def test_single_element(self) -> None:
        from kairos.loop.persist import _percentile

        assert _percentile([42.0], 90) == 42.0


class TestNightForTrace:
    """_night_for_trace() buckets by UTC date of started_at."""

    def test_buckets_by_utc_date(self) -> None:
        from scripts.backfill import _night_for_trace

        env = _FakeEnvelope(
            trace_id="t1",
            started_at=datetime(2026, 6, 10, 23, 59, 0, tzinfo=UTC),
        )
        assert _night_for_trace(env) == date(2026, 6, 10)

    def test_fallback_when_no_timestamp(self) -> None:
        from scripts.backfill import _night_for_trace

        env = _FakeEnvelope(trace_id="t2", started_at=None)
        # Should return today (not raise).
        result = _night_for_trace(env)
        assert isinstance(result, date)


class TestGrepSecrets:
    """grep_secrets() catches known secret patterns."""

    def test_catches_sk_token(self) -> None:
        from kairos.loop.persist import grep_secrets

        text = "my api key is sk-abc123def456ghi789jkl012mno"
        assert grep_secrets(text)

    def test_clean_text(self) -> None:
        from kairos.loop.persist import grep_secrets

        text = "evidence_steps=[1, 2, 3] tokens=500 struggle=0.5"
        assert not grep_secrets(text)


# ── Integration tests (require live kairos-pg) ────────────────────────────────


@_skip_no_db
class TestPersistFindingsIdempotency:
    """Running persist_findings twice for the same night MUST NOT double-count rows."""

    def test_run_twice_identical_row_counts(self) -> None:
        from kairos.loop.db import apply_migrations, get_connection
        from kairos.loop.persist import persist_findings

        apply_migrations()

        night = date(2026, 1, 2)  # stable test date, different from other tests
        trace_id = f"idem-test-{uuid.uuid4().hex[:8]}"
        finding = _FakeFinding(
            pattern_name="struggle_ratio",
            trace_id=trace_id,
            severity="warning",
            affected_step_indices=[1, 3, 5],
        )
        result, envelopes = _make_result_with_findings(trace_id, night, [finding])
        agent_by_trace = {trace_id: "claudecoder"}
        cfg_hash = "test_hash_idem"

        with get_connection() as conn:
            # First run.
            count1 = persist_findings(
                night_id=night,
                result=result,
                envelopes=envelopes,
                agent_by_trace=agent_by_trace,
                config_hash=cfg_hash,
                conn=conn,
            )
            row_count_after_1 = conn.execute(
                "SELECT count(*) FROM findings WHERE night_id = %s AND trace_id = %s",
                (night, trace_id),
            ).fetchone()[0]

            # Second run — must be identical.
            count2 = persist_findings(
                night_id=night,
                result=result,
                envelopes=envelopes,
                agent_by_trace=agent_by_trace,
                config_hash=cfg_hash,
                conn=conn,
            )
            row_count_after_2 = conn.execute(
                "SELECT count(*) FROM findings WHERE night_id = %s AND trace_id = %s",
                (night, trace_id),
            ).fetchone()[0]

        assert row_count_after_1 == row_count_after_2, (
            f"Row count changed between runs: {row_count_after_1} → {row_count_after_2}"
        )
        assert count1 == count2 == 1

        # Cleanup.
        with get_connection() as conn:
            conn.execute(
                "DELETE FROM findings WHERE night_id = %s AND trace_id = %s",
                (night, trace_id),
            )
            conn.commit()


@_skip_no_db
class TestPersistFindingsRedaction:
    """findings rows MUST NOT contain raw tool output or secret-looking fields."""

    def test_no_raw_output_in_row(self) -> None:
        """Confirm that evidence_steps is int[] (no text) and no raw_output column exists."""
        from kairos.loop.db import apply_migrations, get_connection
        from kairos.loop.persist import persist_findings

        apply_migrations()

        night = date(2026, 1, 3)
        trace_id = f"redact-test-{uuid.uuid4().hex[:8]}"
        finding = _FakeFinding(
            pattern_name="unrecovered_error",
            trace_id=trace_id,
            severity="info",
            affected_step_indices=[2, 4],
            evidence={"tool": "Bash", "step_index": 2},  # dict — never persisted
        )
        result, envelopes = _make_result_with_findings(trace_id, night, [finding])
        agent_by_trace = {trace_id: "cto"}

        with get_connection() as conn:
            persist_findings(
                night_id=night,
                result=result,
                envelopes=envelopes,
                agent_by_trace=agent_by_trace,
                config_hash="test_hash_redact",
                conn=conn,
            )
            row = conn.execute(
                "SELECT evidence_steps, tokens, struggle, outcome, workflow, agent "
                "FROM findings WHERE night_id = %s AND trace_id = %s",
                (night, trace_id),
            ).fetchone()

        assert row is not None, "No row found after persist_findings"
        evidence_steps = row[0]
        tokens = row[1]
        struggle = row[2]
        outcome = row[3]
        workflow = row[4]
        agent = row[5]

        # evidence_steps must be a list of ints (or empty list).
        assert isinstance(evidence_steps, list), f"evidence_steps is not a list: {type(evidence_steps)}"
        for idx in evidence_steps:
            assert isinstance(idx, int), f"Non-int in evidence_steps: {idx!r}"

        # No secret-pattern text in any scalar field.
        from kairos.loop.persist import grep_secrets

        for val in [str(tokens), str(struggle), outcome, workflow, agent]:
            hits = grep_secrets(val)
            assert not hits, f"Secret pattern found in column value {val!r}: {hits}"

        # Cleanup.
        with get_connection() as conn:
            conn.execute(
                "DELETE FROM findings WHERE night_id = %s AND trace_id = %s",
                (night, trace_id),
            )
            conn.commit()


@_skip_no_db
class TestNightlyRollupMath:
    """persist_nightly_rollup() writes correct aggregated metrics."""

    def test_outcome_rate_and_finding_counts(self) -> None:
        """Two units: one pass + one fail → outcome_rate = 0.5; finding_counts correct."""
        from kairos.loop.db import apply_migrations, get_connection
        from kairos.loop.persist import persist_nightly_rollup

        apply_migrations()

        night = date(2026, 1, 4)
        t1 = f"rollup-t1-{uuid.uuid4().hex[:6]}"
        t2 = f"rollup-t2-{uuid.uuid4().hex[:6]}"

        env1 = _FakeEnvelope(
            trace_id=t1,
            total_tokens=800,
            step_count=10,
            error_count=0,
            started_at=datetime(2026, 1, 4, 2, 0, tzinfo=UTC),
        )
        env2 = _FakeEnvelope(
            trace_id=t2,
            total_tokens=1200,
            step_count=15,
            error_count=5,
            started_at=datetime(2026, 1, 4, 3, 0, tzinfo=UTC),
        )

        f1 = _FakeFinding(
            pattern_name="struggle_ratio",
            trace_id=t2,
            severity="warning",
            affected_step_indices=[1],
        )
        ws = _FakeWorkflowSummary(
            operation_name="Code Implementation",
            outcome=_FakeOutcomeSummary(
                workflow_name="Code Implementation",
                per_trace_results=[
                    _FakeOutcomeResult(trace_id=t1, outcome_pass=True, computable=True),
                    _FakeOutcomeResult(trace_id=t2, outcome_pass=False, computable=True),
                ],
            ),
            deterministic_findings=[f1],
            primary_trace_ids={t1, t2},
            member_envelopes=[env1, env2],
        )
        us1 = _FakeUnitSummary(
            unit_id=t1,
            correlation_key_value=None,
            trace_ids=[t1],
            unit_outcome_pass=True,
            unit_computable=True,
            unit_findings=[],
            unit_total_tokens=800,
        )
        us2 = _FakeUnitSummary(
            unit_id=t2,
            correlation_key_value=None,
            trace_ids=[t2],
            unit_outcome_pass=False,
            unit_computable=True,
            unit_findings=[f1],
            unit_total_tokens=1200,
        )
        result = _FakeAnalysisResult(
            workflows=[ws],
            unit_summaries=[us1, us2],
        )
        envelopes = [env1, env2]
        agent_by_trace = {t1: "claudecoder", t2: "claudecoder"}

        # Use a unique workflow name so this test doesn't collide.
        ws.operation_name = f"TestWorkflow_{uuid.uuid4().hex[:6]}"

        # Patch primary_trace_ids so the workflow lookup works.
        ws.primary_trace_ids = {t1, t2}

        with get_connection() as conn:
            persist_nightly_rollup(
                night_id=night,
                result=result,
                envelopes=envelopes,
                agent_by_trace=agent_by_trace,
                config_hash="test_hash_rollup",
                conn=conn,
            )
            row = conn.execute(
                "SELECT outcome_rate, finding_counts, units, traces "
                "FROM nightly_rollup WHERE night_id = %s AND workflow = %s",
                (night, ws.operation_name),
            ).fetchone()

        assert row is not None, "No rollup row found"
        outcome_rate = row[0]
        finding_counts = row[1]
        units = row[2]
        traces = row[3]

        assert abs(outcome_rate - 0.5) < 1e-4, f"Expected 0.5 outcome_rate, got {outcome_rate}"
        assert units == 2
        assert traces == 2
        assert "struggle_ratio" in finding_counts
        assert finding_counts["struggle_ratio"] == 1

        # Cleanup.
        with get_connection() as conn:
            conn.execute(
                "DELETE FROM nightly_rollup WHERE night_id = %s AND workflow = %s",
                (night, ws.operation_name),
            )
            conn.commit()


@_skip_no_db
class TestBaselineBreakOnHashChange:
    """config_hash change → baseline_break sentinel row written."""

    def test_baseline_break_row_written(self) -> None:
        from kairos.loop.db import apply_migrations, get_connection
        from kairos.loop.persist import persist_nightly_rollup

        apply_migrations()

        # Insert a prior-night row with hash_A so the "latest persisted" is hash_A.
        hash_a = "aaaaaaaabbbbbbbb"  # 16-char hex-like string
        hash_b = "ccccccccdddddddd"
        prior_night = date(2026, 1, 5)
        test_night = date(2026, 1, 6)

        # Seed the prior row.
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO nightly_rollup
                    (night_id, workflow, agent, units, traces, outcome_rate,
                     struggle_p50, struggle_p90, coordination_waste_per_trace,
                     tokens_per_unit, finding_counts, config_hash)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (night_id, workflow, agent) DO UPDATE SET config_hash = EXCLUDED.config_hash
                """,
                (prior_night, "SeedWorkflow", "agent", 1, 1, 1.0, 0.1, 0.2, 0.0, 100.0, "{}", hash_a),
            )
            conn.commit()

        # Now persist_nightly_rollup for test_night with hash_b (different hash).
        # Build a minimal result.
        trace_id = f"bb-trace-{uuid.uuid4().hex[:6]}"
        env = _FakeEnvelope(
            trace_id=trace_id,
            total_tokens=500,
            step_count=5,
            error_count=0,
            started_at=datetime(2026, 1, 6, 2, 0, tzinfo=UTC),
        )
        ws = _FakeWorkflowSummary(
            operation_name=f"BreakTestWf_{uuid.uuid4().hex[:4]}",
            outcome=_FakeOutcomeSummary(
                workflow_name="BreakTestWf",
                per_trace_results=[
                    _FakeOutcomeResult(trace_id=trace_id, outcome_pass=True, computable=True),
                ],
            ),
            primary_trace_ids={trace_id},
            member_envelopes=[env],
        )
        us = _FakeUnitSummary(
            unit_id=trace_id,
            correlation_key_value=None,
            trace_ids=[trace_id],
            unit_outcome_pass=True,
            unit_computable=True,
        )
        result = _FakeAnalysisResult(workflows=[ws], unit_summaries=[us])

        with get_connection() as conn:
            persist_nightly_rollup(
                night_id=test_night,
                result=result,
                envelopes=[env],
                agent_by_trace={trace_id: "claudecoder"},
                config_hash=hash_b,
                conn=conn,
            )
            # Check that a baseline_break sentinel row exists for test_night.
            row = conn.execute(
                "SELECT baseline_break FROM nightly_rollup WHERE night_id = %s AND baseline_break = true",
                (test_night,),
            ).fetchone()

        assert row is not None, "Expected a baseline_break=true sentinel row"
        assert row[0] is True

        # Cleanup.
        with get_connection() as conn:
            conn.execute(
                "DELETE FROM nightly_rollup WHERE night_id IN (%s, %s)",
                (prior_night, test_night),
            )
            conn.commit()


@_skip_no_db
class TestRollupIdempotency:
    """Running persist_nightly_rollup twice yields identical row counts."""

    def test_run_twice_same_rows(self) -> None:
        from kairos.loop.db import apply_migrations, get_connection
        from kairos.loop.persist import persist_nightly_rollup

        apply_migrations()

        night = date(2026, 1, 7)
        trace_id = f"rollup-idem-{uuid.uuid4().hex[:6]}"
        env = _FakeEnvelope(
            trace_id=trace_id,
            total_tokens=600,
            step_count=8,
            error_count=1,
            started_at=datetime(2026, 1, 7, 2, 0, tzinfo=UTC),
        )
        wf_name = f"IdemWf_{uuid.uuid4().hex[:4]}"
        ws = _FakeWorkflowSummary(
            operation_name=wf_name,
            outcome=_FakeOutcomeSummary(
                workflow_name=wf_name,
                per_trace_results=[
                    _FakeOutcomeResult(trace_id=trace_id, outcome_pass=True, computable=True),
                ],
            ),
            primary_trace_ids={trace_id},
            member_envelopes=[env],
        )
        us = _FakeUnitSummary(
            unit_id=trace_id,
            correlation_key_value=None,
            trace_ids=[trace_id],
            unit_outcome_pass=True,
            unit_computable=True,
        )
        result = _FakeAnalysisResult(workflows=[ws], unit_summaries=[us])

        with get_connection() as conn:
            persist_nightly_rollup(
                night_id=night,
                result=result,
                envelopes=[env],
                agent_by_trace={trace_id: "claudecoder"},
                config_hash="idem_hash",
                conn=conn,
            )
            count1 = conn.execute(
                "SELECT count(*) FROM nightly_rollup WHERE night_id = %s AND workflow = %s",
                (night, wf_name),
            ).fetchone()[0]

            persist_nightly_rollup(
                night_id=night,
                result=result,
                envelopes=[env],
                agent_by_trace={trace_id: "claudecoder"},
                config_hash="idem_hash",
                conn=conn,
            )
            count2 = conn.execute(
                "SELECT count(*) FROM nightly_rollup WHERE night_id = %s AND workflow = %s",
                (night, wf_name),
            ).fetchone()[0]

        assert count1 == count2 == 1

        # Cleanup.
        with get_connection() as conn:
            conn.execute(
                "DELETE FROM nightly_rollup WHERE night_id = %s AND workflow = %s",
                (night, wf_name),
            )
            conn.commit()
