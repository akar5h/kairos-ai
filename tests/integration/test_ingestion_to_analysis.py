"""Board-mandated integration test: ingestion proves out end-to-end.

A Phoenix-sourced trace AND an offline (JSONStore) export are each normalized
to the one IR — with tool calls (+args), tokens, and status populated — then
fed through ``KairosEngine.analyze`` to produce an ``AnalysisResult``. One path,
both sources, no fallback.
"""

from __future__ import annotations

from typing import Any

import pytest

from kairos.engine.pipeline import AnalysisResult, KairosEngine
from kairos.models.enums import StepStatus, StepType, TerminalStatus
from kairos.models.trace import Step, TraceEnvelope
from kairos.readers.phoenix import spans_to_envelope
from kairos.store.json_store import JSONStore
from kairos.taxonomy.business_context import BusinessContext, BusinessOperation

TOOL = "submit_order"
TRACE_ID_PHOENIX = "0123456789abcdef0123456789abcdef"
TRACE_ID_OFFLINE = "fedcba9876543210fedcba9876543210"


def _phoenix_span(
    *,
    name: str,
    span_id: str,
    parent_id: str | None = None,
    attributes: dict[str, Any] | None = None,
    start_time: str = "2026-05-07T12:00:00.000000+00:00",
    end_time: str = "2026-05-07T12:00:01.000000+00:00",
    status_code: str = "OK",
) -> dict[str, Any]:
    return {
        "id": f"phoenix-{span_id}",
        "name": name,
        "context": {"trace_id": TRACE_ID_PHOENIX, "span_id": span_id},
        "parent_id": parent_id,
        "span_kind": "UNKNOWN",
        "start_time": start_time,
        "end_time": end_time,
        "status_code": status_code,
        "status_message": "",
        "attributes": attributes or {},
        "events": [],
    }


def _phoenix_envelope() -> TraceEnvelope:
    """Live source: OTel spans pulled from Phoenix → IR."""
    spans = [
        _phoenix_span(
            name="kairos.task",
            span_id="1111111111111111",
            attributes={"kairos.agent.name": "order_agent", "kairos.user_input": "place an order for a widget"},
        ),
        _phoenix_span(
            name="openai.chat",
            span_id="2222222222222222",
            parent_id="1111111111111111",
            start_time="2026-05-07T12:00:00.300000+00:00",
            attributes={
                "gen_ai.system": "openai",
                "gen_ai.request.model": "gpt-4o-mini",
                "gen_ai.usage.input_tokens": 120,
                "gen_ai.usage.output_tokens": 30,
                "gen_ai.usage.total_tokens": 150,
            },
        ),
        _phoenix_span(
            name="tool.submit_order",
            span_id="3333333333333333",
            parent_id="1111111111111111",
            start_time="2026-05-07T12:00:00.600000+00:00",
            attributes={
                "tool.name": TOOL,
                "tool.parameters": {"item": "widget", "qty": 1},
                "output.value": "order #42 confirmed",
            },
        ),
    ]
    return spans_to_envelope(spans)


def _offline_envelope() -> TraceEnvelope:
    """Offline source: a normalized IR envelope as written by the SDK."""
    return TraceEnvelope(
        trace_id=TRACE_ID_OFFLINE,
        source="langfuse",
        user_input="place an order for a gadget",
        agent_type="order_agent",
        terminal_status=TerminalStatus.COMPLETED,
        steps=[
            Step(
                step_index=0,
                step_type=StepType.LLM,
                llm_model="gpt-4o-mini",
                input_tokens=90,
                output_tokens=25,
                total_tokens=115,
                status=StepStatus.OK,
            ),
            Step(
                step_index=1,
                step_type=StepType.TOOL_CALL,
                tool_name=TOOL,
                tool_args={"item": "gadget", "qty": 2},
                tool_output="order #43 confirmed",
                status=StepStatus.OK,
            ),
        ],
    )


def _context() -> BusinessContext:
    return BusinessContext(
        agent_name="order_agent",
        agent_description="Places customer orders.",
        operations=[
            BusinessOperation(
                name="place_order",
                description="Place a single customer order end-to-end.",
                expected_tools=[TOOL],
                required_side_effect_tools=[TOOL],
                priority="high",
            )
        ],
    )


def _assert_ir_populated(env: TraceEnvelope) -> None:
    """The IR carries tool calls (+args), tokens, and status — the ingestion contract."""
    tool_steps = [s for s in env.steps if s.step_type == StepType.TOOL_CALL]
    assert tool_steps, f"{env.trace_id}: no tool-call step"
    submit = next(s for s in tool_steps if s.tool_name == TOOL)
    assert submit.tool_args, f"{env.trace_id}: tool args not populated"
    assert submit.status == StepStatus.OK

    llm_steps = [s for s in env.steps if s.step_type == StepType.LLM]
    assert llm_steps, f"{env.trace_id}: no LLM step"
    assert any(s.input_tokens for s in llm_steps), f"{env.trace_id}: tokens not populated"

    assert TOOL in env.tool_sequence
    assert env.terminal_status is not TerminalStatus.UNKNOWN


@pytest.mark.integration
def test_phoenix_and_offline_sources_produce_analysis_result(tmp_path: Any) -> None:
    phoenix_env = _phoenix_envelope()

    # Offline export round-trips through the on-disk store, proving the offline path.
    store = JSONStore(tmp_path)
    store.save(_offline_envelope())
    offline_env = store.load(TRACE_ID_OFFLINE)
    assert offline_env is not None

    # Both sources land on the same IR with the ingestion fields populated.
    _assert_ir_populated(phoenix_env)
    _assert_ir_populated(offline_env)

    result = KairosEngine().analyze([phoenix_env, offline_env], _context())

    assert isinstance(result, AnalysisResult)
    assert result.evidence_coverage.total_traces == 2
    assert result.llm_used is False

    place_order = next(w for w in result.workflows if w.operation_name == "place_order")
    assert place_order.full_trace_count + place_order.attempted_trace_count == 2
