"""Reference-behavior extraction.

Given a list of traces and a ``BusinessOperation``, select the efficient,
error-free, representative subset that defines "what good looks like" for
the operation. The resulting ``ReferenceCohort`` exposes a reference DFG,
a reference path, and p75 budgets over the reference traces.

Eligibility for reference consideration (ALL must hold):
    1. ``trace.terminal_status == TerminalStatus.COMPLETED``
    2. ``trace.error_count == 0``
    3. Not a loop (via ``loops.loop_assertion(min_repeats=3)``)
    4. Not "critical" redundancy (3+ consecutive same-tool calls with
       Jaccard ≥ 0.85 on normalized args)
    5. If ``operation.expected_tools`` is non-empty:
       required tool coverage ≥ 0.8
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import StrEnum

from kairos.analysis.workflow_membership import MembershipKind, WorkflowMembership  # noqa: TCH001
from kairos.detection.loops import loop_assertion
from kairos.detection.similarity import jaccard_dict_similarity
from kairos.log import get_logger
from kairos.models.enums import StepStatus, StepType, TerminalStatus
from kairos.models.trace import Step, TraceEnvelope  # noqa: TCH001
from kairos.taxonomy.business_context import BusinessOperation  # noqa: TCH001
from kairos.taxonomy.dfg import DFG, DFGBuilder

logger = get_logger(__name__)


class ReferenceConfidence(StrEnum):
    """Confidence in the reference cohort based on eligible-trace count."""

    HIGH = "high"  # >= 50 eligible
    MEDIUM = "medium"  # >= 20
    LOW = "low"  # 5-19
    NONE = "none"  # < 5


@dataclass
class ReferenceCohort:
    """Reference behavior extracted for a business operation."""

    eligible_traces: list[TraceEnvelope]
    reference_traces: list[TraceEnvelope]
    confidence: ReferenceConfidence
    reference_dfg: DFG | None
    reference_edges: set[tuple[str, str]]
    reference_path: list[str]
    step_budget_p75: float | None
    token_budget_p75: float | None


_CRITICAL_REDUNDANCY_THRESHOLD = 0.85
_CRITICAL_REDUNDANCY_MIN_RUN = 3
_COVERAGE_MIN_RATIO = 0.8
_TOKEN_COVERAGE_MIN_RATIO = 0.8
_REFERENCE_PATH_MAX_STEPS = 20

_CONFIDENCE_HIGH_MIN = 50
_CONFIDENCE_MEDIUM_MIN = 20
_CONFIDENCE_LOW_MIN = 5


def segment_trace_for_workflow(
    trace: TraceEnvelope,
    operation: BusinessOperation,
) -> list[Step]:
    """Keep only tool-call steps whose tool_name is in operation.expected_tools.

    Order is preserved. Non-tool steps (LLM, retrieval) are dropped.
    If ``operation.expected_tools`` is empty returns [].
    """
    expected = set(operation.expected_tools)
    if not expected:
        return []
    return [step for step in trace.steps if step.tool_name in expected]


def _build_dfg_from_sequences(sequences: list[list[str]]) -> DFG:
    """Build a DFG directly from tool sequences (bypasses TraceEnvelope)."""
    edges: dict[tuple[str, str], int] = {}
    nodes: dict[str, int] = {}
    for seq in sequences:
        for tool in seq:
            nodes[tool] = nodes.get(tool, 0) + 1
        for i in range(len(seq) - 1):
            bigram = (seq[i], seq[i + 1])
            edges[bigram] = edges.get(bigram, 0) + 1
    return DFG(edges=edges, nodes=nodes, total_traces=len(sequences))


def extract_reference_behavior(
    traces: list[TraceEnvelope],
    operation: BusinessOperation,
    *,
    memberships: dict[str, list[WorkflowMembership]] | None = None,
) -> ReferenceCohort:
    """Extract the reference cohort + DFG + budgets for *operation*.

    When ``memberships`` is provided (Slice B.1), filter to traces whose
    membership for ``operation.name`` is FULL and segment each eligible
    trace to the workflow's tool footprint before building the reference
    DFG. When ``memberships`` is None the legacy single-label path runs:
    all traces are candidates and the full envelope tool sequences build
    the DFG.
    """
    workflow_scoped = memberships is not None

    if workflow_scoped:
        assert memberships is not None  # narrow for mypy
        filtered = _filter_full_members(traces, operation, memberships)
    else:
        filtered = traces

    eligible = [t for t in filtered if _is_eligible(t, operation)]
    confidence = _confidence_tier(len(eligible))

    if not eligible:
        logger.info(
            "reference_behavior.empty",
            operation=operation.name,
            eligible=0,
            traces=len(traces),
        )
        return ReferenceCohort(
            eligible_traces=eligible,
            reference_traces=[],
            confidence=confidence,
            reference_dfg=None,
            reference_edges=set(),
            reference_path=[],
            step_budget_p75=None,
            token_budget_p75=None,
        )

    reference_traces = _select_reference_traces(eligible)

    # Low-confidence cohorts still surface reference_traces, but the DFG,
    # reference path, and budgets remain unset so the caller does not
    # over-trust thin data.
    if confidence == ReferenceConfidence.NONE:
        logger.info(
            "reference_behavior.low_confidence",
            operation=operation.name,
            eligible=len(eligible),
            reference=len(reference_traces),
            confidence=confidence.value,
        )
        return ReferenceCohort(
            eligible_traces=eligible,
            reference_traces=reference_traces,
            confidence=confidence,
            reference_dfg=None,
            reference_edges=set(),
            reference_path=[],
            step_budget_p75=None,
            token_budget_p75=None,
        )

    if workflow_scoped:
        # Build the DFG from segmented tool sequences so out-of-workflow
        # tools don't appear in reference_edges.
        segmented_sequences = [
            [s.tool_name for s in segment_trace_for_workflow(t, operation) if s.tool_name] for t in reference_traces
        ]
        reference_dfg = _build_dfg_from_sequences(segmented_sequences)
        reference_edges = set(reference_dfg.edges.keys())
        reference_path = _greedy_reference_path_from_sequences(reference_dfg, segmented_sequences)
    else:
        reference_dfg = DFGBuilder().build(reference_traces)
        reference_edges = set(reference_dfg.edges.keys())
        reference_path = _greedy_reference_path(reference_dfg, reference_traces)

    step_counts = sorted(t.step_count for t in reference_traces)
    n = len(step_counts)
    idx = min(int(0.75 * n), n - 1)
    step_budget_p75 = float(step_counts[idx])

    token_budget_p75: float | None = None
    if _coverage_ratio([t.total_tokens for t in reference_traces]) >= _TOKEN_COVERAGE_MIN_RATIO:
        token_counts = sorted(t.total_tokens for t in reference_traces)
        token_budget_p75 = float(token_counts[min(int(0.75 * n), n - 1)])

    logger.info(
        "reference_behavior.extracted",
        operation=operation.name,
        eligible=len(eligible),
        reference=len(reference_traces),
        confidence=confidence.value,
        step_budget_p75=step_budget_p75,
        token_budget_p75=token_budget_p75,
    )

    return ReferenceCohort(
        eligible_traces=eligible,
        reference_traces=reference_traces,
        confidence=confidence,
        reference_dfg=reference_dfg,
        reference_edges=reference_edges,
        reference_path=reference_path,
        step_budget_p75=step_budget_p75,
        token_budget_p75=token_budget_p75,
    )


def _filter_full_members(
    traces: list[TraceEnvelope],
    operation: BusinessOperation,
    memberships: dict[str, list[WorkflowMembership]],
) -> list[TraceEnvelope]:
    """Return only traces whose membership for operation.name is FULL."""
    result: list[TraceEnvelope] = []
    for trace in traces:
        trace_memberships = memberships.get(trace.trace_id, [])
        for m in trace_memberships:
            if m.operation_name == operation.name and m.kind == MembershipKind.FULL:
                result.append(trace)
                break
    return result


# ── Eligibility ────────────────────────────────────────────────────────


def _is_eligible(trace: TraceEnvelope, operation: BusinessOperation) -> bool:
    if trace.terminal_status != TerminalStatus.COMPLETED:
        return False
    if trace.error_count != 0:
        return False
    if _is_loop(trace):
        return False
    if _is_critical_redundancy(trace):
        return False
    return not (operation.expected_tools and _required_tool_coverage(trace, operation) < _COVERAGE_MIN_RATIO)


def _is_loop(trace: TraceEnvelope) -> bool:
    """True when the trace contains a repeating-with-no-progress loop."""
    return len(loop_assertion(trace, min_repeats=3)) > 0


def _is_critical_redundancy(trace: TraceEnvelope) -> bool:
    """True when there are 3+ consecutive same-tool calls with near-identical args.

    Two calls are "near-identical" when their normalized-arg Jaccard
    similarity is ≥ 0.85. A critical run is a chain of such pairs of
    length ≥ 3.
    """
    tool_steps = [s for s in trace.steps if s.step_type == StepType.TOOL_CALL and s.tool_name]
    if len(tool_steps) < _CRITICAL_REDUNDANCY_MIN_RUN:
        return False

    run_length = 1
    for i in range(len(tool_steps) - 1):
        curr = tool_steps[i]
        nxt = tool_steps[i + 1]
        if curr.tool_name != nxt.tool_name:
            run_length = 1
            continue
        args_a = curr.tool_args_normalized or curr.tool_args
        args_b = nxt.tool_args_normalized or nxt.tool_args
        if jaccard_dict_similarity(args_a, args_b) >= _CRITICAL_REDUNDANCY_THRESHOLD:
            run_length += 1
            if run_length >= _CRITICAL_REDUNDANCY_MIN_RUN:
                return True
        else:
            run_length = 1
    return False


def _required_tool_coverage(trace: TraceEnvelope, operation: BusinessOperation) -> float:
    """Fraction of expected_tools that appear as a successful step in the trace."""
    if not operation.expected_tools:
        return 1.0

    successful_tools = {
        step.tool_name
        for step in trace.steps
        if step.tool_name is not None and step.status == StepStatus.OK and not step.error_message
    }
    hit = sum(1 for expected in operation.expected_tools if expected in successful_tools)
    return hit / len(operation.expected_tools)


# ── Confidence tier ────────────────────────────────────────────────────


def _confidence_tier(n_eligible: int) -> ReferenceConfidence:
    if n_eligible >= _CONFIDENCE_HIGH_MIN:
        return ReferenceConfidence.HIGH
    if n_eligible >= _CONFIDENCE_MEDIUM_MIN:
        return ReferenceConfidence.MEDIUM
    if n_eligible >= _CONFIDENCE_LOW_MIN:
        return ReferenceConfidence.LOW
    return ReferenceConfidence.NONE


# ── Reference selection ────────────────────────────────────────────────


def _select_reference_traces(
    eligible: list[TraceEnvelope],
) -> list[TraceEnvelope]:
    """Select reference traces as those matching the mode tool_sequence.

    The mode is the most frequent tool_sequence tuple across eligible traces.
    Ties broken by earliest occurrence in the input list. If no sequence
    repeats, the single most common sequence is selected. This is simpler
    and more honest than a 3-weight efficiency model whose weights are
    arbitrary until enough data exists to calibrate them.
    """
    if not eligible:
        return []

    seq_counts: Counter[tuple[str, ...]] = Counter(
        tuple(t.tool_sequence) for t in eligible
    )
    # Ties broken by shorter sequence first (simpler = better reference),
    # then lexicographically for full determinism.
    mode_seq = max(seq_counts, key=lambda k: (seq_counts[k], -len(k), tuple(-ord(c) for c in "".join(k))))
    result = [t for t in eligible if tuple(t.tool_sequence) == mode_seq]
    return result if result else eligible[:1]


def _coverage_ratio(values: list[int]) -> float:
    """Fraction of values that are > 0 (treating 0 as 'missing')."""
    if not values:
        return 0.0
    nonzero = sum(1 for v in values if v and v > 0)
    return nonzero / len(values)


# ── Reference path (greedy walk) ────────────────────────────────────────


def _greedy_reference_path(
    dfg: DFG,
    reference_traces: list[TraceEnvelope],
) -> list[str]:
    """Greedy walk starting at the tool that appears first most often.

    Ties on first-tool frequency resolved alphabetically. At each node,
    follow the highest-weight outgoing edge; ties resolved alphabetically.
    Stops at: no outgoing edge, already-visited node, or after 20 steps.
    """
    sequences = [list(t.tool_sequence) for t in reference_traces]
    return _greedy_reference_path_from_sequences(dfg, sequences)


def _greedy_reference_path_from_sequences(
    dfg: DFG,
    sequences: list[list[str]],
) -> list[str]:
    """Greedy walk over *dfg* seeded from the first tool across *sequences*."""
    if not dfg.edges:
        return []

    first_tool_counts: dict[str, int] = {}
    for seq in sequences:
        if seq:
            first = seq[0]
            first_tool_counts[first] = first_tool_counts.get(first, 0) + 1

    if not first_tool_counts:
        return []

    # Deterministic start: highest count, then alphabetical
    start = min(
        first_tool_counts.keys(),
        key=lambda t: (-first_tool_counts[t], t),
    )

    path: list[str] = [start]
    visited: set[str] = {start}
    current = start

    while len(path) < _REFERENCE_PATH_MAX_STEPS:
        outgoing = {b: w for (a, b), w in dfg.edges.items() if a == current}
        if not outgoing:
            break
        # Sort by highest weight, ties alphabetical
        next_tool = min(outgoing.keys(), key=lambda t: (-outgoing[t], t))
        if next_tool in visited:
            break
        path.append(next_tool)
        visited.add(next_tool)
        current = next_tool

    return path
