"""Tests for the week-1 outcome metric evaluator.

Target module (not yet implemented):
    src.kairos.analysis.outcome_metric

Expected surface:
    @dataclass OutcomeResult(trace_id, outcome_pass: bool, computable: bool, reason: str | None)
    @dataclass WorkflowOutcomeSummary(
        workflow_name,
        total_traces,
        computable_count,
        passed_count,
        outcome_rate: float | None,
        pending_reason: str | None,
    )
    def evaluate_outcome(trace: TraceEnvelope, operation: BusinessOperation) -> OutcomeResult
    def compute_outcome_rate(
        traces: list[TraceEnvelope], operation: BusinessOperation,
    ) -> WorkflowOutcomeSummary
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kairos.analysis.outcome_metric import (
    OutcomeResult,
    WorkflowOutcomeSummary,
    compute_outcome_rate,
    evaluate_outcome,
)
from kairos.models.enums import StepStatus, StepType, TerminalStatus
from kairos.models.trace import Step, TraceEnvelope
from kairos.taxonomy.business_context import BusinessOperation

FIXTURES = Path(__file__).parent.parent / "fixtures"
WORKFLOW_YAML = FIXTURES / "week1_workflow_mapping.yaml"


# ── Synthetic envelope helpers ─────────────────────────────────────────


def _hr_operation(
    *,
    required_side_effects: list[str] | None = None,
) -> BusinessOperation:
    """Build the canonical HR screening operation used throughout this suite."""
    return BusinessOperation(
        name="Candidate Screening",
        description="Evaluate one candidate end-to-end",
        expected_tools=["get_rubric", "parse_resume", "submit_evaluation"],
        priority="high",
        business_goal="Reduce recruiter review time.",
        reliability_metric="percent of completed screenings with full evidence.",
        bad_run_means="Missing evidence or unsupported recommendation.",
        required_side_effect_tools=required_side_effects
        if required_side_effects is not None
        else ["submit_evaluation"],
    )


def _step(
    i: int,
    tool: str,
    *,
    status: StepStatus = StepStatus.OK,
    output: str | None = "ok",
    error: str | None = None,
) -> Step:
    return Step(
        step_index=i,
        step_type=StepType.TOOL_CALL,
        tool_name=tool,
        tool_args={"stub": True},
        tool_output=output,
        status=status,
        error_message=error,
    )


def _happy_trace(trace_id: str = "t-happy") -> TraceEnvelope:
    return TraceEnvelope(
        trace_id=trace_id,
        user_input="evaluate candidate",
        steps=[
            _step(0, "get_rubric"),
            _step(1, "parse_resume"),
            _step(2, "submit_evaluation", output="submitted"),
        ],
        terminal_status=TerminalStatus.COMPLETED,
    )


# ── TESTS ─────────────────────────────────────────────────────────────


class TestEvaluateOutcomePass:
    """Happy-path evaluation."""

    def test_outcome_pass_happy_path(self) -> None:
        op = _hr_operation()
        trace = _happy_trace()
        result = evaluate_outcome(trace, op)
        assert isinstance(result, OutcomeResult)
        assert result.outcome_pass is True
        assert result.computable is True


class TestEvaluateOutcomeFailureModes:
    """Failure modes across the four outcome_pass conditions."""

    def test_outcome_fail_by_terminal_error(self) -> None:
        op = _hr_operation()
        trace = TraceEnvelope(
            trace_id="t-term-error",
            user_input="evaluate candidate",
            steps=[
                _step(0, "get_rubric"),
                _step(1, "parse_resume"),
                _step(2, "submit_evaluation", output="submitted"),
            ],
            terminal_status=TerminalStatus.ERROR,
        )
        result = evaluate_outcome(trace, op)
        assert result.outcome_pass is False
        assert result.computable is True
        assert result.reason is not None
        assert "terminal" in result.reason.lower()

    def test_outcome_fail_by_missing_distinctive_tool(self) -> None:
        # Post-fix: coverage==1.0 applies to required_side_effect_tools, not
        # all expected_tools. An op declaring two distinctive tools must have
        # BOTH succeed for outcome pass; optional context tools like get_rubric
        # no longer gate the outcome.
        op = _hr_operation(required_side_effects=["submit_evaluation", "shortlist_candidate"])
        trace = TraceEnvelope(
            trace_id="t-missing-tool",
            user_input="evaluate candidate",
            steps=[
                _step(0, "get_rubric"),
                _step(1, "parse_resume"),
                # submit_evaluation succeeds but shortlist_candidate never called
                _step(2, "submit_evaluation", output="submitted"),
            ],
            terminal_status=TerminalStatus.COMPLETED,
        )
        result = evaluate_outcome(trace, op)
        assert result.outcome_pass is False
        assert result.computable is True
        assert result.reason is not None
        assert "coverage" in result.reason.lower() or "tool" in result.reason.lower()

    def test_missing_optional_expected_tool_does_not_fail(self) -> None:
        # Regression test for rule-too-strict fix #1: expected_tools beyond the
        # declared distinctive set shouldn't gate outcome. The agent here skips
        # parse_resume (used parallel_gather for a batch flow) but successfully
        # submits — this must pass.
        op = _hr_operation()  # required = ["submit_evaluation"]
        trace = TraceEnvelope(
            trace_id="t-optional-skip",
            user_input="evaluate candidate",
            steps=[
                _step(0, "get_rubric"),
                # parse_resume intentionally not called
                _step(1, "submit_evaluation", output="submitted"),
            ],
            terminal_status=TerminalStatus.COMPLETED,
        )
        result = evaluate_outcome(trace, op)
        assert result.outcome_pass is True
        assert result.computable is True

    def test_outcome_fail_by_critical_tool_error(self) -> None:
        """Expected tool errors and is never retried -> critical tool error."""
        op = _hr_operation()
        trace = TraceEnvelope(
            trace_id="t-critical-err",
            user_input="evaluate candidate",
            steps=[
                _step(0, "get_rubric"),
                _step(
                    1,
                    "parse_resume",
                    status=StepStatus.ERROR,
                    output=None,
                    error="parse failed",
                ),
                _step(2, "submit_evaluation", output="submitted"),
            ],
            terminal_status=TerminalStatus.COMPLETED,
        )
        result = evaluate_outcome(trace, op)
        assert result.outcome_pass is False
        assert result.computable is True
        assert result.reason is not None
        assert "critical" in result.reason.lower() or "error" in result.reason.lower()

    def test_outcome_fail_by_missing_side_effect(self) -> None:
        """required_side_effect_tools declared, but no successful call for them."""
        op = _hr_operation(required_side_effects=["submit_evaluation"])
        # submit_evaluation errors with no later success
        trace = TraceEnvelope(
            trace_id="t-missing-side-effect",
            user_input="evaluate candidate",
            steps=[
                _step(0, "get_rubric"),
                _step(1, "parse_resume"),
                _step(
                    2,
                    "submit_evaluation",
                    status=StepStatus.ERROR,
                    output="validation failed",
                    error="submission rejected",
                ),
            ],
            terminal_status=TerminalStatus.COMPLETED,
        )
        result = evaluate_outcome(trace, op)
        assert result.outcome_pass is False
        assert result.computable is True

    def test_late_tool_error_after_earlier_success_does_not_fail(self) -> None:
        # Regression test for rule-too-strict fix #3: critical_tool_error now
        # checks past-or-future for recovery. parse_resume succeeds at step 1
        # (serving the main evaluation), then fails at step 3 on a separate
        # file read. The main workflow already completed — late failure on
        # the same tool should not fail the outcome.
        op = _hr_operation()
        trace = TraceEnvelope(
            trace_id="t-late-parse-error",
            user_input="evaluate candidate",
            steps=[
                _step(0, "get_rubric"),
                _step(1, "parse_resume"),
                _step(2, "submit_evaluation", output="submitted"),
                _step(
                    3,
                    "parse_resume",
                    status=StepStatus.ERROR,
                    output=None,
                    error="No such file or directory",
                ),
            ],
            terminal_status=TerminalStatus.COMPLETED,
        )
        result = evaluate_outcome(trace, op)
        assert result.outcome_pass is True
        assert result.computable is True

    def test_multi_submit_one_error_among_successes_passes(self) -> None:
        # Regression test for rule-too-strict fix #2: _side_effect_result is
        # any-of across successful calls. 2 clean submit outputs + 1 noisy
        # output containing "Error invoking tool" text must pass, because at
        # least one submission was clean. (Previously short-circuited on the
        # first failure marker.)
        op = _hr_operation()
        trace = TraceEnvelope(
            trace_id="t-multi-submit",
            user_input="evaluate candidate",
            steps=[
                _step(0, "get_rubric"),
                _step(1, "parse_resume"),
                _step(2, "submit_evaluation", output='{"success": true, "id": "a"}'),
                _step(3, "submit_evaluation", output="Error invoking tool 'submit_evaluation' with kwargs"),
                _step(4, "submit_evaluation", output='{"success": true, "id": "b"}'),
            ],
            terminal_status=TerminalStatus.COMPLETED,
        )
        result = evaluate_outcome(trace, op)
        assert result.outcome_pass is True
        assert result.computable is True

    def test_all_submits_have_failure_markers_still_fails(self) -> None:
        # Under any-of semantics: if every successful submit output has a
        # failure marker, the side-effect check must still fail.
        op = _hr_operation()
        trace = TraceEnvelope(
            trace_id="t-all-failed-submits",
            user_input="evaluate candidate",
            steps=[
                _step(0, "get_rubric"),
                _step(1, "parse_resume"),
                _step(2, "submit_evaluation", output="validation failed"),
                _step(3, "submit_evaluation", output="submission denied"),
            ],
            terminal_status=TerminalStatus.COMPLETED,
        )
        result = evaluate_outcome(trace, op)
        assert result.outcome_pass is False
        assert result.computable is True
        assert result.reason is not None
        assert "side_effect" in result.reason.lower()

    def test_non_critical_tool_error_with_retry_passes(self) -> None:
        """Expected tool errors once then succeeds -> pass."""
        op = _hr_operation()
        trace = TraceEnvelope(
            trace_id="t-retry",
            user_input="evaluate candidate",
            steps=[
                _step(0, "get_rubric"),
                _step(
                    1,
                    "parse_resume",
                    status=StepStatus.ERROR,
                    output=None,
                    error="transient parse error",
                ),
                _step(2, "parse_resume"),  # successful retry
                _step(3, "submit_evaluation", output="submitted"),
            ],
            terminal_status=TerminalStatus.COMPLETED,
        )
        result = evaluate_outcome(trace, op)
        assert result.outcome_pass is True
        assert result.computable is True


class TestEvaluateOutcomePending:
    """When the outcome can't be computed, result is pending."""

    def test_pending_when_terminal_status_missing(self) -> None:
        op = _hr_operation()
        trace = TraceEnvelope(
            trace_id="t-pending-terminal",
            user_input="evaluate candidate",
            steps=[
                _step(0, "get_rubric"),
                _step(1, "parse_resume"),
                _step(2, "submit_evaluation", output="submitted"),
            ],
            terminal_status=TerminalStatus.UNKNOWN,
        )
        result = evaluate_outcome(trace, op)
        assert result.computable is False
        assert result.reason is not None
        assert "terminal" in result.reason.lower() or "status" in result.reason.lower()

    def test_pending_when_required_tool_coverage_not_computable(self) -> None:
        """If no step has reliable status info, tool-success becomes non-computable."""
        op = _hr_operation()
        trace = TraceEnvelope(
            trace_id="t-pending-cov",
            user_input="evaluate candidate",
            # No TOOL_CALL steps at all -> coverage is not computable for an op that
            # expects tools.
            steps=[],
            terminal_status=TerminalStatus.UNKNOWN,
        )
        result = evaluate_outcome(trace, op)
        assert result.computable is False
        assert result.reason is not None


class TestRequiredSideEffectsOptional:
    """Operations without required_side_effect_tools should not block pass."""

    def test_required_side_effect_tools_empty_does_not_block_pass(self) -> None:
        op = _hr_operation(required_side_effects=[])
        trace = _happy_trace()
        result = evaluate_outcome(trace, op)
        assert result.outcome_pass is True
        assert result.computable is True


class TestComputeOutcomeRate:
    """Aggregation across a population of traces."""

    def test_compute_outcome_rate_aggregates(self) -> None:
        op = _hr_operation()
        traces = [
            _happy_trace("t-pass-1"),
            _happy_trace("t-pass-2"),
            TraceEnvelope(
                trace_id="t-fail",
                user_input="evaluate candidate",
                steps=[
                    _step(0, "get_rubric"),
                    _step(1, "parse_resume"),
                    _step(2, "submit_evaluation"),
                ],
                terminal_status=TerminalStatus.ERROR,
            ),
        ]
        summary = compute_outcome_rate(traces, op)
        assert isinstance(summary, WorkflowOutcomeSummary)
        assert summary.workflow_name == op.name
        assert summary.total_traces == 3
        assert summary.computable_count == 3
        assert summary.passed_count == 2
        assert summary.outcome_rate is not None
        assert summary.outcome_rate == pytest.approx(2 / 3, rel=1e-3)
        assert summary.pending_reason is None

    def test_compute_outcome_rate_all_pending_returns_pending_reason(self) -> None:
        op = _hr_operation()
        traces = [
            TraceEnvelope(
                trace_id=f"t-pending-{i}",
                user_input="evaluate candidate",
                steps=[],
                terminal_status=TerminalStatus.UNKNOWN,
            )
            for i in range(3)
        ]
        summary = compute_outcome_rate(traces, op)
        assert summary.total_traces == 3
        assert summary.computable_count == 0
        assert summary.outcome_rate is None
        assert summary.pending_reason is not None
        assert len(summary.pending_reason) > 0
