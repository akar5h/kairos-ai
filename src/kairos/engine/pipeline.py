"""Week 1 pipeline orchestrator.

Glues together the existing Week 1 analysis primitives:

    1. evidence coverage (computed once over all envelopes)
    2. multi-label trace -> workflow mapping via per-op recall against
       ``BusinessOperation.expected_tools`` with a three-tier membership
       model (FULL / ATTEMPTED / NONE)
    3. per-workflow:
        - outcome rate (over FULL + ATTEMPTED members)
        - reference behavior cohort (FULL members, segmented)
        - Tier 1 deterministic findings
        - workflow divergence findings
        - top pattern names
        - optional semantic decision-state findings via LLM
    4. unmapped activity summary (sample trace IDs + top tools) — a trace
       is "unmapped" only when its membership is NONE for every operation.

The orchestrator is deterministic when no LLM client is provided. Patching
points (`detect_tier1`, `compute_evidence_coverage`,
`analyze_flagged_traces`) are imported by name so test mocks resolve via
`patch("kairos.engine.pipeline.<name>", ...)`.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from statistics import median
from typing import TYPE_CHECKING

from kairos.analysis.decision_state import extract_packet
from kairos.analysis.evidence_coverage import compute_evidence_coverage
from kairos.analysis.outcome_metric import compute_outcome_rate
from kairos.analysis.reference_behavior import extract_reference_behavior
from kairos.analysis.semantic_decision import analyze_flagged_traces
from kairos.analysis.workflow_divergence import detect_workflow_divergence
from kairos.analysis.workflow_membership import MembershipKind, WorkflowMembership
from kairos.detection.runner import detect_tier1
from kairos.log import get_logger
from kairos.models.enums import StepStatus
from kairos.taxonomy.business_context import default_membership_threshold

if TYPE_CHECKING:
    from kairos.analysis.decision_state import DecisionStatePacket
    from kairos.analysis.evidence_coverage import EvidenceCoverage
    from kairos.analysis.llm_client import LLMClient
    from kairos.analysis.outcome_metric import WorkflowOutcomeSummary
    from kairos.analysis.reference_behavior import ReferenceCohort
    from kairos.analysis.semantic_decision import SemanticDecisionFinding
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


@dataclass
class WorkflowSummary:
    """Per-workflow rollup: outcome, reference, deterministic + semantic findings.

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
    semantic_findings: list[SemanticDecisionFinding] = field(default_factory=list)
    top_pattern_names: list[str] = field(default_factory=list)

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
    """Top-level Week 1 pipeline result."""

    workflows: list[WorkflowSummary]
    unmapped: UnmappedActivity
    evidence_coverage: EvidenceCoverage
    llm_used: bool


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
                  and every distinctive tool succeeded at least once
      ATTEMPTED — at least one distinctive tool present, recall >= threshold,
                  but one or more distinctive tools are missing or failed
      NONE      — op has no distinctive tools declared (utility pattern),
                  no distinctive tool touched, recall below threshold,
                  op has no expected tools, or envelope has no tools
    """
    if not op.expected_tools or not envelope.tool_sequence:
        return WorkflowMembership(op.name, MembershipKind.NONE, 0.0)

    required_tools = set(op.required_side_effect_tools)

    # Ops without a signature tool are utility patterns, not workflows —
    # they can't be distinguished from arbitrary supporting activity.
    # (Taxonomy validation surfaces this once at pipeline startup; we stay
    # silent here to avoid per-envelope log spam.)
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

    all_required_succeeded = all(count > 0 for count in required_success_counts.values())
    if all_required_succeeded:
        return WorkflowMembership(op.name, MembershipKind.FULL, recall)

    return WorkflowMembership(op.name, MembershipKind.ATTEMPTED, recall)


def map_envelope_multilabel(
    envelope: TraceEnvelope,
    operations: list[BusinessOperation],
) -> list[WorkflowMembership]:
    """Return zero-or-more memberships, one per op where membership != NONE."""
    memberships = [classify_membership(envelope, op) for op in operations]
    return [m for m in memberships if m.kind != MembershipKind.NONE]


def validate_taxonomy(context: BusinessContext) -> list[str]:
    """Return the names of ops that lack distinctive tools.

    Called once at pipeline startup. Each flagged op is also logged with a
    review hint so operators can decide whether to add a signature tool or
    drop the op as a utility pattern. Returns the list of flagged op names
    so callers can surface them in reports.
    """
    flagged: list[str] = []
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
    if flagged:
        logger.warning(
            "taxonomy.utility_patterns_detected",
            count=len(flagged),
            operations=flagged,
        )
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


def _safe_step_index(trace: TraceEnvelope, target: int | None) -> int | None:
    """Return *target* if it points at an actual step, else best-effort fallback."""
    if not trace.steps:
        return None
    valid_indices = {step.step_index for step in trace.steps}
    if target is not None and target in valid_indices:
        return target
    midpoint = trace.step_count // 2
    if midpoint in valid_indices:
        return midpoint
    return trace.steps[0].step_index


def _build_packets(
    workflow_traces: list[TraceEnvelope],
    operation: BusinessOperation,
    coverage: EvidenceCoverage,
    reference: ReferenceCohort,
    findings: list[Finding],
    divergences: list[DivergenceFinding],
) -> dict[str, list[DecisionStatePacket]]:
    """Build per-pattern packets for the semantic decision pass."""
    traces_by_id: dict[str, TraceEnvelope] = {t.trace_id: t for t in workflow_traces}
    packets_by_pattern: dict[str, list[DecisionStatePacket]] = {}

    for finding in findings:
        trace = traces_by_id.get(finding.trace_id)
        if trace is None:
            continue
        target_step = finding.affected_step_indices[0] if finding.affected_step_indices else None
        step_index = _safe_step_index(trace, target_step)
        if step_index is None:
            continue
        try:
            packet = extract_packet(
                trace=trace,
                step_index=step_index,
                operation=operation,
                coverage=coverage,
                reference=reference,
                deterministic_flags=[finding.pattern_name],
            )
        except (ValueError, KeyError, AttributeError, IndexError) as exc:
            logger.warning(
                "week1_pipeline.packet_extraction_failed",
                trace_id=finding.trace_id,
                pattern=finding.pattern_name,
                error=str(exc)[:200],
            )
            continue
        packets_by_pattern.setdefault(finding.pattern_name, []).append(packet)

    for divergence in divergences:
        if divergence.first_divergence_step is None:
            continue
        trace = traces_by_id.get(divergence.trace_id)
        if trace is None:
            continue
        step_index = _safe_step_index(trace, divergence.first_divergence_step)
        if step_index is None:
            continue
        try:
            packet = extract_packet(
                trace=trace,
                step_index=step_index,
                operation=operation,
                coverage=coverage,
                reference=reference,
                deterministic_flags=[WORKFLOW_DIVERGENCE_PATTERN_NAME],
            )
        except (ValueError, KeyError, AttributeError, IndexError) as exc:
            logger.warning(
                "week1_pipeline.packet_extraction_failed",
                trace_id=divergence.trace_id,
                pattern=WORKFLOW_DIVERGENCE_PATTERN_NAME,
                error=str(exc)[:200],
            )
            continue
        packets_by_pattern.setdefault(WORKFLOW_DIVERGENCE_PATTERN_NAME, []).append(packet)

    return packets_by_pattern


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


def _run_semantic_pass(
    operation: BusinessOperation,
    workflow_traces: list[TraceEnvelope],
    coverage: EvidenceCoverage,
    reference: ReferenceCohort,
    deterministic_findings: list[Finding],
    divergences: list[DivergenceFinding],
    llm_client: LLMClient | None,
    top_n_patterns: int,
    per_pattern_trace_limit: int,
) -> list[SemanticDecisionFinding]:
    """Run the semantic decision pass. Returns [] when LLM is off or no packets."""
    if llm_client is None or not workflow_traces:
        return []

    packets_by_pattern = _build_packets(
        workflow_traces,
        operation,
        coverage,
        reference,
        deterministic_findings,
        divergences,
    )
    if not packets_by_pattern:
        return []

    trace_metrics = {t.trace_id: (t.step_count, t.total_tokens) for t in workflow_traces}
    return analyze_flagged_traces(
        packets_by_pattern,
        llm_client,
        trace_metrics=trace_metrics,
        top_n_patterns=top_n_patterns,
        per_pattern_trace_limit=per_pattern_trace_limit,
    )


def run_week1_pipeline(
    envelopes: list[TraceEnvelope],
    context: BusinessContext,
    llm_client: LLMClient | None = None,
    *,
    semantic_top_patterns: int = DEFAULT_SEMANTIC_TOP_PATTERNS,
    semantic_per_pattern: int = DEFAULT_SEMANTIC_PER_PATTERN,
) -> AnalysisResult:
    """Run the Week 1 analysis pipeline end-to-end.

    Parameters
    ----------
    envelopes:
        Normalized trace envelopes to analyze.
    context:
        Customer-supplied BusinessContext (operations + expected tools).
    llm_client:
        Optional LLMClient. When ``None`` the semantic pass is skipped and
        the result is fully deterministic.
    semantic_top_patterns:
        Forwarded to :func:`analyze_flagged_traces` — top-N patterns to
        analyze semantically.
    semantic_per_pattern:
        Forwarded to :func:`analyze_flagged_traces` — per-pattern trace
        cap for the semantic pass.
    """
    validate_taxonomy(context)
    coverage = compute_evidence_coverage(envelopes)

    operations = list(context.operations)

    # Step 1: classify every envelope against every operation.
    memberships_per_envelope: dict[str, list[WorkflowMembership]] = {
        env.trace_id: map_envelope_multilabel(env, operations) for env in envelopes
    }

    workflows: list[WorkflowSummary] = []
    semantic_used = False

    for op in operations:
        full_members: list[TraceEnvelope] = []
        attempted_members: list[TraceEnvelope] = []
        for env in envelopes:
            for m in memberships_per_envelope.get(env.trace_id, []):
                if m.operation_name != op.name:
                    continue
                if m.kind == MembershipKind.FULL:
                    full_members.append(env)
                elif m.kind == MembershipKind.ATTEMPTED:
                    attempted_members.append(env)

        all_members = full_members + attempted_members

        outcome = compute_outcome_rate(all_members, op)
        reference = extract_reference_behavior(
            all_members,
            op,
            memberships=memberships_per_envelope,
        )

        cluster_median_steps = float(median(t.step_count for t in all_members)) if all_members else 0.0

        deterministic_findings = detect_tier1(all_members, cluster_median_steps)
        divergences = detect_workflow_divergence(all_members, reference)
        top_patterns = _top_pattern_names(deterministic_findings, divergences)

        semantic_findings = _run_semantic_pass(
            operation=op,
            workflow_traces=all_members,
            coverage=coverage,
            reference=reference,
            deterministic_findings=deterministic_findings,
            divergences=divergences,
            llm_client=llm_client,
            top_n_patterns=semantic_top_patterns,
            per_pattern_trace_limit=semantic_per_pattern,
        )
        if semantic_findings:
            semantic_used = True

        workflows.append(
            WorkflowSummary(
                operation_name=op.name,
                full_trace_count=len(full_members),
                attempted_trace_count=len(attempted_members),
                outcome=outcome,
                reference=reference,
                deterministic_findings=deterministic_findings,
                divergences=divergences,
                semantic_findings=semantic_findings,
                top_pattern_names=top_patterns,
            )
        )

    # Step 4: unmapped = traces with zero memberships across all ops.
    mapped_trace_ids: set[str] = {trace_id for trace_id, memberships in memberships_per_envelope.items() if memberships}
    unmapped_envelopes = [e for e in envelopes if e.trace_id not in mapped_trace_ids]
    unmapped = _summarize_unmapped(unmapped_envelopes)

    llm_used = llm_client is not None and semantic_used

    logger.info(
        "week1_pipeline.completed",
        total_traces=len(envelopes),
        workflows=len(workflows),
        unmapped=unmapped.trace_count,
        llm_used=llm_used,
    )

    return AnalysisResult(
        workflows=workflows,
        unmapped=unmapped,
        evidence_coverage=coverage,
        llm_used=llm_used,
    )


# Public name retained for ported tests; AnalysisResult is the canonical SDK type.
Week1Result = AnalysisResult


class KairosEngine:
    """On-demand analysis engine — the single public entrypoint.

    Reads only the IR (``TraceEnvelope``). Sources are explicit at the call
    site; the engine itself runs one deterministic path (plus an optional
    semantic pass when an ``LLMClient`` is supplied).
    """

    def analyze(
        self,
        envelopes: list[TraceEnvelope],
        context: BusinessContext,
        llm_client: LLMClient | None = None,
        *,
        semantic_top_patterns: int = DEFAULT_SEMANTIC_TOP_PATTERNS,
        semantic_per_pattern: int = DEFAULT_SEMANTIC_PER_PATTERN,
    ) -> AnalysisResult:
        """Analyze normalized envelopes against a business context."""
        return run_week1_pipeline(
            envelopes,
            context,
            llm_client,
            semantic_top_patterns=semantic_top_patterns,
            semantic_per_pattern=semantic_per_pattern,
        )
