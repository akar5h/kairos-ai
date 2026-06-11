"""Presenter: ``AnalysisResult`` → JSON view payload for Paperclip-native UI.

Phase C-UI (XER-71): the board chose the split-UI option. Raw per-trace spans
stay in the Phoenix UI — we deep-link into it, we do not fork it (a fork stays
ELv2 / not MIT). The differentiated analysis — cohort, workflow-divergence,
correctness — renders in Paperclip-native MIT views built from the engine's
``AnalysisResult``.

This module is the contract between the two halves. ``build_analysis_view``
flattens the engine's mixed dataclass/Pydantic graph into a single, typed,
JSON-serializable ``AnalysisView`` whose finding rows each carry a Phoenix
deep-link for drill-down. Rendering those views (React/HTML) is the Paperclip
frontend's job (XER-78) — none of that lives here, honoring Kairos's "No UI"
rule: this is data, not UI.

XER-169 additions:
  - Workflows with zero traces are filtered from the view.
  - ``WorkflowView`` carries ``show_reference_sections``, ``finding_count``,
    and ``max_severity`` so the frontend can hide null sections and surface
    severity prominently without re-deriving them.
  - ``AnalysisView`` carries a top-level ``summary`` hero card and a static
    ``metric_descriptions`` dict (plain-English tooltips for every metric label).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING
from urllib.parse import quote

from pydantic import BaseModel


class AnalysisMeta(BaseModel):
    """Provenance block attached to every AnalysisView produced by the engine.

    Carries enough information to reproduce or audit the run:
      engine_version     — importlib.metadata.version("kairos-ai")
      context_path       — absolute path to the context YAML used
      context_sha256     — SHA-256 hex digest of the raw context file bytes
      operation_count    — number of operations loaded from the context
      trace_count_fetched   — envelopes resolved from the data source
      trace_count_analyzed  — envelopes that passed normalization and entered the pipeline

    No timestamps here — the engine is deterministic; wall-clock time is stamped
    by the plugin on the saved filename, not inside the payload.
    """

    engine_version: str
    context_path: str
    context_sha256: str
    operation_count: int
    trace_count_fetched: int
    trace_count_analyzed: int


# Builds a Phoenix deep-link from a trace id; closed over the base-url/project
# chosen for one ``build_analysis_view`` call.
_Linker = Callable[[str], str]

if TYPE_CHECKING:
    from kairos.analysis.reference_behavior import ReferenceCohort
    from kairos.analysis.workflow_divergence import DivergenceFinding
    from kairos.detection.models import Finding
    from kairos.engine.pipeline import AnalysisResult, UnmappedActivity, WorkflowSummary

# ───────────────────────── Phoenix deep-link ─────────────────────────

DEFAULT_PHOENIX_BASE_URL: str = "http://localhost:6006"
DEFAULT_PHOENIX_PROJECT: str = "default"
# Phoenix UI route for a single trace. ``{base}`` carries no trailing slash.
# Overridable because Phoenix's URL scheme has shifted across versions; the
# default targets the current ``/projects/{project}/traces/{trace_id}`` route.
DEFAULT_TRACE_URL_TEMPLATE: str = "{base}/projects/{project}/traces/{trace_id}"


def phoenix_trace_url(
    trace_id: str,
    *,
    base_url: str = DEFAULT_PHOENIX_BASE_URL,
    project: str = DEFAULT_PHOENIX_PROJECT,
    url_template: str = DEFAULT_TRACE_URL_TEMPLATE,
) -> str:
    """Build a deep-link into the Phoenix UI for one trace.

    Pure string builder, no network. ``trace_id`` and ``project`` are
    URL-quoted; ``base_url`` is stripped of any trailing slash so the template's
    separators are exact.
    """
    if not trace_id:
        msg = "trace_id must be non-empty to build a Phoenix deep-link"
        raise ValueError(msg)
    return url_template.format(
        base=base_url.rstrip("/"),
        project=quote(project, safe=""),
        trace_id=quote(trace_id, safe=""),
    )


# ───────────────────────── Static metric descriptions ─────────────────────────

METRIC_DESCRIPTIONS: dict[str, str] = {
    "confidence": (
        "How many clean, completed sessions we have to define 'normal' behavior. "
        "High = 50+, Medium = 20–49, Low = 5–19, None = fewer than 5."
    ),
    "eligible_trace_count": (
        "Sessions that completed without errors and are used to build the reference behavior model."
    ),
    "reference_trace_count": (
        "The subset of eligible sessions that match the most common tool sequence — "
        "the 'gold standard' path for this workflow."
    ),
    "full_trace_count": "Sessions where every expected tool ran successfully.",
    "attempted_trace_count": (
        "Sessions where the workflow was started but at least one expected tool was missing or failed."
    ),
    "step_budget_p75": (
        "75th-percentile step count from reference sessions. "
        "Sessions above this are using more steps than 75% of healthy runs."
    ),
    "token_budget_p75": (
        "75th-percentile token usage from reference sessions. "
        "Sessions above this are consuming more tokens than 75% of healthy runs."
    ),
    "outcome_rate": (
        "Fraction of sessions that produced a successful outcome (e.g., a task completed, a lead converted)."
    ),
    "extra_rate": (
        "Fraction of tool calls in a session that go beyond what the reference path expects — a proxy for wasted work."
    ),
    "coverage": "How much of the reference tool path this session covered.",
    "estimated_token_waste": ("Tokens estimated to be consumed by the inefficient pattern — useful for cost triage."),
    "severity": (
        "How serious this finding is: 'warning' = notable but not urgent, "
        "'critical' = likely hurting outcomes or burning significant tokens."
    ),
    "show_reference_sections": (
        "When false, the reference path, budgets, and divergence sections are hidden "
        "because there are not yet enough clean sessions to compute them reliably."
    ),
}

# Severity ordering for max_severity computation: higher index = worse.
_SEVERITY_RANK: dict[str, int] = {"warning": 0, "critical": 1}


def _max_severity(severities: list[str]) -> str | None:
    """Return the worst severity in *severities*, or None if the list is empty."""
    if not severities:
        return None
    return max(severities, key=lambda s: _SEVERITY_RANK.get(s, -1))


# ───────────────────────── View DTOs ─────────────────────────


class TraceLink(BaseModel):
    """A trace id paired with its Phoenix deep-link."""

    trace_id: str
    phoenix_url: str


class CohortView(BaseModel):
    """Reference-cohort summary for one workflow."""

    workflow_name: str
    confidence: str
    eligible_trace_count: int
    reference_trace_count: int
    full_trace_count: int
    attempted_trace_count: int
    reference_path: list[str]
    step_budget_p75: float | None
    token_budget_p75: float | None


class DivergenceRow(BaseModel):
    """One trace's divergence against the reference cohort, with deep-link."""

    trace_id: str
    phoenix_url: str
    first_divergence_step: int | None
    expected_transition: tuple[str, str] | None
    actual_transition: tuple[str, str] | None
    extra_rate: float
    coverage: float
    variant_candidate: bool


class FindingRow(BaseModel):
    """A deterministic detector finding on one trace, with deep-link."""

    trace_id: str
    phoenix_url: str
    pattern_name: str
    tier: int
    severity: str
    confidence: float
    affected_step_indices: list[int]
    estimated_token_waste: int


class CorrectnessView(BaseModel):
    """Outcome rate + all findings for one workflow."""

    workflow_name: str
    outcome_rate: float | None
    total_traces: int
    computable_count: int
    passed_count: int
    pending_reason: str | None
    deterministic_findings: list[FindingRow]


class WorkflowView(BaseModel):
    """The three differentiated views for a single workflow.

    XER-169 additions:
      ``show_reference_sections`` — False when confidence is 'none'; the frontend
        should collapse the reference-path, budgets, and divergence panels and show
        "Not enough clean sessions yet. Findings below still apply."
      ``finding_count`` — total deterministic finding rows for this workflow.
      ``max_severity`` — worst severity across all findings ('critical' > 'warning'),
        or None when there are no findings. Surface as a badge.
    """

    operation_name: str
    cohort: CohortView
    divergence: list[DivergenceRow]
    correctness: CorrectnessView
    top_pattern_names: list[str]
    show_reference_sections: bool
    finding_count: int
    max_severity: str | None


class UnmappedView(BaseModel):
    """Traces that mapped to no workflow, with sample deep-links."""

    trace_count: int
    top_tools: list[str]
    sample_traces: list[TraceLink]


class AnalysisSummary(BaseModel):
    """Hero-card metrics surfaced at the top of the analysis view (XER-169).

    ``total_pattern_issues`` — total finding rows across all workflows.
    ``affected_sessions``     — unique trace ids that have at least one finding.
    ``workflows_with_findings`` — number of workflows that have at least one finding.
    """

    total_pattern_issues: int
    affected_sessions: int
    workflows_with_findings: int


class AnalysisView(BaseModel):
    """Top-level Paperclip-native view payload built from an ``AnalysisResult``.

    Self-serializing (``model_dump_json``): this is exactly the JSON the
    Paperclip frontend renders.

    XER-169 additions:
      ``summary``             — hero-card counts (issues / sessions / workflows).
      ``metric_descriptions`` — plain-English tooltip text keyed by field name.

    Day 1.2 additions:
      ``meta``        — provenance block; None when parsing old saved files.
      ``reliability`` — values are ``float | None``; None when the run had zero
                        envelopes (null-reliability invariant — no vacuous 1.0).
    """

    phoenix_base_url: str
    phoenix_project: str
    workflows: list[WorkflowView]
    unmapped: UnmappedView
    reliability: dict[str, float | None]
    summary: AnalysisSummary
    metric_descriptions: dict[str, str]
    meta: AnalysisMeta | None = None


# ───────────────────────── Builder ─────────────────────────


def build_analysis_view(
    result: AnalysisResult,
    *,
    phoenix_base_url: str = DEFAULT_PHOENIX_BASE_URL,
    phoenix_project: str = DEFAULT_PHOENIX_PROJECT,
    meta: AnalysisMeta | None = None,
) -> AnalysisView:
    """Flatten an ``AnalysisResult`` into a JSON-serializable ``AnalysisView``.

    Every finding/divergence/sample row gets a Phoenix deep-link built from
    ``phoenix_base_url`` and ``phoenix_project``.

    XER-169: workflows with zero total traces are filtered out (they represent
    lead-pipeline operations that had no activity and would render as empty tables).

    Day 1.2: ``meta`` carries run provenance (engine version, context path/sha,
    trace counts). Pass it from the CLI; it is optional so old saved files parse
    without it.
    """

    def _link(trace_id: str) -> str:
        return phoenix_trace_url(trace_id, base_url=phoenix_base_url, project=phoenix_project)

    # Filter workflows with no trace activity before building view objects.
    active_workflows = [w for w in result.workflows if (w.full_trace_count + w.attempted_trace_count) > 0]

    workflow_views = [_workflow_view(w, _link) for w in active_workflows]
    summary = _build_summary(workflow_views)

    return AnalysisView(
        phoenix_base_url=phoenix_base_url,
        phoenix_project=phoenix_project,
        workflows=workflow_views,
        unmapped=_unmapped_view(result.unmapped, _link),
        reliability=result.reliability,
        summary=summary,
        metric_descriptions=METRIC_DESCRIPTIONS,
        meta=meta,
    )


def _build_summary(workflows: list[WorkflowView]) -> AnalysisSummary:
    """Compute hero-card metrics from the already-built workflow views."""
    total_issues = sum(wf.finding_count for wf in workflows)
    affected: set[str] = set()
    for wf in workflows:
        for row in wf.correctness.deterministic_findings:
            affected.add(row.trace_id)
    workflows_with_findings = sum(1 for wf in workflows if wf.finding_count > 0)
    return AnalysisSummary(
        total_pattern_issues=total_issues,
        affected_sessions=len(affected),
        workflows_with_findings=workflows_with_findings,
    )


def _workflow_view(summary: WorkflowSummary, link: _Linker) -> WorkflowView:
    findings = [_finding_row(f, link) for f in summary.deterministic_findings]
    show_ref = summary.reference.confidence.value != "none"
    return WorkflowView(
        operation_name=summary.operation_name,
        cohort=_cohort_view(summary),
        divergence=[_divergence_row(d, link) for d in summary.divergences],
        correctness=_correctness_view(summary, findings),
        top_pattern_names=list(summary.top_pattern_names),
        show_reference_sections=show_ref,
        finding_count=len(findings),
        max_severity=_max_severity([f.severity for f in findings]),
    )


def _cohort_view(summary: WorkflowSummary) -> CohortView:
    ref: ReferenceCohort = summary.reference
    return CohortView(
        workflow_name=summary.operation_name,
        confidence=ref.confidence.value,
        eligible_trace_count=len(ref.eligible_traces),
        reference_trace_count=len(ref.reference_traces),
        full_trace_count=summary.full_trace_count,
        attempted_trace_count=summary.attempted_trace_count,
        reference_path=list(ref.reference_path),
        step_budget_p75=ref.step_budget_p75,
        token_budget_p75=ref.token_budget_p75,
    )


def _divergence_row(finding: DivergenceFinding, link: _Linker) -> DivergenceRow:
    return DivergenceRow(
        trace_id=finding.trace_id,
        phoenix_url=link(finding.trace_id),
        first_divergence_step=finding.first_divergence_step,
        expected_transition=finding.expected_transition,
        actual_transition=finding.actual_transition,
        extra_rate=finding.extra_rate,
        coverage=finding.coverage,
        variant_candidate=finding.variant_candidate,
    )


def _correctness_view(summary: WorkflowSummary, findings: list[FindingRow]) -> CorrectnessView:
    outcome = summary.outcome
    return CorrectnessView(
        workflow_name=summary.operation_name,
        outcome_rate=outcome.outcome_rate,
        total_traces=outcome.total_traces,
        computable_count=outcome.computable_count,
        passed_count=outcome.passed_count,
        pending_reason=outcome.pending_reason,
        deterministic_findings=findings,
    )


def _finding_row(finding: Finding, link: _Linker) -> FindingRow:
    return FindingRow(
        trace_id=finding.trace_id,
        phoenix_url=link(finding.trace_id),
        pattern_name=finding.pattern_name,
        tier=finding.tier,
        severity=finding.severity,
        confidence=finding.confidence,
        affected_step_indices=list(finding.affected_step_indices),
        estimated_token_waste=finding.estimated_token_waste,
    )


def _unmapped_view(unmapped: UnmappedActivity, link: _Linker) -> UnmappedView:
    return UnmappedView(
        trace_count=unmapped.trace_count,
        top_tools=list(unmapped.top_tools),
        sample_traces=[TraceLink(trace_id=tid, phoenix_url=link(tid)) for tid in unmapped.sample_trace_ids],
    )
