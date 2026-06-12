"""Day 3 outcome v2 tests.

Phase test matrix items (from sprint-exec-1-truth.md):
  W3 | "0 errors" tail              | pass
  W3 | status ERROR + clean text    | fail (rung 2 wins)
  W3 | status OK + "error" text     | pass (rung 2 wins)
  W3 | blocked_on_user session      | HUMAN_ESCALATION, pass-eligible, escalation_rate counts
  W3 | orphan parent span           | (Day 4 scope — not implemented yet)

Also covers:
  - failure_reason enum populated on every computable fail
  - evidence.step_index and evidence.rung populated on textual failures
  - human_escalation_rate in WorkflowOutcomeSummary
  - StepStatusSource propagation through the evidence ladder
  - ClaudeCodeNormalizer.step_outcome() — harness prefix detection
"""

from __future__ import annotations

import pytest

from kairos.analysis.outcome_metric import (
    OutcomeEvidence,
    OutcomeResult,
    WorkflowOutcomeSummary,
    _textual_failure,
    compute_outcome_rate,
    evaluate_outcome,
)
from kairos.models.enums import FailureReason, StepStatus, StepStatusSource, StepType, TerminalStatus
from kairos.models.trace import Step, TraceEnvelope
from kairos.normalization.agents.claude_code import ClaudeCodeNormalizer
from kairos.taxonomy.business_context import BusinessOperation

# ── Helpers ──────────────────────────────────────────────────────────────


def _op(*, required_side_effects: list[str] | None = None) -> BusinessOperation:
    return BusinessOperation(
        name="Test Workflow",
        description="test",
        expected_tools=["Write", "Bash"],
        priority="high",
        required_side_effect_tools=required_side_effects if required_side_effects is not None else ["Write"],
    )


def _step(
    i: int,
    tool: str,
    *,
    status: StepStatus = StepStatus.OK,
    status_source: StepStatusSource = StepStatusSource.NONE,
    output: str | None = "ok",
    error: str | None = None,
    attrs: dict | None = None,
) -> Step:
    return Step(
        step_index=i,
        step_type=StepType.TOOL_CALL,
        tool_name=tool,
        tool_output=output,
        status=status,
        status_source=status_source,
        error_message=error,
        attrs=attrs,
    )


def _trace(
    steps: list[Step],
    *,
    terminal: TerminalStatus = TerminalStatus.COMPLETED,
    trace_id: str = "t-test",
) -> TraceEnvelope:
    return TraceEnvelope(
        trace_id=trace_id,
        user_input="test",
        steps=steps,
        terminal_status=terminal,
    )


# ── W3: Rung 4 textual failure function ──────────────────────────────────


class TestTextualFailure:
    """Normative verdict table from spec (rung 4 standalone)."""

    @pytest.mark.parametrize(
        ("tail", "expected"),
        [
            # pass cases
            ("build complete. 0 errors, 0 warnings", False),
            ("no errors found", False),
            ("", False),
            ("all tests passed", False),
            ("no failures detected", False),
            ("zero errors reported", False),
            ("without errors", False),
            # fail cases
            ("Error: ENOENT no such file", True),
            ("exception: NullPointerException", True),
            ("validation failed: schema mismatch", True),
            ("access denied", True),
            ("submission failure", True),
        ],
    )
    def test_textual_failure_verdict_table(self, tail: str, expected: bool) -> None:
        assert _textual_failure(tail) is expected

    def test_textual_failure_only_last_500_chars(self) -> None:
        """Only the last 500 chars matter — earlier errors ignored."""
        prefix = "Error: old error\n" + "x" * 600
        suffix = "all good now"
        assert _textual_failure(prefix + suffix) is False

    def test_error_in_middle_with_clean_end_passes(self) -> None:
        """'error' earlier in the string but the last 500 chars are clean → pass."""
        output = "had an error earlier\n" + "x" * 600 + "\ncompleted successfully"
        assert _textual_failure(output) is False


# ── W3: status_source wins over rung 4 ───────────────────────────────────


class TestStatusSourceWinsOverTextual:
    """Rungs 1–3 short-circuit rung 4."""

    def test_status_ok_with_error_text_passes(self) -> None:
        """W3: status OK + 'error' text → pass (rung 2 wins, rung 4 never runs)."""
        op = _op()
        trace = _trace(
            [
                _step(
                    0,
                    "Write",
                    status=StepStatus.OK,
                    status_source=StepStatusSource.ATTR_SUCCESS,
                    output="had an error in the previous run but fixed it",
                )
            ]
        )
        result = evaluate_outcome(trace, op)
        assert result.outcome_pass is True
        assert result.computable is True

    def test_status_error_with_clean_text_fails(self) -> None:
        """W3: status ERROR + clean text → fail (rung 2 wins)."""
        op = _op()
        trace = _trace(
            [
                _step(
                    0,
                    "Write",
                    status=StepStatus.ERROR,
                    status_source=StepStatusSource.OTEL_STATUS,
                    output="completed successfully",  # clean text, but status says ERROR
                    error="internal error",
                )
            ]
        )
        result = evaluate_outcome(trace, op)
        # Critical tool error: Write errored with no recovery.
        assert result.outcome_pass is False
        assert result.computable is True
        assert result.failure_reason == FailureReason.CRITICAL_TOOL_ERROR

    def test_textual_failure_only_when_status_source_none(self) -> None:
        """Rung 4 fires only when status_source == NONE."""
        op = _op()
        # status_source=NONE + failure text → rung 4 fires → side_effect_output_failed
        trace = _trace(
            [
                _step(
                    0,
                    "Write",
                    status=StepStatus.OK,
                    status_source=StepStatusSource.NONE,
                    output="validation failed: schema error",
                )
            ]
        )
        result = evaluate_outcome(trace, op)
        assert result.outcome_pass is False
        assert result.computable is True
        assert result.failure_reason == FailureReason.SIDE_EFFECT_OUTPUT_FAILED

    def test_textual_pass_when_status_source_none_and_clean(self) -> None:
        """status_source=NONE + clean output → pass (rung 4 says OK)."""
        op = _op()
        trace = _trace(
            [
                _step(
                    0,
                    "Write",
                    status=StepStatus.OK,
                    status_source=StepStatusSource.NONE,
                    output="file written successfully",
                )
            ]
        )
        result = evaluate_outcome(trace, op)
        assert result.outcome_pass is True


# ── W3: failure_reason enum populated ────────────────────────────────────


class TestFailureReasonEnum:
    """Every computable fail must have failure_reason set."""

    def test_terminal_error_reason(self) -> None:
        op = _op()
        trace = _trace([], terminal=TerminalStatus.ERROR)
        result = evaluate_outcome(trace, op)
        assert result.outcome_pass is False
        assert result.computable is True
        assert result.failure_reason == FailureReason.TERMINAL_ERROR

    def test_terminal_unknown_reason(self) -> None:
        op = _op()
        trace = _trace([], terminal=TerminalStatus.UNKNOWN)
        result = evaluate_outcome(trace, op)
        assert result.computable is False
        assert result.failure_reason == FailureReason.TERMINAL_UNKNOWN

    def test_critical_tool_error_reason(self) -> None:
        op = _op()
        trace = _trace(
            [
                _step(0, "Write", status=StepStatus.ERROR, output=None, error="disk full"),
            ]
        )
        result = evaluate_outcome(trace, op)
        assert result.outcome_pass is False
        assert result.computable is True
        assert result.failure_reason == FailureReason.CRITICAL_TOOL_ERROR

    def test_missing_side_effect_reason(self) -> None:
        op = _op()
        # No Write call at all
        trace = _trace([_step(0, "Bash", output="ran bash")])
        result = evaluate_outcome(trace, op)
        assert result.outcome_pass is False
        assert result.computable is True
        assert result.failure_reason == FailureReason.MISSING_SIDE_EFFECT

    def test_side_effect_output_failed_reason(self) -> None:
        op = _op()
        # Write succeeded (OK status) but rung 4 textual says failed.
        trace = _trace(
            [
                _step(
                    0,
                    "Write",
                    status=StepStatus.OK,
                    status_source=StepStatusSource.NONE,
                    output="validation failed: bad schema",
                )
            ]
        )
        result = evaluate_outcome(trace, op)
        assert result.outcome_pass is False
        assert result.computable is True
        assert result.failure_reason == FailureReason.SIDE_EFFECT_OUTPUT_FAILED

    def test_pass_has_no_failure_reason(self) -> None:
        op = _op()
        trace = _trace([_step(0, "Write", status=StepStatus.OK, output="written")])
        result = evaluate_outcome(trace, op)
        assert result.outcome_pass is True
        assert result.failure_reason is None

    def test_partial_trace_enum_exists(self) -> None:
        """FailureReason.PARTIAL_TRACE exists (Day 4 will wire it)."""
        assert FailureReason.PARTIAL_TRACE == "partial_trace"


# ── W3: HUMAN_ESCALATION is pass-eligible ────────────────────────────────


class TestHumanEscalation:
    """W3: blocked_on_user session → HUMAN_ESCALATION, pass-eligible."""

    def test_human_escalation_passes_outcome(self) -> None:
        op = _op()
        trace = _trace(
            [_step(0, "Write", status=StepStatus.OK, output="written")],
            terminal=TerminalStatus.HUMAN_ESCALATION,
        )
        result = evaluate_outcome(trace, op)
        assert result.outcome_pass is True
        assert result.computable is True
        assert result.failure_reason is None

    def test_human_escalation_counted_in_escalation_rate(self) -> None:
        """W3: escalation_rate = escalated / computable."""
        op = _op()
        traces = [
            _trace([_step(0, "Write", output="ok")], terminal=TerminalStatus.COMPLETED, trace_id="t1"),
            _trace(
                [_step(0, "Write", output="ok")],
                terminal=TerminalStatus.HUMAN_ESCALATION,
                trace_id="t2",
            ),
            _trace(
                [_step(0, "Write", output="ok")],
                terminal=TerminalStatus.HUMAN_ESCALATION,
                trace_id="t3",
            ),
        ]
        summary = compute_outcome_rate(traces, op)
        assert summary.computable_count == 3
        assert summary.human_escalation_rate is not None
        assert summary.human_escalation_rate == pytest.approx(2 / 3, rel=1e-3)

    def test_human_escalation_rate_none_when_no_computable(self) -> None:
        op = _op()
        traces = [_trace([], terminal=TerminalStatus.UNKNOWN, trace_id=f"t{i}") for i in range(3)]
        summary = compute_outcome_rate(traces, op)
        assert summary.human_escalation_rate is None

    def test_human_escalation_rate_zero_when_none_escalated(self) -> None:
        op = _op()
        traces = [
            _trace([_step(0, "Write", output="ok")], terminal=TerminalStatus.COMPLETED, trace_id=f"t{i}")
            for i in range(3)
        ]
        summary = compute_outcome_rate(traces, op)
        assert summary.human_escalation_rate == pytest.approx(0.0)

    def test_human_escalation_present_in_summary_fields(self) -> None:
        op = _op()
        traces = [_trace([_step(0, "Write", output="ok")], trace_id="t1")]
        summary = compute_outcome_rate(traces, op)
        assert isinstance(summary, WorkflowOutcomeSummary)
        assert hasattr(summary, "human_escalation_rate")


# ── W3: ClaudeCodeNormalizer.step_outcome hook ───────────────────────────


class TestClaudeCodeStepOutcome:
    """Rung 3: adapter extractor for ClaudeCode."""

    def setup_method(self) -> None:
        self._normalizer = ClaudeCodeNormalizer()

    def test_no_opinion_on_step_without_signals(self) -> None:
        step = _step(0, "Read", output="file contents")
        assert self._normalizer.step_outcome(step) is None

    def test_harness_error_prefix_error(self) -> None:
        step = _step(0, "Read", output="Error: file not found")
        assert self._normalizer.step_outcome(step) == StepStatus.ERROR

    def test_input_validation_error_prefix(self) -> None:
        step = _step(0, "Edit", output="InputValidationError: missing required field")
        assert self._normalizer.step_outcome(step) == StepStatus.ERROR

    def test_permission_error_prefix(self) -> None:
        step = _step(0, "Bash", output="PermissionError: access denied to /etc/shadow")
        assert self._normalizer.step_outcome(step) == StepStatus.ERROR

    def test_error_not_at_char_0_is_no_opinion(self) -> None:
        """'Error:' mid-string (not at char 0) must not fire the prefix check."""
        step = _step(0, "Bash", output="ran command. Error: something happened mid-output")
        assert self._normalizer.step_outcome(step) is None

    def test_exit_code_zero_ok(self) -> None:
        step = _step(0, "Bash", attrs={"exit_code": 0})
        assert self._normalizer.step_outcome(step) == StepStatus.OK

    def test_exit_code_nonzero_error(self) -> None:
        step = _step(0, "Bash", attrs={"exit_code": 1})
        assert self._normalizer.step_outcome(step) == StepStatus.ERROR

    def test_exit_code_string_coerced(self) -> None:
        step = _step(0, "Bash", attrs={"exit_code": "0"})
        assert self._normalizer.step_outcome(step) == StepStatus.OK

    def test_exit_code_absent_falls_through_to_prefix_check(self) -> None:
        step = _step(0, "Bash", output="Error: something", attrs={"duration_ms": 100})
        assert self._normalizer.step_outcome(step) == StepStatus.ERROR

    def test_none_output_is_no_opinion(self) -> None:
        step = _step(0, "Read", output=None)
        assert self._normalizer.step_outcome(step) is None


# ── W3: StepStatusSource propagation ─────────────────────────────────────


class TestStepStatusSource:
    """status_source is present on Step and defaults to NONE."""

    def test_step_default_status_source_none(self) -> None:
        step = Step(step_index=0, step_type=StepType.TOOL_CALL)
        assert step.status_source == StepStatusSource.NONE

    def test_step_status_source_attr_success(self) -> None:
        step = _step(0, "Bash", status_source=StepStatusSource.ATTR_SUCCESS)
        assert step.status_source == StepStatusSource.ATTR_SUCCESS

    def test_all_source_values_defined(self) -> None:
        for val in ["attr_success", "otel_status", "kairos_outcome", "adapter", "textual", "none"]:
            assert StepStatusSource(val)

    def test_outcome_evidence_fields(self) -> None:
        evidence = OutcomeEvidence(step_index=5, rung=4)
        assert evidence.step_index == 5
        assert evidence.rung == 4

    def test_outcome_result_has_failure_reason_and_evidence(self) -> None:
        result = OutcomeResult(
            trace_id="t1",
            outcome_pass=False,
            computable=True,
            reason="test",
            failure_reason=FailureReason.TERMINAL_ERROR,
            evidence=OutcomeEvidence(step_index=0, rung=2),
        )
        assert result.failure_reason == FailureReason.TERMINAL_ERROR
        assert result.evidence.rung == 2


# ── W3: tau-bench regression ─────────────────────────────────────────────


class TestTauBenchRegression:
    """Rung 4 textual tier still works when structured signals absent (tau-bench corpus)."""

    def test_textual_failure_fires_on_unstructured_trace(self) -> None:
        """Traces without status_source signal rely on rung 4."""
        op = _op()
        trace = _trace(
            [_step(0, "Write", status_source=StepStatusSource.NONE, output="validation failed: required field missing")]
        )
        result = evaluate_outcome(trace, op)
        assert result.outcome_pass is False
        assert result.failure_reason == FailureReason.SIDE_EFFECT_OUTPUT_FAILED

    def test_textual_pass_fires_on_unstructured_trace(self) -> None:
        """Clean output with no status_source → pass."""
        op = _op()
        trace = _trace([_step(0, "Write", status_source=StepStatusSource.NONE, output="written 42 bytes to file.py")])
        result = evaluate_outcome(trace, op)
        assert result.outcome_pass is True

    def test_empty_output_with_ok_status_passes(self) -> None:
        """Empty output + OK status (OWNER-DECISION default: silence + OK = consent)."""
        op = _op()
        trace = _trace([_step(0, "Write", status=StepStatus.OK, status_source=StepStatusSource.NONE, output="")])
        result = evaluate_outcome(trace, op)
        assert result.outcome_pass is True

    def test_0_errors_tail_passes(self) -> None:
        """Spec verdict table: '0 errors' → pass."""
        assert _textual_failure("build complete. 0 errors, 0 warnings") is False

    def test_no_errors_found_passes(self) -> None:
        """Spec verdict table: 'no errors found' → pass."""
        assert _textual_failure("linter ran: no errors found") is False


# ── Day 4 fix: structured status satisfies side-effect without output ─────


class TestStructuredEvidenceSideEffect:
    """Condition 4: structured status_source (rungs 1–3) verifies a side-effect
    call WITHOUT readable output. Live claude_code spans carry no tool_output;
    before this fix, pass was structurally impossible on live data.
    """

    def test_live_shaped_trace_passes_with_structured_ok_no_output(self) -> None:
        """Success attrs + no outputs + all side-effect tools OK → computable PASS."""
        op = _op()
        trace = _trace(
            [
                _step(
                    0,
                    "Write",
                    status=StepStatus.OK,
                    status_source=StepStatusSource.ATTR_SUCCESS,
                    output=None,  # live claude_code: no tool_output instrumented
                ),
                _step(
                    1,
                    "Bash",
                    status=StepStatus.OK,
                    status_source=StepStatusSource.ATTR_SUCCESS,
                    output=None,
                ),
            ]
        )
        result = evaluate_outcome(trace, op)
        assert result.computable is True
        assert result.outcome_pass is True
        assert result.failure_reason is None

    @pytest.mark.parametrize(
        "source",
        [
            StepStatusSource.ATTR_SUCCESS,
            StepStatusSource.OTEL_STATUS,
            StepStatusSource.KAIROS_OUTCOME,
            StepStatusSource.ADAPTER,
        ],
    )
    def test_every_structured_source_satisfies_without_output(self, source: StepStatusSource) -> None:
        """All four structured rung sources count as verified evidence."""
        op = _op()
        trace = _trace([_step(0, "Write", status=StepStatus.OK, status_source=source, output=None)])
        result = evaluate_outcome(trace, op)
        assert result.computable is True
        assert result.outcome_pass is True

    def test_no_output_and_status_source_none_still_non_computable(self) -> None:
        """Successful call, no output, status_source NONE → genuinely no evidence → non-computable."""
        op = _op()
        trace = _trace(
            [_step(0, "Write", status=StepStatus.OK, status_source=StepStatusSource.NONE, output=None)]
        )
        result = evaluate_outcome(trace, op)
        assert result.computable is False
        assert result.reason == "side effect computability unknown"

    def test_side_effect_tool_absent_still_fails_missing_side_effect(self) -> None:
        """Side-effect tool absent → computable FAIL missing_side_effect (unchanged)."""
        op = _op()
        trace = _trace(
            [_step(0, "Bash", status=StepStatus.OK, status_source=StepStatusSource.ATTR_SUCCESS, output=None)]
        )
        result = evaluate_outcome(trace, op)
        assert result.computable is True
        assert result.outcome_pass is False
        assert result.failure_reason == FailureReason.MISSING_SIDE_EFFECT

    def test_readable_failing_outputs_downgrade_structured_ok(self) -> None:
        """Outputs exist and ALL readable outputs fail → side_effect_output_failed,
        even when another successful call of the same tool carries structured OK.
        Readable text contradicting silence is surfaced, not suppressed.
        """
        op = _op()
        trace = _trace(
            [
                # Structured OK, no output (live-shaped).
                _step(
                    0,
                    "Write",
                    status=StepStatus.OK,
                    status_source=StepStatusSource.ATTR_SUCCESS,
                    output=None,
                ),
                # Unstructured call with failing readable output.
                _step(
                    1,
                    "Write",
                    status=StepStatus.OK,
                    status_source=StepStatusSource.NONE,
                    output="validation failed: schema mismatch",
                ),
            ]
        )
        result = evaluate_outcome(trace, op)
        assert result.computable is True
        assert result.outcome_pass is False
        assert result.failure_reason == FailureReason.SIDE_EFFECT_OUTPUT_FAILED
        assert result.evidence.step_index == 1
        assert result.evidence.rung == 4

    def test_clean_readable_output_still_passes_regardless_of_source(self) -> None:
        """tau-bench-shaped: outputs present, no status attrs → existing behavior unchanged."""
        op = _op()
        trace = _trace(
            [_step(0, "Write", status=StepStatus.OK, status_source=StepStatusSource.NONE, output="written ok")]
        )
        result = evaluate_outcome(trace, op)
        assert result.computable is True
        assert result.outcome_pass is True
