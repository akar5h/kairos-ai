"""Pipeline orchestrator.

Glues together the analysis primitives:

    1. pre-flight check (warns when trace population is too sparse)
    2. multi-label trace -> workflow mapping via per-op recall against
       ``BusinessOperation.expected_tools`` with a three-tier membership
       model (FULL / ATTEMPTED / NONE)
    3. per-workflow:
        - outcome rate (over FULL + ATTEMPTED members)
        - reference behavior cohort (FULL members, segmented)
        - Tier 1 deterministic findings
        - optional workflow divergence findings (behind enable_divergence flag)
        - top pattern names
    4. unmapped activity summary (sample trace IDs + top tools) — a trace
       is "unmapped" only when its membership is NONE for every operation.

The orchestrator is deterministic. Patching points (`detect_tier1`) are
imported by name so test mocks resolve via
`patch("kairos.engine.pipeline.<name>", ...)`.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from statistics import median
from typing import TYPE_CHECKING

from kairos.analysis.outcome_metric import compute_outcome_rate
from kairos.analysis.reference_behavior import extract_reference_behavior
from kairos.analysis.unit_outcome import rollup_units
from kairos.analysis.workflow_divergence import detect_workflow_divergence
from kairos.analysis.workflow_membership import MembershipKind, WorkflowMembership
from kairos.detection.runner import detect_tier1
from kairos.log import get_logger
from kairos.models.enums import StepStatus, TerminalStatus
from kairos.taxonomy.business_context import default_membership_threshold

if TYPE_CHECKING:
    from kairos.analysis.outcome_metric import WorkflowOutcomeSummary
    from kairos.analysis.reference_behavior import ReferenceCohort
    from kairos.analysis.unit_outcome import UnitOutcomeSummary
    from kairos.analysis.workflow_divergence import DivergenceFinding
    from kairos.detection.models import Finding
    from kairos.models.trace import TraceEnvelope
    from kairos.taxonomy.business_context import BusinessContext, BusinessOperation

logger = get_logger(__name__)

MAPPING_RECALL_THRESHOLD: float = 0.8
MAPPING_TIEBREAK_LOWER: float = 0.5
DEFAULT_SEMANTIC_TOP_PATTERNS: int = 3
DEFAULT_SEMANTIC_PER_PATTERN: int = 5

WORKFLOW_DIVERGENCE_PATTERN_NAME: str = "workflow_divergence"
_TOP_PATTERN_LIMIT: int = 3
_UNMAPPED_SAMPLE_LIMIT: int = 5
_UNMAPPED_TOP_TOOLS_LIMIT: int = 10

# Pre-flight thresholds
_PREFLIGHT_TERMINAL_STATUS_MIN: float = 0.80
_PREFLIGHT_TOOL_SEQUENCE_MIN: float = 0.70


@dataclass
class WorkflowSummary:
    """Per-workflow rollup: outcome, reference, deterministic findings.

    Slice B.0 multi-label model: a trace can be a ``FULL`` or ``ATTEMPTED``
    member of this workflow. ``mapped_trace_count`` is a backwards-compat
    property exposing ``full + attempted``.
    """

    operation_name: str
    full_trace_count: int
    attempted_trace_count: int
    outcome: WorkflowOutcomeSummary
    reference: ReferenceCohort
    deterministic_findings: list[Finding]
    divergences: list[DivergenceFinding]
    top_pattern_names: list[str] = field(default_factory=list)
    member_envelopes: list[TraceEnvelope] = field(default_factory=list)
    """All member envelopes (full + attempted) for this workflow.

    Carried so build_analysis_view can resolve per-trace status_source_of_evidence
    for the outcome_rows table without a second pass through the engine.
    """
    secondary_membership_count: int = 0
    primary_trace_ids: set[str] = field(default_factory=set)
    """Trace IDs for which this workflow is the primary (deduplicated) label."""

    @property
    def mapped_trace_count(self) -> int:
        """Backwards-compat convenience: total members (full + attempted)."""
        return self.full_trace_count + self.attempted_trace_count


@dataclass
class UnmappedActivity:
    """Summary of traces that could not be mapped to any workflow."""

    trace_count: int
    sample_trace_ids: list[str]
    top_tools: list[str]


@dataclass
class AnalysisResult:
    """Top-level pipeline result."""

    workflows: list[WorkflowSummary]
    unmapped: UnmappedActivity
    reliability: dict[str, float | None]
    unit_summaries: list[UnitOutcomeSummary] = field(default_factory=list)
    """Per-unit rollup produced by the correlation-key rollup stage (Day 9).

    When ``BusinessContext.correlation_key`` is ``None``, each entry mirrors
    its per-trace ``OutcomeResult`` exactly (unit == trace, backward-compat).
    When a key is configured, traces sharing the same key value are grouped
    into one unit with last-wins outcome, union findings, and summed cost.

    Always populated (even when correlation_key is None) so callers have a
    uniform API.  The per-trace ``WorkflowOutcomeSummary.per_trace_results``
    are still present inside each ``WorkflowSummary.outcome``.
    """


# ── Multi-label membership (Slice B.0) ─────────────────────────────────


def classify_membership(
    envelope: TraceEnvelope,
    op: BusinessOperation,
) -> WorkflowMembership:
    """Classify one envelope against one workflow operation.

    Distinctive-tool gating: ``required_side_effect_tools`` is the op's
    signature. At least one of those tools must appear in the trace for
    membership to be considered at all. Tool-set recall then promotes it
    from "touched this workflow" to FULL or ATTEMPTED based on whether
    every distinctive tool actually succeeded.

    Tiers:
      FULL      — at least one distinctive tool present, recall >= threshold,
                  and the distinctive tools satisfied per ``side_effect_match``:
                  "all" → every distinctive tool succeeded at least once;
                  "any" → at least one distinctive tool succeeded
      ATTEMPTED — at least one distinctive tool present, recall >= threshold,
                  but the side_effect_match requirement is not met
      NONE      — op has no distinctive tools declared (utility pattern),
                  no distinctive tool touched, recall below threshold,
                  op has no expected tools, or envelope has no tools
    """
    if not op.expected_tools or not envelope.tool_sequence:
        return WorkflowMembership(op.name, MembershipKind.NONE, 0.0)

    # excluded_tools gate: any successful call of an excluded tool → NONE
    if op.excluded_tools:
        excluded_set = set(op.excluded_tools)
        for step in envelope.steps:
            if step.tool_name in excluded_set and step.status == StepStatus.OK and not step.error_message:
                return WorkflowMembership(op.name, MembershipKind.NONE, 0.0)

    required_tools = set(op.required_side_effect_tools)

    # Ops without a signature tool are utility patterns, not workflows —
    # they can't be distinguished from arbitrary supporting activity.
    if not required_tools:
        return WorkflowMembership(op.name, MembershipKind.NONE, 0.0)

    expected = set(op.expected_tools)
    trace_tools = set(envelope.tool_sequence)

    # Gate: at least one distinctive tool must be present in the trace.
    if not (required_tools & trace_tools):
        return WorkflowMembership(op.name, MembershipKind.NONE, 0.0)

    overlap = expected & trace_tools
    recall = len(overlap) / len(expected)

    threshold = default_membership_threshold(op)
    if recall < threshold:
        return WorkflowMembership(op.name, MembershipKind.NONE, recall)

    # Each distinctive tool must have at least one successful call for FULL.
    required_success_counts: dict[str, int] = dict.fromkeys(required_tools, 0)
    for step in envelope.steps:
        if step.tool_name in required_tools and step.status == StepStatus.OK and not step.error_message:
            required_success_counts[step.tool_name] += 1

    if op.side_effect_match == "any":
        required_satisfied = any(count > 0 for count in required_success_counts.values())
    else:
        required_satisfied = all(count > 0 for count in required_success_counts.values())
    if required_satisfied:
        return WorkflowMembership(op.name, MembershipKind.FULL, recall)

    return WorkflowMembership(op.name, MembershipKind.ATTEMPTED, recall)


def map_envelope_multilabel(
    envelope: TraceEnvelope,
    operations: list[BusinessOperation],
) -> list[WorkflowMembership]:
    """Return zero-or-more memberships, one per op where membership != NONE."""
    memberships = [classify_membership(envelope, op) for op in operations]
    return [m for m in memberships if m.kind != MembershipKind.NONE]


_PRIORITY_RANK: dict[str, int] = {"high": 2, "medium": 1, "low": 0}


def _priority_rank(op: BusinessOperation) -> int:
    return _PRIORITY_RANK.get(op.priority, 1)


def _primary_workflow(
    memberships: list[WorkflowMembership],
    op_by_name: dict[str, BusinessOperation],
) -> WorkflowMembership | None:
    """Select the primary workflow for a trace from its memberships.

    Tiebreak order (descending priority):
      1. FULL beats ATTEMPTED
      2. Higher recall wins
      3. Higher op priority rank wins
      4. Op name lexicographic (ascending) — deterministic final tiebreak
    """
    if not memberships:
        return None
    full_members = [m for m in memberships if m.kind == MembershipKind.FULL]
    candidates = full_members if full_members else memberships

    def _key(m: WorkflowMembership) -> tuple[float, int, str]:
        op = op_by_name.get(m.operation_name)
        rank = _priority_rank(op) if op else 1
        return (m.recall, rank, m.operation_name)

    # max by recall and rank; min by name (negate name via negated string comparison not possible — sort instead)
    # Sort descending by (recall, rank), ascending by name
    sorted_candidates = sorted(candidates, key=lambda m: (-_key(m)[0], -_key(m)[1], _key(m)[2]))
    return sorted_candidates[0]


def validate_taxonomy(context: BusinessContext) -> list[str]:
    """Return the names of ops that lack distinctive tools.

    Called once at pipeline startup. Each flagged op is also logged with a
    review hint so operators can decide whether to add a signature tool or
    drop the op as a utility pattern. Returns the list of flagged op names
    so callers can surface them in reports.

    Hard error: when ALL operations are unusable (every op either has no
    expected_tools, or has expected_tools but no required_side_effect_tools),
    the pipeline cannot match any trace and would silently produce an empty
    result. Raise ``ValueError`` so the misconfiguration is visible immediately.
    """
    flagged: list[str] = []
    usable: list[str] = []
    for op in context.operations:
        if op.expected_tools and not op.required_side_effect_tools:
            flagged.append(op.name)
            logger.warning(
                "taxonomy.op_missing_distinctive_tools",
                operation=op.name,
                expected_tools=op.expected_tools,
                hint=(
                    "Add required_side_effect_tools (a signature tool that "
                    "distinguishes this workflow) or remove the op from the "
                    "taxonomy — it will never match under distinctive-tool gating."
                ),
            )
        else:
            usable.append(op.name)
    if flagged:
        logger.warning(
            "taxonomy.utility_patterns_detected",
            count=len(flagged),
            operations=flagged,
        )
    if context.operations and not usable:
        msg = (
            f"All {len(context.operations)} operation(s) in the taxonomy are unusable "
            f"(missing required_side_effect_tools): {flagged}. "
            "The pipeline cannot classify any trace. "
            "Add required_side_effect_tools to at least one operation."
        )
        raise ValueError(msg)
    return flagged


# ── Deterministic-finding helpers ──────────────────────────────────────


def _deterministic_pattern_counts(findings: list[Finding]) -> Counter[str]:
    """Count distinct affected traces per deterministic pattern name."""
    per_pattern: dict[str, set[str]] = {}
    for finding in findings:
        per_pattern.setdefault(finding.pattern_name, set()).add(finding.trace_id)
    return Counter({name: len(trace_ids) for name, trace_ids in per_pattern.items()})


def _divergence_count(divergences: list[DivergenceFinding]) -> int:
    """Number of divergences with a real (non-variant) divergence step."""
    return sum(1 for d in divergences if d.first_divergence_step is not None)


def _top_pattern_names(
    findings: list[Finding],
    divergences: list[DivergenceFinding],
    *,
    limit: int = _TOP_PATTERN_LIMIT,
) -> list[str]:
    """Top-N pattern names ranked by affected trace count, ties broken alphabetically."""
    counts = _deterministic_pattern_counts(findings)

    div_count = _divergence_count(divergences)
    if div_count > 0:
        counts[WORKFLOW_DIVERGENCE_PATTERN_NAME] = counts.get(WORKFLOW_DIVERGENCE_PATTERN_NAME, 0) + div_count

    if not counts:
        return []

    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [name for name, _count in ranked[:limit]]


def _summarize_unmapped(unmapped_envelopes: list[TraceEnvelope]) -> UnmappedActivity:
    """Build the unmapped-activity rollup. Deterministic ordering throughout."""
    if not unmapped_envelopes:
        return UnmappedActivity(trace_count=0, sample_trace_ids=[], top_tools=[])

    sample_trace_ids = sorted(envelope.trace_id for envelope in unmapped_envelopes)[:_UNMAPPED_SAMPLE_LIMIT]

    tool_counter: Counter[str] = Counter()
    for envelope in unmapped_envelopes:
        tool_counter.update(envelope.tool_sequence)

    ranked_tools = sorted(tool_counter.items(), key=lambda kv: (-kv[1], kv[0]))
    top_tools = [name for name, _count in ranked_tools[:_UNMAPPED_TOP_TOOLS_LIMIT]]

    return UnmappedActivity(
        trace_count=len(unmapped_envelopes),
        sample_trace_ids=sample_trace_ids,
        top_tools=top_tools,
    )


def _preflight_check(envelopes: list[TraceEnvelope]) -> dict[str, float | None]:
    """Warn on sparse trace population. Returns field-rate dict.

    When the envelope list is empty the reliability rates are ``None`` — not
    ``1.0``.  Returning 1.0 for an empty run is vacuously true and misleading;
    ``None`` signals "not computable" to consumers (UI renders ``—``).
    """
    n = len(envelopes)
    if n == 0:
        return {"terminal_status_rate": None, "tool_sequence_rate": None}
    terminal_rate = sum(1 for e in envelopes if e.terminal_status != TerminalStatus.UNKNOWN) / n
    tool_seq_rate = sum(1 for e in envelopes if e.tool_sequence) / n
    if terminal_rate < _PREFLIGHT_TERMINAL_STATUS_MIN:
        logger.warning("preflight.sparse_terminal_status", rate=terminal_rate, threshold=_PREFLIGHT_TERMINAL_STATUS_MIN)
    if tool_seq_rate < _PREFLIGHT_TOOL_SEQUENCE_MIN:
        logger.warning("preflight.sparse_tool_sequence", rate=tool_seq_rate, threshold=_PREFLIGHT_TOOL_SEQUENCE_MIN)
    return {"terminal_status_rate": terminal_rate, "tool_sequence_rate": tool_seq_rate}


def run_pipeline(
    envelopes: list[TraceEnvelope],
    context: BusinessContext,
    llm_client: object | None = None,  # deprecated, ignored — semantic pass removed
    *,
    enable_divergence: bool = False,
    semantic_top_patterns: int = DEFAULT_SEMANTIC_TOP_PATTERNS,
    semantic_per_pattern: int = DEFAULT_SEMANTIC_PER_PATTERN,
) -> AnalysisResult:
    """Run the analysis pipeline end-to-end.

    Parameters
    ----------
    envelopes:
        Normalized trace envelopes to analyze.
    context:
        Customer-supplied BusinessContext (operations + expected tools).
    llm_client:
        Deprecated. Ignored. Semantic pass has been removed.
    enable_divergence:
        When True, run workflow divergence detection (E). Off by default
        until reference cohorts have ≥20 eligible traces for two weeks.
    """
    validate_taxonomy(context)
    reliability = _preflight_check(envelopes)

    operations = list(context.operations)
    op_by_name: dict[str, BusinessOperation] = {op.name: op for op in operations}

    # Step 1: classify every envelope against every operation.
    memberships_per_envelope: dict[str, list[WorkflowMembership]] = {
        env.trace_id: map_envelope_multilabel(env, operations) for env in envelopes
    }

    # Step 2: compute primary workflow per trace (deterministic tiebreak).
    def _primary_name(memberships: list[WorkflowMembership]) -> str | None:
        pw = _primary_workflow(memberships, op_by_name)
        return pw.operation_name if pw is not None else None

    primary_per_trace: dict[str, str | None] = {
        trace_id: _primary_name(memberships)
        for trace_id, memberships in memberships_per_envelope.items()
    }

    # Step 3: count secondary memberships per trace (memberships that are not the primary).
    secondary_count_per_op: dict[str, int] = {op.name: 0 for op in operations}
    for trace_id, memberships in memberships_per_envelope.items():
        primary_name = primary_per_trace.get(trace_id)
        for m in memberships:
            if m.operation_name != primary_name:
                secondary_count_per_op[m.operation_name] += 1

    # Pre-index members by op name (avoids O(N²) loop).
    op_full: dict[str, list[TraceEnvelope]] = {op.name: [] for op in operations}
    op_attempted: dict[str, list[TraceEnvelope]] = {op.name: [] for op in operations}
    for env in envelopes:
        for m in memberships_per_envelope.get(env.trace_id, []):
            if m.kind == MembershipKind.FULL:
                op_full[m.operation_name].append(env)
            elif m.kind == MembershipKind.ATTEMPTED:
                op_attempted[m.operation_name].append(env)

    # Step 4: compute per-workflow medians for detection (primary workflow's median).
    op_median_steps: dict[str, float] = {}
    for op in operations:
        all_members = op_full[op.name] + op_attempted[op.name]
        op_median_steps[op.name] = float(median(t.step_count for t in all_members)) if all_members else 0.0

    # Step 5: run tier-1 detection ONCE per trace, attributed to its primary workflow.
    # CHANGELOG: cluster_median_steps is now the primary workflow's median (was per-op).
    # Slight behavior change: traces with primary in a small workflow use that workflow's
    # median rather than the larger pool. Covered by Day 7 labeling.
    per_trace_findings: dict[str, list[Finding]] = {}
    for env in envelopes:
        primary_name = primary_per_trace.get(env.trace_id)
        if primary_name is None:
            continue
        cluster_median = op_median_steps.get(primary_name, 0.0)
        per_trace_findings[env.trace_id] = detect_tier1([env], cluster_median)

    workflows: list[WorkflowSummary] = []

    for op in operations:
        full_members = op_full[op.name]
        attempted_members = op_attempted[op.name]
        all_members = full_members + attempted_members

        outcome = compute_outcome_rate(all_members, op)
        reference = extract_reference_behavior(
            all_members,
            op,
            memberships=memberships_per_envelope,
        )

        # Findings: only from traces whose primary workflow is this op.
        deterministic_findings = [
            f
            for trace_id, findings in per_trace_findings.items()
            if primary_per_trace.get(trace_id) == op.name
            for f in findings
        ]

        divergences = detect_workflow_divergence(all_members, reference) if enable_divergence else []

        top_patterns = _top_pattern_names(deterministic_findings, divergences)

        workflows.append(
            WorkflowSummary(
                operation_name=op.name,
                full_trace_count=len(full_members),
                attempted_trace_count=len(attempted_members),
                outcome=outcome,
                reference=reference,
                deterministic_findings=deterministic_findings,
                divergences=divergences,
                top_pattern_names=top_patterns,
                member_envelopes=all_members,
                secondary_membership_count=secondary_count_per_op[op.name],
                primary_trace_ids={tid for tid, name in primary_per_trace.items() if name == op.name},
            )
        )

    # Step 6: unmapped = traces with zero memberships across all ops.
    mapped_trace_ids: set[str] = {trace_id for trace_id, memberships in memberships_per_envelope.items() if memberships}
    unmapped_envelopes = [e for e in envelopes if e.trace_id not in mapped_trace_ids]
    unmapped = _summarize_unmapped(unmapped_envelopes)

    # Step 7: correlation-key rollup (Day 9).
    # Collect all per-trace OutcomeResults from the per-workflow summaries.
    all_outcome_results = [
        result
        for ws in workflows
        for result in ws.outcome.per_trace_results
    ]
    unit_summaries = rollup_units(
        envelopes,
        all_outcome_results,
        per_trace_findings,
        correlation_key=context.correlation_key,
    )

    logger.info(
        "pipeline.completed",
        total_traces=len(envelopes),
        workflows=len(workflows),
        unmapped=unmapped.trace_count,
        units=len(unit_summaries),
        correlation_key=context.correlation_key,
    )

    return AnalysisResult(
        workflows=workflows,
        unmapped=unmapped,
        reliability=reliability,
        unit_summaries=unit_summaries,
    )


# Backward-compat aliases.
run_week1_pipeline = run_pipeline
Week1Result = AnalysisResult


class KairosEngine:
    """Deprecated wrapper. Use :func:`run_pipeline` directly."""

    def analyze(
        self,
        envelopes: list[TraceEnvelope],
        context: BusinessContext,
        llm_client: object | None = None,
        *,
        semantic_top_patterns: int = DEFAULT_SEMANTIC_TOP_PATTERNS,
        semantic_per_pattern: int = DEFAULT_SEMANTIC_PER_PATTERN,
    ) -> AnalysisResult:
        return run_pipeline(envelopes, context, llm_client)
