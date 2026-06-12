"""Outcome metric evaluator for week-1 analysis.

Evaluates a single trace against a BusinessOperation using a 4-condition
pass/fail formula, and aggregates results across a population into a
WorkflowOutcomeSummary.

Evidence ladder (per-step, ordered and short-circuiting):
  Rung 1  kairos.outcome attr — explicit override (set in genai_mapping / StepStatusSource.KAIROS_OUTCOME)
  Rung 2  OTel / success attr — primary structured signal (StepStatusSource.ATTR_SUCCESS / OTEL_STATUS)
  Rung 3  adapter extractor   — per-agent hook (StepStatusSource.ADAPTER)
  Rung 4  textual last resort — word-boundary regex on last 500 chars ONLY when status_source==NONE

Rung 4 NEVER overrides rungs 1–3. A step with status_source != NONE is taken
as-is and textual scanning is skipped entirely.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from kairos.log import get_logger
from kairos.models.enums import FailureReason, StepStatus, StepStatusSource, TerminalStatus

if TYPE_CHECKING:
    from kairos.models.trace import Step, TraceEnvelope
    from kairos.taxonomy.business_context import BusinessOperation

logger = get_logger(__name__)


# Terminal statuses that indicate hard failure at the trace level.
_TERMINAL_FAILURE_STATUSES: frozenset[TerminalStatus] = frozenset(
    {TerminalStatus.ERROR, TerminalStatus.TIMEOUT},
)

# Structured evidence sources (rungs 1–3 of the evidence ladder). A successful
# side-effect call with one of these status_sources is verified WITHOUT needing
# readable tool_output — structured status wins, text is last resort.
_STRUCTURED_STATUS_SOURCES: frozenset[StepStatusSource] = frozenset(
    {
        StepStatusSource.ATTR_SUCCESS,
        StepStatusSource.OTEL_STATUS,
        StepStatusSource.KAIROS_OUTCOME,
        StepStatusSource.ADAPTER,
    },
)

# ─── Rung 4: textual last-resort markers ─────────────────────────────────────
# Applied ONLY to the last 500 chars of tool_output when status_source == NONE.
# Word-boundary anchored so "error" in "no errors found" is masked by _NEGATED_RE.

_MARKER_RE: re.Pattern[str] = re.compile(
    r"\b(failed|failure|error|exception|denied|validation failed|not submitted)\b",
    re.IGNORECASE,
)

# Negation phrases: "no errors", "0 errors", "zero failures", "without error", etc.
# These are deleted from the tail string BEFORE _MARKER_RE runs.
_NEGATED_RE: re.Pattern[str] = re.compile(
    r"\b(no|0|zero|without)\s+(errors?|failures?)\b",
    re.IGNORECASE,
)

_TEXTUAL_TAIL_CHARS: int = 500


def _textual_failure(output: str) -> bool:
    """Return True when the last 500 chars of *output* contain an unmasked failure marker.

    Algorithm (spec-normative):
      1. Take the last 500 chars.
      2. Delete all negation phrases (e.g. "no errors") so they can't be false-positives.
      3. Run _MARKER_RE on the result.

    This is rung 4 — ONLY consulted when status_source == NONE.
    Binary/non-UTF8 inputs: caller is responsible for passing a str; non-UTF8
    decoding failures in the pipeline should skip rung 4 entirely (no textual opinion).
    """
    tail = output[-_TEXTUAL_TAIL_CHARS:]
    # Mask negated phrases by replacing with spaces of equal length (preserves other word positions).
    masked = _NEGATED_RE.sub(lambda m: " " * len(m.group()), tail)
    return bool(_MARKER_RE.search(masked))


@dataclass
class OutcomeEvidence:
    """Pointer to the specific step that caused a failure."""

    step_index: int | None = None
    rung: int | None = None
    """Which evidence-ladder rung produced the verdict (1–4)."""


@dataclass
class OutcomeResult:
    """Per-trace outcome evaluation."""

    trace_id: str
    outcome_pass: bool
    computable: bool
    reason: str | None
    failure_reason: FailureReason | None = None
    """Structured failure category — set when outcome_pass is False and computable is True."""
    evidence: OutcomeEvidence = field(default_factory=OutcomeEvidence)
    """Pointer to the step/rung that produced the verdict."""


@dataclass
class WorkflowOutcomeSummary:
    """Aggregated outcome rate across a population of traces."""

    workflow_name: str
    total_traces: int
    computable_count: int
    passed_count: int
    outcome_rate: float | None
    pending_reason: str | None
    human_escalation_rate: float | None = None
    """Fraction of computable traces that ended in HUMAN_ESCALATION.

    HUMAN_ESCALATION is pass-eligible — escalating correctly is a success mode.
    This metric tracks the autonomy dial: high rate = agent escalates frequently.
    None when computable_count == 0.
    """
    per_trace_results: list[OutcomeResult] = field(default_factory=list)
    """Per-trace OutcomeResult objects used to build outcome_rows in the view.

    Populated by compute_outcome_rate; consumed by build_analysis_view to
    produce the CorrectnessView.outcome_rows table.
    """


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
        # PERF: O(n²) worst case on pathological traces — acceptable at current scale.
        recovered = any(j != i and _is_successful_tool_step(other, step.tool_name) for j, other in enumerate(steps))
        if not recovered:
            return True

    return False


def _step_is_output_failed(step: Step) -> bool:
    """Return True when rung 4 (textual) signals failure on *step*.

    Only consulted when status_source == NONE (no structured signal available).
    """
    if step.status_source != StepStatusSource.NONE:
        return False
    if step.tool_output is None:
        return False
    # Skip binary / non-decodable output — no textual opinion.
    try:
        output_str = step.tool_output if isinstance(step.tool_output, str) else str(step.tool_output)
    except Exception:  # noqa: BLE001
        return False
    return _textual_failure(output_str)


def _side_effect_result(
    trace: TraceEnvelope,
    side_effect_tools: list[str],
) -> tuple[bool, bool, str | None, FailureReason | None, OutcomeEvidence]:
    """Evaluate the final required side-effect condition.

    Returns ``(passed, computable, reason, failure_reason, evidence)``.
    """
    if not side_effect_tools:
        return True, True, None, None, OutcomeEvidence()

    for tool_name in side_effect_tools:
        successful_calls = [step for step in trace.steps if _is_successful_tool_step(step, tool_name)]
        if not successful_calls:
            # Any attempted calls? A call with missing status is non-computable.
            attempts = [step for step in trace.steps if step.tool_name == tool_name]
            if attempts and all(step.tool_output is None and step.status == StepStatus.OK for step in attempts):
                return False, False, "side effect computability unknown", None, OutcomeEvidence()
            evidence = OutcomeEvidence(
                step_index=attempts[0].step_index if attempts else None,
                rung=None,
            )
            return False, True, "missing_side_effect", FailureReason.MISSING_SIDE_EFFECT, evidence

        # Structured evidence (rungs 1–3): a successful call whose status_source is
        # ATTR_SUCCESS / OTEL_STATUS / KAIROS_OUTCOME / ADAPTER already carries a
        # verified OK verdict — readable output is NOT required to confirm it.
        # Live claude_code spans carry no tool_output; without this, pass is
        # structurally impossible on live data (healthy traces → non-computable).
        has_structured_ok = any(call.status_source in _STRUCTURED_STATUS_SOURCES for call in successful_calls)

        # Multi-call any-of over readable outputs: the side-effect passes if at
        # least one successful call has a clean tool_output. Readable outputs can
        # DOWNGRADE structured evidence: if outputs exist and every readable output
        # carries a failure marker, fail with side_effect_output_failed.
        any_clean = False
        any_readable = False
        failing_step: Step | None = None
        for call in successful_calls:
            if call.tool_output is None:
                continue
            any_readable = True
            if not _step_is_output_failed(call):
                any_clean = True
                break
            elif failing_step is None:
                failing_step = call

        if any_clean:
            continue  # at least one clean readable output → satisfied
        if any_readable:
            # Outputs exist and ALL readable outputs failed → downgrade, even
            # when structured evidence said OK (text contradicts; surface it).
            evidence = OutcomeEvidence(
                step_index=failing_step.step_index if failing_step is not None else None,
                rung=4,
            )
            return (
                False,
                True,
                "missing_side_effect",
                FailureReason.SIDE_EFFECT_OUTPUT_FAILED,
                evidence,
            )
        # No readable outputs at all:
        if has_structured_ok:
            continue  # structured OK (rungs 1–3) is sufficient evidence → satisfied
        # Successful calls exist but status_source is NONE everywhere and no output
        # is readable — genuinely no evidence either way → non-computable.
        return False, False, "side effect computability unknown", None, OutcomeEvidence()

    return True, True, None, None, OutcomeEvidence()


def evaluate_outcome(trace: TraceEnvelope, operation: BusinessOperation) -> OutcomeResult:
    """Evaluate a trace against the 4-condition outcome formula.

    Returns an OutcomeResult with ``outcome_pass``, ``computable``, and an
    optional ``reason`` citing which condition failed.
    """
    # Day 4: integrity check — partial traces are non-computable.
    # NEVER score a partial trace as failed; outcome is unknown because spans are missing.
    if trace.integrity == "partial":
        return OutcomeResult(
            trace_id=trace.trace_id,
            outcome_pass=False,
            computable=False,
            reason="partial_trace",
            failure_reason=FailureReason.PARTIAL_TRACE,
        )

    # Condition 1: terminal status
    if trace.terminal_status == TerminalStatus.UNKNOWN:
        return OutcomeResult(
            trace_id=trace.trace_id,
            outcome_pass=False,
            computable=False,
            reason="terminal_status missing",
            failure_reason=FailureReason.TERMINAL_UNKNOWN,
        )

    if trace.terminal_status in _TERMINAL_FAILURE_STATUSES:
        return OutcomeResult(
            trace_id=trace.trace_id,
            outcome_pass=False,
            computable=True,
            reason="terminal_error",
            failure_reason=FailureReason.TERMINAL_ERROR,
        )

    # Terminal status must be COMPLETED or HUMAN_ESCALATION to continue.
    if trace.terminal_status not in {TerminalStatus.COMPLETED, TerminalStatus.HUMAN_ESCALATION}:
        return OutcomeResult(
            trace_id=trace.trace_id,
            outcome_pass=False,
            computable=True,
            reason="terminal_error",
            failure_reason=FailureReason.TERMINAL_ERROR,
        )

    # Condition 2: required tool coverage is computable
    if operation.expected_tools and not trace.steps:
        return OutcomeResult(
            trace_id=trace.trace_id,
            outcome_pass=False,
            computable=False,
            reason="tool status not computable",
            failure_reason=FailureReason.TERMINAL_UNKNOWN,
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
            failure_reason=FailureReason.CRITICAL_TOOL_ERROR,
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
                failure_reason=FailureReason.MISSING_SIDE_EFFECT,
            )

    # Condition 4: final required side-effect
    side_passed, side_computable, side_reason, side_failure_reason, side_evidence = _side_effect_result(
        trace, operation.required_side_effect_tools
    )
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
            failure_reason=side_failure_reason,
            evidence=side_evidence,
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

    # Human escalation rate: fraction of computable traces ending in HUMAN_ESCALATION.
    # HUMAN_ESCALATION is pass-eligible, but tracked separately as an autonomy metric.
    escalated = sum(
        1
        for t in traces
        if t.terminal_status == TerminalStatus.HUMAN_ESCALATION
        # Only count computable traces.
        and next((r for r in results if r.trace_id == t.trace_id), None) is not None
        and next(r for r in results if r.trace_id == t.trace_id).computable
    )
    human_escalation_rate = escalated / computable_count if computable_count > 0 else None

    if computable_count == 0:
        summary = WorkflowOutcomeSummary(
            workflow_name=operation.name,
            total_traces=total,
            computable_count=0,
            passed_count=passed_count,
            outcome_rate=None,
            pending_reason="no computable traces",
            human_escalation_rate=None,
            per_trace_results=results,
        )
    else:
        summary = WorkflowOutcomeSummary(
            workflow_name=operation.name,
            total_traces=total,
            computable_count=computable_count,
            passed_count=passed_count,
            outcome_rate=passed_count / computable_count,
            pending_reason=None,
            human_escalation_rate=human_escalation_rate,
            per_trace_results=results,
        )

    logger.info(
        "outcome_rate.computed",
        workflow=operation.name,
        total_traces=total,
        computable_count=computable_count,
        passed_count=passed_count,
        outcome_rate=summary.outcome_rate,
        pending_reason=summary.pending_reason,
        human_escalation_rate=summary.human_escalation_rate,
    )

    return summary
