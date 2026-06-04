"""Codex adapter: data-completeness gate + engine round-trip."""

from __future__ import annotations

from pathlib import Path

import pytest

from kairos.engine import AnalysisResult, KairosEngine
from kairos.models.enums import StepType, TerminalStatus
from kairos.normalization.agents.codex import CodexNormalizer
from kairos.taxonomy.business_context import BusinessContext

FIXTURE = Path(__file__).parent.parent.parent / "fixtures" / "agents" / "codex_rollout.jsonl"


@pytest.fixture
def envelope():
    return CodexNormalizer().normalize_jsonl(FIXTURE)


def test_provenance(envelope) -> None:
    assert envelope.source == "codex"
    assert envelope.trace_id == "codex-1"
    assert envelope.is_valid is True


def test_step_count(envelope) -> None:
    # 2 assistant turns (LLM) + 2 tool calls + 1 web search (retrieval) = 5.
    assert envelope.step_count == 5
    by_type = {t: [s for s in envelope.steps if s.step_type is t] for t in StepType}
    assert len(by_type[StepType.LLM]) == 2
    assert len(by_type[StepType.TOOL_CALL]) == 2
    assert len(by_type[StepType.RETRIEVAL]) == 1


def test_user_input_and_terminal(envelope) -> None:
    assert envelope.user_input == "audit the repo and patch the bug"
    assert envelope.terminal_status is TerminalStatus.COMPLETED


def test_tool_sequence(envelope) -> None:
    assert envelope.tool_sequence == ["exec_command", "apply_patch"]


def test_function_call_args_round_trip(envelope) -> None:
    exec_step = next(s for s in envelope.steps if s.tool_name == "exec_command")
    # arguments JSON string parsed back to a dict, verbatim.
    assert exec_step.tool_args == {"cmd": "pwd", "workdir": "/repo"}
    assert exec_step.tool_output is not None
    assert "/repo" in exec_step.tool_output


def test_custom_tool_call_preserves_freeform_input(envelope) -> None:
    patch_step = next(s for s in envelope.steps if s.tool_name == "apply_patch")
    # apply_patch input is not JSON — kept under "input" verbatim.
    assert patch_step.tool_args["input"].startswith("*** Begin Patch")
    assert patch_step.tool_output is not None
    assert "Success" in patch_step.tool_output


def test_web_search_retrieval(envelope) -> None:
    retrieval = next(s for s in envelope.steps if s.step_type is StepType.RETRIEVAL)
    assert retrieval.retrieval_query == "python argparse subcommands"


def test_model_captured(envelope) -> None:
    llm = next(s for s in envelope.steps if s.step_type is StepType.LLM)
    assert llm.llm_model == "gpt-5-codex"


def test_timing(envelope) -> None:
    exec_step = next(s for s in envelope.steps if s.tool_name == "exec_command")
    # call ts 01.500 → output ts 01.800 = 300 ms.
    assert exec_step.latency_ms == 300


def test_flows_through_engine(envelope) -> None:
    context = BusinessContext.from_dict(
        {
            "agent_name": "codex",
            "agent_description": "coding agent",
            "operations": [
                {
                    "name": "patch_bug",
                    "description": "patch a bug",
                    "expected_tools": ["exec_command", "apply_patch"],
                    "required_side_effect_tools": ["apply_patch"],
                }
            ],
        }
    )
    result = KairosEngine().analyze([envelope], context)
    assert isinstance(result, AnalysisResult)
