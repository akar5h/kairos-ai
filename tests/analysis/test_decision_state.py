"""Red-phase tests for the DecisionStatePacket extractor.

Target module (not yet implemented):
    src.kairos.analysis.decision_state

Expected surface:
    class MissingReason(StrEnum):
        NOT_INSTRUMENTED | NOT_USED_BEFORE_STEP | TRACE_FIELD_MISSING |
        STEP_FIELD_MISSING | PRESENT_EMPTY | PRESENT | UNKNOWN

    @dataclass DecisionStatePacket(
        trace_id, workflow_name, step_index,
        business_goal, reliability_metric, bad_run_means,
        user_input, system_instruction_summary, available_tools,
        tool_schema_summary,
        memory_reads_before_step, memory_reads_missing_reason,
        retrieved_context_before_step, retrieved_context_missing_reason,
        prior_tool_calls, prior_tool_outputs_missing_reason,
        current_step_tool_name, current_step_tool_args, current_step_tool_output,
        current_step_error_message,
        reference_expected_transition, actual_transition,
        deterministic_flags,
    )

    def extract_packet(
        trace, step_index, operation, coverage, reference, deterministic_flags,
    ) -> DecisionStatePacket

    MAX_TEXT_FIELD_CHARS = 800
    NOT_INSTRUMENTED_THRESHOLD = 0.30
"""

from __future__ import annotations

from typing import Any

from kairos.analysis.decision_state import (
    MAX_TEXT_FIELD_CHARS,
    NOT_INSTRUMENTED_THRESHOLD,
    DecisionStatePacket,
    MissingReason,
    extract_packet,
)
from kairos.analysis.evidence_coverage import (
    CONTEXT_FIELD_KEYS,
    REQUIRED_FIELD_KEYS,
    EvidenceCoverage,
)
from kairos.analysis.reference_behavior import ReferenceCohort, ReferenceConfidence
from kairos.models.enums import StepStatus, StepType, TerminalStatus
from kairos.models.trace import Step, TraceEnvelope
from kairos.taxonomy.business_context import BusinessOperation
from kairos.taxonomy.dfg import DFG

# ── Helpers ────────────────────────────────────────────────────────────


def _operation(
    *,
    name: str = "Candidate Screening",
    business_goal: str | None = "Reduce recruiter review time.",
    reliability_metric: str | None = "percent of completed screenings with full evidence.",
    bad_run_means: str | None = "Missing evidence or unsupported recommendation.",
    expected_tools: list[str] | None = None,
) -> BusinessOperation:
    return BusinessOperation(
        name=name,
        description="Evaluate one candidate end-to-end",
        expected_tools=(
            ["get_rubric", "parse_resume", "submit_evaluation"] if expected_tools is None else expected_tools
        ),
        priority="high",
        business_goal=business_goal,
        reliability_metric=reliability_metric,
        bad_run_means=bad_run_means,
    )


def _tool_step(
    i: int,
    tool: str,
    *,
    args: dict[str, Any] | None = None,
    output: str | None = "ok",
    status: StepStatus = StepStatus.OK,
    error: str | None = None,
) -> Step:
    return Step(
        step_index=i,
        step_type=StepType.TOOL_CALL,
        tool_name=tool,
        tool_args=args if args is not None else {"stub": True},
        tool_args_normalized=args if args is not None else {"stub": True},
        tool_output=output,
        status=status,
        error_message=error,
    )


def _llm_step(i: int, *, llm_input: str | None = None, llm_output: str | None = None) -> Step:
    return Step(
        step_index=i,
        step_type=StepType.LLM,
        llm_input=llm_input,
        llm_output=llm_output,
    )


def _retrieval_step(i: int, chunks: list[str] | None) -> Step:
    return Step(
        step_index=i,
        step_type=StepType.RETRIEVAL,
        retrieval_query="q",
        retrieval_chunks=chunks,
    )


def _coverage(
    *,
    total_traces: int,
    context_counts: dict[str, int] | None = None,
) -> EvidenceCoverage:
    required_counts = {key: total_traces for key in REQUIRED_FIELD_KEYS}
    context_counts = {**{key: 0 for key in CONTEXT_FIELD_KEYS}, **(context_counts or {})}
    return EvidenceCoverage(
        total_traces=total_traces,
        valid_traces=total_traces,
        invalid_traces=0,
        required_field_counts=required_counts,
        context_field_counts=context_counts,
    )


def _reference(
    edges: dict[tuple[str, str], int] | None = None,
    *,
    path: list[str] | None = None,
    confidence: ReferenceConfidence = ReferenceConfidence.MEDIUM,
) -> ReferenceCohort:
    edges = edges if edges is not None else {}
    nodes: dict[str, int] = {}
    for (a, b), w in edges.items():
        nodes[a] = nodes.get(a, 0) + w
        nodes[b] = nodes.get(b, 0) + w
    dfg = DFG(
        edges=edges,
        nodes=nodes,
        total_traces=max(1, max(edges.values()) if edges else 1),
    )
    return ReferenceCohort(
        eligible_traces=[],
        reference_traces=[],
        confidence=confidence,
        reference_dfg=dfg if edges else None,
        reference_edges=set(edges.keys()),
        reference_path=path if path is not None else [],
        step_budget_p75=None,
        token_budget_p75=None,
    )


def _trace(
    trace_id: str,
    steps: list[Step],
    *,
    user_input: str = "evaluate candidate",
    system_prompt: str | None = "You are a screening agent.",
) -> TraceEnvelope:
    return TraceEnvelope(
        trace_id=trace_id,
        user_input=user_input,
        system_prompt=system_prompt,
        steps=steps,
        terminal_status=TerminalStatus.COMPLETED,
    )


# ── TESTS ─────────────────────────────────────────────────────────────


class TestMissingReasonEnum:
    """Semantics for the MissingReason enum across context fields."""

    def test_not_instrumented_when_field_coverage_below_threshold(self) -> None:
        """Coverage ratio 0.2 (< 0.30) for retrieval_chunks → NOT_INSTRUMENTED."""
        op = _operation()
        # Total traces 10, retrieval instrumentation count 2 → ratio 0.2
        coverage = _coverage(total_traces=10, context_counts={"retrieval_chunks": 2})
        reference = _reference()
        trace = _trace(
            "t-1",
            steps=[
                _tool_step(0, "get_rubric"),
                _tool_step(1, "parse_resume"),
                _tool_step(2, "submit_evaluation"),
            ],
        )
        packet = extract_packet(
            trace=trace,
            step_index=2,
            operation=op,
            coverage=coverage,
            reference=reference,
            deterministic_flags=[],
        )
        assert packet.retrieved_context_missing_reason == MissingReason.NOT_INSTRUMENTED

    def test_present_when_coverage_high_and_trace_has_field_before_step(self) -> None:
        """Coverage 0.9 and trace has retrieval at step 2, extract for step 5 → PRESENT."""
        op = _operation()
        coverage = _coverage(total_traces=10, context_counts={"retrieval_chunks": 9})
        reference = _reference()
        trace = _trace(
            "t-2",
            steps=[
                _tool_step(0, "get_rubric"),
                _tool_step(1, "parse_resume"),
                _retrieval_step(2, ["chunk-a", "chunk-b"]),
                _tool_step(3, "parse_resume"),
                _tool_step(4, "submit_evaluation"),
                _tool_step(5, "submit_evaluation"),
            ],
        )
        packet = extract_packet(
            trace=trace,
            step_index=5,
            operation=op,
            coverage=coverage,
            reference=reference,
            deterministic_flags=[],
        )
        assert packet.retrieved_context_missing_reason == MissingReason.PRESENT
        assert packet.retrieved_context_before_step == ["chunk-a", "chunk-b"]

    def test_not_used_before_step_when_instrumented_but_not_before(self) -> None:
        """Coverage high, trace has retrieval only at step 7, extract for step 3 → NOT_USED_BEFORE_STEP."""
        op = _operation()
        coverage = _coverage(total_traces=10, context_counts={"retrieval_chunks": 9})
        reference = _reference()
        trace = _trace(
            "t-3",
            steps=[
                _tool_step(0, "get_rubric"),
                _tool_step(1, "parse_resume"),
                _tool_step(2, "parse_resume"),
                _tool_step(3, "submit_evaluation"),
                _tool_step(4, "submit_evaluation"),
                _tool_step(5, "submit_evaluation"),
                _tool_step(6, "submit_evaluation"),
                _retrieval_step(7, ["late-chunk"]),
            ],
        )
        packet = extract_packet(
            trace=trace,
            step_index=3,
            operation=op,
            coverage=coverage,
            reference=reference,
            deterministic_flags=[],
        )
        assert packet.retrieved_context_missing_reason == MissingReason.NOT_USED_BEFORE_STEP

    def test_trace_field_missing_when_coverage_high_but_this_trace_empty(self) -> None:
        """Coverage 0.8, but THIS trace has zero retrieval events → TRACE_FIELD_MISSING."""
        op = _operation()
        coverage = _coverage(total_traces=10, context_counts={"retrieval_chunks": 8})
        reference = _reference()
        trace = _trace(
            "t-4",
            steps=[
                _tool_step(0, "get_rubric"),
                _tool_step(1, "parse_resume"),
                _tool_step(2, "submit_evaluation"),
            ],
        )
        packet = extract_packet(
            trace=trace,
            step_index=2,
            operation=op,
            coverage=coverage,
            reference=reference,
            deterministic_flags=[],
        )
        assert packet.retrieved_context_missing_reason == MissingReason.TRACE_FIELD_MISSING

    def test_present_empty_when_field_exists_but_empty(self) -> None:
        """Trace has a retrieval step with chunks=[] → PRESENT_EMPTY."""
        op = _operation()
        coverage = _coverage(total_traces=10, context_counts={"retrieval_chunks": 9})
        reference = _reference()
        trace = _trace(
            "t-5",
            steps=[
                _tool_step(0, "get_rubric"),
                _retrieval_step(1, []),  # present, but empty
                _tool_step(2, "submit_evaluation"),
            ],
        )
        packet = extract_packet(
            trace=trace,
            step_index=2,
            operation=op,
            coverage=coverage,
            reference=reference,
            deterministic_flags=[],
        )
        assert packet.retrieved_context_missing_reason == MissingReason.PRESENT_EMPTY


class TestPacketExtraction:
    """Packet fields populated correctly from trace + operation + reference."""

    def test_packet_populates_business_fields_from_operation(self) -> None:
        op = _operation(
            name="Candidate Screening",
            business_goal="Goal X",
            reliability_metric="Metric Y",
            bad_run_means="Bad Z",
        )
        coverage = _coverage(total_traces=5)
        reference = _reference()
        trace = _trace(
            "t-biz",
            steps=[_tool_step(0, "get_rubric"), _tool_step(1, "parse_resume"), _tool_step(2, "submit_evaluation")],
        )
        packet = extract_packet(
            trace=trace,
            step_index=2,
            operation=op,
            coverage=coverage,
            reference=reference,
            deterministic_flags=[],
        )
        assert packet.workflow_name == "Candidate Screening"
        assert packet.business_goal == "Goal X"
        assert packet.reliability_metric == "Metric Y"
        assert packet.bad_run_means == "Bad Z"

    def test_packet_captures_prior_tool_calls(self) -> None:
        """Trace with 3 tool calls before step_index → packet has 3 prior entries."""
        op = _operation()
        coverage = _coverage(total_traces=5)
        reference = _reference()
        trace = _trace(
            "t-prior",
            steps=[
                _tool_step(0, "get_rubric", args={"a": 1}, output="rubric-ok"),
                _tool_step(1, "parse_resume", args={"b": 2}, output="resume-ok"),
                _tool_step(2, "parse_resume", args={"b": 3}, output="resume-ok-2"),
                _tool_step(3, "submit_evaluation", args={"c": 4}, output="submit-ok"),
            ],
        )
        packet = extract_packet(
            trace=trace,
            step_index=3,
            operation=op,
            coverage=coverage,
            reference=reference,
            deterministic_flags=[],
        )
        assert len(packet.prior_tool_calls) == 3
        # Each entry is a dict with tool_name/args/output_truncated
        names = [call["tool_name"] for call in packet.prior_tool_calls]
        assert names == ["get_rubric", "parse_resume", "parse_resume"]
        for call in packet.prior_tool_calls:
            assert "tool_name" in call
            assert "args" in call
            assert "output_truncated" in call

    def test_packet_captures_deterministic_flags(self) -> None:
        op = _operation()
        coverage = _coverage(total_traces=5)
        reference = _reference()
        trace = _trace(
            "t-flags",
            steps=[_tool_step(0, "get_rubric"), _tool_step(1, "parse_resume")],
        )
        flags = ["redundant_execution", "workflow_divergence"]
        packet = extract_packet(
            trace=trace,
            step_index=1,
            operation=op,
            coverage=coverage,
            reference=reference,
            deterministic_flags=flags,
        )
        assert packet.deterministic_flags == flags

    def test_packet_current_step_fields_when_tool_call(self) -> None:
        op = _operation()
        coverage = _coverage(total_traces=5)
        reference = _reference()
        trace = _trace(
            "t-curr-tool",
            steps=[
                _tool_step(0, "get_rubric"),
                _tool_step(1, "parse_resume", args={"resume": "/x.pdf"}, output="parsed"),
            ],
        )
        packet = extract_packet(
            trace=trace,
            step_index=1,
            operation=op,
            coverage=coverage,
            reference=reference,
            deterministic_flags=[],
        )
        assert packet.current_step_tool_name == "parse_resume"
        assert packet.current_step_tool_args == {"resume": "/x.pdf"}
        assert packet.current_step_tool_output == "parsed"

    def test_packet_current_step_fields_when_llm_step(self) -> None:
        """Current step is an LLM-only step → tool fields are None."""
        op = _operation()
        coverage = _coverage(total_traces=5)
        reference = _reference()
        trace = _trace(
            "t-curr-llm",
            steps=[
                _tool_step(0, "get_rubric"),
                _llm_step(1, llm_input="think", llm_output="planning"),
            ],
        )
        packet = extract_packet(
            trace=trace,
            step_index=1,
            operation=op,
            coverage=coverage,
            reference=reference,
            deterministic_flags=[],
        )
        assert packet.current_step_tool_name is None
        assert packet.current_step_tool_args is None
        assert packet.current_step_tool_output is None

    def test_packet_reference_expected_transition_from_reference_dfg(self) -> None:
        """Reference DFG has (A,B) as highest-weight edge from A; trace is (A,X) → expected (A,B)."""
        op = _operation()
        coverage = _coverage(total_traces=5)
        reference = _reference(
            {
                ("get_rubric", "parse_resume"): 10,
                ("get_rubric", "noise_tool"): 1,
                ("parse_resume", "submit_evaluation"): 10,
            },
            path=["get_rubric", "parse_resume", "submit_evaluation"],
        )
        # Trace deviates from reference: (get_rubric, some_other_tool)
        trace = _trace(
            "t-div",
            steps=[
                _tool_step(0, "get_rubric"),
                _tool_step(1, "some_other_tool"),
                _tool_step(2, "submit_evaluation"),
            ],
        )
        packet = extract_packet(
            trace=trace,
            step_index=1,
            operation=op,
            coverage=coverage,
            reference=reference,
            deterministic_flags=["workflow_divergence"],
        )
        assert packet.reference_expected_transition == ("get_rubric", "parse_resume")

    def test_packet_actual_transition_preserved(self) -> None:
        op = _operation()
        coverage = _coverage(total_traces=5)
        reference = _reference(
            {
                ("get_rubric", "parse_resume"): 10,
                ("parse_resume", "submit_evaluation"): 10,
            },
            path=["get_rubric", "parse_resume", "submit_evaluation"],
        )
        trace = _trace(
            "t-actual",
            steps=[
                _tool_step(0, "get_rubric"),
                _tool_step(1, "some_other_tool"),
                _tool_step(2, "submit_evaluation"),
            ],
        )
        packet = extract_packet(
            trace=trace,
            step_index=1,
            operation=op,
            coverage=coverage,
            reference=reference,
            deterministic_flags=[],
        )
        assert packet.actual_transition == ("get_rubric", "some_other_tool")

    def test_packet_actual_transition_none_for_first_step(self) -> None:
        """When step_index points at the first tool call there is no predecessor → None."""
        op = _operation()
        coverage = _coverage(total_traces=5)
        reference = _reference()
        trace = _trace(
            "t-first",
            steps=[
                _tool_step(0, "get_rubric"),
                _tool_step(1, "parse_resume"),
            ],
        )
        packet = extract_packet(
            trace=trace,
            step_index=0,
            operation=op,
            coverage=coverage,
            reference=reference,
            deterministic_flags=[],
        )
        assert packet.actual_transition is None


class TestTextTruncation:
    """Text fields beyond MAX_TEXT_FIELD_CHARS get truncated with a marker."""

    def test_truncate_long_tool_output(self) -> None:
        """tool_output 2000 chars → truncated to 800 + marker."""
        op = _operation()
        coverage = _coverage(total_traces=5)
        reference = _reference()
        long_output = "x" * 2000
        trace = _trace(
            "t-trunc",
            steps=[
                _tool_step(0, "get_rubric"),
                _tool_step(1, "parse_resume", output=long_output),
            ],
        )
        packet = extract_packet(
            trace=trace,
            step_index=1,
            operation=op,
            coverage=coverage,
            reference=reference,
            deterministic_flags=[],
        )
        assert packet.current_step_tool_output is not None
        # First MAX_TEXT_FIELD_CHARS chars preserved, then truncation marker.
        assert packet.current_step_tool_output.startswith("x" * MAX_TEXT_FIELD_CHARS)
        assert "[truncated]" in packet.current_step_tool_output
        assert len(packet.current_step_tool_output) < len(long_output)

    def test_short_fields_not_truncated(self) -> None:
        """tool_output under 800 chars → unchanged."""
        op = _operation()
        coverage = _coverage(total_traces=5)
        reference = _reference()
        short = "x" * 100
        trace = _trace(
            "t-short",
            steps=[
                _tool_step(0, "get_rubric"),
                _tool_step(1, "parse_resume", output=short),
            ],
        )
        packet = extract_packet(
            trace=trace,
            step_index=1,
            operation=op,
            coverage=coverage,
            reference=reference,
            deterministic_flags=[],
        )
        assert packet.current_step_tool_output == short
        assert "[truncated]" not in (packet.current_step_tool_output or "")

    def test_truncate_prior_tool_call_outputs(self) -> None:
        """Each prior_tool_calls[i].output_truncated is truncated independently."""
        op = _operation()
        coverage = _coverage(total_traces=5)
        reference = _reference()
        long_a = "a" * 1500
        long_b = "b" * 1500
        short_c = "c" * 50
        trace = _trace(
            "t-prior-trunc",
            steps=[
                _tool_step(0, "get_rubric", output=long_a),
                _tool_step(1, "parse_resume", output=long_b),
                _tool_step(2, "parse_resume", output=short_c),
                _tool_step(3, "submit_evaluation", output="ok"),
            ],
        )
        packet = extract_packet(
            trace=trace,
            step_index=3,
            operation=op,
            coverage=coverage,
            reference=reference,
            deterministic_flags=[],
        )
        assert len(packet.prior_tool_calls) == 3
        assert "[truncated]" in packet.prior_tool_calls[0]["output_truncated"]
        assert "[truncated]" in packet.prior_tool_calls[1]["output_truncated"]
        assert "[truncated]" not in packet.prior_tool_calls[2]["output_truncated"]


class TestConstants:
    """Module-level constants visible to the implementer contract."""

    def test_max_text_field_chars_is_800(self) -> None:
        assert MAX_TEXT_FIELD_CHARS == 800

    def test_not_instrumented_threshold_is_0_30(self) -> None:
        assert NOT_INSTRUMENTED_THRESHOLD == 0.30


class TestDecisionStatePacketShape:
    """DecisionStatePacket dataclass shape sanity."""

    def test_extract_returns_decision_state_packet_instance(self) -> None:
        op = _operation()
        coverage = _coverage(total_traces=5)
        reference = _reference()
        trace = _trace(
            "t-shape",
            steps=[_tool_step(0, "get_rubric"), _tool_step(1, "parse_resume")],
        )
        packet = extract_packet(
            trace=trace,
            step_index=1,
            operation=op,
            coverage=coverage,
            reference=reference,
            deterministic_flags=[],
        )
        assert isinstance(packet, DecisionStatePacket)
        assert packet.trace_id == "t-shape"
        assert packet.step_index == 1
