"""Red-phase tests for the reference-behavior extractor.

Target module (not yet implemented):
    src.kairos.analysis.reference_behavior

Expected surface:
    class ReferenceConfidence(StrEnum): HIGH | MEDIUM | LOW | NONE
    @dataclass ReferenceCohort(
        eligible_traces,
        reference_traces,
        confidence,
        reference_dfg,
        reference_edges,
        reference_path,
        step_budget_p75,
        token_budget_p75,
    )
    def extract_reference_behavior(
        traces: list[TraceEnvelope],
        operation: BusinessOperation,
    ) -> ReferenceCohort
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kairos.analysis.reference_behavior import (
    ReferenceCohort,
    ReferenceConfidence,
    extract_reference_behavior,
    segment_trace_for_workflow,
)
from kairos.analysis.workflow_membership import MembershipKind, WorkflowMembership
from kairos.models.enums import StepStatus, StepType, TerminalStatus
from kairos.models.trace import Step, TraceEnvelope
from kairos.taxonomy.business_context import BusinessOperation

FIXTURES = Path(__file__).parent.parent / "fixtures"


# ── Synthesis helpers ──────────────────────────────────────────────────


def _hr_operation(
    expected_tools: list[str] | None = None,
) -> BusinessOperation:
    """Canonical HR-screening business operation used throughout this suite."""
    return BusinessOperation(
        name="Candidate Screening",
        description="Evaluate one candidate end-to-end",
        expected_tools=(
            ["get_rubric", "parse_resume", "submit_evaluation"] if expected_tools is None else expected_tools
        ),
        priority="high",
        required_side_effect_tools=[],
    )


def _step(
    i: int,
    tool: str,
    *,
    status: StepStatus = StepStatus.OK,
    tool_args: dict[str, Any] | None = None,
    tool_output: str | None = "ok",
    error: str | None = None,
    total_tokens: int | None = None,
) -> Step:
    return Step(
        step_index=i,
        step_type=StepType.TOOL_CALL,
        tool_name=tool,
        tool_args=tool_args if tool_args is not None else {"stub": True},
        tool_args_normalized=tool_args if tool_args is not None else {"stub": True},
        tool_output=tool_output,
        status=status,
        error_message=error,
        total_tokens=total_tokens,
    )


def _happy_trace(
    trace_id: str,
    tools: list[str] | None = None,
    *,
    total_tokens: int = 100,
    total_latency_ms: int = 500,
    step_tokens: int | None = None,
    tool_args_per_step: list[dict[str, Any]] | None = None,
) -> TraceEnvelope:
    """Construct a COMPLETED, error-free trace with the canonical HR tools."""
    tools = tools if tools is not None else ["get_rubric", "parse_resume", "submit_evaluation"]
    steps: list[Step] = []
    for i, tool in enumerate(tools):
        args = (
            tool_args_per_step[i]
            if tool_args_per_step is not None and i < len(tool_args_per_step)
            else {"tool": tool, "i": i}
        )
        steps.append(
            _step(
                i,
                tool,
                tool_args=args,
                tool_output=f"{tool}-done",
                total_tokens=step_tokens,
            )
        )
    return TraceEnvelope(
        trace_id=trace_id,
        user_input="evaluate candidate",
        steps=steps,
        terminal_status=TerminalStatus.COMPLETED,
        total_tokens=total_tokens,
        total_latency_ms=total_latency_ms,
    )


# ── TESTS ─────────────────────────────────────────────────────────────


class TestEligibilityFilter:
    """Eligibility = COMPLETED ∧ error_count=0 ∧ ¬loop ∧ ¬critical_redundancy ∧ coverage ≥ 0.8."""

    def test_completed_error_free_trace_with_all_tools_is_eligible(self) -> None:
        op = _hr_operation()
        good = _happy_trace("t-good")
        result = extract_reference_behavior([good], op)
        assert len(result.eligible_traces) == 1
        assert result.eligible_traces[0].trace_id == "t-good"

    def test_error_terminal_excluded(self) -> None:
        op = _hr_operation()
        bad = TraceEnvelope(
            trace_id="t-err-terminal",
            user_input="evaluate candidate",
            steps=[
                _step(0, "get_rubric"),
                _step(1, "parse_resume"),
                _step(2, "submit_evaluation"),
            ],
            terminal_status=TerminalStatus.ERROR,
        )
        result = extract_reference_behavior([bad], op)
        assert result.eligible_traces == []

    def test_completed_but_error_count_nonzero_excluded(self) -> None:
        op = _hr_operation()
        trace = TraceEnvelope(
            trace_id="t-inner-err",
            user_input="evaluate candidate",
            steps=[
                _step(0, "get_rubric"),
                _step(1, "parse_resume", status=StepStatus.ERROR, error="boom"),
                _step(2, "submit_evaluation"),
            ],
            terminal_status=TerminalStatus.COMPLETED,
        )
        result = extract_reference_behavior([trace], op)
        assert result.eligible_traces == []

    def test_loop_trace_excluded(self) -> None:
        """3+ consecutive same-tool calls (period-1 loop) → not eligible."""
        op = _hr_operation()
        steps: list[Step] = []
        # get_rubric x 3 (period-1 loop, same output) + submit_evaluation
        for i in range(3):
            steps.append(_step(i, "get_rubric", tool_output="same"))
        steps.append(_step(3, "submit_evaluation", tool_output="submitted"))
        trace = TraceEnvelope(
            trace_id="t-loop",
            user_input="evaluate candidate",
            steps=steps,
            terminal_status=TerminalStatus.COMPLETED,
        )
        result = extract_reference_behavior([trace], op)
        assert result.eligible_traces == []

    def test_critical_redundancy_excluded(self) -> None:
        """3 consecutive same-tool calls with near-identical args → critical redundancy."""
        op = _hr_operation()
        # Three identical consecutive parse_resume calls (cluster size = 3).
        identical_args = {"resume": "/app/alice.pdf"}
        trace = TraceEnvelope(
            trace_id="t-redundant",
            user_input="evaluate candidate",
            steps=[
                _step(0, "get_rubric"),
                _step(1, "parse_resume", tool_args=identical_args),
                _step(2, "parse_resume", tool_args=identical_args),
                _step(3, "parse_resume", tool_args=identical_args),
                _step(4, "submit_evaluation"),
            ],
            terminal_status=TerminalStatus.COMPLETED,
        )
        result = extract_reference_behavior([trace], op)
        assert result.eligible_traces == []

    def test_missing_required_tool_coverage_excluded(self) -> None:
        """Operation expects A, B, C; trace only calls A and B → coverage 2/3 < 0.8 → exclude."""
        op = _hr_operation(expected_tools=["get_rubric", "parse_resume", "submit_evaluation"])
        trace = TraceEnvelope(
            trace_id="t-missing-tool",
            user_input="evaluate candidate",
            steps=[
                _step(0, "get_rubric"),
                _step(1, "parse_resume"),
                # submit_evaluation never called
            ],
            terminal_status=TerminalStatus.COMPLETED,
        )
        result = extract_reference_behavior([trace], op)
        assert result.eligible_traces == []

    def test_no_expected_tools_skips_coverage_check(self) -> None:
        """Operation with empty expected_tools skips the coverage check → trace is eligible."""
        op = _hr_operation(expected_tools=[])
        trace = TraceEnvelope(
            trace_id="t-no-expected",
            user_input="evaluate candidate",
            steps=[_step(0, "anything")],
            terminal_status=TerminalStatus.COMPLETED,
        )
        result = extract_reference_behavior([trace], op)
        assert len(result.eligible_traces) == 1


class TestConfidenceTiers:
    """Confidence tier is a pure function of eligible-trace count."""

    def test_zero_eligible_traces_confidence_none(self) -> None:
        op = _hr_operation()
        result = extract_reference_behavior([], op)
        assert result.confidence == ReferenceConfidence.NONE
        assert result.eligible_traces == []
        assert result.reference_traces == []
        assert result.reference_dfg is None
        assert result.reference_edges == set()
        assert result.reference_path == []
        assert result.step_budget_p75 is None
        assert result.token_budget_p75 is None

    def test_four_eligible_traces_confidence_none(self) -> None:
        op = _hr_operation()
        traces = [_happy_trace(f"t-{i}") for i in range(4)]
        result = extract_reference_behavior(traces, op)
        assert result.confidence == ReferenceConfidence.NONE

    def test_five_eligible_traces_confidence_low(self) -> None:
        op = _hr_operation()
        traces = [_happy_trace(f"t-{i}") for i in range(5)]
        result = extract_reference_behavior(traces, op)
        assert result.confidence == ReferenceConfidence.LOW

    def test_twenty_eligible_traces_confidence_medium(self) -> None:
        op = _hr_operation()
        traces = [_happy_trace(f"t-{i}") for i in range(20)]
        result = extract_reference_behavior(traces, op)
        assert result.confidence == ReferenceConfidence.MEDIUM

    def test_fifty_eligible_traces_confidence_high(self) -> None:
        op = _hr_operation()
        traces = [_happy_trace(f"t-{i}") for i in range(50)]
        result = extract_reference_behavior(traces, op)
        assert result.confidence == ReferenceConfidence.HIGH


class TestReferenceSelection:
    """Reference cohort = traces matching the mode tool_sequence."""

    def test_reference_is_mode_tool_sequence(self) -> None:
        """6 eligible traces: 4 share a canonical sequence, 2 have exotic sequences."""
        op = _hr_operation()
        canonical = ["get_rubric", "parse_resume", "submit_evaluation"]
        traces: list[TraceEnvelope] = [_happy_trace(f"can-{i}", tools=canonical) for i in range(4)]
        for i in range(2):
            traces.append(
                TraceEnvelope(
                    trace_id=f"ext-{i}",
                    user_input="evaluate candidate",
                    steps=[
                        _step(0, "get_rubric"),
                        _step(1, "parse_resume"),
                        _step(2, "some_extra_tool"),
                        _step(3, "submit_evaluation"),
                    ],
                    terminal_status=TerminalStatus.COMPLETED,
                )
            )
        result = extract_reference_behavior(traces, op)
        assert len(result.eligible_traces) == 6
        # Reference = the 4 traces whose sequence is the mode.
        ref_ids = {t.trace_id for t in result.reference_traces}
        assert ref_ids == {"can-0", "can-1", "can-2", "can-3"}

    def test_minimum_reference_size_is_one(self) -> None:
        op = _hr_operation()
        traces = [_happy_trace("only-one")]
        result = extract_reference_behavior(traces, op)
        assert len(result.reference_traces) >= 1

    def test_reference_all_unique_sequences_returns_one(self) -> None:
        """When every trace has a unique sequence, mode selects exactly one."""
        op = _hr_operation()
        traces: list[TraceEnvelope] = []
        for i in range(5):
            extra_steps = (
                [_step(0, "get_rubric")]
                + [_step(j + 1, "parse_resume", tool_output=f"p-{j}") for j in range(i)]
                + [_step(i + 1, "submit_evaluation")]
            )
            for idx, s in enumerate(extra_steps):
                s.step_index = idx
            traces.append(
                TraceEnvelope(
                    trace_id=f"t-{i}",
                    user_input="x",
                    steps=extra_steps,
                    terminal_status=TerminalStatus.COMPLETED,
                )
            )
        result = extract_reference_behavior(traces, op)
        assert len(result.reference_traces) == 1


class TestReferenceDfgAndPath:
    """Reference DFG is built only from reference_traces."""

    def test_reference_dfg_built_only_from_reference_traces(self) -> None:
        """Non-reference eligible traces' bigrams must not appear in reference_edges."""
        op = _hr_operation()
        traces: list[TraceEnvelope] = []
        # 4 canonical traces (2 of which will be reference).
        for i in range(4):
            traces.append(
                _happy_trace(
                    f"can-{i}",
                    tools=["get_rubric", "parse_resume", "submit_evaluation"],
                    total_tokens=100,
                    total_latency_ms=100,
                )
            )
        # 4 eligible-but-non-reference traces with an exotic bigram that
        # should be ABSENT from reference_edges.
        for i in range(4):
            exotic = TraceEnvelope(
                trace_id=f"ext-{i}",
                user_input="evaluate candidate",
                steps=[
                    _step(0, "get_rubric", tool_args={"i": i}),
                    _step(1, "parse_resume", tool_args={"i": i}),
                    # Exotic detour, then submit_evaluation to keep coverage.
                    _step(2, "some_exotic_tool", tool_args={"i": i}),
                    _step(3, "submit_evaluation", tool_args={"i": i}),
                ],
                terminal_status=TerminalStatus.COMPLETED,
                total_tokens=9999,
                total_latency_ms=9999,
            )
            traces.append(exotic)
        result = extract_reference_behavior(traces, op)
        # The exotic bigram must not be in reference_edges.
        assert ("parse_resume", "some_exotic_tool") not in result.reference_edges
        assert ("some_exotic_tool", "submit_evaluation") not in result.reference_edges

    def test_reference_path_is_greedy_walk(self) -> None:
        """Greedy walk follows the highest-weight edges in the reference DFG."""
        op = _hr_operation()
        traces = [_happy_trace(f"t-{i}") for i in range(8)]
        result = extract_reference_behavior(traces, op)
        assert result.reference_path[:3] == [
            "get_rubric",
            "parse_resume",
            "submit_evaluation",
        ]

    def test_reference_path_stops_at_cycle_or_terminal(self) -> None:
        op = _hr_operation()
        traces = [_happy_trace(f"t-{i}") for i in range(8)]
        result = extract_reference_behavior(traces, op)
        # Must terminate (no infinite loops).
        assert len(result.reference_path) <= 20
        # Must have no duplicates (greedy walk avoids revisits).
        assert len(result.reference_path) == len(set(result.reference_path))

    def test_confidence_none_gives_empty_reference_edges_and_path(self) -> None:
        op = _hr_operation()
        result = extract_reference_behavior([], op)
        assert result.confidence == ReferenceConfidence.NONE
        assert result.reference_dfg is None
        assert result.reference_edges == set()
        assert result.reference_path == []


class TestBudgets:
    """Step and token budgets are p75 over the reference cohort."""

    def test_step_budget_p75(self) -> None:
        """6 mode-reference traces with canonical sequence → p75 of step counts."""
        op = _hr_operation()
        # All 6 have the same canonical sequence so they all form the mode.
        # ≥5 ensures confidence > NONE so budgets are computed.
        traces: list[TraceEnvelope] = []
        # Use canonical sequence; vary step counts by using different total_tokens
        # (but keep tool_sequence identical so mode selects all of them).
        for i in range(6):
            traces.append(
                _happy_trace(
                    f"ref-{i}",
                    total_tokens=100 + i * 10,  # 100,110,...,150
                )
            )
        result = extract_reference_behavior(traces, op)
        assert result.step_budget_p75 is not None
        # All traces share the canonical 3-step sequence → all selected as reference.
        # step_counts all = 3 → p75 = 3.
        assert result.step_budget_p75 == pytest.approx(3.0, abs=0.01)

    def test_token_budget_disabled_when_token_coverage_low(self) -> None:
        """If <80% of eligible traces have total_tokens → token_budget_p75 is None."""
        op = _hr_operation()
        traces: list[TraceEnvelope] = []
        # 5 eligible traces, only 1 has tokens > 0 → 20% coverage.
        for i in range(5):
            t = _happy_trace(f"t-{i}", total_tokens=(100 if i == 0 else 0))
            traces.append(t)
        result = extract_reference_behavior(traces, op)
        assert result.token_budget_p75 is None

    def test_token_budget_computed_when_tokens_present(self) -> None:
        op = _hr_operation()
        traces = [_happy_trace(f"t-{i}", total_tokens=100 + i) for i in range(8)]
        result = extract_reference_behavior(traces, op)
        assert result.token_budget_p75 is not None
        assert result.token_budget_p75 > 0


class TestWorkflowScopedReferenceBehavior:
    """Slice B.1: reference cohort is filtered by FULL memberships and segmented to the workflow's tools."""

    def test_segment_trace_keeps_only_expected_tools(self) -> None:
        op = _hr_operation(expected_tools=["A", "B", "C"])
        # 8 steps — 3 of which are A/B/C, interleaved with unrelated tools.
        steps = [
            _step(0, "A", tool_args={"i": 0}),
            _step(1, "X", tool_args={"i": 1}),
            _step(2, "B", tool_args={"i": 2}),
            _step(3, "Y", tool_args={"i": 3}),
            _step(4, "C", tool_args={"i": 4}),
            _step(5, "Z", tool_args={"i": 5}),
            _step(6, "W", tool_args={"i": 6}),
            _step(7, "Q", tool_args={"i": 7}),
        ]
        trace = TraceEnvelope(
            trace_id="t-seg",
            user_input="x",
            steps=steps,
            terminal_status=TerminalStatus.COMPLETED,
        )
        segmented = segment_trace_for_workflow(trace, op)
        assert [s.tool_name for s in segmented] == ["A", "B", "C"]
        assert len(segmented) == 3

    def test_segment_trace_preserves_order(self) -> None:
        op = _hr_operation(expected_tools=["A", "B", "C"])
        steps = [
            _step(0, "A", tool_args={"i": 0}),
            _step(1, "X", tool_args={"i": 1}),
            _step(2, "B", tool_args={"i": 2}),
            _step(3, "Y", tool_args={"i": 3}),
            _step(4, "C", tool_args={"i": 4}),
        ]
        trace = TraceEnvelope(
            trace_id="t-seg-order",
            user_input="x",
            steps=steps,
            terminal_status=TerminalStatus.COMPLETED,
        )
        segmented = segment_trace_for_workflow(trace, op)
        assert [s.tool_name for s in segmented] == ["A", "B", "C"]

    def test_segment_trace_empty_when_no_expected_tools_appear(self) -> None:
        op = _hr_operation(expected_tools=["Z"])
        steps = [
            _step(0, "A", tool_args={"i": 0}),
            _step(1, "B", tool_args={"i": 1}),
            _step(2, "C", tool_args={"i": 2}),
        ]
        trace = TraceEnvelope(
            trace_id="t-seg-none",
            user_input="x",
            steps=steps,
            terminal_status=TerminalStatus.COMPLETED,
        )
        segmented = segment_trace_for_workflow(trace, op)
        assert segmented == []

    def test_extract_reference_behavior_accepts_memberships_kwarg(self) -> None:
        op = _hr_operation()
        traces = [_happy_trace(f"t-{i}") for i in range(5)]
        memberships: dict[str, list[WorkflowMembership]] = {
            t.trace_id: [
                WorkflowMembership(
                    operation_name=op.name,
                    kind=MembershipKind.FULL,
                    recall=1.0,
                )
            ]
            for t in traces
        }
        # Does not raise; returns a ReferenceCohort.
        result = extract_reference_behavior(traces, op, memberships=memberships)
        assert isinstance(result, ReferenceCohort)

    def test_memberships_filters_to_full_only(self) -> None:
        op = _hr_operation()
        # 2 FULL + 1 ATTEMPTED + 1 absent-from-memberships (not a member).
        full_traces = [_happy_trace("full-0"), _happy_trace("full-1")]
        attempted_trace = _happy_trace("attempted-0")
        non_member_trace = _happy_trace("non-member-0")
        traces = [*full_traces, attempted_trace, non_member_trace]

        memberships: dict[str, list[WorkflowMembership]] = {
            "full-0": [WorkflowMembership(op.name, MembershipKind.FULL, 1.0)],
            "full-1": [WorkflowMembership(op.name, MembershipKind.FULL, 1.0)],
            "attempted-0": [WorkflowMembership(op.name, MembershipKind.ATTEMPTED, 0.67)],
            # non-member-0 has no entry at all.
        }

        result = extract_reference_behavior(traces, op, memberships=memberships)
        ref_ids = {t.trace_id for t in result.eligible_traces}
        # Only the 2 FULL members are considered.
        assert ref_ids == {"full-0", "full-1"}
        assert "attempted-0" not in ref_ids
        assert "non-member-0" not in ref_ids

    def test_reference_dfg_excludes_out_of_workflow_tools(self) -> None:
        op = _hr_operation(expected_tools=["parse_resume", "submit_evaluation"])
        # Trace includes an out-of-workflow tool (send_candidate_email) between
        # the two workflow tools. After segmentation, only the workflow-scoped
        # edge (parse_resume, submit_evaluation) should appear in the DFG.
        traces: list[TraceEnvelope] = []
        for i in range(5):
            steps = [
                _step(0, "parse_resume", tool_args={"i": i}),
                _step(1, "send_candidate_email", tool_args={"i": i}),
                _step(2, "submit_evaluation", tool_args={"i": i}),
            ]
            traces.append(
                TraceEnvelope(
                    trace_id=f"seg-{i}",
                    user_input="x",
                    steps=steps,
                    terminal_status=TerminalStatus.COMPLETED,
                )
            )
        memberships: dict[str, list[WorkflowMembership]] = {
            t.trace_id: [WorkflowMembership(op.name, MembershipKind.FULL, 1.0)] for t in traces
        }
        result = extract_reference_behavior(traces, op, memberships=memberships)
        # Workflow-scoped reference contains the direct edge between the two
        # expected tools…
        assert ("parse_resume", "submit_evaluation") in result.reference_edges
        # …and must NOT contain edges involving the out-of-workflow tool.
        assert ("parse_resume", "send_candidate_email") not in result.reference_edges
        assert ("send_candidate_email", "submit_evaluation") not in result.reference_edges

    def test_no_memberships_arg_preserves_legacy_behavior(self) -> None:
        op = _hr_operation()
        traces = [_happy_trace(f"t-{i}") for i in range(5)]
        # Legacy path — omit memberships kwarg.
        legacy = extract_reference_behavior(traces, op)
        # ReferenceCohort shape is unchanged.
        assert isinstance(legacy, ReferenceCohort)
        assert isinstance(legacy.confidence, ReferenceConfidence)
        # All 5 happy traces are eligible; the legacy single-label pass is
        # defined by eligibility, not by membership filtering.
        assert len(legacy.eligible_traces) == 5
        assert len(legacy.reference_traces) >= 1
