"""OpenCode session → TraceEnvelope.

OpenCode persists a session as JSON files under its data dir
(``~/.local/share/opencode/storage`` by default):

- ``storage/message/<sessionID>/<messageID>.json`` — one message:
  ``{id, role: "user"|"assistant", sessionID, time:{created, completed},
     modelID, providerID, tokens:{input, output, reasoning, cache:{read,write}}}``
- ``storage/part/<sessionID>/<messageID>/<partID>.json`` — message parts:
  ``{type: "text"|"reasoning"|"tool"|"step-start"|"step-finish", text, tool,
     callID, state:{status, input, output, time:{start,end}}}``

Mapping: each assistant message → one ``LLMCall`` (text parts joined, token
usage incl. cache); each ``tool`` part → one ``ToolCall`` carrying the full
``state.input`` args, ``state.output`` result, status (``error`` → ERROR), and
``state.time`` timing — input and output live on the same part, so pairing is
exact. The first user message's text → ``TraceStart.user_input``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kairos.models.enums import OutputType, StepStatus, TerminalStatus
from kairos.normalization.agents.base import AgentTranscriptNormalizer, parse_ts
from kairos.normalization.events import (
    AnyEvent,
    LLMCall,
    ToolCall,
    TraceEnd,
    TraceStart,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


def _ms_ts(value: Any) -> datetime | None:
    """OpenCode times are epoch milliseconds."""
    if value is None:
        return None
    if isinstance(value, bool):
        msg = f"cannot parse bool as timestamp: {value!r}"
        raise ValueError(msg)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000.0, tz=UTC)
    return parse_ts(value)


def _text_parts(parts: list[Any]) -> str | None:
    out = [str(p["text"]) for p in parts if isinstance(p, dict) and p.get("type") == "text" and p.get("text")]
    return "\n".join(out) if out else None


def _input_tokens(tokens: Mapping[str, Any]) -> int | None:
    if not tokens:
        return None
    base = tokens.get("input")
    cache = tokens.get("cache") or {}
    total = (int(base) if base is not None else 0) + _cache_total(cache)
    return total if (base is not None or cache) else None


def _cache_total(cache: Mapping[str, Any]) -> int:
    return sum(int(cache[k]) for k in ("read", "write") if cache.get(k) is not None)


def _serialize(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value)


class OpenCodeNormalizer(AgentTranscriptNormalizer):
    """Normalize an OpenCode session (message + part JSON files)."""

    source = "opencode"

    def to_events(self, records: Sequence[Mapping[str, Any]]) -> list[AnyEvent]:
        # records: messages (with embedded "parts"), in chronological order.
        messages = sorted(records, key=lambda m: _created(m))
        trace_id = _trace_id(messages)
        first_ts = _first_ts(messages)

        events: list[AnyEvent] = []
        idx = 0
        for msg in messages:
            role = msg.get("role")
            parts = msg.get("parts") or []
            created = _ms_ts(_time(msg).get("created")) or first_ts

            if role == "user":
                if not events:
                    events.append(
                        TraceStart(
                            trace_id=trace_id,
                            span_id=str(msg.get("id") or f"start-{idx}"),
                            step_index=idx,
                            emitted_at=created,
                            agent_name=self.source,
                            user_input=_text_parts(parts),
                            metadata=None,
                        )
                    )
                    idx += 1
                continue

            if role != "assistant":
                continue

            completed = _ms_ts(_time(msg).get("completed")) or created
            tokens = msg.get("tokens") or {}
            span_id = str(msg.get("id") or f"llm-{idx}")
            events.append(
                LLMCall(
                    trace_id=trace_id,
                    span_id=span_id,
                    step_index=idx,
                    emitted_at=created,
                    model=str(msg.get("modelID") or "unknown"),
                    provider=str(msg.get("providerID") or "unknown"),
                    messages_in=[],
                    content_out=_text_parts(parts),
                    input_tokens=_input_tokens(tokens),
                    output_tokens=tokens.get("output"),
                    total_tokens=_total_tokens(tokens),
                    started_at=created,
                    ended_at=completed,
                )
            )
            idx += 1

            for part in parts:
                if not (isinstance(part, dict) and part.get("type") == "tool"):
                    continue
                state = part.get("state") or {}
                t_start = _ms_ts((state.get("time") or {}).get("start")) or created
                t_end = _ms_ts((state.get("time") or {}).get("end")) or t_start
                status = StepStatus.ERROR if state.get("status") == "error" else StepStatus.OK
                error = state.get("error") if status is StepStatus.ERROR else None
                call_id = str(part.get("callID") or f"{span_id}-tool-{idx}")
                events.append(
                    ToolCall(
                        trace_id=trace_id,
                        span_id=call_id,
                        parent_span_id=span_id,
                        step_index=idx,
                        emitted_at=t_start,
                        tool_call_id=call_id,
                        name=str(part.get("tool") or "unknown"),
                        args=_args(state.get("input")),
                        output=_serialize(state.get("output")),
                        status=status,
                        error_message=_serialize(error),
                        started_at=t_start,
                        ended_at=t_end,
                    )
                )
                idx += 1

        if events:
            events.append(
                TraceEnd(
                    trace_id=trace_id,
                    span_id=f"end-{idx}",
                    step_index=idx,
                    emitted_at=_last_ts(messages) or first_ts,
                    terminal_status=TerminalStatus.COMPLETED,
                    output_type=OutputType.TEXT,
                )
            )
        return events

    def normalize_session(self, session_id: str, storage_root: str | Path | None = None) -> Any:
        """Load a session's message + part files from disk and normalize it."""
        return self.normalize(self.load_session(session_id, storage_root))

    @staticmethod
    def load_session(session_id: str, storage_root: str | Path | None = None) -> list[dict[str, Any]]:
        """Assemble a session's messages (with embedded parts) from storage."""
        root = Path(storage_root) if storage_root else Path.home() / ".local" / "share" / "opencode" / "storage"
        msg_dir = root / "message" / session_id
        part_root = root / "part" / session_id
        records: list[dict[str, Any]] = []
        for msg_file in sorted(msg_dir.glob("*.json")):
            message = json.loads(msg_file.read_text(encoding="utf-8"))
            part_dir = part_root / msg_file.stem
            parts = (
                [json.loads(p.read_text(encoding="utf-8")) for p in sorted(part_dir.glob("*.json"))]
                if part_dir.is_dir()
                else []
            )
            message["parts"] = parts
            records.append(message)
        return records

    @staticmethod
    def discover_sessions(storage_root: str | Path | None = None) -> list[str]:
        """List session ids that have stored messages."""
        root = Path(storage_root) if storage_root else Path.home() / ".local" / "share" / "opencode" / "storage"
        msg_root = root / "message"
        if not msg_root.is_dir():
            return []
        return sorted(p.name for p in msg_root.iterdir() if p.is_dir())


def _args(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return {str(k): v for k, v in value.items()}
    return {}


def _time(msg: Mapping[str, Any]) -> dict[str, Any]:
    time = msg.get("time")
    return time if isinstance(time, dict) else {}


def _created(msg: Mapping[str, Any]) -> float:
    created = _time(msg).get("created")
    return float(created) if isinstance(created, (int, float)) else 0.0


def _trace_id(messages: Sequence[Mapping[str, Any]]) -> str:
    for msg in messages:
        sid = msg.get("sessionID")
        if sid:
            return str(sid)
    return "unknown-opencode-session"


def _first_ts(messages: Sequence[Mapping[str, Any]]) -> datetime:
    for msg in messages:
        ts = _ms_ts(_time(msg).get("created"))
        if ts is not None:
            return ts
    return datetime.fromtimestamp(0, tz=UTC)


def _last_ts(messages: Sequence[Mapping[str, Any]]) -> datetime | None:
    last: datetime | None = None
    for msg in messages:
        ts = _ms_ts(_time(msg).get("completed")) or _ms_ts(_time(msg).get("created"))
        if ts is not None:
            last = ts
    return last


def _total_tokens(tokens: Mapping[str, Any]) -> int | None:
    inp = _input_tokens(tokens)
    out = tokens.get("output")
    reasoning = tokens.get("reasoning")
    if inp is None and out is None and reasoning is None:
        return None
    return (inp or 0) + (int(out) if out is not None else 0) + (int(reasoning) if reasoning is not None else 0)
