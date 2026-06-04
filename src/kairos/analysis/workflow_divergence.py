"""Workflow divergence detector.

Compares each trace's tool-transition graph against a reference DFG and
flags the first off-reference transition. Short detours that rejoin the
reference path quickly (variant candidates) are distinguished from
structural divergences.
"""

from __future__ import annotations

from dataclasses import dataclass

from kairos.analysis.reference_behavior import ReferenceCohort, ReferenceConfidence
from kairos.log import get_logger
from kairos.models.enums import StepType
from kairos.models.trace import TraceEnvelope  # noqa: TCH001

logger = get_logger(__name__)

# A detour is a "variant" when ≤ 2 extra tools are inserted before the
# trace rejoins the reference. Entering + exiting the detour contributes
# at minimum 2 off-ref bigrams; with 2 extras inserted the run is 3
# consecutive off-ref bigrams. Hence ``<= 3``.
_MAX_VARIANT_OFF_REF_RUN = 3
_MAX_VARIANT_EXTRA_RATE = 0.20


@dataclass
class DivergenceFinding:
    """Per-trace divergence report against a reference cohort."""

    trace_id: str
    first_divergence_step: int | None
    expected_transition: tuple[str, str] | None
    actual_transition: tuple[str, str] | None
    extra_rate: float
    coverage: float
    variant_candidate: bool


def detect_workflow_divergence(
    traces: list[TraceEnvelope],
    reference: ReferenceCohort,
) -> list[DivergenceFinding]:
    """Emit one DivergenceFinding per trace, in input order.

    Returns ``[]`` when the reference cohort has no usable reference
    (confidence NONE, or no reference edges).
    """
    if reference.confidence == ReferenceConfidence.NONE or not reference.reference_edges:
        logger.info(
            "workflow_divergence.skipped_no_reference",
            confidence=reference.confidence.value,
            reference_edges=len(reference.reference_edges),
        )
        return []

    findings: list[DivergenceFinding] = []
    for trace in traces:
        findings.append(_analyze_trace(trace, reference))
    return findings


def _analyze_trace(trace: TraceEnvelope, reference: ReferenceCohort) -> DivergenceFinding:
    reference_edges = reference.reference_edges
    trace_edges_list = list(trace.tool_bigrams)
    trace_edges_set: set[tuple[str, str]] = set(trace_edges_list)

    # Coverage = fraction of reference edges covered by the trace.
    ref_denom = max(1, len(reference_edges))
    coverage = len(trace_edges_set & reference_edges) / ref_denom

    # Extra rate = fraction of trace edges that are not in reference.
    if trace_edges_list:
        extra_edges = trace_edges_set - reference_edges
        extra_rate = len(extra_edges) / len(trace_edges_set)
    else:
        extra_rate = 0.0

    # Find the first off-reference bigram.
    first_off_ref_idx: int | None = None
    for idx, bigram in enumerate(trace_edges_list):
        if bigram not in reference_edges:
            first_off_ref_idx = idx
            break

    if first_off_ref_idx is None:
        return DivergenceFinding(
            trace_id=trace.trace_id,
            first_divergence_step=None,
            expected_transition=None,
            actual_transition=None,
            extra_rate=extra_rate,
            coverage=coverage,
            variant_candidate=False,
        )

    actual_transition = trace_edges_list[first_off_ref_idx]
    expected_transition = _expected_transition(actual_transition[0], reference)

    # Decide variant vs structural divergence by off-ref run length.
    off_ref_run = _off_ref_run_length(trace_edges_list, first_off_ref_idx, reference_edges)
    is_variant = off_ref_run <= _MAX_VARIANT_OFF_REF_RUN and extra_rate <= _MAX_VARIANT_EXTRA_RATE

    # Variants keep expected/actual for explanation but suppress the step anchor.
    first_divergence_step: int | None = None if is_variant else _bigram_to_step_index(trace, first_off_ref_idx)

    return DivergenceFinding(
        trace_id=trace.trace_id,
        first_divergence_step=first_divergence_step,
        expected_transition=expected_transition,
        actual_transition=actual_transition,
        extra_rate=extra_rate,
        coverage=coverage,
        variant_candidate=is_variant,
    )


def _off_ref_run_length(
    bigrams: list[tuple[str, str]],
    start_idx: int,
    reference_edges: set[tuple[str, str]],
) -> int:
    """Length of the consecutive off-reference run starting at ``start_idx``."""
    run = 0
    for idx in range(start_idx, len(bigrams)):
        if bigrams[idx] in reference_edges:
            break
        run += 1
    return run


def _expected_transition(
    source: str,
    reference: ReferenceCohort,
) -> tuple[str, str] | None:
    """Highest-weight outgoing edge from ``source`` in the reference DFG."""
    dfg = reference.reference_dfg
    if dfg is None:
        return None
    outgoing = {b: w for (a, b), w in dfg.edges.items() if a == source}
    if not outgoing:
        return None
    # Ties broken alphabetically on destination tool name.
    best = min(outgoing.keys(), key=lambda b: (-outgoing[b], b))
    return (source, best)


def _bigram_to_step_index(trace: TraceEnvelope, bigram_idx: int) -> int | None:
    """Map a tool-bigram index to the ``step_index`` of the second tool in the pair.

    ``trace.tool_bigrams`` is computed from ``trace.tool_sequence`` which
    filters out non-tool steps. We iterate ``trace.steps`` to recover the
    actual step index of the ``bigram_idx + 1``-th tool step.
    """
    tool_step_count = 0
    for step in trace.steps:
        if step.step_type != StepType.TOOL_CALL or step.tool_name is None:
            continue
        if tool_step_count == bigram_idx + 1:
            return step.step_index
        tool_step_count += 1
    return None
