"""Tests for field extractors: user_input, output_type, terminal_status."""

from kairos.models.enums import OutputType, StepStatus, StepType, TerminalStatus
from kairos.models.trace import Step
from kairos.normalization.field_extractors import (
    extract_user_input,
    infer_output_type,
    infer_terminal_status,
)

# ---------------------------------------------------------------------------
# extract_user_input
# ---------------------------------------------------------------------------


class TestExtractUserInput:
    def test_plain_string_returns_string_and_none(self):
        user_input, system_prompt = extract_user_input("Hello world", None)
        assert user_input == "Hello world"
        assert system_prompt is None

    def test_messages_with_system_and_user(self):
        trace_input = {
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "What is 2+2?"},
            ]
        }
        user_input, system_prompt = extract_user_input(trace_input, None)
        assert user_input == "What is 2+2?"
        assert system_prompt == "You are a helpful assistant."

    def test_none_inputs_returns_none_none(self):
        user_input, system_prompt = extract_user_input(None, None)
        assert user_input is None
        assert system_prompt is None

    def test_fallback_to_first_generation_input(self):
        first_gen = {
            "messages": [
                {"role": "user", "content": "From generation"},
            ]
        }
        user_input, system_prompt = extract_user_input(None, first_gen)
        assert user_input == "From generation"
        assert system_prompt is None

    def test_trace_input_no_messages_falls_back(self):
        trace_input = {"other_key": "value"}
        first_gen = {
            "messages": [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "fallback user"},
            ]
        }
        user_input, system_prompt = extract_user_input(trace_input, first_gen)
        assert user_input == "fallback user"
        assert system_prompt == "sys"

    def test_dict_with_empty_messages_falls_back(self):
        trace_input = {"messages": []}
        first_gen = "plain fallback"
        user_input, system_prompt = extract_user_input(trace_input, first_gen)
        assert user_input == "plain fallback"
        assert system_prompt is None


# ---------------------------------------------------------------------------
# infer_output_type
# ---------------------------------------------------------------------------


class TestInferOutputType:
    def test_empty_steps_returns_unknown(self):
        assert infer_output_type([], None) == OutputType.UNKNOWN

    def test_file_indicators_return_file(self):
        steps = [
            Step(
                step_index=0,
                step_type=StepType.TOOL_CALL,
                tool_name="save",
                tool_output="Saved to /tmp/report.pdf",
            ),
        ]
        assert infer_output_type(steps, None) == OutputType.FILE

    def test_api_indicators_return_api_call(self):
        steps = [
            Step(
                step_index=0,
                step_type=StepType.TOOL_CALL,
                tool_name="post_data",
                tool_output="status_code: 201, created successfully",
            ),
        ]
        assert infer_output_type(steps, None) == OutputType.API_CALL

    def test_no_tool_output_returns_text(self):
        steps = [
            Step(step_index=0, step_type=StepType.LLM, llm_output="Just text"),
        ]
        assert infer_output_type(steps, None) == OutputType.TEXT

    def test_tool_output_without_indicators_returns_mixed(self):
        steps = [
            Step(
                step_index=0,
                step_type=StepType.TOOL_CALL,
                tool_name="search",
                tool_output="some results here",
            ),
        ]
        assert infer_output_type(steps, None) == OutputType.MIXED


# ---------------------------------------------------------------------------
# infer_terminal_status
# ---------------------------------------------------------------------------


class TestInferTerminalStatus:
    def test_empty_steps_returns_unknown(self):
        assert infer_terminal_status([], None) == TerminalStatus.UNKNOWN

    def test_last_step_error_returns_error(self):
        steps = [
            Step(
                step_index=0,
                step_type=StepType.TOOL_CALL,
                tool_name="fail",
                status=StepStatus.ERROR,
                error_message="Something broke",
            ),
        ]
        assert infer_terminal_status(steps, None) == TerminalStatus.ERROR

    def test_timeout_keyword_returns_timeout(self):
        steps = [
            Step(
                step_index=0,
                step_type=StepType.LLM,
                status=StepStatus.OK,
                error_message="Request timed out after 30s",
            ),
        ]
        assert infer_terminal_status(steps, None) == TerminalStatus.TIMEOUT

    def test_human_escalation_tool_name(self):
        steps = [
            Step(
                step_index=0,
                step_type=StepType.TOOL_CALL,
                tool_name="human_handoff",
            ),
        ]
        assert infer_terminal_status(steps, None) == TerminalStatus.HUMAN_ESCALATION

    def test_escalation_tool_name(self):
        steps = [
            Step(
                step_index=0,
                step_type=StepType.TOOL_CALL,
                tool_name="escalate_to_agent",
            ),
        ]
        assert infer_terminal_status(steps, None) == TerminalStatus.HUMAN_ESCALATION

    def test_no_errors_returns_completed(self):
        steps = [
            Step(step_index=0, step_type=StepType.LLM),
            Step(step_index=1, step_type=StepType.TOOL_CALL, tool_name="search"),
        ]
        assert infer_terminal_status(steps, None) == TerminalStatus.COMPLETED
