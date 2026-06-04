"""Codex CLI rollout → TraceEnvelope.

Codex persists each session as a rollout JSONL under
``~/.codex/sessions/<yyyy>/<mm>/<dd>/rollout-*.jsonl``. Every line is
``{timestamp, type, payload}``. The authoritative model-I/O stream is the
``response_item`` lines (``event_msg`` lines are UI echoes / telemetry):

- ``response_item/message``     — ``role`` developer|user|assistant, ``content``
  blocks (``input_text`` / ``output_text``).
- ``response_item/function_call``        — a tool call: ``name``, ``arguments``
  (a JSON string), ``call_id``.
- ``response_item/function_call_output`` — its result, paired on ``call_id``.
- ``response_item/custom_tool_call`` / ``custom_tool_call_output`` — freeform
  tools like ``apply_patch`` (``input`` is not JSON), paired on ``call_id``.
- ``response_item/web_search_call``      — a web search (``action.query``).
- ``session_meta``  — trace identity (``id``, ``cwd``).
- ``event_msg/task_complete`` — the turn finished.

Mapping: assistant message → ``LLMCall``; function/custom tool call → ``ToolCall``
(full args + paired output + timing); web search → ``Retrieval``. Codex rollouts
do not record per-turn token counts, so those stay ``None``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from kairos.models.enums import OutputType, StepStatus, TerminalStatus
from kairos.normalization.agents.base import AgentTranscriptNormalizer, parse_ts
from kairos.normalization.events import (
    AnyEvent,
    LLMCall,
    Retrieval,
    ToolCall,
    TraceEnd,
    TraceStart,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

_PROVIDER = "openai"
_TEXT_BLOCKS = {"input_text", "output_text", "text"}


def _payload(rec: Mapping[str, Any]) -> dict[str, Any]:
    payload = rec.get("payload")
    return payload if isinstance(payload, dict) else {}


def _ptype(rec: Mapping[str, Any]) -> str | None:
    return _payload(rec).get("type")


def _text(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text") for b in content if isinstance(b, dict) and b.get("type") in _TEXT_BLOCKS]
        joined = "\n".join(p for p in parts if p)
        return joined or None
    return None


def _parse_arguments(arguments: Any) -> dict[str, Any]:
    """function_call.arguments is a JSON string. Keep raw if it is not JSON."""
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            return {"_raw_arguments": arguments}
        return parsed if isinstance(parsed, dict) else {"_value": parsed}
    return {}


class CodexNormalizer(AgentTranscriptNormalizer):
    """Normalize a Codex CLI rollout JSONL transcript."""

    source = "codex"

    def to_events(self, records: Sequence[Mapping[str, Any]]) -> list[AnyEvent]:
        # Index tool-call outputs by call_id (function + custom tool variants).
        outputs: dict[str, dict[str, Any]] = {}
        for rec in records:
            if rec.get("type") != "response_item":
                continue
            p = _payload(rec)
            if p.get("type") in ("function_call_output", "custom_tool_call_output"):
                cid = p.get("call_id")
                if cid is not None:
                    outputs[str(cid)] = {"output": _output_text(p.get("output")), "ts": rec.get("timestamp")}

        trace_id = _trace_id(records)
        first_ts = _first_ts(records)
        user_input = _first_user_input(records)
        completed = any(rec.get("type") == "event_msg" and _ptype(rec) == "task_complete" for rec in records)

        events: list[AnyEvent] = [
            TraceStart(
                trace_id=trace_id,
                span_id="start",
                step_index=0,
                emitted_at=first_ts,
                agent_name=self.source,
                user_input=user_input,
                metadata=_session_metadata(records),
            )
        ]
        idx = 1

        for rec in records:
            if rec.get("type") != "response_item":
                continue
            p = _payload(rec)
            ptype = p.get("type")
            ts = parse_ts(rec.get("timestamp")) or first_ts

            if ptype == "message" and p.get("role") == "assistant":
                events.append(
                    LLMCall(
                        trace_id=trace_id,
                        span_id=f"llm-{idx}",
                        step_index=idx,
                        emitted_at=ts,
                        model=_model(records) or "unknown",
                        provider=_PROVIDER,
                        messages_in=[],
                        content_out=_text(p.get("content")),
                        started_at=ts,
                        ended_at=ts,
                    )
                )
                idx += 1

            elif ptype in ("function_call", "custom_tool_call"):
                cid = str(p.get("call_id"))
                result = outputs.get(cid, {})
                args = _parse_arguments(p.get("arguments")) if ptype == "function_call" else _custom_args(p)
                ended = parse_ts(result.get("ts")) or ts
                status = StepStatus.OK if p.get("status") in (None, "completed") else StepStatus.ERROR
                events.append(
                    ToolCall(
                        trace_id=trace_id,
                        span_id=cid,
                        step_index=idx,
                        emitted_at=ts,
                        tool_call_id=cid,
                        name=str(p.get("name")),
                        args=args,
                        output=result.get("output"),
                        status=status,
                        started_at=ts,
                        ended_at=ended,
                    )
                )
                idx += 1

            elif ptype == "web_search_call":
                action = p.get("action") or {}
                events.append(
                    Retrieval(
                        trace_id=trace_id,
                        span_id=f"search-{idx}",
                        step_index=idx,
                        emitted_at=ts,
                        query=str(action.get("query") or ""),
                        source="web_search",
                        started_at=ts,
                        ended_at=ts,
                    )
                )
                idx += 1

        events.append(
            TraceEnd(
                trace_id=trace_id,
                span_id=f"end-{idx}",
                step_index=idx,
                emitted_at=_last_ts(records) or first_ts,
                terminal_status=TerminalStatus.COMPLETED if completed else TerminalStatus.UNKNOWN,
                output_type=OutputType.TEXT,
            )
        )
        return events


def _output_text(output: Any) -> str | None:
    if output is None:
        return None
    if isinstance(output, str):
        return output
    return json.dumps(output)


def _custom_args(payload: Mapping[str, Any]) -> dict[str, Any]:
    """custom_tool_call carries a freeform ``input`` (e.g. an apply_patch body)."""
    raw = payload.get("input")
    if isinstance(raw, dict):
        return raw
    return {"input": raw if isinstance(raw, str) else json.dumps(raw)}


def _trace_id(records: Sequence[Mapping[str, Any]]) -> str:
    for rec in records:
        if rec.get("type") == "session_meta":
            sid = _payload(rec).get("id")
            if sid:
                return str(sid)
    return "unknown-codex-session"


def _model(records: Sequence[Mapping[str, Any]]) -> str | None:
    for rec in records:
        if rec.get("type") == "turn_context":
            model = _payload(rec).get("model")
            if model:
                return str(model)
    return None


def _first_ts(records: Sequence[Mapping[str, Any]]) -> datetime:
    for rec in records:
        ts = parse_ts(rec.get("timestamp"))
        if ts is not None:
            return ts
    return datetime.fromtimestamp(0, tz=UTC)


def _last_ts(records: Sequence[Mapping[str, Any]]) -> datetime | None:
    for rec in reversed(records):
        ts = parse_ts(rec.get("timestamp"))
        if ts is not None:
            return ts
    return None


def _first_user_input(records: Sequence[Mapping[str, Any]]) -> str | None:
    for rec in records:
        if rec.get("type") == "response_item":
            p = _payload(rec)
            if p.get("type") == "message" and p.get("role") == "user":
                text = _text(p.get("content"))
                if text:
                    return text
        if rec.get("type") == "event_msg" and _ptype(rec) == "user_message":
            msg = _payload(rec).get("message")
            if isinstance(msg, str) and msg:
                return msg
    return None


def _session_metadata(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    for rec in records:
        if rec.get("type") == "session_meta":
            p = _payload(rec)
            return {k: p.get(k) for k in ("cwd", "cli_version", "originator") if p.get(k) is not None}
    return {}
