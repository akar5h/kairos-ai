"""Claude Code adapter: data-completeness gate + engine round-trip."""

from __future__ import annotations

from pathlib import Path

import pytest

from kairos.engine import AnalysisResult, KairosEngine
from kairos.models.enums import StepStatus, StepStatusSource, StepType, TerminalStatus
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


# ───────────── Rung 3 wiring: adapter extractor on the transcript path ─────────────
#
# Day 3 review fix: AgentTranscriptNormalizer.normalize() applies
# apply_step_outcomes() so the adapter extractor (rung 3) actually runs on
# transcript-sourced envelopes (tau-bench / Day 6 path — no `success` attr).


def _records_with_tool_result(content: str, *, is_error: bool = False) -> list[dict]:
    """Minimal transcript: prompt → assistant tool_use → tool_result → final text."""
    return [
        {
            "type": "user",
            "sessionId": "sess-rung3",
            "uuid": "u1",
            "timestamp": "2026-06-04T10:00:00.000Z",
            "message": {"role": "user", "content": "do the thing"},
        },
        {
            "type": "assistant",
            "sessionId": "sess-rung3",
            "uuid": "a1",
            "parentUuid": "u1",
            "timestamp": "2026-06-04T10:00:01.000Z",
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-8",
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "content": [{"type": "tool_use", "id": "toolu_x", "name": "Read", "input": {"file_path": "/x"}}],
            },
        },
        {
            "type": "user",
            "sessionId": "sess-rung3",
            "uuid": "u2",
            "parentUuid": "a1",
            "timestamp": "2026-06-04T10:00:02.000Z",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_x", "is_error": is_error, "content": content}
                ],
            },
        },
        {
            "type": "assistant",
            "sessionId": "sess-rung3",
            "uuid": "a2",
            "parentUuid": "u2",
            "timestamp": "2026-06-04T10:00:03.000Z",
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-8",
                "usage": {"input_tokens": 12, "output_tokens": 6},
                "content": [{"type": "text", "text": "done"}],
            },
        },
    ]


def test_rung3_harness_prefix_sets_error_with_adapter_source() -> None:
    """Review test 1: transcript step, output 'Error: ENOENT...', no status info
    (is_error=False → OK, status_source=NONE) → adapter flips to ERROR/ADAPTER."""
    env = ClaudeCodeNormalizer().normalize(_records_with_tool_result("Error: ENOENT no such file"))
    step = next(s for s in env.steps if s.tool_name == "Read")
    assert step.status is StepStatus.ERROR
    assert step.status_source is StepStatusSource.ADAPTER
    # error_count was recomputed after the rung-3 pass.
    assert env.error_count == 1


def test_rung3_no_opinion_leaves_status_source_none() -> None:
    """Clean output, no signals → adapter has no opinion → NONE stays (rung 4 eligible)."""
    env = ClaudeCodeNormalizer().normalize(_records_with_tool_result("file contents here"))
    step = next(s for s in env.steps if s.tool_name == "Read")
    assert step.status is StepStatus.OK
    assert step.status_source is StepStatusSource.NONE


def test_rung3_failure_text_mid_output_not_flipped_by_adapter() -> None:
    """'failed' mid-output is NOT a harness prefix → adapter silent → NONE stays.
    Rung 4 (outcome_metric) remains the only tier allowed to judge this text."""
    env = ClaudeCodeNormalizer().normalize(_records_with_tool_result("deploy failed: connection refused"))
    step = next(s for s in env.steps if s.tool_name == "Read")
    assert step.status is StepStatus.OK
    assert step.status_source is StepStatusSource.NONE


def test_rung3_does_not_override_is_error_verdict() -> None:
    """is_error=True already set ERROR; adapter pass must not weaken it."""
    env = ClaudeCodeNormalizer().normalize(
        _records_with_tool_result("ENOENT: no such file or directory", is_error=True)
    )
    step = next(s for s in env.steps if s.tool_name == "Read")
    assert step.status is StepStatus.ERROR
