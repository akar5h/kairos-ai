"""OTel span → Kairos event mapping (pure functions).

Maps OpenTelemetry genai-semantic-convention spans (as produced by
OpenLLMetry, OpenInference, or any OTel-compliant instrumentation) to
the Kairos live event vocabulary.

Convention notes:
    - LLM spans carry ``gen_ai.system`` / ``gen_ai.request.model`` / etc.
    - Tool execution spans carry ``gen_ai.tool.name`` (newer convention)
      or vendor-specific ``traceloop.entity.name == "tool"``.
    - Vector-DB / retrieval spans carry ``db.system`` (chroma, pinecone,
      qdrant, weaviate, milvus) and ``db.query.text``.
    - The "task" / "agent run" boundary is host-driven: the host creates
      a span named ``kairos.task`` (or sets ``kairos.span.kind=task``)
      and we treat that as the trace root for synthesizing TraceStart /
      TraceEnd events.

These functions are pure: no IO, no global state. They never raise on
ill-formed input — they return ``None`` (for the converters) or
``"other"`` (for the classifier) so the caller can decide what to do.
TraceStart / TraceEnd never return None because they're synthesized
from a span the host explicitly marked as a task root.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from opentelemetry.trace import StatusCode

from kairos.log import get_logger
from kairos.models.enums import OutputType, StepStatus, TerminalStatus
from kairos.normalization.events import (
    LLMCall,
    LLMMessage,
    Retrieval,
    ToolCall,
    ToolCallEmitted,
    TraceEnd,
    TraceStart,
)

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import ReadableSpan

logger = get_logger(__name__)


SpanKind = Literal["llm", "tool", "retrieval", "task", "other"]


# Regex matching dotted prompt-message keys: ``gen_ai.prompt.<i>.<field>``
_PROMPT_KEY_RE: re.Pattern[str] = re.compile(r"^gen_ai\.prompt\.(\d+)\.(role|content)$")
# Regex matching dotted completion tool-call keys:
# ``gen_ai.completion.<i>.tool_calls.<j>.<field>``
_COMPLETION_TOOL_CALL_RE: re.Pattern[str] = re.compile(
    r"^gen_ai\.completion\.(\d+)\.tool_calls\.(\d+)\.(id|name|arguments)$"
)
# OpenInference (Arize / Phoenix) message keys:
# ``llm.input_messages.<i>.message.<field>``
_OI_INPUT_MSG_RE: re.Pattern[str] = re.compile(r"^llm\.input_messages\.(\d+)\.message\.(role|content)$")
# ``llm.output_messages.<i>.message.tool_calls.<j>.tool_call.[function.]<field>``
_OI_OUTPUT_TOOL_CALL_RE: re.Pattern[str] = re.compile(
    r"^llm\.output_messages\.(\d+)\.message\.tool_calls\.(\d+)\.tool_call\.(?:function\.)?(id|name|arguments)$"
)
# Retrieval document content keys: ``retrieval.documents.<i>.document.content``
_OI_DOC_CONTENT_RE: re.Pattern[str] = re.compile(r"^retrieval\.documents\.(\d+)\.document\.content$")

_KAIROS_METADATA_PREFIX: str = "kairos.metadata."

# OpenInference span.kind → SpanKind. Only kinds we have an event for.
# CHAIN, AGENT, GUARDRAIL, EVALUATOR, RERANKER, UNKNOWN currently fall
# through to "other" — we'll add events for them when there's an analysis
# need.
_OI_KIND_MAP: dict[str, SpanKind] = {
    "LLM": "llm",
    "TOOL": "tool",
    "RETRIEVER": "retrieval",
    "EMBEDDING": "retrieval",
}


# ────────────────────────────── helpers ──────────────────────────────


def _attrs(span: ReadableSpan) -> dict[str, Any]:
    """Return span.attributes as a plain dict (OTel may give a Mapping)."""
    raw = getattr(span, "attributes", None) or {}
    return dict(raw)


def _resource_attrs(span: ReadableSpan) -> dict[str, Any]:
    resource = getattr(span, "resource", None)
    if resource is None:
        return {}
    raw = getattr(resource, "attributes", None) or {}
    return dict(raw)


def _format_trace_id(trace_id: int) -> str:
    return f"{trace_id:032x}"


def _format_span_id(span_id: int) -> str:
    return f"{span_id:016x}"


def _ns_to_dt(ns: int | None) -> datetime | None:
    if ns is None:
        return None
    return datetime.fromtimestamp(ns / 1e9, tz=UTC)


def _trace_id_str(span: ReadableSpan) -> str:
    ctx = getattr(span, "context", None)
    if ctx is None:
        return _format_trace_id(0)
    return _format_trace_id(int(ctx.trace_id))


def _span_id_str(span: ReadableSpan) -> str:
    ctx = getattr(span, "context", None)
    if ctx is None:
        return _format_span_id(0)
    return _format_span_id(int(ctx.span_id))


def _parent_span_id_str(span: ReadableSpan) -> str | None:
    parent = getattr(span, "parent", None)
    if parent is None:
        return None
    return _format_span_id(int(parent.span_id))


def _status_code(span: ReadableSpan) -> Any:
    status = getattr(span, "status", None)
    if status is None:
        return StatusCode.UNSET
    return getattr(status, "status_code", StatusCode.UNSET)


def _step_status(span: ReadableSpan) -> StepStatus:
    code = _status_code(span)
    if code == StatusCode.ERROR:
        return StepStatus.ERROR
    return StepStatus.OK


def _exception_message(span: ReadableSpan) -> str | None:
    """Pull the first exception event's message, if present."""
    events = getattr(span, "events", None) or []
    for ev in events:
        if getattr(ev, "name", None) == "exception":
            attrs = getattr(ev, "attributes", None) or {}
            msg = attrs.get("exception.message")
            if isinstance(msg, str) and msg:
                return msg
    return None


def _error_message(span: ReadableSpan) -> str | None:
    """Best-effort error message: exception event first, then status description."""
    msg = _exception_message(span)
    if msg is not None:
        return msg
    status = getattr(span, "status", None)
    if status is None:
        return None
    desc = getattr(status, "description", None)
    if isinstance(desc, str) and desc:
        return desc
    return None


def _parse_json_args(raw: Any) -> dict[str, Any]:
    """Parse a JSON-string of args into a dict.

    Falls back to ``{"_raw": <str>}`` if it isn't a JSON object. Returns
    an empty dict when ``raw`` is None.
    """
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if not isinstance(raw, str):
        return {"_raw": str(raw)}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {"_raw": raw}
    if isinstance(parsed, dict):
        return parsed
    return {"_raw": raw}


def _collect_kairos_metadata(attrs: dict[str, Any]) -> dict[str, Any]:
    """Collect ``kairos.metadata.*`` attributes plus session/user IDs."""
    metadata: dict[str, Any] = {}
    for key, value in attrs.items():
        if key.startswith(_KAIROS_METADATA_PREFIX):
            metadata[key[len(_KAIROS_METADATA_PREFIX) :]] = value
    session_id = attrs.get("session.id")
    if session_id is not None:
        metadata["session_id"] = session_id
    user_id = attrs.get("user.id")
    if user_id is not None:
        metadata["user_id"] = user_id
    return metadata


# ───────────────────────── classifier ────────────────────────────────


def classify_span(span: ReadableSpan) -> SpanKind:
    """Classify a span by its attributes / name.

    Order of precedence (first match wins):
        1. ``task``      — host-marked trace boundary (Kairos)
        2. OpenInference ``openinference.span.kind`` (LLM/TOOL/RETRIEVER/EMBEDDING)
        3. ``llm``       — has ``gen_ai.system`` (OpenLLMetry)
        4. ``tool``      — gen_ai/traceloop tool signals or ``tool.*`` name
        5. ``retrieval`` — db.system, embedding op, or traceloop retrieval
        6. ``other``
    """
    name = getattr(span, "name", "") or ""
    attrs = _attrs(span)

    if name == "kairos.task" or attrs.get("kairos.span.kind") == "task":
        return "task"

    # OpenInference (Phoenix dialect).
    oi_kind = attrs.get("openinference.span.kind")
    if isinstance(oi_kind, str):
        mapped = _OI_KIND_MAP.get(oi_kind.upper())
        if mapped is not None:
            return mapped

    # OTel-genai (OpenLLMetry dialect).
    if attrs.get("gen_ai.system"):
        return "llm"

    op_name = attrs.get("gen_ai.operation.name")
    if (
        op_name == "execute_tool"
        or attrs.get("gen_ai.tool.name")
        or attrs.get("traceloop.entity.name") == "tool"
        or name.startswith("tool.")
    ):
        return "tool"

    if attrs.get("db.system") or attrs.get("traceloop.entity.name") == "retrieval" or op_name == "embedding":
        return "retrieval"

    return "other"


# ─────────────────────────── LLM mapping ─────────────────────────────


def _build_messages(by_index: dict[int, dict[str, str]]) -> list[LLMMessage]:
    """Build LLMMessages from a {idx: {role,content}} map, sorted by idx."""
    messages: list[LLMMessage] = []
    for idx in sorted(by_index):
        slot = by_index[idx]
        # Default role to "user" if absent — keeps the message addressable
        # without dropping content.
        role = slot.get("role", "user")
        content = slot.get("content")
        messages.append(LLMMessage(role=role, content=content))
    return messages


def _extract_messages_in(attrs: dict[str, Any]) -> list[LLMMessage]:
    """Collect input messages from OpenInference (preferred) or OpenLLMetry."""
    # OpenInference: llm.input_messages.{i}.message.{role,content}
    oi_by_idx: dict[int, dict[str, str]] = {}
    for key, value in attrs.items():
        match = _OI_INPUT_MSG_RE.match(key)
        if not match:
            continue
        idx = int(match.group(1))
        field = match.group(2)
        slot = oi_by_idx.setdefault(idx, {})
        slot[field] = value if isinstance(value, str) else str(value)
    if oi_by_idx:
        return _build_messages(oi_by_idx)

    # OpenLLMetry: gen_ai.prompt.{i}.{role,content}
    by_index: dict[int, dict[str, str]] = {}
    for key, value in attrs.items():
        match = _PROMPT_KEY_RE.match(key)
        if not match:
            continue
        idx = int(match.group(1))
        field = match.group(2)
        slot = by_index.setdefault(idx, {})
        slot[field] = value if isinstance(value, str) else str(value)
    return _build_messages(by_index)


def _extract_tool_calls_emitted(attrs: dict[str, Any]) -> list[ToolCallEmitted]:
    """Collect emitted tool calls from OpenInference (preferred) or OpenLLMetry."""
    # OpenInference: llm.output_messages.0.message.tool_calls.{j}.tool_call.[function.]<field>
    oi_by_j: dict[int, dict[str, Any]] = {}
    for key, value in attrs.items():
        match = _OI_OUTPUT_TOOL_CALL_RE.match(key)
        if not match:
            continue
        msg_idx = int(match.group(1))
        if msg_idx != 0:
            continue
        j = int(match.group(2))
        field = match.group(3)
        oi_by_j.setdefault(j, {})[field] = value
    if oi_by_j:
        emitted: list[ToolCallEmitted] = []
        for j in sorted(oi_by_j):
            slot = oi_by_j[j]
            tc_id = slot.get("id")
            tc_name = slot.get("name")
            if not isinstance(tc_id, str) or not isinstance(tc_name, str):
                continue
            args = _parse_json_args(slot.get("arguments"))
            emitted.append(ToolCallEmitted(id=tc_id, name=tc_name, args=args))
        return emitted

    # OpenLLMetry: gen_ai.completion.0.tool_calls.{j}.<field>
    by_j: dict[int, dict[str, Any]] = {}
    for key, value in attrs.items():
        match = _COMPLETION_TOOL_CALL_RE.match(key)
        if not match:
            continue
        comp_idx = int(match.group(1))
        if comp_idx != 0:
            continue
        j = int(match.group(2))
        field = match.group(3)
        by_j.setdefault(j, {})[field] = value

    out: list[ToolCallEmitted] = []
    for j in sorted(by_j):
        slot = by_j[j]
        tc_id = slot.get("id")
        tc_name = slot.get("name")
        if not isinstance(tc_id, str) or not isinstance(tc_name, str):
            continue
        args = _parse_json_args(slot.get("arguments"))
        out.append(ToolCallEmitted(id=tc_id, name=tc_name, args=args))
    return out


def _extract_temperature(attrs: dict[str, Any]) -> float | None:
    """Read temperature from OTel-genai or parse out of OI invocation_parameters."""
    raw = attrs.get("gen_ai.request.temperature")
    if isinstance(raw, int | float):
        return float(raw)
    inv_params = attrs.get("llm.invocation_parameters")
    if isinstance(inv_params, str):
        try:
            parsed = json.loads(inv_params)
        except (json.JSONDecodeError, ValueError):
            return None
        if isinstance(parsed, dict):
            t = parsed.get("temperature")
            if isinstance(t, int | float):
                return float(t)
    return None


def span_to_llm_call(span: ReadableSpan, *, step_index: int) -> LLMCall | None:
    """Convert an LLM-classified span to an LLMCall event.

    Returns ``None`` if essential fields (model AND provider) cannot be
    resolved from either OpenInference or OpenLLMetry attributes.
    """
    attrs = _attrs(span)

    # Provider: OpenInference llm.provider / llm.system, then OTel-genai gen_ai.system.
    provider = attrs.get("llm.provider") or attrs.get("llm.system") or attrs.get("gen_ai.system")
    # Model: OpenInference llm.model_name, then OTel-genai gen_ai.request.model.
    model = attrs.get("llm.model_name") or attrs.get("gen_ai.request.model")
    if not provider or not model:
        return None

    # Tokens: OpenInference first, then OTel-genai (modern + legacy names).
    input_tokens = (
        attrs.get("llm.token_count.prompt")
        or attrs.get("gen_ai.usage.input_tokens")
        or attrs.get("gen_ai.usage.prompt_tokens")
    )
    output_tokens = (
        attrs.get("llm.token_count.completion")
        or attrs.get("gen_ai.usage.output_tokens")
        or attrs.get("gen_ai.usage.completion_tokens")
    )
    total_tokens = attrs.get("llm.token_count.total") or attrs.get("gen_ai.usage.total_tokens")

    temperature = _extract_temperature(attrs)

    messages_in = _extract_messages_in(attrs)
    # content_out: OpenInference llm.output_messages.0.message.content, then gen_ai.completion.0.content.
    content_out = attrs.get("llm.output_messages.0.message.content") or attrs.get("gen_ai.completion.0.content")
    tool_calls = _extract_tool_calls_emitted(attrs)

    status = _step_status(span)
    err_msg = _error_message(span) if status == StepStatus.ERROR else None

    started_at = _ns_to_dt(getattr(span, "start_time", None))
    ended_at = _ns_to_dt(getattr(span, "end_time", None))
    if started_at is None or ended_at is None:
        return None

    return LLMCall(
        trace_id=_trace_id_str(span),
        span_id=_span_id_str(span),
        parent_span_id=_parent_span_id_str(span),
        step_index=step_index,
        emitted_at=ended_at,
        model=str(model),
        provider=str(provider),
        messages_in=messages_in,
        temperature=temperature,
        content_out=str(content_out) if isinstance(content_out, str) else None,
        tool_calls_emitted=tool_calls,
        input_tokens=int(input_tokens) if isinstance(input_tokens, int) else None,
        output_tokens=int(output_tokens) if isinstance(output_tokens, int) else None,
        total_tokens=int(total_tokens) if isinstance(total_tokens, int) else None,
        started_at=started_at,
        ended_at=ended_at,
        status=status,
        error_message=err_msg,
    )


# ─────────────────────────── Tool mapping ────────────────────────────


def _resolve_tool_name(span: ReadableSpan, attrs: dict[str, Any]) -> str | None:
    # OpenInference tool.name first, then OTel-genai gen_ai.tool.name, then span name prefix.
    name = attrs.get("tool.name") or attrs.get("gen_ai.tool.name")
    if isinstance(name, str) and name:
        return name
    span_name = getattr(span, "name", "") or ""
    if span_name.startswith("tool."):
        stripped = span_name[len("tool.") :]
        if stripped:
            return stripped
    return None


def span_to_tool_call(span: ReadableSpan, *, step_index: int) -> ToolCall | None:
    """Convert a tool-classified span to a ToolCall event.

    Reads OpenInference (``tool.*``, ``input.value``, ``output.value``)
    first, falls back to OpenLLMetry (``gen_ai.tool.*``) and Traceloop
    (``traceloop.entity.*``). Returns ``None`` if the tool name can't
    be resolved.
    """
    attrs = _attrs(span)
    name = _resolve_tool_name(span, attrs)
    if name is None:
        return None

    # Tool call id: OpenInference tool_call.id, then OTel-genai, then span_id fallback.
    tool_call_id = attrs.get("tool_call.id") or attrs.get("gen_ai.tool.call.id")
    if not isinstance(tool_call_id, str) or not tool_call_id:
        tool_call_id = _span_id_str(span)

    # Args: OpenInference tool.parameters, then OpenInference input.value, then
    # OpenLLMetry gen_ai.tool.call.arguments, then traceloop.entity.input.
    raw_args = (
        attrs.get("tool.parameters")
        or attrs.get("input.value")
        or attrs.get("gen_ai.tool.call.arguments")
        or attrs.get("traceloop.entity.input")
    )
    args = _parse_json_args(raw_args)

    # Output: OpenInference output.value, then OpenLLMetry gen_ai.tool.call.result,
    # then traceloop.entity.output.
    output: str | dict[str, Any] | None = None
    raw_out = attrs.get("output.value")
    if raw_out is None:
        raw_out = attrs.get("gen_ai.tool.call.result")
    if raw_out is None:
        raw_out = attrs.get("traceloop.entity.output")
    if isinstance(raw_out, str):
        output = raw_out
    elif isinstance(raw_out, dict):
        output = dict(raw_out)
    elif raw_out is not None:
        output = str(raw_out)

    status = _step_status(span)
    err_msg = _error_message(span) if status == StepStatus.ERROR else None

    started_at = _ns_to_dt(getattr(span, "start_time", None))
    ended_at = _ns_to_dt(getattr(span, "end_time", None))
    if started_at is None or ended_at is None:
        return None

    return ToolCall(
        trace_id=_trace_id_str(span),
        span_id=_span_id_str(span),
        parent_span_id=_parent_span_id_str(span),
        step_index=step_index,
        emitted_at=ended_at,
        tool_call_id=tool_call_id,
        name=name,
        args=args,
        output=output,
        status=status,
        error_message=err_msg,
        started_at=started_at,
        ended_at=ended_at,
    )


# ─────────────────────────── Retrieval mapping ───────────────────────


def _coerce_chunks(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, tuple):
        return [str(x) for x in raw]
    return [str(raw)]


def _extract_oi_documents(attrs: dict[str, Any]) -> list[str]:
    """Collect ``retrieval.documents.{i}.document.content`` chunks, sorted by i."""
    by_idx: dict[int, str] = {}
    for key, value in attrs.items():
        match = _OI_DOC_CONTENT_RE.match(key)
        if not match:
            continue
        idx = int(match.group(1))
        by_idx[idx] = value if isinstance(value, str) else str(value)
    return [by_idx[i] for i in sorted(by_idx)]


def span_to_retrieval(span: ReadableSpan, *, step_index: int) -> Retrieval | None:
    """Convert a retrieval/vector-db span to a Retrieval event.

    Reads OpenInference (``input.value`` + ``retrieval.documents.*``) first,
    falls back to OpenLLMetry (``gen_ai.retrieval.*``) and DB conventions
    (``db.query.text``). Returns ``None`` if no query can be resolved.
    """
    attrs = _attrs(span)

    # Query: OpenInference input.value (when span_kind=RETRIEVER), then
    # db.query.text, then gen_ai.retrieval.query, then traceloop.entity.input.
    query: Any = None
    oi_kind = attrs.get("openinference.span.kind")
    if isinstance(oi_kind, str) and oi_kind.upper() in {"RETRIEVER", "EMBEDDING"}:
        query = attrs.get("input.value")
    if not isinstance(query, str) or not query:
        query = attrs.get("db.query.text")
    if not isinstance(query, str) or not query:
        query = attrs.get("gen_ai.retrieval.query")
    if not isinstance(query, str) or not query:
        traceloop_input = attrs.get("traceloop.entity.input")
        if isinstance(traceloop_input, str) and traceloop_input:
            query = traceloop_input
    if not isinstance(query, str) or not query:
        return None

    # Chunks: OpenInference retrieval.documents.{i}.document.content first.
    chunks = _extract_oi_documents(attrs)
    if not chunks:
        chunks_raw = attrs.get("gen_ai.retrieval.documents") or attrs.get("db.query.result")
        chunks = _coerce_chunks(chunks_raw)

    source = attrs.get("db.system")
    if not isinstance(source, str) or not source:
        source = attrs.get("gen_ai.retrieval.source")
    source_str = source if isinstance(source, str) and source else None

    started_at = _ns_to_dt(getattr(span, "start_time", None))
    ended_at = _ns_to_dt(getattr(span, "end_time", None))
    if started_at is None or ended_at is None:
        return None

    return Retrieval(
        trace_id=_trace_id_str(span),
        span_id=_span_id_str(span),
        parent_span_id=_parent_span_id_str(span),
        step_index=step_index,
        emitted_at=ended_at,
        query=query,
        chunks=chunks,
        source=source_str,
        started_at=started_at,
        ended_at=ended_at,
    )


# ─────────────────────── TraceStart / TraceEnd ───────────────────────


def span_to_trace_start(span: ReadableSpan, *, step_index: int = 0) -> TraceStart:
    """Synthesize TraceStart from a root (kairos.task) span at start.

    ``tools_registered`` is always empty: OTel doesn't expose a clean
    "tools available at agent boot" signal, so we leave the list to the
    LiveNormalizer / downstream layer rather than guess.
    """
    attrs = _attrs(span)
    resource_attrs = _resource_attrs(span)

    agent_name_raw = attrs.get("kairos.agent.name") or resource_attrs.get("service.name") or "unknown"
    agent_version_raw = attrs.get("kairos.agent.version") or resource_attrs.get("service.version")

    user_input = attrs.get("kairos.user_input")
    if not isinstance(user_input, str) or not user_input:
        # Fallback: first input message in either dialect.
        oi_first = attrs.get("llm.input_messages.0.message.content")
        if isinstance(oi_first, str) and oi_first:
            user_input = oi_first
        else:
            prompt_zero = attrs.get("gen_ai.prompt.0.content")
            user_input = prompt_zero if isinstance(prompt_zero, str) and prompt_zero else None

    system_prompt_raw = attrs.get("kairos.system_prompt")
    system_prompt = system_prompt_raw if isinstance(system_prompt_raw, str) else None

    business_op_raw = attrs.get("kairos.business_op")
    business_op = business_op_raw if isinstance(business_op_raw, str) else None

    metadata = _collect_kairos_metadata(attrs)

    started_at = _ns_to_dt(getattr(span, "start_time", None))
    if started_at is None:
        started_at = datetime.now(tz=UTC)

    return TraceStart(
        trace_id=_trace_id_str(span),
        span_id=_span_id_str(span),
        parent_span_id=_parent_span_id_str(span),
        step_index=step_index,
        emitted_at=started_at,
        agent_name=str(agent_name_raw),
        agent_version=str(agent_version_raw) if agent_version_raw is not None else None,
        user_input=user_input,
        system_prompt=system_prompt,
        business_op=business_op,
        metadata=metadata or None,
    )


def _resolve_terminal_status(span: ReadableSpan, attrs: dict[str, Any]) -> TerminalStatus:
    override = attrs.get("kairos.terminal_status")
    if isinstance(override, str):
        try:
            return TerminalStatus(override)
        except ValueError:
            logger.warning("unknown kairos.terminal_status override on span", value=override)
    code = _status_code(span)
    if code == StatusCode.ERROR:
        return TerminalStatus.ERROR
    return TerminalStatus.COMPLETED


def _resolve_output_type(attrs: dict[str, Any]) -> OutputType:
    raw = attrs.get("kairos.output_type")
    if isinstance(raw, str):
        try:
            return OutputType(raw)
        except ValueError:
            logger.warning("unknown kairos.output_type override on span", value=raw)
    return OutputType.UNKNOWN


def span_to_trace_end(span: ReadableSpan, *, step_index: int) -> TraceEnd:
    """Synthesize TraceEnd from a root (kairos.task) span at end."""
    attrs = _attrs(span)

    terminal_status = _resolve_terminal_status(span, attrs)
    output_type = _resolve_output_type(attrs)

    final_output_raw = attrs.get("kairos.final_output")
    final_output: str | dict[str, Any] | None
    if isinstance(final_output_raw, str | dict):
        final_output = final_output_raw
    elif final_output_raw is None:
        final_output = None
    else:
        final_output = str(final_output_raw)

    metadata = _collect_kairos_metadata(attrs)

    ended_at = _ns_to_dt(getattr(span, "end_time", None))
    if ended_at is None:
        ended_at = datetime.now(tz=UTC)

    return TraceEnd(
        trace_id=_trace_id_str(span),
        span_id=_span_id_str(span),
        parent_span_id=_parent_span_id_str(span),
        step_index=step_index,
        emitted_at=ended_at,
        terminal_status=terminal_status,
        output_type=output_type,
        final_output=final_output,
        metadata=metadata or None,
    )
