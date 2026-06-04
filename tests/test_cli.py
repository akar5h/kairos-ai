"""CLI tests — `kairos analyze` offline path + explicit-source guard."""

from __future__ import annotations

import json
from typing import Any

from click.testing import CliRunner

from kairos.cli import cli
from kairos.models.enums import StepStatus, StepType, TerminalStatus
from kairos.models.trace import Step, TraceEnvelope
from kairos.store.json_store import JSONStore

_CONTEXT_YAML = """
agent_name: order_agent
agent_description: Places customer orders.
operations:
  - name: place_order
    description: Place a single customer order end-to-end.
    expected_tools: [submit_order]
    required_side_effect_tools: [submit_order]
    priority: high
"""


def _seed_offline_trace(directory: Any) -> None:
    JSONStore(directory).save(
        TraceEnvelope(
            trace_id="abc123",
            user_input="order a widget",
            agent_type="order_agent",
            terminal_status=TerminalStatus.COMPLETED,
            steps=[
                Step(step_index=0, step_type=StepType.LLM, llm_model="m", input_tokens=10, status=StepStatus.OK),
                Step(
                    step_index=1,
                    step_type=StepType.TOOL_CALL,
                    tool_name="submit_order",
                    tool_args={"item": "widget"},
                    status=StepStatus.OK,
                ),
            ],
        )
    )


def test_analyze_offline_dir_emits_analysis_result_json(tmp_path: Any) -> None:
    traces_dir = tmp_path / "traces"
    traces_dir.mkdir()
    _seed_offline_trace(traces_dir)
    ctx = tmp_path / "ctx.yaml"
    ctx.write_text(_CONTEXT_YAML)
    out = tmp_path / "result.json"

    result = CliRunner().invoke(
        cli,
        ["analyze", "--normalized-dir", str(traces_dir), "--context", str(ctx), "--output", str(out)],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(out.read_text())
    assert "workflows" in payload
    assert payload["evidence_coverage"]["total_traces"] == 1
    assert any(w["operation_name"] == "place_order" for w in payload["workflows"])


def test_analyze_requires_exactly_one_source(tmp_path: Any) -> None:
    ctx = tmp_path / "ctx.yaml"
    ctx.write_text(_CONTEXT_YAML)

    # Neither source → usage error.
    result = CliRunner().invoke(cli, ["analyze", "--context", str(ctx)])
    assert result.exit_code != 0
    assert "exactly one source" in result.output
