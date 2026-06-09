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
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING
from urllib.parse import quote

from pydantic import BaseModel

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
    """The three differentiated views for a single workflow."""

    operation_name: str
    cohort: CohortView
    divergence: list[DivergenceRow]
    correctness: CorrectnessView
    top_pattern_names: list[str]


class UnmappedView(BaseModel):
    """Traces that mapped to no workflow, with sample deep-links."""

    trace_count: int
    top_tools: list[str]
    sample_traces: list[TraceLink]


class AnalysisView(BaseModel):
    """Top-level Paperclip-native view payload built from an ``AnalysisResult``.

    Self-serializing (``model_dump_json``): this is exactly the JSON the
    Paperclip frontend renders.
    """

    phoenix_base_url: str
    phoenix_project: str
    workflows: list[WorkflowView]
    unmapped: UnmappedView
    reliability: dict[str, float]


# ───────────────────────── Builder ─────────────────────────


def build_analysis_view(
    result: AnalysisResult,
    *,
    phoenix_base_url: str = DEFAULT_PHOENIX_BASE_URL,
    phoenix_project: str = DEFAULT_PHOENIX_PROJECT,
) -> AnalysisView:
    """Flatten an ``AnalysisResult`` into a JSON-serializable ``AnalysisView``.

    Every finding/divergence/sample row gets a Phoenix deep-link built from
    ``phoenix_base_url`` and ``phoenix_project``.
    """

    def _link(trace_id: str) -> str:
        return phoenix_trace_url(trace_id, base_url=phoenix_base_url, project=phoenix_project)

    return AnalysisView(
        phoenix_base_url=phoenix_base_url,
        phoenix_project=phoenix_project,
        workflows=[_workflow_view(w, _link) for w in result.workflows],
        unmapped=_unmapped_view(result.unmapped, _link),
        reliability=result.reliability,
    )


def _workflow_view(summary: WorkflowSummary, link: _Linker) -> WorkflowView:
    return WorkflowView(
        operation_name=summary.operation_name,
        cohort=_cohort_view(summary),
        divergence=[_divergence_row(d, link) for d in summary.divergences],
        correctness=_correctness_view(summary, link),
        top_pattern_names=list(summary.top_pattern_names),
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


def _correctness_view(summary: WorkflowSummary, link: _Linker) -> CorrectnessView:
    outcome = summary.outcome
    return CorrectnessView(
        workflow_name=summary.operation_name,
        outcome_rate=outcome.outcome_rate,
        total_traces=outcome.total_traces,
        computable_count=outcome.computable_count,
        passed_count=outcome.passed_count,
        pending_reason=outcome.pending_reason,
        deterministic_findings=[_finding_row(f, link) for f in summary.deterministic_findings],
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


