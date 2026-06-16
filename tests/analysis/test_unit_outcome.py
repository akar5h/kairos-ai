"""Tests for Day 9 correlation-key rollup.

Covers:
  - BusinessContext.correlation_key parsing (present / absent / empty string)
  - TraceEnvelope.correlation_key_value field (default None)
  - spans_to_envelope correlation_key_attr extraction
  - rollup_units grouping, last-wins, union findings, summed cost
  - unattributed handling (key absent → own unit)
  - backward-compat: None key → unit == trace, outcome mirrors per-trace exactly
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from kairos.analysis.outcome_metric import OutcomeResult
from kairos.analysis.unit_outcome import UnitOutcomeSummary, rollup_units
from kairos.detection.models import Finding
from kairos.engine.pipeline import run_pipeline
from kairos.models.enums import StepStatus, StepStatusSource, StepType, TerminalStatus
from kairos.models.trace import Step, TraceEnvelope
from kairos.taxonomy.business_context import BusinessContext, BusinessOperation

# ─── Fixture helpers ────────────────────────────────────────────────────


def _step(
    i: int,
    tool: str,
    *,
    status: StepStatus = StepStatus.OK,
    status_source: StepStatusSource = StepStatusSource.ATTR_SUCCESS,
    tool_output: str | None = "done",
) -> Step:
    return Step(
        step_index=i,
        step_type=StepType.TOOL_CALL,
        tool_name=tool,
        tool_args={"i": i},
        tool_output=tool_output,
        status=status,
        status_source=status_source,
    )


def _trace(
    trace_id: str,
    tools: list[str],
    *,
    terminal: TerminalStatus = TerminalStatus.COMPLETED,
    correlation_key_value: str | None = None,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
    error_count_override: int | None = None,
) -> TraceEnvelope:
    steps = [_step(i, t) for i, t in enumerate(tools)]
    env = TraceEnvelope(
        trace_id=trace_id,
        source="test",
        steps=steps,
        terminal_status=terminal,
        correlation_key_value=correlation_key_value,
        started_at=started_at,
        ended_at=ended_at,
        user_input="test",
    )
    if error_count_override is not None:
        env.error_count = error_count_override
    return env


def _op(
    name: str = "Op",
    *,
    tools: list[str] | None = None,
    side_effects: list[str] | None = None,
) -> BusinessOperation:
    t = tools or ["Edit"]
    se = side_effects or ["Edit"]
    return BusinessOperation(
        name=name,
        description="test op",
        expected_tools=t,
        required_side_effect_tools=se,
    )


def _ctx(*, correlation_key: str | None = None) -> BusinessContext:
    return BusinessContext(
        agent_name="TestAgent",
        agent_description="test",
        operations=[_op()],
        correlation_key=correlation_key,
    )


def _result(
    trace_id: str,
    *,
    outcome_pass: bool = True,
    computable: bool = True,
) -> OutcomeResult:
    return OutcomeResult(
        trace_id=trace_id,
        outcome_pass=outcome_pass,
        computable=computable,
        reason=None,
    )


def _finding(trace_id: str, pattern: str = "loop") -> Finding:
    return Finding(
        pattern_name=pattern,
        tier=1,
        trace_id=trace_id,
        confidence=0.9,
        severity="warning",
    )


# ─── BusinessContext.correlation_key parsing ────────────────────────────


class TestCorrelationKeyParsing:
    """correlation_key field loads from from_dict / from_yaml, defaults to None."""

    def _minimal_dict(self, **extra: object) -> dict[str, object]:
        op = {"name": "Op1", "description": "d", "expected_tools": ["Edit"], "required_side_effect_tools": ["Edit"]}
        return {"agent_name": "A", "agent_description": "B", "operations": [op], **extra}

    def test_absent_defaults_to_none(self) -> None:
        ctx = BusinessContext.from_dict(self._minimal_dict())
        assert ctx.correlation_key is None

    def test_present_is_loaded(self) -> None:
        ctx = BusinessContext.from_dict(self._minimal_dict(**{"correlation_key": "paperclip.issue"}))
        assert ctx.correlation_key == "paperclip.issue"

    def test_empty_string_becomes_none(self) -> None:
        """Empty string in YAML is treated as absent (falsy → None)."""
        ctx = BusinessContext.from_dict(self._minimal_dict(**{"correlation_key": ""}))
        assert ctx.correlation_key is None

    def test_existing_context_yaml_still_loads(self, tmp_path: Any) -> None:
        """Regression: existing context.yaml must load cleanly and have the correct correlation_key."""
        from pathlib import Path

        repo_root = Path(__file__).parent.parent.parent
        ctx_path = repo_root / "config" / "context.yaml"
        if not ctx_path.exists():
            pytest.skip("config/context.yaml not found")
        ctx = BusinessContext.from_yaml(ctx_path)
        # Must load with no error.  As of Day 9, correlation_key is set to
        # "paperclip.issue" in the live context.yaml (live-verified 2026-06-13).
        assert ctx.correlation_key == "paperclip.issue"


# ─── TraceEnvelope.correlation_key_value ────────────────────────────────


class TestTraceEnvelopeCorrelationKeyValue:
    """The new field defaults to None and round-trips through model_copy."""

    def test_default_is_none(self) -> None:
        env = TraceEnvelope(trace_id="t1", source="test")
        assert env.correlation_key_value is None

    def test_can_be_set(self) -> None:
        env = TraceEnvelope(trace_id="t1", source="test", correlation_key_value="issue-123")
        assert env.correlation_key_value == "issue-123"

    def test_model_copy_preserves_value(self) -> None:
        env = TraceEnvelope(trace_id="t1", source="test", correlation_key_value="abc")
        env2 = env.model_copy(update={"trace_id": "t2"})
        assert env2.correlation_key_value == "abc"


# ─── spans_to_envelope correlation_key_attr extraction ──────────────────


class TestSpansToEnvelopeCorrelationKey:
    """spans_to_envelope populates correlation_key_value when attr is given."""

    def _minimal_span_dict(self, trace_id: str, span_id: str, **extra_attrs: Any) -> dict[str, Any]:
        """Minimal Phoenix-dict span that spans_to_envelope can consume."""
        attrs: dict[str, Any] = {
            "kairos.span.kind": "tool",
            "tool_name": "Bash",
            "success": True,
        }
        attrs.update(extra_attrs)
        return {
            "name": "claude_code.tool",
            "context": {"trace_id": trace_id, "span_id": span_id},
            "parent_id": None,
            "attributes": attrs,
            "start_time": "2024-01-01T00:00:00+00:00",
            "end_time": "2024-01-01T00:00:01+00:00",
            "status_code": "OK",
            "events": [],
        }

    def test_attr_absent_leaves_none(self) -> None:
        from kairos.readers.phoenix import spans_to_envelope

        span = self._minimal_span_dict("aaa" * 10 + "aa", "bbb" * 5 + "b")
        env = spans_to_envelope([span], correlation_key_attr="paperclip.issue")
        assert env.correlation_key_value is None

    def test_attr_present_is_extracted(self) -> None:
        from kairos.readers.phoenix import spans_to_envelope

        span = self._minimal_span_dict(
            "aaa" * 10 + "aa",
            "bbb" * 5 + "b",
            **{"paperclip.issue": "issue-uuid-001"},
        )
        env = spans_to_envelope([span], correlation_key_attr="paperclip.issue")
        assert env.correlation_key_value == "issue-uuid-001"

    def test_no_attr_name_leaves_none(self) -> None:
        from kairos.readers.phoenix import spans_to_envelope

        span = self._minimal_span_dict(
            "aaa" * 10 + "aa",
            "bbb" * 5 + "b",
            **{"paperclip.issue": "issue-xyz"},
        )
        # correlation_key_attr not passed → default None → value not extracted.
        env = spans_to_envelope([span])
        assert env.correlation_key_value is None


# ─── rollup_units: core grouping ────────────────────────────────────────


class TestRollupUnitsGrouping:
    """Grouping and last-wins semantics."""

    def test_none_key_each_trace_is_own_unit(self) -> None:
        t1 = _trace("t1", ["Edit"])
        t2 = _trace("t2", ["Edit"])
        results = [_result("t1"), _result("t2")]
        units = rollup_units([t1, t2], results, {}, correlation_key=None)
        assert len(units) == 2
        unit_ids = {u.unit_id for u in units}
        assert "t1" in unit_ids
        assert "t2" in unit_ids

    def test_none_key_unit_outcome_matches_per_trace(self) -> None:
        t1 = _trace("t1", ["Edit"])
        t2 = _trace("t2", ["Edit"])
        results = [_result("t1", outcome_pass=True), _result("t2", outcome_pass=False)]
        units = rollup_units([t1, t2], results, {}, correlation_key=None)
        by_id = {u.unit_id: u for u in units}
        assert by_id["t1"].unit_outcome_pass is True
        assert by_id["t2"].unit_outcome_pass is False

    def test_grouped_traces_share_unit(self) -> None:
        t1 = _trace("t1", ["Edit"], correlation_key_value="issue-1")
        t2 = _trace("t2", ["Edit"], correlation_key_value="issue-1")
        t3 = _trace("t3", ["Edit"], correlation_key_value="issue-2")
        results = [_result("t1"), _result("t2"), _result("t3")]
        units = rollup_units([t1, t2, t3], results, {}, correlation_key="paperclip.issue")
        assert len(units) == 2
        by_ckv: dict[str | None, UnitOutcomeSummary] = {u.correlation_key_value: u for u in units}
        issue1_unit = by_ckv["issue-1"]
        assert set(issue1_unit.trace_ids) == {"t1", "t2"}
        assert by_ckv["issue-2"] is not None

    def test_last_wins_outcome_intermediate_fail_then_pass(self) -> None:
        """Intermediate fail + final pass → unit passes."""
        ts1 = datetime(2024, 1, 1, 10, 0, tzinfo=UTC)
        ts2 = datetime(2024, 1, 1, 11, 0, tzinfo=UTC)
        t1 = _trace("t1", ["Edit"], correlation_key_value="issue-1", started_at=ts1)
        t2 = _trace("t2", ["Edit"], correlation_key_value="issue-1", started_at=ts2)
        results = [
            _result("t1", outcome_pass=False, computable=True),  # earlier, fails
            _result("t2", outcome_pass=True, computable=True),  # later, passes
        ]
        units = rollup_units([t1, t2], results, {}, correlation_key="paperclip.issue")
        assert len(units) == 1
        assert units[0].unit_outcome_pass is True

    def test_last_wins_outcome_pass_then_fail(self) -> None:
        """Pass then fail → unit fails (last wins)."""
        ts1 = datetime(2024, 1, 1, 10, 0, tzinfo=UTC)
        ts2 = datetime(2024, 1, 1, 11, 0, tzinfo=UTC)
        t1 = _trace("t1", ["Edit"], correlation_key_value="issue-1", started_at=ts1)
        t2 = _trace("t2", ["Edit"], correlation_key_value="issue-1", started_at=ts2)
        results = [
            _result("t1", outcome_pass=True, computable=True),
            _result("t2", outcome_pass=False, computable=True),
        ]
        units = rollup_units([t1, t2], results, {}, correlation_key="paperclip.issue")
        assert units[0].unit_outcome_pass is False

    def test_last_wins_skips_noncomputable(self) -> None:
        """Non-computable traces are skipped for last-wins; last computable wins."""
        ts1 = datetime(2024, 1, 1, 10, 0, tzinfo=UTC)
        ts2 = datetime(2024, 1, 1, 11, 0, tzinfo=UTC)
        ts3 = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
        t1 = _trace("t1", ["Edit"], correlation_key_value="issue-1", started_at=ts1)
        t2 = _trace("t2", ["Edit"], correlation_key_value="issue-1", started_at=ts2)
        t3 = _trace("t3", ["Edit"], correlation_key_value="issue-1", started_at=ts3)
        results = [
            _result("t1", outcome_pass=True, computable=True),
            _result("t2", outcome_pass=False, computable=True),  # t2 fails
            _result("t3", computable=False),  # t3 not computable
        ]
        units = rollup_units([t1, t2, t3], results, {}, correlation_key="paperclip.issue")
        # Last computable = t2 (fails) even though t3 is chronologically last.
        assert units[0].unit_outcome_pass is False

    def test_all_noncomputable_unit_computable_is_false(self) -> None:
        t1 = _trace("t1", ["Edit"], correlation_key_value="issue-x")
        results = [_result("t1", computable=False)]
        units = rollup_units([t1], results, {}, correlation_key="paperclip.issue")
        assert units[0].unit_computable is False
        assert units[0].unit_outcome_pass is None


# ─── rollup_units: union findings ───────────────────────────────────────


class TestRollupUnitsFindings:
    """Unit findings are UNION across all traces in the group."""

    def test_union_findings_from_grouped_traces(self) -> None:
        t1 = _trace("t1", ["Edit"], correlation_key_value="issue-1")
        t2 = _trace("t2", ["Edit"], correlation_key_value="issue-1")
        results = [_result("t1"), _result("t2")]
        fpt = {
            "t1": [_finding("t1", "loop"), _finding("t1", "redundant")],
            "t2": [_finding("t2", "loop")],
        }
        units = rollup_units([t1, t2], results, fpt, correlation_key="paperclip.issue")
        assert len(units) == 1
        assert len(units[0].unit_findings) == 3

    def test_no_findings_gives_empty_list(self) -> None:
        t1 = _trace("t1", ["Edit"])
        results = [_result("t1")]
        units = rollup_units([t1], results, {}, correlation_key=None)
        assert units[0].unit_findings == []


# ─── rollup_units: cost metrics ─────────────────────────────────────────


class TestRollupUnitsCost:
    """unit_total_tokens and unit_struggle are summed across the group."""

    def test_tokens_summed(self) -> None:
        t1 = _trace("t1", ["Edit"], correlation_key_value="issue-1")
        t2 = _trace("t2", ["Edit"], correlation_key_value="issue-1")
        t1.total_tokens = 1000
        t2.total_tokens = 500
        results = [_result("t1"), _result("t2")]
        units = rollup_units([t1, t2], results, {}, correlation_key="paperclip.issue")
        assert units[0].unit_total_tokens == 1500

    def test_struggle_summed(self) -> None:
        t1 = _trace("t1", ["Edit"], correlation_key_value="issue-1", error_count_override=3)
        t2 = _trace("t2", ["Edit"], correlation_key_value="issue-1", error_count_override=2)
        results = [_result("t1"), _result("t2")]
        units = rollup_units([t1, t2], results, {}, correlation_key="paperclip.issue")
        assert units[0].unit_struggle == 5


# ─── rollup_units: time span ─────────────────────────────────────────────


class TestRollupUnitsTimeSpan:
    def test_unit_span_first_start_last_end(self) -> None:
        ts_a_start = datetime(2024, 1, 1, 10, 0, tzinfo=UTC)
        ts_a_end = datetime(2024, 1, 1, 10, 30, tzinfo=UTC)
        ts_b_start = datetime(2024, 1, 1, 11, 0, tzinfo=UTC)
        ts_b_end = datetime(2024, 1, 1, 11, 45, tzinfo=UTC)
        t1 = _trace("t1", ["Edit"], correlation_key_value="issue-1", started_at=ts_a_start, ended_at=ts_a_end)
        t2 = _trace("t2", ["Edit"], correlation_key_value="issue-1", started_at=ts_b_start, ended_at=ts_b_end)
        results = [_result("t1"), _result("t2")]
        units = rollup_units([t1, t2], results, {}, correlation_key="paperclip.issue")
        assert units[0].unit_started_at == ts_a_start
        assert units[0].unit_ended_at == ts_b_end

    def test_none_timestamps_handled(self) -> None:
        t1 = _trace("t1", ["Edit"], correlation_key_value="issue-1")
        results = [_result("t1")]
        units = rollup_units([t1], results, {}, correlation_key="paperclip.issue")
        assert units[0].unit_started_at is None
        assert units[0].unit_ended_at is None


# ─── rollup_units: unattributed handling ────────────────────────────────


class TestRollupUnitsUnattributed:
    """Traces with no key value are unattributed: each becomes its own unit."""

    def test_unattributed_traces_are_own_units(self) -> None:
        # correlation_key configured but these traces have no value.
        t1 = _trace("t1", ["Edit"])  # correlation_key_value=None
        t2 = _trace("t2", ["Edit"])
        results = [_result("t1"), _result("t2")]
        units = rollup_units([t1, t2], results, {}, correlation_key="paperclip.issue")
        # Two separate units, not merged.
        assert len(units) == 2
        for u in units:
            assert u.correlation_key_value is None
            assert len(u.trace_ids) == 1

    def test_unattributed_mix_with_attributed(self) -> None:
        t1 = _trace("t1", ["Edit"])  # no key
        t2 = _trace("t2", ["Edit"], correlation_key_value="issue-A")
        t3 = _trace("t3", ["Edit"], correlation_key_value="issue-A")
        results = [_result("t1"), _result("t2"), _result("t3")]
        units = rollup_units([t1, t2, t3], results, {}, correlation_key="paperclip.issue")
        # 1 unattributed unit + 1 issue-A unit = 2 units.
        assert len(units) == 2
        attributed = [u for u in units if u.correlation_key_value == "issue-A"]
        unattrib = [u for u in units if u.correlation_key_value is None]
        assert len(attributed) == 1
        assert len(unattrib) == 1
        assert len(attributed[0].trace_ids) == 2
        assert len(unattrib[0].trace_ids) == 1


# ─── Backward-compat: None key → unit == trace ──────────────────────────


class TestBackwardCompat:
    """When correlation_key is None, rollup produces one unit per trace,
    and each unit's outcome mirrors the per-trace OutcomeResult exactly."""

    def test_unit_count_equals_trace_count(self) -> None:
        traces = [_trace(f"t{i}", ["Edit"]) for i in range(5)]
        results = [_result(f"t{i}") for i in range(5)]
        units = rollup_units(traces, results, {}, correlation_key=None)
        assert len(units) == 5

    def test_outcome_mirrors_per_trace_pass(self) -> None:
        t = _trace("t1", ["Edit"])
        r = _result("t1", outcome_pass=True, computable=True)
        units = rollup_units([t], [r], {}, correlation_key=None)
        assert units[0].unit_outcome_pass is True
        assert units[0].unit_computable is True

    def test_outcome_mirrors_per_trace_fail(self) -> None:
        t = _trace("t1", ["Edit"])
        r = _result("t1", outcome_pass=False, computable=True)
        units = rollup_units([t], [r], {}, correlation_key=None)
        assert units[0].unit_outcome_pass is False

    def test_outcome_mirrors_noncomputable(self) -> None:
        t = _trace("t1", ["Edit"])
        r = _result("t1", computable=False)
        units = rollup_units([t], [r], {}, correlation_key=None)
        assert units[0].unit_computable is False
        assert units[0].unit_outcome_pass is None

    def test_empty_trace_list(self) -> None:
        units = rollup_units([], [], {}, correlation_key=None)
        assert units == []


# ─── Pipeline integration: unit_summaries present ───────────────────────


class TestPipelineIntegration:
    """run_pipeline produces unit_summaries on AnalysisResult."""

    def _minimal_context(self, *, correlation_key: str | None = None) -> BusinessContext:
        return BusinessContext(
            agent_name="A",
            agent_description="B",
            operations=[
                BusinessOperation(
                    name="Code",
                    description="code op",
                    expected_tools=["Edit"],
                    required_side_effect_tools=["Edit"],
                )
            ],
            correlation_key=correlation_key,
        )

    def test_unit_summaries_present_on_result(self) -> None:
        env = _trace("t1", ["Edit"])
        ctx = self._minimal_context()
        result = run_pipeline([env], ctx)
        # unit_summaries must exist (not None, not missing).
        assert result.unit_summaries is not None

    def test_none_key_unit_count_equals_trace_count(self) -> None:
        envs = [_trace(f"t{i}", ["Edit"]) for i in range(3)]
        ctx = self._minimal_context(correlation_key=None)
        result = run_pipeline(envs, ctx)
        # One unit per trace — but only mapped traces have OutcomeResults.
        # All traces map to "Code" (they call Edit), so 3 units.
        assert len(result.unit_summaries) == 3

    def test_with_correlation_key_groups_traces(self) -> None:
        t1 = _trace("t1", ["Edit"], correlation_key_value="issue-A")
        t2 = _trace("t2", ["Edit"], correlation_key_value="issue-A")
        t3 = _trace("t3", ["Edit"], correlation_key_value="issue-B")
        ctx = self._minimal_context(correlation_key="paperclip.issue")
        result = run_pipeline([t1, t2, t3], ctx)
        # 2 units: issue-A (2 traces) and issue-B (1 trace).
        assert len(result.unit_summaries) == 2
        by_ckv = {u.correlation_key_value: u for u in result.unit_summaries}
        assert len(by_ckv["issue-A"].trace_ids) == 2
        assert len(by_ckv["issue-B"].trace_ids) == 1

    def test_per_trace_results_still_available(self) -> None:
        """WorkflowSummary.outcome.per_trace_results must remain intact (not removed)."""
        env = _trace("t1", ["Edit"])
        ctx = self._minimal_context()
        result = run_pipeline([env], ctx)
        for ws in result.workflows:
            # per_trace_results is still present on the per-workflow summary.
            assert ws.outcome.per_trace_results is not None
