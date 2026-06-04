"""Red-phase tests for the Agent Correctness Score math (Slice A).

Target module (not yet implemented):
    src.kairos.analysis.correctness_score

Expected surface:
    class DimensionVerdict(StrEnum)
    @dataclass DimensionScore
    @dataclass AgentCorrectnessScore
    def compute_correctness_score(
        workflow: WorkflowSummary,
        mapped_traces: list[TraceEnvelope],
        *,
        operation: BusinessOperation,    # needed to read correctness_criteria
        llm_client: LLMClient | None = None,
        budget: int = 200,
        seed_key: str = "",
    ) -> AgentCorrectnessScore
    def wilson_ci(successes: int, total: int, z: float = 1.96) -> tuple[float, float]

Slice A rules:
    - LLM judges are stubs (no network, no LLM calls)
    - 3 of 5 dimensions are stubbed: output_correctness, decision_quality, context_handling
    - 2 of 5 are computed deterministically: task_execution, path_integrity
    - intersection_rate is None when any dimension is stubbed
    - composite_rate averages only the computed dimensions
    - provisional=True whenever any dimension is stubbed
"""

from __future__ import annotations

import math
from typing import Any

from kairos.analysis.correctness_score import (
    AgentCorrectnessScore,
    DimensionScore,
    DimensionVerdict,
    compute_correctness_score,
    wilson_ci,
)
from kairos.analysis.outcome_metric import WorkflowOutcomeSummary
from kairos.analysis.reference_behavior import ReferenceCohort, ReferenceConfidence
from kairos.analysis.workflow_divergence import DivergenceFinding
from kairos.detection.models import Finding
from kairos.engine.pipeline import WorkflowSummary
from kairos.models.enums import StepStatus, StepType, TerminalStatus
from kairos.models.trace import Step, TraceEnvelope
from kairos.taxonomy.business_context import BusinessOperation

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


def _op(
    *,
    name: str = "Candidate Screening",
    expected_tools: list[str] | None = None,
    correctness_criteria: list[str] | None = None,
    business_goal: str | None = "Reduce recruiter review time.",
    required_side_effect_tools: list[str] | None = None,
) -> BusinessOperation:
    return BusinessOperation(
        name=name,
        description="An operation",
        expected_tools=expected_tools
        if expected_tools is not None
        else ["get_rubric", "parse_resume", "submit_evaluation"],
        priority="high",
        business_goal=business_goal,
        reliability_metric="percent of completed screenings",
        bad_run_means="missing evidence",
        required_side_effect_tools=required_side_effect_tools
        if required_side_effect_tools is not None
        else ["submit_evaluation"],
        correctness_criteria=correctness_criteria if correctness_criteria is not None else [],
    )


def _empty_reference() -> ReferenceCohort:
    return ReferenceCohort(
        eligible_traces=[],
        reference_traces=[],
        confidence=ReferenceConfidence.NONE,
        reference_dfg=None,
        reference_edges=set(),
        reference_path=[],
        step_budget_p75=None,
        token_budget_p75=None,
    )


def _workflow_summary(
    *,
    operation_name: str = "Candidate Screening",
    mapped_trace_count: int = 0,
    total: int = 0,
    computable: int = 0,
    passed: int = 0,
    deterministic_findings: list[Finding] | None = None,
    divergences: list[DivergenceFinding] | None = None,
) -> WorkflowSummary:
    # Slice B.0: WorkflowSummary uses full_trace_count + attempted_trace_count,
    # and exposes mapped_trace_count as a computed property. These tests don't
    # care about the full/attempted split — they only care about the total —
    # so assign the whole count to full_trace_count.
    outcome_rate: float | None = passed / computable if computable > 0 else None
    outcome = WorkflowOutcomeSummary(
        workflow_name=operation_name,
        total_traces=total,
        computable_count=computable,
        passed_count=passed,
        outcome_rate=outcome_rate,
        pending_reason=None if computable > 0 else "no computable traces",
    )
    return WorkflowSummary(
        operation_name=operation_name,
        full_trace_count=mapped_trace_count,
        attempted_trace_count=0,
        outcome=outcome,
        reference=_empty_reference(),
        deterministic_findings=deterministic_findings or [],
        divergences=divergences or [],
        semantic_findings=[],
        top_pattern_names=[],
    )


def _finding(trace_id: str, pattern: str = "loop_detected", step: int = 1) -> Finding:
    return Finding(
        pattern_name=pattern,
        tier=1,
        trace_id=trace_id,
        confidence=0.9,
        severity="warning",
        affected_step_indices=[step],
    )


def _divergence(trace_id: str, first_step: int | None = 2) -> DivergenceFinding:
    return DivergenceFinding(
        trace_id=trace_id,
        first_divergence_step=first_step,
        expected_transition=("get_rubric", "parse_resume"),
        actual_transition=("get_rubric", "submit_evaluation"),
        extra_rate=0.4,
        coverage=0.5,
        variant_candidate=False,
    )


# ── TESTS ─────────────────────────────────────────────────────────────


class TestWilsonCI:
    """Wilson score interval for binomial proportions."""

    def test_wilson_ci_standard_case(self) -> None:
        low, high = wilson_ci(50, 100)
        # Center is around 0.5 and margin is around 0.098 for n=100
        assert 0.39 < low < 0.41
        assert 0.59 < high < 0.61

    def test_wilson_ci_total_zero_returns_full_interval(self) -> None:
        low, high = wilson_ci(0, 0)
        assert low == 0.0
        assert high == 1.0

    def test_wilson_ci_perfect_score_clamped_to_one(self) -> None:
        low, high = wilson_ci(10, 10)
        assert 0.0 <= low <= 1.0
        assert high <= 1.0
        assert high > low

    def test_wilson_ci_zero_success_clamped_to_zero(self) -> None:
        low, high = wilson_ci(0, 10)
        assert low >= 0.0
        assert 0.0 <= high <= 1.0
        assert high > low

    def test_wilson_ci_wider_for_small_sample(self) -> None:
        small_low, small_high = wilson_ci(4, 5)
        large_low, large_high = wilson_ci(40, 50)
        small_width = small_high - small_low
        large_width = large_high - large_low
        assert small_width > large_width


class TestDimensionScore:
    """Per-dimension scores in the Slice A stub-heavy regime."""

    def test_task_execution_deterministic_rate_matches_outcome(self) -> None:
        ws = _workflow_summary(
            mapped_trace_count=67,
            total=67,
            computable=67,
            passed=17,
        )
        mapped = [_trace(f"t-{i}", ["get_rubric", "parse_resume", "submit_evaluation"]) for i in range(67)]
        score = compute_correctness_score(ws, mapped, operation=_op(), llm_client=None)

        task = score.dimensions["task_execution"]
        assert isinstance(task, DimensionScore)
        assert task.mode == "deterministic"
        assert task.deterministic_rate is not None
        assert math.isclose(task.deterministic_rate, 17 / 67, rel_tol=1e-9)
        assert task.estimated_rate == task.deterministic_rate
        assert task.sample_size == 0

    def test_path_integrity_pass_when_no_findings_and_no_divergence(self) -> None:
        ws = _workflow_summary(mapped_trace_count=3)
        mapped = [_trace(f"t-{i}", ["get_rubric", "parse_resume", "submit_evaluation"]) for i in range(3)]
        score = compute_correctness_score(ws, mapped, operation=_op(), llm_client=None)

        path = score.dimensions["path_integrity"]
        assert path.mode == "deterministic"
        assert path.deterministic_rate == 1.0

    def test_path_integrity_fail_when_any_finding_affects_trace(self) -> None:
        # 3 traces, finding affects t-0 → 2/3 pass
        findings = [_finding("t-0")]
        ws = _workflow_summary(mapped_trace_count=3, deterministic_findings=findings)
        mapped = [_trace(f"t-{i}", ["get_rubric", "parse_resume", "submit_evaluation"]) for i in range(3)]
        score = compute_correctness_score(ws, mapped, operation=_op(), llm_client=None)

        path = score.dimensions["path_integrity"]
        assert path.mode == "deterministic"
        assert path.deterministic_rate is not None
        assert math.isclose(path.deterministic_rate, 2 / 3, rel_tol=1e-9)

    def test_path_integrity_fail_when_divergence_has_real_step(self) -> None:
        # 3 traces, divergence on t-0 with a real step → t-0 fails
        divs = [_divergence("t-0", first_step=2)]
        ws = _workflow_summary(mapped_trace_count=3, divergences=divs)
        mapped = [_trace(f"t-{i}", ["get_rubric", "parse_resume", "submit_evaluation"]) for i in range(3)]
        score = compute_correctness_score(ws, mapped, operation=_op(), llm_client=None)

        path = score.dimensions["path_integrity"]
        assert path.mode == "deterministic"
        assert path.deterministic_rate is not None
        assert math.isclose(path.deterministic_rate, 2 / 3, rel_tol=1e-9)

    def test_path_integrity_divergence_variant_does_not_fail(self) -> None:
        # Variant divergence (first_divergence_step=None) must NOT fail the trace.
        divs = [_divergence("t-0", first_step=None)]
        ws = _workflow_summary(mapped_trace_count=3, divergences=divs)
        mapped = [_trace(f"t-{i}", ["get_rubric", "parse_resume", "submit_evaluation"]) for i in range(3)]
        score = compute_correctness_score(ws, mapped, operation=_op(), llm_client=None)

        path = score.dimensions["path_integrity"]
        assert path.deterministic_rate == 1.0

    def test_output_correctness_stubbed_when_criteria_present(self) -> None:
        ws = _workflow_summary(mapped_trace_count=3)
        mapped = [_trace(f"t-{i}", ["get_rubric"]) for i in range(3)]
        op = _op(correctness_criteria=["must return pdf", "must include summary"])
        score = compute_correctness_score(ws, mapped, operation=op, llm_client=None)

        out = score.dimensions["output_correctness"]
        assert out.mode == "stubbed"
        assert out.note is not None
        assert "Slice B" in out.note or "slice b" in out.note.lower()

    def test_output_correctness_stubbed_when_criteria_empty(self) -> None:
        ws = _workflow_summary(mapped_trace_count=3)
        mapped = [_trace(f"t-{i}", ["get_rubric"]) for i in range(3)]
        op = _op(correctness_criteria=[])
        score = compute_correctness_score(ws, mapped, operation=op, llm_client=None)

        out = score.dimensions["output_correctness"]
        assert out.mode == "stubbed"
        assert out.note is not None
        assert "correctness_criteria not defined" in out.note

    def test_decision_quality_stubbed(self) -> None:
        ws = _workflow_summary(mapped_trace_count=3)
        mapped = [_trace(f"t-{i}", ["get_rubric"]) for i in range(3)]
        score = compute_correctness_score(ws, mapped, operation=_op(), llm_client=None)

        dq = score.dimensions["decision_quality"]
        assert dq.mode == "stubbed"

    def test_context_handling_stubbed(self) -> None:
        ws = _workflow_summary(mapped_trace_count=3)
        mapped = [_trace(f"t-{i}", ["get_rubric"]) for i in range(3)]
        score = compute_correctness_score(ws, mapped, operation=_op(), llm_client=None)

        ch = score.dimensions["context_handling"]
        assert ch.mode == "stubbed"


class TestHeadlineAggregation:
    """intersection_rate and composite_rate roll-ups."""

    def test_intersection_rate_none_when_any_dimension_stubbed(self) -> None:
        # Slice A always has 3 stubs → intersection is never computable.
        ws = _workflow_summary(
            mapped_trace_count=4,
            total=4,
            computable=4,
            passed=3,
        )
        mapped = [_trace(f"t-{i}", ["get_rubric", "parse_resume", "submit_evaluation"]) for i in range(4)]
        score = compute_correctness_score(ws, mapped, operation=_op(), llm_client=None)

        assert score.intersection_rate is None

    def test_composite_rate_averages_computed_dimensions(self) -> None:
        # Set up task=0.8 (4/5) and path_integrity=0.6 (3/5 pass, 2 flagged).
        findings = [_finding("t-0"), _finding("t-1")]
        ws = _workflow_summary(
            mapped_trace_count=5,
            total=5,
            computable=5,
            passed=4,
            deterministic_findings=findings,
        )
        mapped = [_trace(f"t-{i}", ["get_rubric", "parse_resume", "submit_evaluation"]) for i in range(5)]
        score = compute_correctness_score(ws, mapped, operation=_op(), llm_client=None)

        # task_execution = 4/5 = 0.8; path_integrity = 3/5 = 0.6 → composite = 0.7
        assert score.composite_rate is not None
        assert math.isclose(score.composite_rate, 0.7, rel_tol=1e-9)

    def test_composite_rate_none_when_no_dimensions_computed(self) -> None:
        # Empty mapped traces → task_execution & path_integrity both become
        # un-computable → composite is None.
        ws = _workflow_summary(mapped_trace_count=0, total=0, computable=0, passed=0)
        score = compute_correctness_score(ws, [], operation=_op(), llm_client=None)
        assert score.composite_rate is None


class TestProvisionalFlag:
    """provisional flag on AgentCorrectnessScore."""

    def test_provisional_true_when_any_dimension_stubbed(self) -> None:
        ws = _workflow_summary(mapped_trace_count=3, total=3, computable=3, passed=3)
        mapped = [_trace(f"t-{i}", ["get_rubric", "parse_resume", "submit_evaluation"]) for i in range(3)]
        score = compute_correctness_score(ws, mapped, operation=_op(), llm_client=None)
        # Slice A always has 3 stubbed → provisional True.
        assert score.provisional is True

    def test_provisional_false_when_no_stubs(self) -> None:
        # Pure dataclass test — ensure AgentCorrectnessScore can represent a
        # non-provisional score (no stubs). The compute function never returns
        # this in Slice A; we construct the dataclass directly.
        non_stub = DimensionScore(
            name="task_execution",
            deterministic_rate=0.9,
            estimated_rate=0.9,
            ci_low=0.8,
            ci_high=1.0,
            sample_size=0,
            mode="deterministic",
            note=None,
        )
        score = AgentCorrectnessScore(
            workflow_name="Candidate Screening",
            mapped_trace_count=5,
            dimensions={
                "task_execution": non_stub,
                "output_correctness": non_stub,
                "path_integrity": non_stub,
                "decision_quality": non_stub,
                "context_handling": non_stub,
            },
            intersection_rate=0.9,
            composite_rate=0.9,
            provisional=False,
            budget_used=0,
            budget_cap_hit=False,
        )
        assert score.provisional is False


class TestNoTaxonomy:
    """Annotate dimensions when the business context lacks correctness criteria."""

    def test_missing_correctness_criteria_annotates_dimension(self) -> None:
        ws = _workflow_summary(mapped_trace_count=3)
        mapped = [_trace(f"t-{i}", ["get_rubric"]) for i in range(3)]
        op = _op(correctness_criteria=[])
        score = compute_correctness_score(ws, mapped, operation=op, llm_client=None)

        out = score.dimensions["output_correctness"]
        assert out.note is not None
        assert "correctness_criteria not defined" in out.note

    def test_missing_business_goal_annotates_dimension(self) -> None:
        ws = _workflow_summary(mapped_trace_count=3)
        mapped = [_trace(f"t-{i}", ["get_rubric"]) for i in range(3)]
        op = _op(business_goal=None, correctness_criteria=[])
        score = compute_correctness_score(ws, mapped, operation=op, llm_client=None)

        out = score.dimensions["output_correctness"]
        assert out.note is not None
        # Note explicitly calls out which taxonomy fields are missing (at
        # least "correctness_criteria" when it's empty).
        assert "correctness_criteria not defined" in out.note


class TestBudget:
    """LLM budget accounting. Slice A makes zero LLM calls."""

    def test_budget_used_is_zero_in_slice_a(self) -> None:
        ws = _workflow_summary(mapped_trace_count=3, total=3, computable=3, passed=3)
        mapped = [_trace(f"t-{i}", ["get_rubric", "parse_resume", "submit_evaluation"]) for i in range(3)]
        score = compute_correctness_score(ws, mapped, operation=_op(), llm_client=None)
        assert score.budget_used == 0

    def test_budget_cap_hit_is_false_in_slice_a(self) -> None:
        ws = _workflow_summary(mapped_trace_count=3, total=3, computable=3, passed=3)
        mapped = [_trace(f"t-{i}", ["get_rubric", "parse_resume", "submit_evaluation"]) for i in range(3)]
        score = compute_correctness_score(ws, mapped, operation=_op(), llm_client=None)
        assert score.budget_cap_hit is False


class TestEmptyMappedTraces:
    """Graceful handling when there are no mapped traces."""

    def test_empty_mapped_traces_returns_non_crashing_score(self) -> None:
        ws = _workflow_summary(mapped_trace_count=0, total=0, computable=0, passed=0)
        score = compute_correctness_score(ws, [], operation=_op(), llm_client=None)

        assert isinstance(score, AgentCorrectnessScore)
        assert score.mapped_trace_count == 0
        # All 5 dimensions must be present regardless.
        for key in (
            "task_execution",
            "output_correctness",
            "path_integrity",
            "decision_quality",
            "context_handling",
        ):
            assert key in score.dimensions


class TestDimensionVerdict:
    """DimensionVerdict StrEnum must carry the three documented labels."""

    def test_verdict_values(self) -> None:
        assert DimensionVerdict.PASS.value == "pass"
        assert DimensionVerdict.FAIL.value == "fail"
        assert DimensionVerdict.INSUFFICIENT_EVIDENCE.value == "insufficient_evidence"
