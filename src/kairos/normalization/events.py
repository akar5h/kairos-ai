"""Live event vocabulary.

Six typed events that an agent runtime emits in order, plus a shared
envelope. Every event carries:
    event_type      : str discriminator
    trace_id        : str — stable across the whole trace
    span_id         : str — unique per event
    parent_span_id  : str | None — causal parent (e.g. tool_call → llm_call)
    step_index      : int — monotonic in trace, assigned by Tracer
    emitted_at      : datetime — when the SDK emitted (not when op happened)

The events are the on-the-wire format: the Sink serializes them to JSON
or holds them in memory; the LiveNormalizer later folds a list of events
into a TraceEnvelope.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — pydantic needs the runtime type
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter

from kairos.models.enums import OutputType, StepStatus, TerminalStatus


class _Envelope(BaseModel):
    """Fields shared by every event. Not emitted directly; subclasses extend it."""

    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    step_index: int
    emitted_at: datetime


class ToolSchema(BaseModel):
    """A tool the agent could pick from at a given moment."""

    name: str
    description: str | None = None
    parameters: dict[str, Any] | None = None


class LLMMessage(BaseModel):
    """A single message in the chat history seen by an LLM call.

    `role` is a free-form string so adapters can pass through provider-specific
    roles (system, user, assistant, tool, function, ...). `tool_call_id` is set
    on tool-result messages.
    """

    role: str
    content: str | None = None
    tool_call_id: str | None = None
    name: str | None = None


class ToolCallEmitted(BaseModel):
    """A tool the LLM asked to invoke (lives on LLMCall.tool_calls_emitted)."""

    id: str
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


# ─────────────────────────────── Events ──────────────────────────────────


class TraceStart(_Envelope):
    event_type: Literal["trace_start"] = "trace_start"

    agent_name: str
    agent_version: str | None = None
    user_input: str | None = None
    system_prompt: str | None = None
    tools_registered: list[ToolSchema] = Field(default_factory=list)
    business_op: str | None = None
    metadata: dict[str, Any] | None = None


class TraceEnd(_Envelope):
    event_type: Literal["trace_end"] = "trace_end"

    terminal_status: TerminalStatus
    output_type: OutputType
    final_output: str | dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None


class LLMCall(_Envelope):
    event_type: Literal["llm_call"] = "llm_call"

    model: str
    provider: str
    messages_in: list[LLMMessage]
    tools_available: list[ToolSchema] = Field(default_factory=list)
    temperature: float | None = None
    content_out: str | None = None
    tool_calls_emitted: list[ToolCallEmitted] = Field(default_factory=list)
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    started_at: datetime
    ended_at: datetime
    status: StepStatus = StepStatus.OK
    error_message: str | None = None


class ToolCall(_Envelope):
    event_type: Literal["tool_call"] = "tool_call"

    tool_call_id: str
    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    output: str | dict[str, Any] | None = None
    status: StepStatus = StepStatus.OK
    error_message: str | None = None
    started_at: datetime
    ended_at: datetime


class Retrieval(_Envelope):
    event_type: Literal["retrieval"] = "retrieval"

    query: str
    chunks: list[str] = Field(default_factory=list)
    source: str | None = None
    started_at: datetime
    ended_at: datetime


class MemoryEvent(_Envelope):
    event_type: Literal["memory_event"] = "memory_event"

    kind: Literal["read", "write"]
    key: str
    value: str | dict[str, Any] | None = None
    scope: str | None = None


# ────────────────────── Discriminated union for parsing ─────────────────────


AnyEvent = Annotated[
    TraceStart | TraceEnd | LLMCall | ToolCall | Retrieval | MemoryEvent,
    Field(discriminator="event_type"),
]


_EVENT_ADAPTER: TypeAdapter[AnyEvent] = TypeAdapter(AnyEvent)


def parse_event(payload: dict[str, Any]) -> AnyEvent:
    """Parse a dict into the right event subclass via the event_type discriminator.

    Raises pydantic.ValidationError if the payload is not a valid event.
    """
    return _EVENT_ADAPTER.validate_python(payload)
