"""Outcome metric evaluator for week-1 analysis.

Evaluates a single trace against a BusinessOperation using a 4-condition
pass/fail formula, and aggregates results across a population into a
WorkflowOutcomeSummary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from kairos.log import get_logger
from kairos.models.enums import StepStatus, TerminalStatus

if TYPE_CHECKING:
    from kairos.models.trace import Step, TraceEnvelope
    from kairos.taxonomy.business_context import BusinessOperation

logger = get_logger(__name__)


# Terminal statuses that indicate hard failure at the trace level.
_TERMINAL_FAILURE_STATUSES: frozenset[TerminalStatus] = frozenset(
    {TerminalStatus.ERROR, TerminalStatus.TIMEOUT},
)

# Failure markers in tool_output that imply the side-effect call actually failed.
_SIDE_EFFECT_FAILURE_MARKERS: tuple[str, ...] = (
    "failure",
    "failed",
    "error",
    "exception",
    "denied",
    "not submitted",
    "validation failed",
)


@dataclass
class OutcomeResult:
    """Per-trace outcome evaluation."""

    trace_id: str
    outcome_pass: bool
    computable: bool
    reason: str | None


@dataclass
class WorkflowOutcomeSummary:
    """Aggregated outcome rate across a population of traces."""

    workflow_name: str
    total_traces: int
    computable_count: int
    passed_count: int
    outcome_rate: float | None
    pending_reason: str | None


def _is_successful_tool_step(step: Step, tool_name: str) -> bool:
    """A step is a successful call to ``tool_name`` if the tool matches, status is OK, no error_message."""
    if step.tool_name != tool_name:
        return False
    if step.status != StepStatus.OK:
        return False
    return not step.error_message


def _required_tool_coverage(trace: TraceEnvelope, expected_tools: list[str]) -> float:
    """Fraction of expected_tools with at least one successful step in the trace."""
    if not expected_tools:
        return 1.0
    satisfied = 0
    for tool_name in expected_tools:
        if any(_is_successful_tool_step(step, tool_name) for step in trace.steps):
            satisfied += 1
    return satisfied / len(expected_tools)


def _has_critical_tool_error(
    trace: TraceEnvelope,
    expected_tools: list[str],
    side_effect_tools: list[str],
) -> bool:
    """A critical tool error is an expected- or side-effect-tool error with no successful call anywhere.

    Recovery is past-or-future: if the same tool succeeded at least once anywhere in the
    trace (earlier retry, later retry, or a successful invocation elsewhere in the flow),
    the error is considered handled. This stops flagging traces where a tool did its job
    and then failed on a separate, unrelated call later in the session.
    """
    watched = set(expected_tools) | set(side_effect_tools)
    if not watched:
        return False

    steps = trace.steps
    for i, step in enumerate(steps):
        if step.tool_name is None:
            continue
        if step.tool_name not in watched:
            continue
        if step.status != StepStatus.ERROR:
            continue

        # Recovery = any successful call of the same tool anywhere in the trace.
        recovered = any(j != i and _is_successful_tool_step(other, step.tool_name) for j, other in enumerate(steps))
        if not recovered:
            return True

    return False


def _side_effect_result(
    trace: TraceEnvelope,
    side_effect_tools: list[str],
) -> tuple[bool, bool, str | None]:
    """Evaluate the final required side-effect condition.

    Returns ``(passed, computable, reason)``.
    """
    if not side_effect_tools:
        return True, True, None

    for tool_name in side_effect_tools:
        successful_calls = [step for step in trace.steps if _is_successful_tool_step(step, tool_name)]
        if not successful_calls:
            # Any attempted calls? A call with missing status is non-computable.
            attempts = [step for step in trace.steps if step.tool_name == tool_name]
            if attempts and all(step.tool_output is None and step.status == StepStatus.OK for step in attempts):
                return False, False, "side effect computability unknown"
            return False, True, "missing_side_effect"

        # Multi-call any-of: the side-effect passes if at least one successful call
        # has a clean tool_output. It only fails when every successful call either
        # has a failure marker or an unreadable output.
        any_clean = False
        any_readable = False
        for call in successful_calls:
            if call.tool_output is None:
                continue
            any_readable = True
            output_lower = call.tool_output.lower()
            if not any(marker in output_lower for marker in _SIDE_EFFECT_FAILURE_MARKERS):
                any_clean = True
                break

        if not any_readable:
            return False, False, "side effect computability unknown"
        if not any_clean:
            return False, True, "missing_side_effect"

    return True, True, None


def evaluate_outcome(trace: TraceEnvelope, operation: BusinessOperation) -> OutcomeResult:
    """Evaluate a trace against the 4-condition outcome formula.

    Returns an OutcomeResult with ``outcome_pass``, ``computable``, and an
    optional ``reason`` citing which condition failed.
    """
    # Condition 1: terminal status
    if trace.terminal_status == TerminalStatus.UNKNOWN:
        return OutcomeResult(
            trace_id=trace.trace_id,
            outcome_pass=False,
            computable=False,
            reason="terminal_status missing",
        )

    if trace.terminal_status in _TERMINAL_FAILURE_STATUSES:
        return OutcomeResult(
            trace_id=trace.trace_id,
            outcome_pass=False,
            computable=True,
            reason="terminal_error",
        )

    # Terminal status must be COMPLETED (or HUMAN_ESCALATION) to pass.
    if trace.terminal_status != TerminalStatus.COMPLETED:
        return OutcomeResult(
            trace_id=trace.trace_id,
            outcome_pass=False,
            computable=True,
            reason="terminal_error",
        )

    # Condition 2: required tool coverage is computable
    if operation.expected_tools and not trace.steps:
        return OutcomeResult(
            trace_id=trace.trace_id,
            outcome_pass=False,
            computable=False,
            reason="tool status not computable",
        )

    # Condition 3: no critical tool error (checked before coverage so an
    # unrecovered error on an expected tool surfaces as a critical error
    # rather than as a generic "missing tool").
    if _has_critical_tool_error(trace, operation.expected_tools, operation.required_side_effect_tools):
        return OutcomeResult(
            trace_id=trace.trace_id,
            outcome_pass=False,
            computable=True,
            reason="critical_tool_error",
        )

    # Condition 2 continued: distinctive tool coverage must be 1.0. ``expected_tools``
    # is the broader context signal (used for recall-based membership); the outcome
    # check only requires that every declared distinctive tool actually succeeded.
    # Optional/utility tools like memory persistence don't gate outcome here.
    if operation.required_side_effect_tools:
        coverage = _required_tool_coverage(trace, operation.required_side_effect_tools)
        if coverage < 1.0:
            return OutcomeResult(
                trace_id=trace.trace_id,
                outcome_pass=False,
                computable=True,
                reason="missing_required_tool (coverage<1.0)",
            )

    # Condition 4: final required side-effect
    side_passed, side_computable, side_reason = _side_effect_result(trace, operation.required_side_effect_tools)
    if not side_computable:
        return OutcomeResult(
            trace_id=trace.trace_id,
            outcome_pass=False,
            computable=False,
            reason=side_reason,
        )
    if not side_passed:
        return OutcomeResult(
            trace_id=trace.trace_id,
            outcome_pass=False,
            computable=True,
            reason=side_reason,
        )

    return OutcomeResult(
        trace_id=trace.trace_id,
        outcome_pass=True,
        computable=True,
        reason=None,
    )


def compute_outcome_rate(
    traces: list[TraceEnvelope],
    operation: BusinessOperation,
) -> WorkflowOutcomeSummary:
    """Aggregate outcome evaluation across a population of traces."""
    results = [evaluate_outcome(trace, operation) for trace in traces]

    computable_count = sum(1 for r in results if r.computable)
    passed_count = sum(1 for r in results if r.outcome_pass)
    total = len(traces)

    if computable_count == 0:
        summary = WorkflowOutcomeSummary(
            workflow_name=operation.name,
            total_traces=total,
            computable_count=0,
            passed_count=passed_count,
            outcome_rate=None,
            pending_reason="no computable traces",
        )
    else:
        summary = WorkflowOutcomeSummary(
            workflow_name=operation.name,
            total_traces=total,
            computable_count=computable_count,
            passed_count=passed_count,
            outcome_rate=passed_count / computable_count,
            pending_reason=None,
        )

    logger.info(
        "outcome_rate.computed",
        workflow=operation.name,
        total_traces=total,
        computable_count=computable_count,
        passed_count=passed_count,
        outcome_rate=summary.outcome_rate,
        pending_reason=summary.pending_reason,
    )

    return summary
