"""Red-phase tests for the Week 1 pipeline orchestrator.

Target module (not yet implemented):
    src.kairos.engine.pipeline

Expected surface:
    @dataclass WorkflowSummary
    @dataclass UnmappedActivity
    @dataclass Week1Result
    def run_week1_pipeline(
        envelopes: list[TraceEnvelope],
        context: BusinessContext,
        llm_client: LLMClient | None = None,
        *,
        semantic_top_patterns: int = 3,
        semantic_per_pattern: int = 5,
    ) -> Week1Result
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kairos.analysis.evidence_coverage import EvidenceCoverage, compute_evidence_coverage
from kairos.analysis.outcome_metric import WorkflowOutcomeSummary
from kairos.analysis.reference_behavior import ReferenceCohort, ReferenceConfidence
from kairos.analysis.semantic_decision import (
    Confidence,
    DecisionAdvanced,
    FindingType,
    FixArea,
    SemanticDecisionFinding,
)
from kairos.analysis.workflow_divergence import DivergenceFinding
from kairos.analysis.workflow_membership import MembershipKind, WorkflowMembership
from kairos.detection.models import Finding
from kairos.engine import pipeline as wp
from kairos.engine.pipeline import (
    UnmappedActivity,
    Week1Result,
    WorkflowSummary,
    classify_membership,
    map_envelope_multilabel,
    run_week1_pipeline,
)
from kairos.models.enums import StepStatus, StepType, TerminalStatus
from kairos.models.trace import Step, TraceEnvelope
from kairos.taxonomy.business_context import BusinessContext, BusinessOperation

# ── Synthesis helpers ──────────────────────────────────────────────────


def _step(
    i: int,
    tool: str,
    *,
    status: StepStatus = StepStatus.OK,
    tool_args: dict[str, Any] | None = None,
    tool_output: str | None = "ok",
    error: str | None = None,
) -> Step:
    return Step(
        step_index=i,
        step_type=StepType.TOOL_CALL,
        tool_name=tool,
        tool_args=tool_args if tool_args is not None else {"i": i, "tool": tool},
        tool_args_normalized=tool_args if tool_args is not None else {"i": i, "tool": tool},
        tool_output=tool_output,
        status=status,
        error_message=error,
    )


def _trace(
    trace_id: str,
    tools: list[str],
    *,
    terminal: TerminalStatus = TerminalStatus.COMPLETED,
    user_input: str = "do the thing",
) -> TraceEnvelope:
    steps = [
        _step(i, tool, tool_args={"trace": trace_id, "i": i}, tool_output=f"{tool}-done")
        for i, tool in enumerate(tools)
    ]
    return TraceEnvelope(
        trace_id=trace_id,
        user_input=user_input,
        steps=steps,
        terminal_status=terminal,
    )


def _hr_op() -> BusinessOperation:
    return BusinessOperation(
        name="Candidate Screening",
        description="Evaluate one candidate end-to-end",
        expected_tools=["get_rubric", "parse_resume", "submit_evaluation"],
        priority="high",
        business_goal="Reduce recruiter review time.",
        reliability_metric="percent of completed screenings.",
        bad_run_means="Missing evidence.",
        required_side_effect_tools=["submit_evaluation"],
    )


def _other_op() -> BusinessOperation:
    return BusinessOperation(
        name="Refund Issuance",
        description="Issue refunds via Stripe",
        expected_tools=["lookup_order", "approve_refund", "issue_refund"],
        priority="medium",
        required_side_effect_tools=[],
    )


def _empty_tools_op() -> BusinessOperation:
    return BusinessOperation(
        name="Empty Tools Op",
        description="Operation with no expected_tools",
        expected_tools=[],
        priority="low",
    )


def _hr_context() -> BusinessContext:
    return BusinessContext(
        agent_name="HR Screening Agent",
        agent_description="Screens candidates",
        operations=[_hr_op()],
    )


def _multi_op_context() -> BusinessContext:
    return BusinessContext(
        agent_name="Multi-Op Agent",
        agent_description="Handles HR + refunds",
        operations=[_hr_op(), _other_op()],
    )


def _ok_finding(trace_id: str, step_index: int = 1) -> SemanticDecisionFinding:
    return SemanticDecisionFinding(
        trace_id=trace_id,
        workflow_name="Candidate Screening",
        step_index=step_index,
        decision_advanced_task=DecisionAdvanced.NO,
        finding_type=FindingType.CONTEXT_IGNORED,
        evidence_refs=["step_1.tool_output"],
        missing_evidence=[],
        likely_fix_area=FixArea.PROMPT,
        confidence=Confidence.MEDIUM,
        ticket_title="Agent ignored retrieved rubric",
        verification_target="Retry with prompt nudge.",
    )


# ── TESTS ─────────────────────────────────────────────────────────────


class TestWorkflowMapping:
    """Mapping rule: tool Jaccard ≥ 0.5, strictly higher than other ops, op must declare expected_tools."""

    def test_trace_maps_to_workflow_when_tool_jaccard_at_or_above_threshold(self) -> None:
        ctx = _hr_context()
        # Trace tools = exactly the expected tools → Jaccard 1.0
        trace = _trace("t-1", ["get_rubric", "parse_resume", "submit_evaluation"])
        result = run_week1_pipeline([trace], ctx, llm_client=None)
        assert isinstance(result, Week1Result)
        assert len(result.workflows) == 1
        ws = result.workflows[0]
        assert ws.operation_name == "Candidate Screening"
        assert ws.mapped_trace_count == 1
        assert result.unmapped.trace_count == 0

    def test_trace_below_threshold_is_unmapped(self) -> None:
        ctx = BusinessContext(
            agent_name="Test",
            agent_description="",
            operations=[
                BusinessOperation(
                    name="Op A",
                    description="",
                    expected_tools=["A", "B", "C", "D"],
                    priority="medium",
                )
            ],
        )
        # Envelope with one tool overlapping out of 4 → Jaccard = 1/4 = 0.25
        trace = _trace("t-low", ["A"])
        result = run_week1_pipeline([trace], ctx, llm_client=None)
        # The op had no traces map to it; trace lands in unmapped
        assert result.unmapped.trace_count == 1
        # Either zero workflows or workflow with mapped_trace_count == 0
        if result.workflows:
            assert all(ws.mapped_trace_count == 0 for ws in result.workflows)

    def test_trace_maps_to_best_matching_op(self) -> None:
        op1 = BusinessOperation(
            name="Op One",
            description="",
            expected_tools=["alpha", "beta", "gamma"],
            required_side_effect_tools=["alpha"],
            priority="medium",
        )
        op2 = BusinessOperation(
            name="Op Two",
            description="",
            expected_tools=["delta", "epsilon", "zeta"],
            required_side_effect_tools=["delta"],
            priority="medium",
        )
        ctx = BusinessContext(agent_name="x", agent_description="", operations=[op1, op2])
        # Trace tools fully overlap with op1, no overlap with op2.
        trace = _trace("t-best", ["alpha", "beta", "gamma"])
        result = run_week1_pipeline([trace], ctx, llm_client=None)

        op1_summary = next((ws for ws in result.workflows if ws.operation_name == "Op One"), None)
        op2_summary = next((ws for ws in result.workflows if ws.operation_name == "Op Two"), None)
        assert op1_summary is not None
        assert op1_summary.mapped_trace_count == 1
        if op2_summary is not None:
            assert op2_summary.mapped_trace_count == 0
        assert result.unmapped.trace_count == 0

    def test_tie_between_ops_maps_trace_to_all_tied_workflows(self) -> None:
        # Week 1.5 Slice B.0 — multi-label: equal recall → trace lands in BOTH buckets.
        # (Previous single-label conservative behavior: tie → unmapped — is gone.)
        op1 = BusinessOperation(
            name="Op One",
            description="",
            expected_tools=["alpha", "beta"],
            required_side_effect_tools=["alpha"],
            priority="medium",
        )
        op2 = BusinessOperation(
            name="Op Two",
            description="",
            expected_tools=["alpha", "beta"],
            required_side_effect_tools=["beta"],
            priority="medium",
        )
        ctx = BusinessContext(agent_name="x", agent_description="", operations=[op1, op2])
        trace = _trace("t-tie", ["alpha", "beta"])
        result = run_week1_pipeline([trace], ctx, llm_client=None)

        ws_by_name = {ws.operation_name: ws for ws in result.workflows}
        assert "Op One" in ws_by_name
        assert "Op Two" in ws_by_name
        # Both workflows must have the trace as a member via the backwards-compat property.
        assert ws_by_name["Op One"].mapped_trace_count == 1
        assert ws_by_name["Op Two"].mapped_trace_count == 1
        # And the trace does not land in the unmapped bucket.
        assert result.unmapped.trace_count == 0

    def test_op_with_empty_expected_tools_never_matches(self) -> None:
        ctx = BusinessContext(
            agent_name="x",
            agent_description="",
            operations=[_empty_tools_op()],
        )
        trace = _trace("t-empty", ["any_tool", "another_tool"])
        result = run_week1_pipeline([trace], ctx, llm_client=None)
        # No mapping possible → trace unmapped
        assert result.unmapped.trace_count == 1
        for ws in result.workflows:
            assert ws.mapped_trace_count == 0

    def test_envelope_with_no_tools_is_unmapped(self) -> None:
        ctx = _hr_context()
        # Trace with no tool steps → tool_sequence = [] → Jaccard undefined → unmapped
        trace = TraceEnvelope(
            trace_id="t-no-tools",
            user_input="hello",
            steps=[],
            terminal_status=TerminalStatus.COMPLETED,
        )
        result = run_week1_pipeline([trace], ctx, llm_client=None)
        assert result.unmapped.trace_count == 1
        for ws in result.workflows:
            assert ws.mapped_trace_count == 0


class TestRecallBasedMapping:
    """Week 1.5 Slice A: trace→workflow mapping uses recall, not Jaccard.

    Recall(op, trace) = |expected ∩ observed| / |expected|

    Rules:
        - exactly one op with recall ≥ MAPPING_RECALL_THRESHOLD → map to it
        - any op in [MAPPING_TIEBREAK_LOWER, MAPPING_RECALL_THRESHOLD) → LLM
          tiebreak. In Slice A the tiebreak stub returns None → unmapped.
        - no op above tiebreak lower → unmapped
    """

    def test_trace_maps_when_recall_exceeds_threshold(self) -> None:
        # 8 expected tools; trace has all 8 + 2 extras → recall = 8/8 = 1.0.
        # Under Jaccard this would have been 8/10 = 0.8 (borderline).
        expected = ["A", "B", "C", "D", "E", "F", "G", "H"]
        op = BusinessOperation(
            name="Op Wide",
            description="",
            expected_tools=expected,
            required_side_effect_tools=["A"],
            priority="medium",
        )
        ctx = BusinessContext(agent_name="x", agent_description="", operations=[op])
        trace = _trace("t-recall-high", expected + ["I", "J"])
        result = run_week1_pipeline([trace], ctx, llm_client=None)

        assert len(result.workflows) == 1
        ws = result.workflows[0]
        assert ws.mapped_trace_count == 1
        assert result.unmapped.trace_count == 0

    def test_trace_maps_with_many_extra_tools(self) -> None:
        # This is the critical case Jaccard penalised: 5 expected tools, 6
        # extras. Jaccard 5/11 = 0.454 (below 0.5 → would not have mapped).
        # Recall 5/5 = 1.0 → maps under the new rule.
        expected = ["A", "B", "C", "D", "E"]
        extras = ["x1", "x2", "x3", "x4", "x5", "x6"]
        op = BusinessOperation(
            name="Op Many Extras",
            description="",
            expected_tools=expected,
            required_side_effect_tools=["A"],
            priority="medium",
        )
        ctx = BusinessContext(agent_name="x", agent_description="", operations=[op])
        trace = _trace("t-extras", expected + extras)
        result = run_week1_pipeline([trace], ctx, llm_client=None)

        assert len(result.workflows) == 1
        ws = result.workflows[0]
        assert ws.mapped_trace_count == 1

    def test_trace_below_recall_threshold_is_unmapped(self) -> None:
        # recall = 2/5 = 0.4 for both ops → below tiebreak lower → unmapped.
        op1 = BusinessOperation(
            name="Op One",
            description="",
            expected_tools=["A", "B", "C", "D", "E"],
            priority="medium",
        )
        op2 = BusinessOperation(
            name="Op Two",
            description="",
            expected_tools=["A", "B", "F", "G", "H"],
            priority="medium",
        )
        ctx = BusinessContext(agent_name="x", agent_description="", operations=[op1, op2])
        trace = _trace("t-low", ["A", "B"])
        result = run_week1_pipeline([trace], ctx, llm_client=None)

        assert result.unmapped.trace_count == 1
        for ws in result.workflows:
            assert ws.mapped_trace_count == 0

    def test_op_without_distinctive_tool_is_utility_pattern_never_matches(self) -> None:
        # Op with no required_side_effect_tools is a utility pattern — no
        # signature, so nothing can belong to it. Trace with recall 3/5 = 0.6
        # (above the 0.5 default threshold) still returns NONE.
        op = BusinessOperation(
            name="Op No Signature",
            description="",
            expected_tools=["A", "B", "C", "D", "E"],
            priority="medium",
        )
        ctx = BusinessContext(agent_name="x", agent_description="", operations=[op])
        trace = _trace("t-no-signature", ["A", "B", "C"])
        result = run_week1_pipeline([trace], ctx, llm_client=None)

        assert result.unmapped.trace_count == 1
        # Op still appears in workflows list with zero members.
        assert len(result.workflows) == 1
        ws = result.workflows[0]
        assert ws.mapped_trace_count == 0

    def test_mapping_recall_threshold_constant_importable(self) -> None:
        from kairos.engine.pipeline import (
            MAPPING_RECALL_THRESHOLD,
            MAPPING_TIEBREAK_LOWER,
        )

        assert MAPPING_RECALL_THRESHOLD == 0.8
        assert MAPPING_TIEBREAK_LOWER == 0.5

    def test_op_with_empty_expected_tools_never_matches_under_recall(self) -> None:
        # Recall is undefined (division by zero) for empty expected tools →
        # treated as 0 → never matches.
        ctx = BusinessContext(
            agent_name="x",
            agent_description="",
            operations=[_empty_tools_op()],
        )
        trace = _trace("t-empty-expected", ["any_tool", "another_tool"])
        result = run_week1_pipeline([trace], ctx, llm_client=None)
        assert result.unmapped.trace_count == 1
        for ws in result.workflows:
            assert ws.mapped_trace_count == 0


class TestPipelineIntegration:
    """End-to-end shape of Week1Result and component wiring."""

    def test_calls_compute_evidence_coverage_once_globally(self) -> None:
        ctx = _hr_context()
        traces = [
            _trace("t-a", ["get_rubric", "parse_resume", "submit_evaluation"]),
            _trace("t-b", ["unknown_tool"]),  # unmapped
            _trace("t-c", ["get_rubric", "parse_resume", "submit_evaluation"]),
        ]
        with patch(
            "kairos.engine.pipeline.compute_evidence_coverage",
            wraps=compute_evidence_coverage,
        ) as mock_cov:
            result = run_week1_pipeline(traces, ctx, llm_client=None)
        # Called exactly once over all envelopes
        assert mock_cov.call_count == 1
        called_with = mock_cov.call_args.args[0]
        assert len(called_with) == len(traces)
        assert isinstance(result.evidence_coverage, EvidenceCoverage)
        assert result.evidence_coverage.total_traces == len(traces)

    def test_workflow_summary_contains_outcome_reference_findings_divergences(self) -> None:
        ctx = _hr_context()
        traces = [_trace(f"t-{i}", ["get_rubric", "parse_resume", "submit_evaluation"]) for i in range(5)]
        result = run_week1_pipeline(traces, ctx, llm_client=None)

        assert len(result.workflows) == 1
        ws = result.workflows[0]
        assert isinstance(ws, WorkflowSummary)
        assert isinstance(ws.outcome, WorkflowOutcomeSummary)
        assert isinstance(ws.reference, ReferenceCohort)
        assert isinstance(ws.deterministic_findings, list)
        for f in ws.deterministic_findings:
            assert isinstance(f, Finding)
        assert isinstance(ws.divergences, list)
        for d in ws.divergences:
            assert isinstance(d, DivergenceFinding)
        assert isinstance(ws.semantic_findings, list)
        assert isinstance(ws.top_pattern_names, list)

    def test_detector_wiring_uses_workflow_median_steps(self) -> None:
        ctx = _hr_context()
        # All traces include submit_evaluation (the HR op's distinctive tool)
        # so they all map to the Candidate Screening workflow. Step counts:
        # 3, 4, 6 → median = 4.
        t_short = _trace("t-short", ["get_rubric", "parse_resume", "submit_evaluation"])
        t_mid = _trace("t-mid", ["get_rubric", "parse_resume", "submit_evaluation", "get_rubric"])
        t_long = _trace(
            "t-long",
            [
                "get_rubric",
                "parse_resume",
                "submit_evaluation",
                "get_rubric",
                "parse_resume",
                "submit_evaluation",
            ],
        )  # 6

        traces = [t_short, t_mid, t_long]
        with patch("kairos.engine.pipeline.detect_tier1", return_value=[]) as mock_detect:
            run_week1_pipeline(traces, ctx, llm_client=None)
        # Should have been called once per workflow (1 here)
        assert mock_detect.call_count == 1
        kwargs = mock_detect.call_args.kwargs
        # Either keyword or positional second arg
        if "cluster_median_steps" in kwargs:
            assert kwargs["cluster_median_steps"] == 4
        else:
            args = mock_detect.call_args.args
            assert len(args) >= 2
            assert args[1] == 4

    def test_top_pattern_names_sorted_by_affected_count_desc(self) -> None:
        ctx = _hr_context()
        traces = [_trace(f"t-{i}", ["get_rubric", "parse_resume", "submit_evaluation"]) for i in range(7)]

        # Synthesize 5 findings of "redundant_execution" + 2 of "loop_detected"
        synthesized = [
            Finding(
                pattern_name="redundant_execution",
                tier=1,
                trace_id=f"t-{i}",
                confidence=0.9,
                severity="warning",
            )
            for i in range(5)
        ] + [
            Finding(
                pattern_name="loop_detected",
                tier=1,
                trace_id=f"t-{i}",
                confidence=0.9,
                severity="warning",
            )
            for i in range(5, 7)
        ]
        with patch("kairos.engine.pipeline.detect_tier1", return_value=synthesized):
            result = run_week1_pipeline(traces, ctx, llm_client=None)

        assert len(result.workflows) == 1
        ws = result.workflows[0]
        assert len(ws.top_pattern_names) >= 2
        assert ws.top_pattern_names[0] == "redundant_execution"
        assert ws.top_pattern_names[1] == "loop_detected"

    def test_unmapped_activity_has_trace_count_and_sample_ids(self) -> None:
        ctx = _hr_context()
        # 8 traces with completely unrelated tools → all unmapped
        traces = [_trace(f"unmapped-{i}", [f"weird_tool_{i}"]) for i in range(8)]
        result = run_week1_pipeline(traces, ctx, llm_client=None)

        assert isinstance(result.unmapped, UnmappedActivity)
        assert result.unmapped.trace_count == 8
        assert len(result.unmapped.sample_trace_ids) <= 5
        # Stable alphabetical ordering of sample_trace_ids
        assert result.unmapped.sample_trace_ids == sorted(result.unmapped.sample_trace_ids)
        # All sample IDs come from the unmapped pool
        for tid in result.unmapped.sample_trace_ids:
            assert tid.startswith("unmapped-")
        # top_tools is derived from unmapped traces
        assert isinstance(result.unmapped.top_tools, list)
        assert len(result.unmapped.top_tools) <= 10


class TestNoLLMPath:
    """When llm_client is None, the semantic pass is skipped entirely."""

    def test_llm_client_none_skips_semantic_pass(self) -> None:
        ctx = _hr_context()
        traces = [_trace(f"t-{i}", ["get_rubric", "parse_resume", "submit_evaluation"]) for i in range(3)]
        result = run_week1_pipeline(traces, ctx, llm_client=None)
        assert result.llm_used is False
        for ws in result.workflows:
            assert ws.semantic_findings == []

    def test_llm_client_none_still_produces_full_result(self) -> None:
        ctx = _hr_context()
        traces = [_trace(f"t-{i}", ["get_rubric", "parse_resume", "submit_evaluation"]) for i in range(5)]
        result = run_week1_pipeline(traces, ctx, llm_client=None)

        assert len(result.workflows) == 1
        ws = result.workflows[0]
        assert isinstance(ws.outcome, WorkflowOutcomeSummary)
        assert isinstance(ws.reference, ReferenceCohort)
        # deterministic_findings/divergences may be empty but must be lists
        assert isinstance(ws.deterministic_findings, list)
        assert isinstance(ws.divergences, list)

    def test_llm_used_true_when_client_provided_and_any_findings(self) -> None:
        ctx = _hr_context()
        # Build enough traces and at least one looping trace so deterministic
        # findings exist → semantic pass has packets to send.
        clean_traces = [_trace(f"clean-{i}", ["get_rubric", "parse_resume", "submit_evaluation"]) for i in range(5)]
        # A looping trace should produce at least one finding. Include
        # submit_evaluation so the trace passes the distinctive-tool gate
        # and maps to the HR workflow.
        loop_steps: list[Step] = []
        for cycle in range(4):
            loop_steps.append(
                _step(
                    cycle * 2,
                    "get_rubric",
                    tool_args={"cycle": cycle},
                    tool_output="stuck",
                )
            )
            loop_steps.append(
                _step(
                    cycle * 2 + 1,
                    "parse_resume",
                    tool_args={"cycle": cycle},
                    tool_output="stuck",
                )
            )
        loop_steps.append(
            _step(
                len(loop_steps),
                "submit_evaluation",
                tool_args={"final": True},
                tool_output="submitted",
            )
        )
        looper = TraceEnvelope(
            trace_id="looper",
            user_input="evaluate candidate",
            steps=loop_steps,
            terminal_status=TerminalStatus.COMPLETED,
        )
        traces = [*clean_traces, looper]

        client = MagicMock()
        client.generate.return_value = _ok_finding("looper")

        # Patch analyze_flagged_traces to deterministically return one finding,
        # avoids dependency on the actual LLM client wiring.
        with patch(
            "kairos.engine.pipeline.analyze_flagged_traces",
            return_value=[_ok_finding("looper")],
        ):
            result = run_week1_pipeline(traces, ctx, llm_client=client)

        # At least one workflow should have semantic findings.
        any_semantic = any(len(ws.semantic_findings) > 0 for ws in result.workflows)
        assert any_semantic
        assert result.llm_used is True


class TestDeterminism:
    """Two pipeline runs with identical inputs (no LLM) must produce equal outputs."""

    def test_two_runs_with_same_inputs_and_no_llm_produce_equal_results(self) -> None:
        ctx = _hr_context()
        traces = [_trace(f"t-{i}", ["get_rubric", "parse_resume", "submit_evaluation"]) for i in range(6)]
        # add an unmapped trace
        traces.append(_trace("u-1", ["xyz_tool"]))

        result_a = run_week1_pipeline(traces, ctx, llm_client=None)
        result_b = run_week1_pipeline(traces, ctx, llm_client=None)

        assert result_a.llm_used == result_b.llm_used
        assert result_a.unmapped.trace_count == result_b.unmapped.trace_count
        assert result_a.unmapped.sample_trace_ids == result_b.unmapped.sample_trace_ids
        assert result_a.unmapped.top_tools == result_b.unmapped.top_tools
        assert len(result_a.workflows) == len(result_b.workflows)
        for ws_a, ws_b in zip(result_a.workflows, result_b.workflows, strict=True):
            assert ws_a.operation_name == ws_b.operation_name
            assert ws_a.mapped_trace_count == ws_b.mapped_trace_count
            assert ws_a.outcome == ws_b.outcome
            assert ws_a.top_pattern_names == ws_b.top_pattern_names
            assert ws_a.reference.confidence == ws_b.reference.confidence
            assert ws_a.reference.reference_path == ws_b.reference.reference_path


class TestEmptyInput:
    """Edge cases: empty inputs at the envelope and operation levels."""

    def test_no_envelopes_returns_empty_result(self) -> None:
        ctx = _hr_context()
        result = run_week1_pipeline([], ctx, llm_client=None)
        assert isinstance(result, Week1Result)
        # Workflow may exist with mapped_trace_count==0, OR be omitted; either
        # way the unmapped count is zero.
        assert result.unmapped.trace_count == 0
        assert isinstance(result.evidence_coverage, EvidenceCoverage)
        assert result.evidence_coverage.total_traces == 0
        assert result.llm_used is False
        for ws in result.workflows:
            assert ws.mapped_trace_count == 0

    def test_no_operations_all_traces_unmapped(self) -> None:
        # Building a BusinessContext with zero ops requires bypassing from_dict
        # validation; the dataclass itself accepts an empty list.
        ctx = BusinessContext(agent_name="empty", agent_description="", operations=[])
        traces = [_trace(f"t-{i}", ["any_tool"]) for i in range(3)]
        result = run_week1_pipeline(traces, ctx, llm_client=None)

        assert result.workflows == []
        assert result.unmapped.trace_count == 3


# Sanity import check: ensure the module-level constants exist where needed.
def test_module_constants_exist() -> None:
    # Week 1.5 Slice A: mapping is recall-based, not Jaccard. The old
    # WORKFLOW_MAPPING_JACCARD_THRESHOLD is gone and replaced with two
    # recall-based thresholds.
    assert wp.MAPPING_RECALL_THRESHOLD == 0.8
    assert wp.MAPPING_TIEBREAK_LOWER == 0.5
    assert wp.DEFAULT_SEMANTIC_TOP_PATTERNS == 3
    assert wp.DEFAULT_SEMANTIC_PER_PATTERN == 5


# Reference confidence integration sanity (ensures pipeline propagates config)
def test_reference_cohort_propagated() -> None:
    ctx = _hr_context()
    # 25 happy traces → MEDIUM confidence reference cohort
    traces = [_trace(f"t-{i}", ["get_rubric", "parse_resume", "submit_evaluation"]) for i in range(25)]
    result = run_week1_pipeline(traces, ctx, llm_client=None)
    assert len(result.workflows) == 1
    ws = result.workflows[0]
    assert ws.reference.confidence in (
        ReferenceConfidence.MEDIUM,
        ReferenceConfidence.HIGH,
    )


# ── Week 1.5 Slice B.0: multi-label mapping ────────────────────────────


def _op_with(
    name: str,
    expected_tools: list[str],
    *,
    required_side_effect_tools: list[str] | None = None,
    membership_recall_threshold: float | None = None,
) -> BusinessOperation:
    """Construct a BusinessOperation with the optional B.0 membership threshold."""
    kwargs: dict[str, Any] = {
        "name": name,
        "description": "op",
        "expected_tools": expected_tools,
        "priority": "medium",
        "required_side_effect_tools": required_side_effect_tools or [],
    }
    if membership_recall_threshold is not None:
        kwargs["membership_recall_threshold"] = membership_recall_threshold
    return BusinessOperation(**kwargs)


def _envelope_with_tools(
    trace_id: str,
    steps_spec: list[tuple[str, StepStatus]],
    *,
    terminal: TerminalStatus = TerminalStatus.COMPLETED,
) -> TraceEnvelope:
    """Build an envelope from a list of (tool_name, status) pairs."""
    steps: list[Step] = []
    for i, (tool, status) in enumerate(steps_spec):
        steps.append(
            Step(
                step_index=i,
                step_type=StepType.TOOL_CALL,
                tool_name=tool,
                tool_args={"i": i},
                tool_args_normalized={"i": i},
                tool_output=f"{tool}-done",
                status=status,
                error_message="boom" if status == StepStatus.ERROR else None,
            )
        )
    return TraceEnvelope(
        trace_id=trace_id,
        user_input="do the thing",
        steps=steps,
        terminal_status=terminal,
    )


class TestMultiLabelMapping:
    """Slice B.0 classify_membership + map_envelope_multilabel semantics."""

    def test_full_membership_when_all_side_effects_succeed(self) -> None:
        op = _op_with(
            "Screening",
            expected_tools=["A", "B", "C"],
            required_side_effect_tools=["C"],
        )
        trace = _envelope_with_tools(
            "t-full",
            [("A", StepStatus.OK), ("B", StepStatus.OK), ("C", StepStatus.OK)],
        )
        membership = classify_membership(trace, op)
        assert isinstance(membership, WorkflowMembership)
        assert membership.operation_name == "Screening"
        assert membership.kind == MembershipKind.FULL
        assert membership.recall == 1.0

    def test_attempted_when_side_effect_called_but_errors(self) -> None:
        op = _op_with(
            "Screening",
            expected_tools=["A", "B", "C"],
            required_side_effect_tools=["C"],
        )
        trace = _envelope_with_tools(
            "t-attempted",
            [("A", StepStatus.OK), ("B", StepStatus.OK), ("C", StepStatus.ERROR)],
        )
        membership = classify_membership(trace, op)
        assert membership.kind == MembershipKind.ATTEMPTED
        assert membership.recall == 1.0

    def test_attempted_when_distinctive_tool_called_but_fails(self) -> None:
        # expected=[A,B,C], required=[C], trace=[A,B,C-ERROR] → recall=1.0,
        # distinctive tool C present in trace so gate passes, but C failed →
        # ATTEMPTED.
        op = _op_with(
            "Screening",
            expected_tools=["A", "B", "C"],
            required_side_effect_tools=["C"],
            membership_recall_threshold=0.5,
        )
        trace = _envelope_with_tools(
            "t-attempted-failed",
            [("A", StepStatus.OK), ("B", StepStatus.OK), ("C", StepStatus.ERROR)],
        )
        membership = classify_membership(trace, op)
        assert membership.kind == MembershipKind.ATTEMPTED
        assert membership.recall == 1.0

    def test_none_when_recall_below_threshold(self) -> None:
        # Distinctive tool C IS present (gate passes), but recall 1/3 < 0.5 → NONE.
        op = _op_with(
            "Screening",
            expected_tools=["A", "B", "C"],
            required_side_effect_tools=["C"],
            membership_recall_threshold=0.5,
        )
        trace = _envelope_with_tools(
            "t-none-low-recall",
            [("C", StepStatus.OK)],
        )
        membership = classify_membership(trace, op)
        assert membership.kind == MembershipKind.NONE
        assert membership.recall == pytest.approx(1 / 3)

    def test_none_when_no_distinctive_tools_declared(self) -> None:
        # Empty required_side_effect_tools → op has no signature → always NONE
        # (utility pattern, not a workflow).
        op = _op_with(
            "Chat",
            expected_tools=["A", "B"],
            required_side_effect_tools=[],
            membership_recall_threshold=0.5,
        )
        trace = _envelope_with_tools(
            "t-chat-no-signature",
            [("A", StepStatus.OK), ("B", StepStatus.OK)],
        )
        membership = classify_membership(trace, op)
        assert membership.kind == MembershipKind.NONE

    def test_none_when_distinctive_tool_absent_from_trace(self) -> None:
        # Distinctive-tool gate: C declared as signature but not called → NONE
        # even when recall over non-distinctive tools would pass.
        op = _op_with(
            "Screening",
            expected_tools=["A", "B", "C"],
            required_side_effect_tools=["C"],
            membership_recall_threshold=0.5,
        )
        trace = _envelope_with_tools(
            "t-no-signature-call",
            [("A", StepStatus.OK), ("B", StepStatus.OK)],
        )
        membership = classify_membership(trace, op)
        assert membership.kind == MembershipKind.NONE

    def test_single_tool_workflow_defaults_to_threshold_1(self) -> None:
        # expected_tools=[X], no explicit threshold → default 1.0.
        op = _op_with("Outreach", expected_tools=["X"], required_side_effect_tools=["X"])

        # Trace with X + Y → recall = 1/1 = 1.0 → meets default threshold → FULL.
        trace_hit = _envelope_with_tools(
            "t-hit",
            [("X", StepStatus.OK), ("Y", StepStatus.OK)],
        )
        hit = classify_membership(trace_hit, op)
        assert hit.kind == MembershipKind.FULL

        # Trace with only Y → recall = 0 < 1.0 → NONE.
        trace_miss = _envelope_with_tools(
            "t-miss",
            [("Y", StepStatus.OK)],
        )
        miss = classify_membership(trace_miss, op)
        assert miss.kind == MembershipKind.NONE

    def test_multi_tool_workflow_defaults_to_threshold_0_5(self) -> None:
        # expected_tools=[A,B,C], no explicit threshold → default 0.5.
        op = _op_with(
            "Screening",
            expected_tools=["A", "B", "C"],
            required_side_effect_tools=["C"],
        )
        # Trace [A,C-ERROR] → distinctive tool C present so gate passes,
        # recall=2/3 ≥ 0.5, but C failed → ATTEMPTED (not FULL).
        trace = _envelope_with_tools(
            "t-two-of-three",
            [("A", StepStatus.OK), ("C", StepStatus.ERROR)],
        )
        membership = classify_membership(trace, op)
        assert membership.kind == MembershipKind.ATTEMPTED

    def test_explicit_yaml_threshold_overrides_default(self) -> None:
        # 5-tool op with explicit threshold 0.8 → recall 3/5 = 0.6 < 0.8 → NONE.
        op = _op_with(
            "Strict",
            expected_tools=["A", "B", "C", "D", "E"],
            required_side_effect_tools=["E"],
            membership_recall_threshold=0.8,
        )
        trace = _envelope_with_tools(
            "t-strict",
            [("A", StepStatus.OK), ("B", StepStatus.OK), ("C", StepStatus.OK)],
        )
        membership = classify_membership(trace, op)
        assert membership.kind == MembershipKind.NONE

    def test_empty_expected_tools_returns_none(self) -> None:
        op = _op_with("Empty", expected_tools=[])
        trace = _envelope_with_tools(
            "t-anything",
            [("A", StepStatus.OK), ("B", StepStatus.OK)],
        )
        membership = classify_membership(trace, op)
        assert membership.kind == MembershipKind.NONE

    def test_empty_tool_sequence_returns_none(self) -> None:
        op = _op_with(
            "Screening",
            expected_tools=["A", "B", "C"],
            required_side_effect_tools=["C"],
        )
        trace = TraceEnvelope(
            trace_id="t-no-tools",
            user_input="hello",
            steps=[],
            terminal_status=TerminalStatus.COMPLETED,
        )
        membership = classify_membership(trace, op)
        assert membership.kind == MembershipKind.NONE

    def test_map_envelope_multilabel_returns_multiple_for_multi_workflow_trace(self) -> None:
        # Two ops with distinct single side-effect tools; trace hits both.
        op1 = _op_with(
            "Screening",
            expected_tools=["submit_evaluation"],
            required_side_effect_tools=["submit_evaluation"],
        )
        op2 = _op_with(
            "Outreach",
            expected_tools=["send_email"],
            required_side_effect_tools=["send_email"],
        )
        trace = _envelope_with_tools(
            "t-multi",
            [
                ("submit_evaluation", StepStatus.OK),
                ("send_email", StepStatus.OK),
            ],
        )
        memberships = map_envelope_multilabel(trace, [op1, op2])
        assert len(memberships) == 2
        names = {m.operation_name for m in memberships}
        assert names == {"Screening", "Outreach"}
        assert all(m.kind == MembershipKind.FULL for m in memberships)

    def test_map_envelope_multilabel_excludes_none_memberships(self) -> None:
        # 3 ops — trace only matches one.
        matching = _op_with(
            "Matching",
            expected_tools=["only_one"],
            required_side_effect_tools=["only_one"],
        )
        other1 = _op_with(
            "Other1",
            expected_tools=["nope_a"],
            required_side_effect_tools=["nope_a"],
        )
        other2 = _op_with(
            "Other2",
            expected_tools=["nope_b"],
            required_side_effect_tools=["nope_b"],
        )
        trace = _envelope_with_tools(
            "t-one-match",
            [("only_one", StepStatus.OK)],
        )
        memberships = map_envelope_multilabel(trace, [matching, other1, other2])
        assert len(memberships) == 1
        assert memberships[0].operation_name == "Matching"

    def test_map_envelope_multilabel_empty_when_no_matches(self) -> None:
        op1 = _op_with(
            "Op1",
            expected_tools=["a", "b"],
            required_side_effect_tools=["a"],
            membership_recall_threshold=0.5,
        )
        op2 = _op_with(
            "Op2",
            expected_tools=["c", "d"],
            required_side_effect_tools=["c"],
            membership_recall_threshold=0.5,
        )
        trace = _envelope_with_tools(
            "t-no-matches",
            [("z", StepStatus.OK)],
        )
        memberships = map_envelope_multilabel(trace, [op1, op2])
        assert memberships == []


class TestWorkflowSummaryShape:
    """Slice B.0 WorkflowSummary contract: full/attempted counts + mapped property."""

    def test_workflow_summary_has_full_and_attempted_counts(self) -> None:
        # Construct a WorkflowSummary via the new kwargs directly.
        outcome = WorkflowOutcomeSummary(
            workflow_name="Screening",
            total_traces=5,
            computable_count=5,
            passed_count=3,
            outcome_rate=0.6,
            pending_reason=None,
        )
        ref = ReferenceCohort(
            eligible_traces=[],
            reference_traces=[],
            confidence=ReferenceConfidence.NONE,
            reference_dfg=None,
            reference_edges=set(),
            reference_path=[],
            step_budget_p75=None,
            token_budget_p75=None,
        )
        ws = WorkflowSummary(
            operation_name="Screening",
            full_trace_count=3,
            attempted_trace_count=2,
            outcome=outcome,
            reference=ref,
            deterministic_findings=[],
            divergences=[],
            semantic_findings=[],
        )
        assert ws.full_trace_count == 3
        assert ws.attempted_trace_count == 2

    def test_workflow_summary_mapped_trace_count_is_sum_property(self) -> None:
        outcome = WorkflowOutcomeSummary(
            workflow_name="Screening",
            total_traces=7,
            computable_count=7,
            passed_count=5,
            outcome_rate=5 / 7,
            pending_reason=None,
        )
        ref = ReferenceCohort(
            eligible_traces=[],
            reference_traces=[],
            confidence=ReferenceConfidence.NONE,
            reference_dfg=None,
            reference_edges=set(),
            reference_path=[],
            step_budget_p75=None,
            token_budget_p75=None,
        )
        ws = WorkflowSummary(
            operation_name="Screening",
            full_trace_count=5,
            attempted_trace_count=2,
            outcome=outcome,
            reference=ref,
            deterministic_findings=[],
            divergences=[],
            semantic_findings=[],
        )
        assert ws.mapped_trace_count == 7
        assert ws.mapped_trace_count == ws.full_trace_count + ws.attempted_trace_count
