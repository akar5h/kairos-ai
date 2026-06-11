"""Tests for the Phase C-UI presenter: AnalysisResult -> AnalysisView + deep-links."""

from __future__ import annotations

import json

import pytest

from kairos.analysis.outcome_metric import WorkflowOutcomeSummary
from kairos.analysis.reference_behavior import ReferenceCohort, ReferenceConfidence
from kairos.analysis.workflow_divergence import DivergenceFinding
from kairos.detection.models import Finding
from kairos.engine.pipeline import AnalysisResult, UnmappedActivity, WorkflowSummary
from kairos.views.analysis_view import (
    METRIC_DESCRIPTIONS,
    AnalysisView,
    build_analysis_view,
    phoenix_trace_url,
)

# ───────────────────────── phoenix_trace_url ─────────────────────────


class TestPhoenixTraceUrl:
    def test_default_scheme(self) -> None:
        url = phoenix_trace_url("abc123")
        assert url == "http://localhost:6006/projects/default/traces/abc123"

    def test_strips_trailing_slash_on_base(self) -> None:
        url = phoenix_trace_url("t1", base_url="http://phoenix.internal:6006/")
        assert url == "http://phoenix.internal:6006/projects/default/traces/t1"

    def test_quotes_trace_id_and_project(self) -> None:
        url = phoenix_trace_url("a/b c", project="proj x")
        assert url == "http://localhost:6006/projects/proj%20x/traces/a%2Fb%20c"

    def test_custom_template(self) -> None:
        url = phoenix_trace_url(
            "t1",
            base_url="http://h",
            url_template="{base}/t/{trace_id}",
        )
        assert url == "http://h/t/t1"

    def test_empty_trace_id_raises(self) -> None:
        with pytest.raises(ValueError, match="trace_id must be non-empty"):
            phoenix_trace_url("")


# ───────────────────────── fixtures ─────────────────────────


def _outcome(name: str = "Screening", total: int = 10) -> WorkflowOutcomeSummary:
    return WorkflowOutcomeSummary(
        workflow_name=name,
        total_traces=total,
        computable_count=8,
        passed_count=6,
        outcome_rate=0.75,
        pending_reason=None,
    )


def _ref(confidence: ReferenceConfidence = ReferenceConfidence.MEDIUM) -> ReferenceCohort:
    return ReferenceCohort(
        eligible_traces=[],
        reference_traces=[],
        confidence=confidence,
        reference_dfg=None,
        reference_edges={("a", "b")},
        reference_path=["a", "b", "c"],
        step_budget_p75=12.0,
        token_budget_p75=3400.0,
    )


def _finding(trace_id: str = "det-1", severity: str = "critical") -> Finding:
    return Finding(
        pattern_name="redundant_execution",
        tier=1,
        trace_id=trace_id,
        confidence=0.9,
        severity=severity,
        evidence={"runs": 3},
        affected_step_indices=[4, 5, 6],
        estimated_token_waste=1200,
    )


def _divergence() -> DivergenceFinding:
    return DivergenceFinding(
        trace_id="div-1",
        first_divergence_step=3,
        expected_transition=("a", "b"),
        actual_transition=("a", "z"),
        extra_rate=0.25,
        coverage=0.8,
        variant_candidate=True,
    )


def _workflow(
    name: str = "Screening",
    full: int = 6,
    attempted: int = 2,
    confidence: ReferenceConfidence = ReferenceConfidence.MEDIUM,
    findings: list[Finding] | None = None,
    divergences: list[DivergenceFinding] | None = None,
) -> WorkflowSummary:
    return WorkflowSummary(
        operation_name=name,
        full_trace_count=full,
        attempted_trace_count=attempted,
        outcome=_outcome(name),
        reference=_ref(confidence),
        deterministic_findings=findings if findings is not None else [_finding()],
        divergences=divergences if divergences is not None else [_divergence()],
        top_pattern_names=["redundant_execution"],
    )


def _sample_result(extra_workflows: list[WorkflowSummary] | None = None) -> AnalysisResult:
    workflows = [_workflow()]
    if extra_workflows:
        workflows.extend(extra_workflows)
    return AnalysisResult(
        workflows=workflows,
        unmapped=UnmappedActivity(
            trace_count=2,
            sample_trace_ids=["unm-1", "unm-2"],
            top_tools=["search", "fetch"],
        ),
        reliability={"terminal_status_rate": 0.92, "tool_sequence_rate": 0.88},
    )


# ───────────────────────── build_analysis_view ─────────────────────────


class TestBuildAnalysisView:
    def test_returns_analysis_view(self) -> None:
        view = build_analysis_view(_sample_result())
        assert isinstance(view, AnalysisView)
        assert len(view.workflows) == 1
        assert isinstance(view.reliability, dict)

    def test_cohort_view(self) -> None:
        wf = build_analysis_view(_sample_result()).workflows[0]
        assert wf.cohort.confidence == "medium"
        assert wf.cohort.reference_path == ["a", "b", "c"]
        assert wf.cohort.full_trace_count == 6
        assert wf.cohort.attempted_trace_count == 2
        assert wf.cohort.step_budget_p75 == 12.0

    def test_divergence_rows_carry_deep_link(self) -> None:
        wf = build_analysis_view(_sample_result(), phoenix_base_url="http://px:6006").workflows[0]
        assert len(wf.divergence) == 1
        row = wf.divergence[0]
        assert row.trace_id == "div-1"
        assert row.phoenix_url == "http://px:6006/projects/default/traces/div-1"
        assert row.variant_candidate is True
        assert row.expected_transition == ("a", "b")

    def test_correctness_findings_carry_deep_link(self) -> None:
        wf = build_analysis_view(_sample_result()).workflows[0]
        corr = wf.correctness
        assert corr.outcome_rate == 0.75
        assert len(corr.deterministic_findings) == 1
        assert corr.deterministic_findings[0].phoenix_url == "http://localhost:6006/projects/default/traces/det-1"
        assert corr.deterministic_findings[0].estimated_token_waste == 1200

    def test_unmapped_sample_links(self) -> None:
        view = build_analysis_view(_sample_result())
        assert view.unmapped.trace_count == 2
        assert [t.trace_id for t in view.unmapped.sample_traces] == ["unm-1", "unm-2"]
        assert view.unmapped.sample_traces[0].phoenix_url.endswith("/traces/unm-1")

    def test_reliability_passthrough(self) -> None:
        view = build_analysis_view(_sample_result())
        assert view.reliability["terminal_status_rate"] == pytest.approx(0.92)
        assert view.reliability["tool_sequence_rate"] == pytest.approx(0.88)

    def test_custom_project_in_links(self) -> None:
        view = build_analysis_view(_sample_result(), phoenix_project="xero")
        assert view.phoenix_project == "xero"
        assert "/projects/xero/traces/" in view.workflows[0].divergence[0].phoenix_url

    def test_view_is_json_serializable(self) -> None:
        view = build_analysis_view(_sample_result())
        payload = view.model_dump_json()
        parsed = json.loads(payload)
        assert parsed["workflows"][0]["cohort"]["confidence"] == "medium"
        assert parsed["workflows"][0]["correctness"]["outcome_rate"] == 0.75

    def test_empty_result(self) -> None:
        empty = AnalysisResult(
            workflows=[],
            unmapped=UnmappedActivity(trace_count=0, sample_trace_ids=[], top_tools=[]),
            reliability={},
        )
        view = build_analysis_view(empty)
        assert view.workflows == []
        assert view.unmapped.sample_traces == []


# ───────────────────────── XER-169: show_reference_sections ─────────────────────────


class TestShowReferenceSections:
    def test_medium_confidence_shows_reference(self) -> None:
        wf = build_analysis_view(_sample_result()).workflows[0]
        assert wf.show_reference_sections is True

    def test_none_confidence_hides_reference(self) -> None:
        result = AnalysisResult(
            workflows=[_workflow(confidence=ReferenceConfidence.NONE)],
            unmapped=UnmappedActivity(trace_count=0, sample_trace_ids=[], top_tools=[]),
            reliability={},
        )
        wf = build_analysis_view(result).workflows[0]
        assert wf.show_reference_sections is False

    def test_low_confidence_shows_reference(self) -> None:
        result = AnalysisResult(
            workflows=[_workflow(confidence=ReferenceConfidence.LOW)],
            unmapped=UnmappedActivity(trace_count=0, sample_trace_ids=[], top_tools=[]),
            reliability={},
        )
        wf = build_analysis_view(result).workflows[0]
        assert wf.show_reference_sections is True


# ───────────────────────── XER-169: finding_count + max_severity ────────────────────


class TestFindingCountAndSeverity:
    def test_finding_count_matches_findings_list(self) -> None:
        result = AnalysisResult(
            workflows=[_workflow(findings=[_finding("t1"), _finding("t2")])],
            unmapped=UnmappedActivity(trace_count=0, sample_trace_ids=[], top_tools=[]),
            reliability={},
        )
        wf = build_analysis_view(result).workflows[0]
        assert wf.finding_count == 2

    def test_max_severity_critical_beats_warning(self) -> None:
        result = AnalysisResult(
            workflows=[_workflow(findings=[_finding("t1", "warning"), _finding("t2", "critical")])],
            unmapped=UnmappedActivity(trace_count=0, sample_trace_ids=[], top_tools=[]),
            reliability={},
        )
        wf = build_analysis_view(result).workflows[0]
        assert wf.max_severity == "critical"

    def test_max_severity_warning_only(self) -> None:
        result = AnalysisResult(
            workflows=[_workflow(findings=[_finding("t1", "warning")])],
            unmapped=UnmappedActivity(trace_count=0, sample_trace_ids=[], top_tools=[]),
            reliability={},
        )
        wf = build_analysis_view(result).workflows[0]
        assert wf.max_severity == "warning"

    def test_max_severity_none_when_no_findings(self) -> None:
        result = AnalysisResult(
            workflows=[_workflow(findings=[])],
            unmapped=UnmappedActivity(trace_count=0, sample_trace_ids=[], top_tools=[]),
            reliability={},
        )
        wf = build_analysis_view(result).workflows[0]
        assert wf.max_severity is None
        assert wf.finding_count == 0


# ───────────────────────── XER-169: zero-trace workflow filtering ─────────────────


class TestZeroTraceFiltering:
    def test_zero_trace_workflow_excluded(self) -> None:
        zero_wf = _workflow(name="LeadScraping", full=0, attempted=0)
        result = _sample_result(extra_workflows=[zero_wf])
        view = build_analysis_view(result)
        names = [w.operation_name for w in view.workflows]
        assert "LeadScraping" not in names
        assert "Screening" in names

    def test_nonzero_workflow_retained(self) -> None:
        result = _sample_result()
        view = build_analysis_view(result)
        assert len(view.workflows) == 1
        assert view.workflows[0].operation_name == "Screening"

    def test_all_zero_trace_yields_empty_workflows(self) -> None:
        result = AnalysisResult(
            workflows=[_workflow(name="A", full=0, attempted=0)],
            unmapped=UnmappedActivity(trace_count=0, sample_trace_ids=[], top_tools=[]),
            reliability={},
        )
        view = build_analysis_view(result)
        assert view.workflows == []


# ───────────────────────── XER-169: summary hero card ───────────────────────────


class TestAnalysisSummary:
    def test_summary_counts_issues_and_sessions(self) -> None:
        result = AnalysisResult(
            workflows=[_workflow(findings=[_finding("t1"), _finding("t2")])],
            unmapped=UnmappedActivity(trace_count=0, sample_trace_ids=[], top_tools=[]),
            reliability={},
        )
        view = build_analysis_view(result)
        assert view.summary.total_pattern_issues == 2
        assert view.summary.affected_sessions == 2
        assert view.summary.workflows_with_findings == 1

    def test_summary_deduplicates_sessions(self) -> None:
        # same trace_id in two findings: only 1 unique affected session
        result = AnalysisResult(
            workflows=[_workflow(findings=[_finding("same"), _finding("same")])],
            unmapped=UnmappedActivity(trace_count=0, sample_trace_ids=[], top_tools=[]),
            reliability={},
        )
        view = build_analysis_view(result)
        assert view.summary.total_pattern_issues == 2
        assert view.summary.affected_sessions == 1

    def test_summary_zero_when_no_findings(self) -> None:
        result = AnalysisResult(
            workflows=[_workflow(findings=[])],
            unmapped=UnmappedActivity(trace_count=0, sample_trace_ids=[], top_tools=[]),
            reliability={},
        )
        view = build_analysis_view(result)
        assert view.summary.total_pattern_issues == 0
        assert view.summary.affected_sessions == 0
        assert view.summary.workflows_with_findings == 0

    def test_summary_workflows_with_findings_count(self) -> None:
        wf_with = _workflow(name="A", findings=[_finding()])
        wf_without = _workflow(name="B", findings=[])
        result = AnalysisResult(
            workflows=[wf_with, wf_without],
            unmapped=UnmappedActivity(trace_count=0, sample_trace_ids=[], top_tools=[]),
            reliability={},
        )
        view = build_analysis_view(result)
        assert view.summary.workflows_with_findings == 1

    def test_summary_in_serialized_json(self) -> None:
        view = build_analysis_view(_sample_result())
        parsed = json.loads(view.model_dump_json())
        assert "summary" in parsed
        assert "total_pattern_issues" in parsed["summary"]
        assert "affected_sessions" in parsed["summary"]
        assert "workflows_with_findings" in parsed["summary"]


# ───────────────────────── XER-169: metric_descriptions ─────────────────────────


class TestMetricDescriptions:
    def test_descriptions_present_in_view(self) -> None:
        view = build_analysis_view(_sample_result())
        assert view.metric_descriptions == METRIC_DESCRIPTIONS

    def test_key_fields_have_descriptions(self) -> None:
        for key in ("confidence", "severity", "step_budget_p75", "token_budget_p75", "outcome_rate"):
            assert key in METRIC_DESCRIPTIONS, f"missing description for '{key}'"
            assert METRIC_DESCRIPTIONS[key]

    def test_descriptions_in_serialized_json(self) -> None:
        view = build_analysis_view(_sample_result())
        parsed = json.loads(view.model_dump_json())
        assert "metric_descriptions" in parsed
        assert "confidence" in parsed["metric_descriptions"]
