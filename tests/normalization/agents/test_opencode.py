"""OpenCode adapter: data-completeness gate + engine round-trip."""

from __future__ import annotations

from pathlib import Path

import pytest

from kairos.engine import AnalysisResult, KairosEngine
from kairos.models.enums import StepStatus, StepType, TerminalStatus
from kairos.normalization.agents.opencode import OpenCodeNormalizer
from kairos.taxonomy.business_context import BusinessContext

STORAGE = Path(__file__).parent.parent.parent / "fixtures" / "agents" / "opencode"


@pytest.fixture
def envelope():
    return OpenCodeNormalizer().normalize_session("ses_1", STORAGE)


def test_discover_and_provenance(envelope) -> None:
    assert OpenCodeNormalizer.discover_sessions(STORAGE) == ["ses_1"]
    assert envelope.source == "opencode"
    assert envelope.trace_id == "ses_1"
    assert envelope.is_valid is True


def test_step_count(envelope) -> None:
    # 2 assistant turns (LLM) + 2 tool parts (TOOL_CALL) = 4 steps.
    assert envelope.step_count == 4
    llm = [s for s in envelope.steps if s.step_type is StepType.LLM]
    tools = [s for s in envelope.steps if s.step_type is StepType.TOOL_CALL]
    assert len(llm) == 2
    assert len(tools) == 2


def test_user_input_and_terminal(envelope) -> None:
    assert envelope.user_input == "refactor the parser"
    assert envelope.terminal_status is TerminalStatus.COMPLETED


def test_tool_sequence(envelope) -> None:
    assert envelope.tool_sequence == ["read", "edit"]


def test_tool_args_round_trip(envelope) -> None:
    read = next(s for s in envelope.steps if s.tool_name == "read")
    assert read.tool_args == {"filePath": "/p.py"}
    assert read.tool_output == "def parse(): ..."
    assert read.status is StepStatus.OK
    assert read.latency_ms == 200


def test_tool_error_captured(envelope) -> None:
    edit = next(s for s in envelope.steps if s.tool_name == "edit")
    assert edit.tool_args == {"filePath": "/p.py", "oldString": "a"}
    assert edit.status is StepStatus.ERROR
    assert edit.error_message == "oldString not found"
    assert envelope.error_count == 1


def test_token_accounting(envelope) -> None:
    first = next(s for s in envelope.steps if s.step_type is StepType.LLM)
    # input 200 + cache read 100 + cache write 20 = 320; output 50.
    assert first.input_tokens == 320
    assert first.output_tokens == 50
    assert first.llm_model == "claude-sonnet-4-6"


def test_flows_through_engine(envelope) -> None:
    context = BusinessContext.from_dict(
        {
            "agent_name": "opencode",
            "agent_description": "coding agent",
            "operations": [
                {
                    "name": "edit_file",
                    "description": "edit a source file",
                    "expected_tools": ["read", "edit"],
                    "required_side_effect_tools": ["edit"],
                }
            ],
        }
    )
    result = KairosEngine().analyze([envelope], context)
    assert isinstance(result, AnalysisResult)
