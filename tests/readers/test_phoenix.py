"""Tests for the Phoenix reader.

PhoenixReader pulls OTel-shaped spans from a Phoenix server, converts
each span via genai_mapping, and produces a TraceEnvelope. Tests use a
fake client (matching the shape Phoenix's Python client returns) so
they don't require a live Phoenix instance.

Phoenix span dict shape (from arize-phoenix-client v2.x):

    {
        "id":             "<phoenix internal id>",
        "name":           "openai.chat",
        "context":        {"trace_id": "<32-hex>", "span_id": "<16-hex>"},
        "parent_id":      "<16-hex or None>",
        "span_kind":      "LLM" | "TOOL" | ... | "UNKNOWN",
        "start_time":     "<ISO 8601>",
        "end_time":       "<ISO 8601>",
        "status_code":    "OK" | "ERROR" | "UNSET",
        "status_message": "...",
        "attributes":     {...},
        "events":         [{"name": "...", "timestamp": "...", "attributes": {...}}],
    }
"""

from __future__ import annotations

from typing import Any

import pytest  # noqa: TC002

from kairos.models.enums import StepType, TerminalStatus
from kairos.readers.phoenix import PhoenixReader, spans_to_envelope


def _phoenix_span(
    *,
    name: str,
    trace_id: str = "0123456789abcdef0123456789abcdef",
    span_id: str = "1111111111111111",
    parent_id: str | None = None,
    attributes: dict[str, Any] | None = None,
    start_time: str = "2026-05-07T12:00:00.000000+00:00",
    end_time: str = "2026-05-07T12:00:01.000000+00:00",
    status_code: str = "UNSET",
    status_message: str = "",
    events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": f"phoenix-{span_id}",
        "name": name,
        "context": {"trace_id": trace_id, "span_id": span_id},
        "parent_id": parent_id,
        "span_kind": "UNKNOWN",
        "start_time": start_time,
        "end_time": end_time,
        "status_code": status_code,
        "status_message": status_message,
        "attributes": attributes or {},
        "events": events or [],
    }


# ─────────────────────────── spans_to_envelope ────────────────────────────


def test_spans_to_envelope_with_task_root_and_llm_child() -> None:
    spans = [
        _phoenix_span(
            name="kairos.task",
            span_id="1111111111111111",
            attributes={
                "kairos.agent.name": "tau_agent",
                "kairos.business_op": "tau_retail",
                "kairos.user_input": "place an order",
            },
        ),
        _phoenix_span(
            name="openai.chat",
            span_id="2222222222222222",
            parent_id="1111111111111111",
            start_time="2026-05-07T12:00:00.500000+00:00",
            end_time="2026-05-07T12:00:00.900000+00:00",
            attributes={
                "gen_ai.system": "openai",
                "gen_ai.request.model": "gpt-4o-mini",
                "gen_ai.usage.input_tokens": 100,
                "gen_ai.usage.output_tokens": 20,
                "gen_ai.usage.total_tokens": 120,
                "gen_ai.prompt.0.role": "user",
                "gen_ai.prompt.0.content": "place an order",
                "gen_ai.completion.0.content": "ok, what item?",
            },
        ),
    ]

    env = spans_to_envelope(spans)
    assert env.trace_id == "0123456789abcdef0123456789abcdef"
    assert env.is_valid is True
    assert env.agent_type == "tau_agent"
    assert env.user_input == "place an order"
    assert env.terminal_status is TerminalStatus.COMPLETED
    assert env.step_count == 1
    assert env.steps[0].step_type is StepType.LLM
    assert env.steps[0].llm_model == "gpt-4o-mini"
    assert env.steps[0].input_tokens == 100


def test_spans_to_envelope_orders_by_start_time_regardless_of_input_order() -> None:
    spans = [
        _phoenix_span(
            name="openai.chat",
            span_id="2222222222222222",
            parent_id="1111111111111111",
            start_time="2026-05-07T12:00:00.500000+00:00",
            attributes={"gen_ai.system": "openai", "gen_ai.request.model": "m"},
        ),
        _phoenix_span(
            name="kairos.task",
            span_id="1111111111111111",
            start_time="2026-05-07T12:00:00.000000+00:00",
            attributes={"kairos.agent.name": "agent"},
        ),
        _phoenix_span(
            name="tool.foo",
            span_id="3333333333333333",
            parent_id="2222222222222222",
            start_time="2026-05-07T12:00:00.700000+00:00",
            attributes={"gen_ai.tool.name": "foo"},
        ),
    ]
    env = spans_to_envelope(spans)
    # Steps: LLM then TOOL_CALL.
    assert [s.step_type for s in env.steps] == [StepType.LLM, StepType.TOOL_CALL]


def test_spans_to_envelope_with_error_status_marks_envelope_error() -> None:
    spans = [
        _phoenix_span(
            name="kairos.task",
            span_id="1111111111111111",
            attributes={"kairos.agent.name": "agent"},
            status_code="ERROR",
        ),
        _phoenix_span(
            name="openai.chat",
            span_id="2222222222222222",
            parent_id="1111111111111111",
            attributes={"gen_ai.system": "openai", "gen_ai.request.model": "m"},
            status_code="ERROR",
            events=[
                {
                    "name": "exception",
                    "timestamp": "2026-05-07T12:00:00.500000+00:00",
                    "attributes": {"exception.message": "rate limited"},
                }
            ],
        ),
    ]
    env = spans_to_envelope(spans)
    assert env.terminal_status is TerminalStatus.ERROR
    assert env.steps[0].error_message == "rate limited"


def test_spans_to_envelope_without_task_root_synthesizes_boundaries() -> None:
    """No kairos.task span — should still produce a usable envelope from the LLM span alone."""
    spans = [
        _phoenix_span(
            name="openai.chat",
            span_id="2222222222222222",
            attributes={"gen_ai.system": "openai", "gen_ai.request.model": "m"},
        ),
    ]
    env = spans_to_envelope(spans)
    # is_valid=False because no TraceStart was synthesized — caller can still
    # see the step.
    assert env.step_count == 1
    assert env.steps[0].step_type is StepType.LLM


def test_spans_to_envelope_empty_returns_invalid() -> None:
    env = spans_to_envelope([])
    assert env.is_valid is False
    assert env.steps == []


def test_spans_to_envelope_tool_parent_resolves_to_step_index() -> None:
    spans = [
        _phoenix_span(
            name="kairos.task",
            span_id="1111111111111111",
            attributes={"kairos.agent.name": "agent"},
        ),
        _phoenix_span(
            name="openai.chat",
            span_id="2222222222222222",
            parent_id="1111111111111111",
            start_time="2026-05-07T12:00:00.100000+00:00",
            attributes={"gen_ai.system": "openai", "gen_ai.request.model": "m"},
        ),
        _phoenix_span(
            name="tool.fetch",
            span_id="3333333333333333",
            parent_id="2222222222222222",  # tool's parent is the LLM span
            start_time="2026-05-07T12:00:00.200000+00:00",
            attributes={"gen_ai.tool.name": "fetch"},
        ),
    ]
    env = spans_to_envelope(spans)
    # 2 steps: LLM (step 0), TOOL (step 1, parent_step_index=0)
    assert env.step_count == 2
    assert env.steps[1].parent_step_index == 0


# ───────────────────────────── PhoenixReader ──────────────────────────────


class _FakePhoenixSpansAccessor:
    """Mimics phoenix.client.Client().spans for unit tests."""

    def __init__(self, by_trace_id: dict[str, list[dict[str, Any]]]) -> None:
        self._by_trace = by_trace_id
        self.last_call: dict[str, Any] | None = None

    def get_spans(  # noqa: D401 — matches phoenix client API
        self,
        *,
        project_identifier: str,
        trace_ids: list[str] | None = None,
        limit: int = 100,
        **_: Any,
    ) -> list[dict[str, Any]]:
        self.last_call = {"project_identifier": project_identifier, "trace_ids": trace_ids, "limit": limit}
        if not trace_ids:
            return []
        out: list[dict[str, Any]] = []
        for tid in trace_ids:
            out.extend(self._by_trace.get(tid, []))
        return out


class _FakePhoenixClient:
    def __init__(self, by_trace_id: dict[str, list[dict[str, Any]]]) -> None:
        self.spans = _FakePhoenixSpansAccessor(by_trace_id)


def test_phoenix_reader_fetch_envelope_calls_client_with_trace_id() -> None:
    spans = [
        _phoenix_span(name="kairos.task", attributes={"kairos.agent.name": "agent"}),
    ]
    client = _FakePhoenixClient({"0123456789abcdef0123456789abcdef": spans})
    reader = PhoenixReader(client=client, project="default")  # type: ignore[arg-type]

    env = reader.fetch_envelope("0123456789abcdef0123456789abcdef")
    assert env.is_valid is True
    assert env.trace_id == "0123456789abcdef0123456789abcdef"
    assert client.spans.last_call == {
        "project_identifier": "default",
        "trace_ids": ["0123456789abcdef0123456789abcdef"],
        "limit": 1000,
    }


def test_phoenix_reader_fails_loud_when_span_limit_hit() -> None:
    # At the fetch limit we cannot tell a complete trace from a truncated one,
    # so we fail loud instead of analyzing a silently clipped trace.
    spans = [_phoenix_span(name="kairos.task", attributes={"kairos.agent.name": "agent"})]
    client = _FakePhoenixClient({"trunc": spans})
    reader = PhoenixReader(client=client, project="default", span_limit=1)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="may be truncated"):
        reader.fetch_envelope("trunc")


def test_phoenix_reader_custom_span_limit_forwarded() -> None:
    client = _FakePhoenixClient({})
    reader = PhoenixReader(client=client, span_limit=5000)  # type: ignore[arg-type]
    reader.fetch_envelope("anything")
    assert client.spans.last_call is not None
    assert client.spans.last_call["limit"] == 5000


def test_phoenix_reader_unknown_trace_returns_invalid_envelope() -> None:
    client = _FakePhoenixClient({})
    reader = PhoenixReader(client=client, project="default")  # type: ignore[arg-type]

    env = reader.fetch_envelope("does-not-exist")
    assert env.is_valid is False
    assert env.steps == []


def test_phoenix_reader_default_project_is_default() -> None:
    """If project not specified, reader uses 'default' (Phoenix's default project)."""
    client = _FakePhoenixClient({})
    reader = PhoenixReader(client=client)  # type: ignore[arg-type]
    reader.fetch_envelope("anything")
    assert client.spans.last_call is not None
    assert client.spans.last_call["project_identifier"] == "default"


def test_phoenix_reader_custom_project() -> None:
    client = _FakePhoenixClient({})
    reader = PhoenixReader(client=client, project="my_project")  # type: ignore[arg-type]
    reader.fetch_envelope("anything")
    assert client.spans.last_call is not None
    assert client.spans.last_call["project_identifier"] == "my_project"


def test_phoenix_reader_endpoint_constructs_default_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """When given an endpoint, PhoenixReader builds a phoenix Client pointing at it."""
    captured: dict[str, str] = {}

    class _StubClient:
        def __init__(self, *, base_url: str | None = None, **_: Any) -> None:
            captured["base_url"] = base_url or ""
            self.spans = _FakePhoenixSpansAccessor({})

    import kairos.readers.phoenix as phx

    monkeypatch.setattr(phx, "Client", _StubClient)
    reader = PhoenixReader(endpoint="http://localhost:6006")
    reader.fetch_envelope("anything")
    assert captured["base_url"] == "http://localhost:6006"
