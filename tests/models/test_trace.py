"""Tests for IR models: Step, TraceEnvelope, NormalizationReport, and enums."""

from kairos.models.enums import OutputType, StepStatus, StepType, TerminalStatus
from kairos.models.trace import NormalizationReport, Step, TraceEnvelope

# ---------------------------------------------------------------------------
# Enum string values
# ---------------------------------------------------------------------------


class TestEnums:
    def test_terminal_status_values(self):
        assert TerminalStatus.COMPLETED == "completed"
        assert TerminalStatus.ERROR == "error"
        assert TerminalStatus.TIMEOUT == "timeout"
        assert TerminalStatus.HUMAN_ESCALATION == "human_escalation"
        assert TerminalStatus.UNKNOWN == "unknown"

    def test_output_type_values(self):
        assert OutputType.TEXT == "text"
        assert OutputType.FILE == "file"
        assert OutputType.API_CALL == "api_call"
        assert OutputType.MIXED == "mixed"
        assert OutputType.UNKNOWN == "unknown"

    def test_step_status_values(self):
        assert StepStatus.OK == "ok"
        assert StepStatus.ERROR == "error"

    def test_step_type_values(self):
        assert StepType.LLM == "llm"
        assert StepType.TOOL_CALL == "tool_call"
        assert StepType.RETRIEVAL == "retrieval"
        assert StepType.AGENT == "agent"
        assert StepType.OTHER == "other"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_steps() -> list[Step]:
    """Create 5 steps: 2 LLM, 2 TOOL_CALL (one with ERROR), 1 RETRIEVAL."""
    return [
        Step(step_index=0, step_type=StepType.LLM, llm_output="Hello"),
        Step(
            step_index=1,
            step_type=StepType.TOOL_CALL,
            tool_name="search",
            tool_args={"q": "test"},
        ),
        Step(
            step_index=2,
            step_type=StepType.RETRIEVAL,
            retrieval_query="find docs",
        ),
        Step(
            step_index=3,
            step_type=StepType.TOOL_CALL,
            tool_name="write_file",
            tool_args={"path": "/data/out.txt"},
            status=StepStatus.ERROR,
            error_message="Permission denied",
        ),
        Step(step_index=4, step_type=StepType.LLM, llm_output="Done"),
    ]


# ---------------------------------------------------------------------------
# TraceEnvelope derived fields
# ---------------------------------------------------------------------------


class TestTraceEnvelopeDerivedFields:
    def test_derived_fields_computed(self):
        steps = _make_steps()
        env = TraceEnvelope(trace_id="t1", steps=steps)

        assert env.step_count == 5
        assert env.tool_sequence == ["search", "write_file"]
        assert env.tool_bigrams == [("search", "write_file")]
        assert env.unique_tool_count == 2
        assert env.error_count == 1
        assert env.has_retrieval is True
        assert env.retrieval_step_count == 1

    def test_empty_steps_derived_fields(self):
        env = TraceEnvelope(trace_id="t2", steps=[])

        assert env.step_count == 0
        assert env.tool_sequence == []
        assert env.tool_bigrams == []
        assert env.unique_tool_count == 0
        assert env.error_count == 0
        assert env.has_retrieval is False
        assert env.retrieval_step_count == 0

    def test_tool_bigrams_three_tools(self):
        steps = [
            Step(step_index=0, step_type=StepType.TOOL_CALL, tool_name="a"),
            Step(step_index=1, step_type=StepType.TOOL_CALL, tool_name="b"),
            Step(step_index=2, step_type=StepType.TOOL_CALL, tool_name="c"),
        ]
        env = TraceEnvelope(trace_id="t3", steps=steps)

        assert env.tool_bigrams == [("a", "b"), ("b", "c")]

    def test_single_tool_no_bigrams(self):
        steps = [
            Step(step_index=0, step_type=StepType.TOOL_CALL, tool_name="only"),
        ]
        env = TraceEnvelope(trace_id="t4", steps=steps)

        assert env.tool_sequence == ["only"]
        assert env.tool_bigrams == []


# ---------------------------------------------------------------------------
# Validation warnings
# ---------------------------------------------------------------------------


class TestValidationWarnings:
    def test_missing_user_input_adds_warning(self):
        env = TraceEnvelope(trace_id="t5", user_input=None)

        assert any("user_input" in w for w in env.validation_warnings)

    def test_present_user_input_no_warning(self):
        env = TraceEnvelope(trace_id="t6", user_input="Hello")

        assert not any("user_input" in w for w in env.validation_warnings)


# ---------------------------------------------------------------------------
# Serialization round-trip
# ---------------------------------------------------------------------------


class TestSerialization:
    def test_json_round_trip(self):
        steps = _make_steps()
        env = TraceEnvelope(
            trace_id="t7",
            user_input="Hello",
            steps=steps,
            terminal_status=TerminalStatus.COMPLETED,
            output_type=OutputType.TEXT,
        )

        json_str = env.model_dump_json()
        restored = TraceEnvelope.model_validate_json(json_str)

        assert restored.trace_id == env.trace_id
        assert restored.step_count == env.step_count
        assert restored.tool_sequence == env.tool_sequence
        assert restored.tool_bigrams == env.tool_bigrams
        assert restored.error_count == env.error_count
        assert restored.terminal_status == env.terminal_status


# ---------------------------------------------------------------------------
# NormalizationReport
# ---------------------------------------------------------------------------


class TestNormalizationReport:
    def test_defaults(self):
        report = NormalizationReport()

        assert report.total_traces_ingested == 0
        assert report.total_traces_normalized == 0
        assert report.errors == []
