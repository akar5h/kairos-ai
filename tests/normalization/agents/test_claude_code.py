"""Claude Code adapter: data-completeness gate + engine round-trip."""

from __future__ import annotations

from pathlib import Path

import pytest

from kairos.engine import AnalysisResult, KairosEngine
from kairos.models.enums import StepStatus, StepType, TerminalStatus
from kairos.normalization.agents.claude_code import ClaudeCodeNormalizer
from kairos.taxonomy.business_context import BusinessContext

FIXTURE = Path(__file__).parent.parent.parent / "fixtures" / "agents" / "claude_code_session.jsonl"


@pytest.fixture
def envelope():
    return ClaudeCodeNormalizer().normalize_jsonl(FIXTURE)


def test_provenance(envelope) -> None:
    assert envelope.source == "claude_code"
    assert envelope.source_trace_id == "sess-cc-1"
    assert envelope.trace_id == "sess-cc-1"
    assert envelope.is_valid is True


def test_step_count(envelope) -> None:
    # 3 assistant turns (LLM) + 2 tool_use blocks (TOOL_CALL) = 5 steps.
    assert envelope.step_count == 5
    llm = [s for s in envelope.steps if s.step_type is StepType.LLM]
    tools = [s for s in envelope.steps if s.step_type is StepType.TOOL_CALL]
    assert len(llm) == 3
    assert len(tools) == 2


def test_user_input_and_terminal(envelope) -> None:
    assert envelope.user_input == "list the repo then read a missing file"
    assert envelope.terminal_status is TerminalStatus.COMPLETED


def test_tool_sequence(envelope) -> None:
    assert envelope.tool_sequence == ["Bash", "Read"]


def test_tool_args_round_trip(envelope) -> None:
    bash = next(s for s in envelope.steps if s.tool_name == "Bash")
    # Full args preserved verbatim (the board's tool-args round-trip gate).
    assert bash.tool_args == {"command": "ls -la", "description": "List files"}
    assert bash.tool_output is not None
    assert "file.txt" in bash.tool_output
    assert bash.status is StepStatus.OK
    # Normalized args are present for the engine's Jaccard comparisons.
    assert bash.tool_args_normalized is not None


def test_tool_error_captured(envelope) -> None:
    read = next(s for s in envelope.steps if s.tool_name == "Read")
    assert read.tool_args == {"file_path": "/repo/missing.txt", "limit": 10}
    assert read.status is StepStatus.ERROR
    assert read.error_message is not None
    assert "ENOENT" in read.error_message
    assert envelope.error_count == 1


def test_token_accounting(envelope) -> None:
    # Input includes cache tokens: turn1 = 100+50+10, turn2 = 120, turn3 = 130.
    assert envelope.total_input_tokens == 160 + 120 + 130
    assert envelope.total_output_tokens == 20 + 15 + 25
    first_llm = next(s for s in envelope.steps if s.step_type is StepType.LLM)
    assert first_llm.input_tokens == 160
    assert first_llm.output_tokens == 20


def test_parent_linking(envelope) -> None:
    # Each tool call's parent_step_index points at its emitting LLM turn.
    bash = next(s for s in envelope.steps if s.tool_name == "Bash")
    parent = envelope.steps[bash.parent_step_index]
    assert parent.step_type is StepType.LLM


def test_timing_populated(envelope) -> None:
    bash = next(s for s in envelope.steps if s.tool_name == "Bash")
    # assistant ts 10:00:01 → result ts 10:00:02 = 1000 ms.
    assert bash.latency_ms == 1000


def test_flows_through_engine(envelope) -> None:
    context = BusinessContext.from_dict(
        {
            "agent_name": "claude_code",
            "agent_description": "coding agent",
            "operations": [
                {
                    "name": "repo_inspection",
                    "description": "inspect a repository",
                    "expected_tools": ["Bash", "Read"],
                    "required_side_effect_tools": ["Bash"],
                }
            ],
        }
    )
    result = KairosEngine().analyze([envelope], context)
    assert isinstance(result, AnalysisResult)


def test_real_session_smoke() -> None:
    """If real Claude Code transcripts exist on this host, they must parse."""
    sessions = ClaudeCodeNormalizer.discover_sessions()
    if not sessions:
        pytest.skip("no local Claude Code sessions")
    env = ClaudeCodeNormalizer().normalize_jsonl(sessions[0])
    assert env.source == "claude_code"
    assert env.step_count >= 0
