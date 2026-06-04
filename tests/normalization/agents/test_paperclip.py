"""Paperclip adapter: wraps an inner transcript + adds run provenance."""

from __future__ import annotations

from pathlib import Path

import pytest

from kairos.engine import AnalysisResult, KairosEngine
from kairos.models.enums import StepType
from kairos.normalization.agents.paperclip import PaperclipNormalizer
from kairos.taxonomy.tool_catalog import business_context_from_tool_catalog

CC_FIXTURE = Path(__file__).parent.parent.parent / "fixtures" / "agents" / "claude_code_session.jsonl"

RUN = {
    "run_id": "run-abc",
    "issue": "XER-35",
    "company_id": "co-1",
    "agent_id": "agent-1",
}


@pytest.fixture
def envelope():
    return PaperclipNormalizer(run_context=RUN).normalize_jsonl(CC_FIXTURE)


def test_source_and_trace_id(envelope) -> None:
    assert envelope.source == "paperclip"
    # trace_id becomes the Paperclip run id, not the raw session id.
    assert envelope.trace_id == "run-abc"
    assert envelope.is_valid is True


def test_run_provenance_in_metadata(envelope) -> None:
    assert envelope.metadata is not None
    assert envelope.metadata["paperclip"] == RUN


def test_wraps_inner_transcript_completely(envelope) -> None:
    # Same shape as the underlying Claude Code transcript: 3 LLM + 2 tools.
    assert envelope.step_count == 5
    assert envelope.tool_sequence == ["Bash", "Read"]
    bash = next(s for s in envelope.steps if s.tool_name == "Bash")
    assert bash.tool_args == {"command": "ls -la", "description": "List files"}


def test_without_run_context_keeps_session_id() -> None:
    env = PaperclipNormalizer().normalize_jsonl(CC_FIXTURE)
    assert env.source == "paperclip"
    assert env.trace_id == "sess-cc-1"


def test_flows_through_engine_with_derived_context(envelope) -> None:
    context = business_context_from_tool_catalog(
        agent_name="paperclip-coder",
        agent_description="Paperclip coding agent",
        tools=["Bash", "Read", "Edit", "mcp__paperclip__create_issue"],
    )
    result = KairosEngine().analyze([envelope], context)
    assert isinstance(result, AnalysisResult)
    assert any(s.step_type is StepType.TOOL_CALL for s in envelope.steps)
