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


# ───────────────────────── build_analysis_view ─────────────────────────


def _sample_result() -> AnalysisResult:
    outcome = WorkflowOutcomeSummary(
        workflow_name="Screening",
        total_traces=10,
        computable_count=8,
        passed_count=6,
        outcome_rate=0.75,
        pending_reason=None,
    )
    ref = ReferenceCohort(
        eligible_traces=[],
        reference_traces=[],
        confidence=ReferenceConfidence.MEDIUM,
        reference_dfg=None,
        reference_edges={("a", "b")},
        reference_path=["a", "b", "c"],
        step_budget_p75=12.0,
        token_budget_p75=3400.0,
    )
    divergence = DivergenceFinding(
        trace_id="div-1",
        first_divergence_step=3,
        expected_transition=("a", "b"),
        actual_transition=("a", "z"),
        extra_rate=0.25,
        coverage=0.8,
        variant_candidate=True,
    )
    finding = Finding(
        pattern_name="redundant_execution",
        tier=1,
        trace_id="det-1",
        confidence=0.9,
        severity="critical",
        evidence={"runs": 3},
        affected_step_indices=[4, 5, 6],
        estimated_token_waste=1200,
    )
    workflow = WorkflowSummary(
        operation_name="Screening",
        full_trace_count=6,
        attempted_trace_count=2,
        outcome=outcome,
        reference=ref,
        deterministic_findings=[finding],
        divergences=[divergence],
        top_pattern_names=["redundant_execution"],
    )
    return AnalysisResult(
        workflows=[workflow],
        unmapped=UnmappedActivity(
            trace_count=2,
            sample_trace_ids=["unm-1", "unm-2"],
            top_tools=["search", "fetch"],
        ),
        reliability={"terminal_status_rate": 0.92, "tool_sequence_rate": 0.88},
    )


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
