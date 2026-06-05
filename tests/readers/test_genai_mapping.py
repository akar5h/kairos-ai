"""Pure-function tests for OTel span → Kairos event mapping.

These tests use lightweight fake span objects (matching the surface area
of ``opentelemetry.sdk.trace.ReadableSpan``) so the mapping module can
be exercised without spinning up the OTel SDK. The exporter (Agent B's
file) is responsible for feeding real ReadableSpans through these
functions in production.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from opentelemetry.trace import StatusCode

from kairos.models.enums import OutputType, StepStatus, TerminalStatus
from kairos.normalization.events import LLMCall, Retrieval, ToolCall, TraceEnd, TraceStart
from kairos.readers.genai_mapping import (
    classify_span,
    span_to_llm_call,
    span_to_retrieval,
    span_to_tool_call,
    span_to_trace_end,
    span_to_trace_start,
)
from kairos.readers.phoenix import _phoenix_dict_to_span

# ───────────────────────── Fake ReadableSpan ─────────────────────────


@dataclass
class FakeContext:
    trace_id: int = 0x0123456789ABCDEF0123456789ABCDEF
    span_id: int = 0xFEDCBA9876543210


@dataclass
class FakeParent:
    span_id: int


@dataclass
class FakeSpanEvent:
    name: str
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass
class FakeSpanStatus:
    status_code: Any = StatusCode.UNSET
    description: str | None = None


class _FakeResource:
    def __init__(self, attrs: dict[str, Any]) -> None:
        self.attributes = attrs


@dataclass
class FakeSpan:
    name: str = "span"
    attributes: dict[str, Any] = field(default_factory=dict)
    resource_attributes: dict[str, Any] = field(default_factory=dict)
    events: list[FakeSpanEvent] = field(default_factory=list)
    start_time: int = 1_700_000_000_000_000_000  # ns
    end_time: int = 1_700_000_001_000_000_000
    context: FakeContext = field(default_factory=FakeContext)
    parent: FakeParent | None = None
    status: FakeSpanStatus = field(default_factory=FakeSpanStatus)

    @property
    def resource(self) -> _FakeResource:
        return _FakeResource(self.resource_attributes)


# ───────────────────────── Classifier tests ──────────────────────────


def test_classify_kairos_task_by_name() -> None:
    span = FakeSpan(name="kairos.task")
    assert classify_span(span) == "task"


def test_classify_kairos_task_by_attribute() -> None:
    span = FakeSpan(name="some.root", attributes={"kairos.span.kind": "task"})
    assert classify_span(span) == "task"


def test_classify_llm_by_gen_ai_system() -> None:
    span = FakeSpan(name="openai.chat", attributes={"gen_ai.system": "openai"})
    assert classify_span(span) == "llm"


def test_classify_tool_by_gen_ai_tool_name() -> None:
    span = FakeSpan(name="execute", attributes={"gen_ai.tool.name": "fetch_rubric"})
    assert classify_span(span) == "tool"


def test_classify_tool_by_operation_execute_tool() -> None:
    span = FakeSpan(name="x", attributes={"gen_ai.operation.name": "execute_tool"})
    assert classify_span(span) == "tool"


def test_classify_tool_by_traceloop_entity() -> None:
    span = FakeSpan(name="x", attributes={"traceloop.entity.name": "tool"})
    assert classify_span(span) == "tool"


def test_classify_tool_by_span_name_prefix() -> None:
    span = FakeSpan(name="tool.fetch_rubric")
    assert classify_span(span) == "tool"


def test_classify_retrieval_by_db_system() -> None:
    span = FakeSpan(name="pinecone.query", attributes={"db.system": "pinecone"})
    assert classify_span(span) == "retrieval"


def test_classify_retrieval_by_embedding_op() -> None:
    span = FakeSpan(name="x", attributes={"gen_ai.operation.name": "embedding"})
    assert classify_span(span) == "retrieval"


def test_classify_retrieval_by_traceloop_entity() -> None:
    span = FakeSpan(name="x", attributes={"traceloop.entity.name": "retrieval"})
    assert classify_span(span) == "retrieval"


def test_classify_other_fallback() -> None:
    span = FakeSpan(name="random.op", attributes={"foo": "bar"})
    assert classify_span(span) == "other"


def test_classify_task_takes_priority_over_llm() -> None:
    # If a span carries both task marker AND gen_ai.system (unusual but
    # well-defined), task wins because it's the host-driven boundary.
    span = FakeSpan(
        name="kairos.task",
        attributes={"gen_ai.system": "openai"},
    )
    assert classify_span(span) == "task"


# ───────────────────────── LLM mapping tests ─────────────────────────


def test_llm_extracts_model_provider_tokens() -> None:
    span = FakeSpan(
        name="openai.chat",
        attributes={
            "gen_ai.system": "openai",
            "gen_ai.request.model": "gpt-4o-mini",
            "gen_ai.request.temperature": 0.7,
            "gen_ai.usage.input_tokens": 120,
            "gen_ai.usage.output_tokens": 45,
            "gen_ai.usage.total_tokens": 165,
        },
        status=FakeSpanStatus(status_code=StatusCode.OK),
    )
    call = span_to_llm_call(span, step_index=3)
    assert isinstance(call, LLMCall)
    assert call.model == "gpt-4o-mini"
    assert call.provider == "openai"
    assert call.temperature == 0.7
    assert call.input_tokens == 120
    assert call.output_tokens == 45
    assert call.total_tokens == 165
    assert call.step_index == 3
    assert call.status == StepStatus.OK


def test_llm_messages_in_sorted_by_index() -> None:
    span = FakeSpan(
        attributes={
            "gen_ai.system": "anthropic",
            "gen_ai.request.model": "claude-3-5-sonnet",
            "gen_ai.prompt.0.role": "system",
            "gen_ai.prompt.0.content": "you are helpful",
            "gen_ai.prompt.2.role": "user",
            "gen_ai.prompt.2.content": "hi",
            "gen_ai.prompt.1.role": "assistant",
            "gen_ai.prompt.1.content": "previous answer",
        },
    )
    call = span_to_llm_call(span, step_index=0)
    assert call is not None
    roles = [m.role for m in call.messages_in]
    contents = [m.content for m in call.messages_in]
    assert roles == ["system", "assistant", "user"]
    assert contents == ["you are helpful", "previous answer", "hi"]


def test_llm_legacy_token_naming() -> None:
    span = FakeSpan(
        attributes={
            "gen_ai.system": "openai",
            "gen_ai.request.model": "gpt-3.5",
            "gen_ai.usage.prompt_tokens": 10,
            "gen_ai.usage.completion_tokens": 20,
        },
    )
    call = span_to_llm_call(span, step_index=0)
    assert call is not None
    assert call.input_tokens == 10
    assert call.output_tokens == 20


def test_llm_tool_calls_emitted_parsed() -> None:
    span = FakeSpan(
        attributes={
            "gen_ai.system": "openai",
            "gen_ai.request.model": "gpt-4o",
            "gen_ai.completion.0.content": "calling tool",
            "gen_ai.completion.0.tool_calls.0.id": "call_abc",
            "gen_ai.completion.0.tool_calls.0.name": "fetch_rubric",
            "gen_ai.completion.0.tool_calls.0.arguments": json.dumps({"id": 7}),
            "gen_ai.completion.0.tool_calls.1.id": "call_def",
            "gen_ai.completion.0.tool_calls.1.name": "lookup",
            "gen_ai.completion.0.tool_calls.1.arguments": json.dumps({"q": "x"}),
        },
    )
    call = span_to_llm_call(span, step_index=0)
    assert call is not None
    assert call.content_out == "calling tool"
    assert len(call.tool_calls_emitted) == 2
    assert call.tool_calls_emitted[0].id == "call_abc"
    assert call.tool_calls_emitted[0].name == "fetch_rubric"
    assert call.tool_calls_emitted[0].args == {"id": 7}
    assert call.tool_calls_emitted[1].args == {"q": "x"}


def test_llm_malformed_tool_call_arguments_fallback_raw() -> None:
    span = FakeSpan(
        attributes={
            "gen_ai.system": "openai",
            "gen_ai.request.model": "gpt-4o",
            "gen_ai.completion.0.tool_calls.0.id": "call_1",
            "gen_ai.completion.0.tool_calls.0.name": "broken",
            "gen_ai.completion.0.tool_calls.0.arguments": "not json {",
        },
    )
    call = span_to_llm_call(span, step_index=0)
    assert call is not None
    assert call.tool_calls_emitted[0].args == {"_raw": "not json {"}


def test_llm_error_status_with_exception_event() -> None:
    span = FakeSpan(
        attributes={
            "gen_ai.system": "openai",
            "gen_ai.request.model": "gpt-4o",
        },
        events=[
            FakeSpanEvent(
                name="exception",
                attributes={"exception.message": "rate limit exceeded"},
            ),
        ],
        status=FakeSpanStatus(status_code=StatusCode.ERROR, description="rate limited"),
    )
    call = span_to_llm_call(span, step_index=0)
    assert call is not None
    assert call.status == StepStatus.ERROR
    assert call.error_message == "rate limit exceeded"


def test_llm_missing_model_returns_none() -> None:
    span = FakeSpan(attributes={"gen_ai.system": "openai"})
    assert span_to_llm_call(span, step_index=0) is None


def test_llm_missing_provider_returns_none() -> None:
    span = FakeSpan(attributes={"gen_ai.request.model": "gpt-4"})
    assert span_to_llm_call(span, step_index=0) is None


def test_llm_span_id_and_trace_id_hex_format() -> None:
    span = FakeSpan(
        attributes={"gen_ai.system": "openai", "gen_ai.request.model": "gpt-4"},
        context=FakeContext(trace_id=0x1, span_id=0x2),
        parent=FakeParent(span_id=0x3),
    )
    call = span_to_llm_call(span, step_index=0)
    assert call is not None
    assert call.trace_id == "00000000000000000000000000000001"
    assert call.span_id == "0000000000000002"
    assert call.parent_span_id == "0000000000000003"


def test_llm_timestamps_in_utc() -> None:
    span = FakeSpan(
        attributes={"gen_ai.system": "openai", "gen_ai.request.model": "gpt-4"},
        start_time=1_700_000_000_000_000_000,
        end_time=1_700_000_002_000_000_000,
    )
    call = span_to_llm_call(span, step_index=0)
    assert call is not None
    assert call.started_at == datetime.fromtimestamp(1.7e9, tz=UTC)
    assert call.ended_at == datetime.fromtimestamp(1.7e9 + 2, tz=UTC)
    assert call.emitted_at.tzinfo is not None


# ───────────────────────── Tool mapping tests ─────────────────────────


def test_tool_happy_path() -> None:
    span = FakeSpan(
        name="execute",
        attributes={
            "gen_ai.tool.name": "fetch_rubric",
            "gen_ai.tool.call.id": "call_xyz",
            "gen_ai.tool.call.arguments": json.dumps({"rubric_id": 42}),
            "gen_ai.tool.call.result": "rubric content",
        },
        status=FakeSpanStatus(status_code=StatusCode.OK),
    )
    tc = span_to_tool_call(span, step_index=2)
    assert isinstance(tc, ToolCall)
    assert tc.name == "fetch_rubric"
    assert tc.tool_call_id == "call_xyz"
    assert tc.args == {"rubric_id": 42}
    assert tc.output == "rubric content"
    assert tc.status == StepStatus.OK
    assert tc.step_index == 2


def test_tool_name_from_span_name_prefix_fallback() -> None:
    span = FakeSpan(name="tool.lookup", attributes={"gen_ai.operation.name": "execute_tool"})
    tc = span_to_tool_call(span, step_index=0)
    assert tc is not None
    assert tc.name == "lookup"


def test_tool_call_id_falls_back_to_span_id() -> None:
    span = FakeSpan(
        name="tool.x",
        context=FakeContext(span_id=0xCAFE),
    )
    tc = span_to_tool_call(span, step_index=0)
    assert tc is not None
    assert tc.tool_call_id == "000000000000cafe"


def test_tool_args_from_traceloop_input() -> None:
    span = FakeSpan(
        attributes={
            "gen_ai.tool.name": "search",
            "traceloop.entity.input": json.dumps({"q": "hello"}),
        },
    )
    tc = span_to_tool_call(span, step_index=0)
    assert tc is not None
    assert tc.args == {"q": "hello"}


def test_tool_missing_name_returns_none() -> None:
    span = FakeSpan(name="some.unrelated.thing")
    assert span_to_tool_call(span, step_index=0) is None


def test_tool_error_with_exception_event() -> None:
    span = FakeSpan(
        attributes={"gen_ai.tool.name": "broken_tool"},
        events=[
            FakeSpanEvent(name="exception", attributes={"exception.message": "boom"}),
        ],
        status=FakeSpanStatus(status_code=StatusCode.ERROR),
    )
    tc = span_to_tool_call(span, step_index=0)
    assert tc is not None
    assert tc.status == StepStatus.ERROR
    assert tc.error_message == "boom"


def test_tool_args_default_empty_dict_when_absent() -> None:
    span = FakeSpan(attributes={"gen_ai.tool.name": "noop"})
    tc = span_to_tool_call(span, step_index=0)
    assert tc is not None
    assert tc.args == {}


# ───────────────────────── Retrieval mapping tests ───────────────────


def test_retrieval_db_query_text_and_result() -> None:
    span = FakeSpan(
        attributes={
            "db.system": "pinecone",
            "db.query.text": "vector neighbours of foo",
            "db.query.result": ["chunk a", "chunk b"],
        },
    )
    r = span_to_retrieval(span, step_index=1)
    assert isinstance(r, Retrieval)
    assert r.query == "vector neighbours of foo"
    assert r.chunks == ["chunk a", "chunk b"]
    assert r.source == "pinecone"
    assert r.step_index == 1


def test_retrieval_gen_ai_alternative_attrs() -> None:
    span = FakeSpan(
        attributes={
            "gen_ai.retrieval.query": "find docs",
            "gen_ai.retrieval.documents": ["d1", "d2"],
            "gen_ai.retrieval.source": "qdrant",
        },
    )
    r = span_to_retrieval(span, step_index=0)
    assert r is not None
    assert r.query == "find docs"
    assert r.chunks == ["d1", "d2"]
    assert r.source == "qdrant"


def test_retrieval_missing_query_returns_none() -> None:
    span = FakeSpan(attributes={"db.system": "pinecone"})
    assert span_to_retrieval(span, step_index=0) is None


def test_retrieval_chunks_default_empty() -> None:
    span = FakeSpan(
        attributes={"db.system": "chroma", "db.query.text": "q"},
    )
    r = span_to_retrieval(span, step_index=0)
    assert r is not None
    assert r.chunks == []


# ───────────────────────── TraceStart / End tests ────────────────────


def test_trace_start_reads_agent_and_user_input() -> None:
    span = FakeSpan(
        name="kairos.task",
        attributes={
            "kairos.agent.name": "essay_grader",
            "kairos.agent.version": "1.0.3",
            "kairos.user_input": "grade this essay",
            "kairos.system_prompt": "you are a grader",
            "kairos.business_op": "grade_essay",
        },
    )
    ts = span_to_trace_start(span)
    assert isinstance(ts, TraceStart)
    assert ts.agent_name == "essay_grader"
    assert ts.agent_version == "1.0.3"
    assert ts.user_input == "grade this essay"
    assert ts.system_prompt == "you are a grader"
    assert ts.business_op == "grade_essay"
    assert ts.step_index == 0


def test_trace_start_falls_back_to_service_name() -> None:
    span = FakeSpan(
        name="kairos.task",
        resource_attributes={"service.name": "my-svc", "service.version": "0.1.0"},
    )
    ts = span_to_trace_start(span)
    assert ts.agent_name == "my-svc"
    assert ts.agent_version == "0.1.0"


def test_trace_start_unknown_agent_when_missing() -> None:
    span = FakeSpan(name="kairos.task")
    ts = span_to_trace_start(span)
    assert ts.agent_name == "unknown"


def test_trace_start_collects_kairos_metadata_and_session() -> None:
    span = FakeSpan(
        name="kairos.task",
        attributes={
            "kairos.agent.name": "a",
            "kairos.metadata.experiment": "v2",
            "kairos.metadata.region": "eu",
            "session.id": "sess-1",
            "user.id": "u-9",
        },
    )
    ts = span_to_trace_start(span)
    assert ts.metadata is not None
    assert ts.metadata["experiment"] == "v2"
    assert ts.metadata["region"] == "eu"
    assert ts.metadata["session_id"] == "sess-1"
    assert ts.metadata["user_id"] == "u-9"


def test_trace_start_user_input_falls_back_to_prompt_zero() -> None:
    span = FakeSpan(
        name="kairos.task",
        attributes={
            "kairos.agent.name": "a",
            "gen_ai.prompt.0.content": "fallback prompt",
        },
    )
    ts = span_to_trace_start(span)
    assert ts.user_input == "fallback prompt"


def test_trace_start_emitted_at_from_start_time() -> None:
    span = FakeSpan(name="kairos.task", start_time=1_700_000_000_000_000_000)
    ts = span_to_trace_start(span)
    assert ts.emitted_at == datetime.fromtimestamp(1.7e9, tz=UTC)


def test_trace_end_ok_status_completed() -> None:
    span = FakeSpan(
        name="kairos.task",
        status=FakeSpanStatus(status_code=StatusCode.OK),
    )
    te = span_to_trace_end(span, step_index=10)
    assert isinstance(te, TraceEnd)
    assert te.terminal_status == TerminalStatus.COMPLETED
    assert te.output_type == OutputType.UNKNOWN
    assert te.step_index == 10


def test_trace_end_unset_status_completed() -> None:
    span = FakeSpan(
        name="kairos.task",
        status=FakeSpanStatus(status_code=StatusCode.UNSET),
    )
    te = span_to_trace_end(span, step_index=5)
    assert te.terminal_status == TerminalStatus.COMPLETED


def test_trace_end_error_status_error() -> None:
    span = FakeSpan(
        name="kairos.task",
        status=FakeSpanStatus(status_code=StatusCode.ERROR),
    )
    te = span_to_trace_end(span, step_index=0)
    assert te.terminal_status == TerminalStatus.ERROR


def test_trace_end_terminal_status_override() -> None:
    span = FakeSpan(
        name="kairos.task",
        attributes={"kairos.terminal_status": "timeout"},
        status=FakeSpanStatus(status_code=StatusCode.OK),
    )
    te = span_to_trace_end(span, step_index=0)
    assert te.terminal_status == TerminalStatus.TIMEOUT


def test_trace_end_output_type_and_final_output() -> None:
    span = FakeSpan(
        name="kairos.task",
        attributes={
            "kairos.output_type": "text",
            "kairos.final_output": "graded: B+",
        },
    )
    te = span_to_trace_end(span, step_index=1)
    assert te.output_type == OutputType.TEXT
    assert te.final_output == "graded: B+"


def test_trace_end_emitted_at_from_end_time() -> None:
    span = FakeSpan(
        name="kairos.task",
        end_time=1_700_000_005_000_000_000,
    )
    te = span_to_trace_end(span, step_index=0)
    assert te.emitted_at == datetime.fromtimestamp(1.7e9 + 5, tz=UTC)


def test_trace_end_collects_metadata() -> None:
    span = FakeSpan(
        name="kairos.task",
        attributes={
            "kairos.metadata.run_id": "r-7",
        },
    )
    te = span_to_trace_end(span, step_index=0)
    assert te.metadata is not None
    assert te.metadata["run_id"] == "r-7"


# ───────────────────────── parametrized smoke tests ──────────────────


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (StatusCode.UNSET, StepStatus.OK),
        (StatusCode.OK, StepStatus.OK),
        (StatusCode.ERROR, StepStatus.ERROR),
    ],
)
def test_llm_status_mapping(status_code: Any, expected: StepStatus) -> None:
    span = FakeSpan(
        attributes={"gen_ai.system": "openai", "gen_ai.request.model": "gpt-4"},
        status=FakeSpanStatus(status_code=status_code),
    )
    call = span_to_llm_call(span, step_index=0)
    assert call is not None
    assert call.status == expected


# ════════════════════════ OpenInference (Arize / Phoenix) ════════════════════════
#
# OpenInference uses ``openinference.span.kind`` ∈ {LLM, TOOL, RETRIEVER,
# EMBEDDING, AGENT, CHAIN, GUARDRAIL, EVALUATOR, RERANKER, UNKNOWN} and
# attribute keys like ``llm.model_name``, ``llm.input_messages.{i}.message.role``,
# ``tool.name``, ``input.value``, ``output.value``,
# ``retrieval.documents.{i}.document.content``. Phoenix's UI and
# rendering keys off these conventions, so when the host uses
# arize-phoenix-otel + openinference-instrumentation-* (rather than
# Traceloop / OpenLLMetry), spans speak this dialect instead.


# ─────────────────── classifier on OpenInference spans ───────────────────


def test_classify_openinference_llm() -> None:
    span = FakeSpan(name="ChatCompletion", attributes={"openinference.span.kind": "LLM"})
    assert classify_span(span) == "llm"


def test_classify_openinference_tool() -> None:
    span = FakeSpan(name="search", attributes={"openinference.span.kind": "TOOL"})
    assert classify_span(span) == "tool"


def test_classify_openinference_retriever() -> None:
    span = FakeSpan(name="rag", attributes={"openinference.span.kind": "RETRIEVER"})
    assert classify_span(span) == "retrieval"


def test_classify_openinference_embedding() -> None:
    span = FakeSpan(name="embed", attributes={"openinference.span.kind": "EMBEDDING"})
    assert classify_span(span) == "retrieval"


def test_classify_openinference_unknown_kind_falls_through_to_other() -> None:
    span = FakeSpan(name="x", attributes={"openinference.span.kind": "GUARDRAIL"})
    assert classify_span(span) == "other"


def test_classify_kairos_task_takes_precedence_over_openinference_kind() -> None:
    span = FakeSpan(
        name="kairos.task",
        attributes={"openinference.span.kind": "LLM"},
    )
    assert classify_span(span) == "task"


# ───────────── span_to_llm_call on OpenInference attributes ──────────────


def test_llm_call_reads_openinference_model_and_provider() -> None:
    span = FakeSpan(
        attributes={
            "openinference.span.kind": "LLM",
            "llm.model_name": "gpt-4o-mini",
            "llm.provider": "openai",
        },
    )
    call = span_to_llm_call(span, step_index=0)
    assert call is not None
    assert call.model == "gpt-4o-mini"
    assert call.provider == "openai"


def test_llm_call_uses_llm_system_when_provider_missing() -> None:
    """OpenInference sometimes uses ``llm.system`` instead of ``llm.provider``."""
    span = FakeSpan(
        attributes={
            "openinference.span.kind": "LLM",
            "llm.model_name": "claude-3-5-sonnet",
            "llm.system": "anthropic",
        },
    )
    call = span_to_llm_call(span, step_index=0)
    assert call is not None
    assert call.provider == "anthropic"


def test_llm_call_reads_openinference_token_counts() -> None:
    span = FakeSpan(
        attributes={
            "openinference.span.kind": "LLM",
            "llm.model_name": "gpt-4",
            "llm.provider": "openai",
            "llm.token_count.prompt": 100,
            "llm.token_count.completion": 25,
            "llm.token_count.total": 125,
        },
    )
    call = span_to_llm_call(span, step_index=0)
    assert call is not None
    assert call.input_tokens == 100
    assert call.output_tokens == 25
    assert call.total_tokens == 125


def test_llm_call_reads_openinference_input_messages() -> None:
    span = FakeSpan(
        attributes={
            "openinference.span.kind": "LLM",
            "llm.model_name": "gpt-4",
            "llm.provider": "openai",
            "llm.input_messages.0.message.role": "system",
            "llm.input_messages.0.message.content": "be helpful",
            "llm.input_messages.1.message.role": "user",
            "llm.input_messages.1.message.content": "hi",
        },
    )
    call = span_to_llm_call(span, step_index=0)
    assert call is not None
    assert len(call.messages_in) == 2
    assert call.messages_in[0].role == "system"
    assert call.messages_in[0].content == "be helpful"
    assert call.messages_in[1].role == "user"


def test_llm_call_reads_openinference_output_message_as_content_out() -> None:
    span = FakeSpan(
        attributes={
            "openinference.span.kind": "LLM",
            "llm.model_name": "gpt-4",
            "llm.provider": "openai",
            "llm.output_messages.0.message.role": "assistant",
            "llm.output_messages.0.message.content": "Hello there",
        },
    )
    call = span_to_llm_call(span, step_index=0)
    assert call is not None
    assert call.content_out == "Hello there"


def test_llm_call_reads_openinference_tool_calls_emitted() -> None:
    span = FakeSpan(
        attributes={
            "openinference.span.kind": "LLM",
            "llm.model_name": "gpt-4",
            "llm.provider": "openai",
            "llm.output_messages.0.message.role": "assistant",
            "llm.output_messages.0.message.tool_calls.0.tool_call.id": "tc-1",
            "llm.output_messages.0.message.tool_calls.0.tool_call.function.name": "search",
            "llm.output_messages.0.message.tool_calls.0.tool_call.function.arguments": json.dumps({"query": "weather"}),
        },
    )
    call = span_to_llm_call(span, step_index=0)
    assert call is not None
    assert len(call.tool_calls_emitted) == 1
    assert call.tool_calls_emitted[0].id == "tc-1"
    assert call.tool_calls_emitted[0].name == "search"
    assert call.tool_calls_emitted[0].args == {"query": "weather"}


def test_llm_call_temperature_from_invocation_parameters() -> None:
    span = FakeSpan(
        attributes={
            "openinference.span.kind": "LLM",
            "llm.model_name": "gpt-4",
            "llm.provider": "openai",
            "llm.invocation_parameters": json.dumps({"temperature": 0.3, "top_p": 0.95}),
        },
    )
    call = span_to_llm_call(span, step_index=0)
    assert call is not None
    assert call.temperature == 0.3


def test_llm_call_invalid_invocation_parameters_does_not_crash() -> None:
    span = FakeSpan(
        attributes={
            "openinference.span.kind": "LLM",
            "llm.model_name": "gpt-4",
            "llm.provider": "openai",
            "llm.invocation_parameters": "{not json",
        },
    )
    call = span_to_llm_call(span, step_index=0)
    assert call is not None
    assert call.temperature is None


# ───────────── span_to_tool_call on OpenInference attributes ─────────────


def test_tool_call_reads_openinference_tool_name_and_parameters() -> None:
    span = FakeSpan(
        name="search",
        attributes={
            "openinference.span.kind": "TOOL",
            "tool.name": "search",
            "tool.parameters": json.dumps({"query": "weather", "limit": 5}),
            "tool_call.id": "tc-1",
            "output.value": json.dumps([{"title": "Sunny"}]),
        },
    )
    call = span_to_tool_call(span, step_index=0)
    assert call is not None
    assert call.name == "search"
    assert call.tool_call_id == "tc-1"
    assert call.args == {"query": "weather", "limit": 5}
    assert call.output == json.dumps([{"title": "Sunny"}])


def test_tool_call_falls_back_to_input_value_for_args() -> None:
    """Some instrumentors only set ``input.value`` (JSON) and skip ``tool.parameters``."""
    span = FakeSpan(
        name="search",
        attributes={
            "openinference.span.kind": "TOOL",
            "tool.name": "search",
            "input.value": json.dumps({"query": "weather"}),
            "input.mime_type": "application/json",
        },
    )
    call = span_to_tool_call(span, step_index=0)
    assert call is not None
    assert call.args == {"query": "weather"}


def test_tool_call_output_value_takes_precedence_over_traceloop() -> None:
    span = FakeSpan(
        name="search",
        attributes={
            "openinference.span.kind": "TOOL",
            "tool.name": "search",
            "output.value": "result string",
            "traceloop.entity.output": "ignored",
        },
    )
    call = span_to_tool_call(span, step_index=0)
    assert call is not None
    assert call.output == "result string"


# ────────── span_to_retrieval on OpenInference attributes ────────────────


def test_retrieval_reads_openinference_input_value_as_query() -> None:
    span = FakeSpan(
        attributes={
            "openinference.span.kind": "RETRIEVER",
            "input.value": "senior python engineer",
        },
    )
    ret = span_to_retrieval(span, step_index=0)
    assert ret is not None
    assert ret.query == "senior python engineer"


def test_retrieval_reads_openinference_documents() -> None:
    span = FakeSpan(
        attributes={
            "openinference.span.kind": "RETRIEVER",
            "input.value": "query",
            "retrieval.documents.0.document.content": "chunk a",
            "retrieval.documents.0.document.id": "doc-1",
            "retrieval.documents.1.document.content": "chunk b",
            "retrieval.documents.1.document.id": "doc-2",
        },
    )
    ret = span_to_retrieval(span, step_index=0)
    assert ret is not None
    assert ret.chunks == ["chunk a", "chunk b"]


# ───────────── span_to_trace_start with OpenInference fallback ───────────


def test_trace_start_user_input_falls_back_to_openinference_first_message() -> None:
    """When kairos.user_input is absent, fall back to llm.input_messages.0.message.content
    BEFORE gen_ai.prompt.0 (since OpenInference is the more current convention)."""
    span = FakeSpan(
        name="kairos.task",
        attributes={
            "kairos.agent.name": "agent",
            "llm.input_messages.0.message.content": "evaluate this resume",
        },
    )
    ts = span_to_trace_start(span, step_index=0)
    assert ts.user_input == "evaluate this resume"


# ──────────────── Claude Code (claude_code.*) dialect ────────────────
#
# Real spans captured from `claude` 2.1.161 (tracer
# com.anthropic.claude_code.tracing) emitting native OTel for a one-shot
# Read-tool run (XER-73 Phase A). PII (user/org/account/session ids,
# email) was scrubbed before committing; the span shape/attributes are
# otherwise verbatim. See tests/readers/fixtures/claude_code_trace.json.

_CC_TRACE: list[dict[str, Any]] = json.loads(
    (Path(__file__).parent / "fixtures" / "claude_code_trace.json").read_text()
)


def _cc_span(name: str) -> Any:
    """Return the first fixture span with ``name`` as a ReadableSpan adapter."""
    raw = next(s for s in _CC_TRACE if s["name"] == name)
    return _phoenix_dict_to_span(raw)


def test_classify_claude_code_interaction_is_task() -> None:
    # The interaction root (span.type == "interaction") is the trace boundary.
    assert classify_span(_cc_span("claude_code.interaction")) == "task"


def test_classify_claude_code_llm_request_is_llm() -> None:
    # Already classified via gen_ai.system — confirm the dialect doesn't regress it.
    assert classify_span(_cc_span("claude_code.llm_request")) == "llm"


def test_classify_claude_code_tool_is_tool() -> None:
    # The gap this change closes: span.type == "tool" with tool_name, no
    # gen_ai.tool.name and name not "tool."-prefixed.
    assert classify_span(_cc_span("claude_code.tool")) == "tool"


def test_classify_claude_code_tool_execution_stays_other() -> None:
    # Internal sub-phase span (span.type == "tool.execution"); must NOT become a
    # second ToolCall for the one logical tool call.
    assert classify_span(_cc_span("claude_code.tool.execution")) == "other"


def test_classify_claude_code_tool_blocked_on_user_stays_other() -> None:
    # Permission-wait sub-phase (span.type == "tool.blocked_on_user").
    assert classify_span(_cc_span("claude_code.tool.blocked_on_user")) == "other"


def test_span_to_tool_call_extracts_claude_code_tool_name() -> None:
    raw = next(s for s in _CC_TRACE if s["name"] == "claude_code.tool")
    call = span_to_tool_call(_phoenix_dict_to_span(raw), step_index=1)
    assert isinstance(call, ToolCall)
    assert call.name == "Read"
    # No tool_call.id attr in the dialect → falls back to the span_id.
    assert call.tool_call_id == raw["context"]["span_id"]


def test_span_to_trace_start_uses_claude_code_user_prompt() -> None:
    ts = span_to_trace_start(_cc_span("claude_code.interaction"), step_index=0)
    # OTEL_LOG_USER_PROMPTS=1 was set during capture, so the real prompt landed.
    assert ts.user_input is not None
    assert "hello.txt" in ts.user_input
