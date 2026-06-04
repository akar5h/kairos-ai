"""Decision-state packet extraction for semantic LLM analysis.

Produces a DecisionStatePacket around a flagged step in a TraceEnvelope.
Missing-reason semantics explicitly distinguish "field not instrumented in
the export" from "field instrumented but not present for this step",
so downstream LLM analysis knows what data to trust vs. ignore.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from kairos.log import get_logger
from kairos.models.enums import StepType

if TYPE_CHECKING:
    from kairos.analysis.evidence_coverage import EvidenceCoverage
    from kairos.analysis.reference_behavior import ReferenceCohort
    from kairos.models.trace import Step, TraceEnvelope
    from kairos.taxonomy.business_context import BusinessOperation

logger = get_logger(__name__)

MAX_TEXT_FIELD_CHARS = 800
TRUNCATION_MARKER = "...[truncated]"
NOT_INSTRUMENTED_THRESHOLD = 0.30


class MissingReason(StrEnum):
    NOT_INSTRUMENTED = "not_instrumented"
    NOT_USED_BEFORE_STEP = "not_used_before_step"
    TRACE_FIELD_MISSING = "trace_field_missing"
    STEP_FIELD_MISSING = "step_field_missing"
    PRESENT_EMPTY = "present_empty"
    PRESENT = "present"
    UNKNOWN = "unknown"


@dataclass
class DecisionStatePacket:
    # identity
    trace_id: str
    workflow_name: str
    step_index: int

    # business context from YAML
    business_goal: str | None
    reliability_metric: str | None
    bad_run_means: str | None

    # agent-side context
    user_input: str | None
    system_instruction_summary: str | None
    available_tools: list[str]
    tool_schema_summary: str | None

    # state before the decision
    memory_reads_before_step: list[str]
    memory_reads_missing_reason: MissingReason
    retrieved_context_before_step: list[str]
    retrieved_context_missing_reason: MissingReason
    prior_tool_calls: list[dict[str, Any]]
    prior_tool_outputs_missing_reason: MissingReason

    # the suspicious step
    current_step_tool_name: str | None
    current_step_tool_args: dict[str, Any] | None
    current_step_tool_output: str | None
    current_step_error_message: str | None

    # reference vs actual
    reference_expected_transition: tuple[str, str] | None
    actual_transition: tuple[str, str] | None

    # deterministic flags
    deterministic_flags: list[str] = field(default_factory=list)


def _truncate(text: str | None) -> str | None:
    if text is None:
        return None
    if len(text) <= MAX_TEXT_FIELD_CHARS:
        return text
    return text[:MAX_TEXT_FIELD_CHARS] + TRUNCATION_MARKER


def _dedup_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _compute_missing_reason(
    coverage: EvidenceCoverage,
    coverage_key: str,
    *,
    is_required: bool,
    trace_has_field_anywhere: bool,
    trace_has_field_before_step: bool,
    trace_has_field_at_step_empty: bool,
) -> MissingReason:
    total = coverage.total_traces or 1
    counts = coverage.required_field_counts if is_required else coverage.context_field_counts
    ratio = counts.get(coverage_key, 0) / total
    if ratio < NOT_INSTRUMENTED_THRESHOLD:
        return MissingReason.NOT_INSTRUMENTED
    if trace_has_field_at_step_empty:
        return MissingReason.PRESENT_EMPTY
    if not trace_has_field_anywhere:
        return MissingReason.TRACE_FIELD_MISSING
    if not trace_has_field_before_step:
        return MissingReason.NOT_USED_BEFORE_STEP
    return MissingReason.PRESENT


def _collect_retrieval_info(
    trace: TraceEnvelope,
    step_index: int,
) -> tuple[list[str], bool, bool, bool]:
    """Return (chunks_before_step, has_anywhere, has_before_step, has_empty_at_or_before)."""
    chunks_before_step: list[str] = []
    has_anywhere = False
    has_before_step = False
    has_empty_at_or_before = False

    for step in trace.steps:
        if step.step_type != StepType.RETRIEVAL:
            continue
        has_anywhere = True
        # Empty chunks (explicit empty list) at or before the flagged step signal PRESENT_EMPTY.
        if step.step_index <= step_index and step.retrieval_chunks == []:
            has_empty_at_or_before = True
        if step.step_index < step_index and step.retrieval_chunks:
            has_before_step = True
            chunks_before_step.extend(step.retrieval_chunks)

    return chunks_before_step, has_anywhere, has_before_step, has_empty_at_or_before


def _collect_memory_info(
    trace: TraceEnvelope,
    step_index: int,
) -> tuple[list[str], bool, bool, bool]:
    """Return (memory_events_before_step, has_anywhere, has_before_step, has_empty_at_or_before).

    Memory is not a first-class field on Step in this codebase; look at
    ``trace.metadata['memory_events']`` if present.
    """
    events_before_step: list[str] = []
    has_anywhere = False
    has_before_step = False
    has_empty_at_or_before = False

    metadata = trace.metadata
    if metadata is None:
        return events_before_step, has_anywhere, has_before_step, has_empty_at_or_before

    raw = metadata.get("memory_events")
    if raw is None:
        return events_before_step, has_anywhere, has_before_step, has_empty_at_or_before

    if isinstance(raw, list):
        if len(raw) == 0:
            has_empty_at_or_before = True
            return events_before_step, has_anywhere, has_before_step, has_empty_at_or_before
        has_anywhere = True
        # Without per-event step indices we treat all events as "before" the step.
        has_before_step = True
        events_before_step = [str(e) for e in raw]
    else:
        has_anywhere = True
        has_before_step = True
        events_before_step = [str(raw)]

    return events_before_step, has_anywhere, has_before_step, has_empty_at_or_before


def _find_step(trace: TraceEnvelope, step_index: int) -> Step | None:
    for step in trace.steps:
        if step.step_index == step_index:
            return step
    return None


def _previous_tool_step(trace: TraceEnvelope, step_index: int) -> Step | None:
    prev: Step | None = None
    for step in trace.steps:
        if step.step_index >= step_index:
            break
        if step.step_type == StepType.TOOL_CALL and step.tool_name:
            prev = step
    return prev


def _expected_next_tool(reference: ReferenceCohort, prev_tool: str) -> str | None:
    dfg = reference.reference_dfg
    if dfg is None:
        return None
    outgoing = {b: w for (a, b), w in dfg.edges.items() if a == prev_tool}
    if not outgoing:
        return None
    # Highest weight; ties broken alphabetically for determinism.
    return min(outgoing.keys(), key=lambda t: (-outgoing[t], t))


def extract_packet(
    *,
    trace: TraceEnvelope,
    step_index: int,
    operation: BusinessOperation,
    coverage: EvidenceCoverage,
    reference: ReferenceCohort,
    deterministic_flags: list[str],
) -> DecisionStatePacket:
    # Prior tool calls (step_index strictly less than flagged).
    prior_tool_calls: list[dict[str, Any]] = []
    for step in trace.steps:
        if step.step_index >= step_index:
            continue
        if step.step_type != StepType.TOOL_CALL or step.tool_name is None:
            continue
        prior_tool_calls.append(
            {
                "tool_name": step.tool_name,
                "args": step.tool_args or {},
                "output_truncated": _truncate(step.tool_output),
            }
        )

    # Current step.
    current_step = _find_step(trace, step_index)
    if current_step is not None and current_step.step_type == StepType.TOOL_CALL:
        current_step_tool_name = current_step.tool_name
        current_step_tool_args = current_step.tool_args
        current_step_tool_output = _truncate(current_step.tool_output)
        current_step_error_message = current_step.error_message
    else:
        current_step_tool_name = None
        current_step_tool_args = None
        current_step_tool_output = None
        current_step_error_message = current_step.error_message if current_step is not None else None

    # Retrieval info.
    (
        retrieval_chunks_before,
        retrieval_has_anywhere,
        retrieval_has_before,
        retrieval_empty_at_or_before,
    ) = _collect_retrieval_info(trace, step_index)
    retrieved_context_missing_reason = _compute_missing_reason(
        coverage,
        "retrieval_chunks",
        is_required=False,
        trace_has_field_anywhere=retrieval_has_anywhere,
        trace_has_field_before_step=retrieval_has_before,
        trace_has_field_at_step_empty=retrieval_empty_at_or_before,
    )

    # Memory info.
    (
        memory_events_before,
        memory_has_anywhere,
        memory_has_before,
        memory_empty_at_or_before,
    ) = _collect_memory_info(trace, step_index)
    memory_reads_missing_reason = _compute_missing_reason(
        coverage,
        "memory_events",
        is_required=False,
        trace_has_field_anywhere=memory_has_anywhere,
        trace_has_field_before_step=memory_has_before,
        trace_has_field_at_step_empty=memory_empty_at_or_before,
    )

    # Prior tool outputs (required field).
    prior_tool_outputs_anywhere = any(
        s.step_type == StepType.TOOL_CALL and s.tool_output is not None and s.tool_output != "" for s in trace.steps
    )
    prior_tool_outputs_before = any(
        s.step_index < step_index
        and s.step_type == StepType.TOOL_CALL
        and s.tool_output is not None
        and s.tool_output != ""
        for s in trace.steps
    )
    prior_tool_outputs_missing_reason = _compute_missing_reason(
        coverage,
        "tool_outputs",
        is_required=True,
        trace_has_field_anywhere=prior_tool_outputs_anywhere,
        trace_has_field_before_step=prior_tool_outputs_before,
        trace_has_field_at_step_empty=False,
    )

    # Transitions.
    prev_tool_step = _previous_tool_step(trace, step_index)
    actual_transition: tuple[str, str] | None = None
    reference_expected_transition: tuple[str, str] | None = None
    if prev_tool_step is not None and prev_tool_step.tool_name is not None:
        if current_step_tool_name is not None:
            actual_transition = (prev_tool_step.tool_name, current_step_tool_name)
        expected_next = _expected_next_tool(reference, prev_tool_step.tool_name)
        if expected_next is not None:
            reference_expected_transition = (prev_tool_step.tool_name, expected_next)

    packet = DecisionStatePacket(
        trace_id=trace.trace_id,
        workflow_name=operation.name,
        step_index=step_index,
        business_goal=operation.business_goal,
        reliability_metric=operation.reliability_metric,
        bad_run_means=operation.bad_run_means,
        user_input=trace.user_input,
        system_instruction_summary=_truncate(trace.system_prompt),
        available_tools=_dedup_preserve_order(list(operation.expected_tools)),
        tool_schema_summary=None,
        memory_reads_before_step=memory_events_before,
        memory_reads_missing_reason=memory_reads_missing_reason,
        retrieved_context_before_step=retrieval_chunks_before,
        retrieved_context_missing_reason=retrieved_context_missing_reason,
        prior_tool_calls=prior_tool_calls,
        prior_tool_outputs_missing_reason=prior_tool_outputs_missing_reason,
        current_step_tool_name=current_step_tool_name,
        current_step_tool_args=current_step_tool_args,
        current_step_tool_output=current_step_tool_output,
        current_step_error_message=current_step_error_message,
        reference_expected_transition=reference_expected_transition,
        actual_transition=actual_transition,
        deterministic_flags=list(deterministic_flags),
    )

    logger.info(
        "decision_state.extracted",
        trace_id=trace.trace_id,
        step_index=step_index,
        workflow=operation.name,
        retrieval_missing_reason=retrieved_context_missing_reason.value,
        memory_missing_reason=memory_reads_missing_reason.value,
        prior_tool_outputs_missing_reason=prior_tool_outputs_missing_reason.value,
    )

    return packet
