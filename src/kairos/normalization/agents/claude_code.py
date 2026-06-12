"""Claude Code transcript → TraceEnvelope.

Claude Code persists each session as a JSONL file under
``~/.claude/projects/<slug>/<session_id>.jsonl``. One JSON object per line.
Relevant line ``type``s:

- ``user``      — ``message.content`` is either a string (the human prompt) or
  a list of ``tool_result`` blocks (``{tool_use_id, content, is_error}``).
- ``assistant`` — ``message`` has ``model``, ``usage`` (token counts), and a
  ``content`` list of blocks: ``text``, ``thinking``, and ``tool_use``
  (``{id, name, input}``).

Mapping:

- first ``user`` line with string content → ``TraceStart`` (``user_input``)
- each ``assistant`` line → one ``LLMCall`` (text output, token usage, the
  ``tool_use`` blocks as ``tool_calls_emitted``)
- each ``tool_use`` block → one ``ToolCall``; its result is the matching
  ``tool_result`` (paired on ``tool_use_id``), carrying full args + output +
  error flag + timing (assistant ts → result ts)
- trailing ``TraceEnd`` (COMPLETED when the session ends on an assistant reply
  with no dangling tool call)

Token accounting includes cache tokens so input totals are not undercounted.
"""

from __future__ import annotations

import glob
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kairos.models.enums import OutputType, StepStatus, TerminalStatus
from kairos.normalization.agents.base import AgentTranscriptNormalizer, parse_ts

if TYPE_CHECKING:
    from kairos.models.trace import Step

from kairos.normalization.events import (
    AnyEvent,
    LLMCall,
    ToolCall,
    ToolCallEmitted,
    TraceEnd,
    TraceStart,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

_PROVIDER = "anthropic"

# Harness error prefixes anchored at character 0 of tool_output.
# These are the strings Claude Code's harness injects at the very start of a
# tool result when the invocation itself fails (not the tool's own error output).
_HARNESS_ERROR_PREFIXES: tuple[str, ...] = (
    "Error:",
    "InputValidationError",
    "PermissionError:",
)


def _blocks(message: Mapping[str, Any]) -> list[Any]:
    """Return the content blocks of a message as a list (string → []) ."""
    content = message.get("content")
    if isinstance(content, list):
        return content
    return []


def _string_content(message: Mapping[str, Any]) -> str | None:
    content = message.get("content")
    return content if isinstance(content, str) else None


def _text_from_blocks(blocks: list[Any]) -> str | None:
    parts = [b["text"] for b in blocks if isinstance(b, dict) and b.get("type") == "text" and b.get("text")]
    return "\n".join(parts) if parts else None


def _result_text(content: Any) -> str:
    """A tool_result's content may be a string or a list of text/content blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                parts.append(block.get("text") or json.dumps(block))
            else:
                parts.append(str(block))
        return "\n".join(parts)
    return json.dumps(content)


def _as_dict(value: Any) -> dict[str, Any]:
    """Coerce a tool-input value to a string-keyed dict (non-dict → empty)."""
    if isinstance(value, dict):
        return {str(k): v for k, v in value.items()}
    return {}


def _input_tokens(usage: Mapping[str, Any]) -> int | None:
    """Total input tokens including cache reads/creation (else they undercount)."""
    keys = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    values = [int(usage[k]) for k in keys if usage.get(k) is not None]
    return sum(values) if values else None


class ClaudeCodeNormalizer(AgentTranscriptNormalizer):
    """Normalize a Claude Code ``*.jsonl`` session transcript."""

    source = "claude_code"

    def step_outcome(self, step: Step) -> StepStatus | None:
        """Rung 3 adapter extractor for Claude Code steps.

        Checks, in order:
          1. ``exit_code`` attribute on Bash tool steps — 0 → OK, non-zero → ERROR.
             NOTE: live claude_code.tool spans do NOT carry ``exit_code`` in the
             current emitter version (2026-06-12); this check is wired for
             forward-compatibility and will silently skip when the attribute is absent.
          2. Harness error prefixes anchored at char 0 of tool_output.
             These indicate the harness itself failed to invoke the tool.

        Returns None when neither signal is present (no opinion).
        """
        # Check exit_code from raw span attrs (Bash tool, forward-compat).
        raw_attrs = step.attrs or {}
        exit_code_raw = raw_attrs.get("exit_code")
        if exit_code_raw is not None:
            try:
                return StepStatus.OK if int(exit_code_raw) == 0 else StepStatus.ERROR
            except (TypeError, ValueError):
                pass

        # Harness error prefixes at char 0 of tool_output.
        if step.tool_output and step.tool_output.startswith(_HARNESS_ERROR_PREFIXES):
            return StepStatus.ERROR

        return None

    def to_events(self, records: Sequence[Mapping[str, Any]]) -> list[AnyEvent]:
        # Pair tool_use_id → its tool_result (content / error / timestamp).
        results: dict[str, dict[str, Any]] = {}
        for rec in records:
            if rec.get("type") != "user":
                continue
            ts = rec.get("timestamp")
            for block in _blocks(rec.get("message", {})):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tid = block.get("tool_use_id")
                    if tid is not None:
                        results[tid] = {
                            "output": _result_text(block.get("content")),
                            "is_error": bool(block.get("is_error")),
                            "ts": ts,
                        }

        events: list[AnyEvent] = []
        idx = 0
        trace_id = _trace_id(records)
        first_ts: datetime | None = None
        last_assistant_text_only = False

        for rec in records:
            rtype = rec.get("type")
            ts = parse_ts(rec.get("timestamp"))
            if first_ts is None and ts is not None:
                first_ts = ts

            if rtype == "user":
                user_text = _string_content(rec.get("message", {}))
                if user_text and not events:
                    events.append(
                        TraceStart(
                            trace_id=trace_id,
                            span_id=str(rec.get("uuid") or f"start-{idx}"),
                            step_index=idx,
                            emitted_at=ts or _epoch(),
                            agent_name=self.source,
                            user_input=user_text,
                            metadata=_line_metadata(rec),
                        )
                    )
                    idx += 1
                continue

            if rtype != "assistant":
                continue

            message = rec.get("message", {})
            blocks = _blocks(message)
            tool_uses = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"]
            usage = message.get("usage") or {}
            span_id = str(rec.get("uuid") or f"llm-{idx}")
            emitted = ts or first_ts or _epoch()
            last_assistant_text_only = bool(_text_from_blocks(blocks)) and not tool_uses

            events.append(
                LLMCall(
                    trace_id=trace_id,
                    span_id=span_id,
                    parent_span_id=_parent(rec),
                    step_index=idx,
                    emitted_at=emitted,
                    model=message.get("model") or "unknown",
                    provider=_PROVIDER,
                    messages_in=[],
                    content_out=_text_from_blocks(blocks),
                    tool_calls_emitted=[
                        ToolCallEmitted(
                            id=str(b.get("id")),
                            name=str(b.get("name")),
                            args=_as_dict(b.get("input")),
                        )
                        for b in tool_uses
                    ],
                    input_tokens=_input_tokens(usage),
                    output_tokens=usage.get("output_tokens"),
                    total_tokens=_total_tokens(usage),
                    started_at=emitted,
                    ended_at=emitted,
                )
            )
            idx += 1

            for block in tool_uses:
                tid = str(block.get("id"))
                result = results.get(tid, {})
                started = emitted
                ended = parse_ts(result.get("ts")) or started
                is_error = bool(result.get("is_error"))
                events.append(
                    ToolCall(
                        trace_id=trace_id,
                        span_id=tid,
                        parent_span_id=span_id,
                        step_index=idx,
                        emitted_at=started,
                        tool_call_id=tid,
                        name=str(block.get("name")),
                        args=_as_dict(block.get("input")),
                        output=result.get("output"),
                        status=StepStatus.ERROR if is_error else StepStatus.OK,
                        error_message=result.get("output") if is_error else None,
                        started_at=started,
                        ended_at=ended,
                    )
                )
                idx += 1

        if events:
            terminal = TerminalStatus.COMPLETED if last_assistant_text_only else TerminalStatus.UNKNOWN
            last_ts = parse_ts(records[-1].get("timestamp")) if records else None
            events.append(
                TraceEnd(
                    trace_id=trace_id,
                    span_id=f"end-{idx}",
                    step_index=idx,
                    emitted_at=last_ts or first_ts or _epoch(),
                    terminal_status=terminal,
                    output_type=OutputType.TEXT,
                )
            )
        return events

    def normalize_session_file(self, path: str | Path) -> Any:
        """Convenience: normalize one Claude Code session JSONL file."""
        return self.normalize_jsonl(path)

    @staticmethod
    def discover_sessions(projects_root: str | Path | None = None) -> list[Path]:
        """List all Claude Code session transcripts under the projects root."""
        root = Path(projects_root) if projects_root else Path.home() / ".claude" / "projects"
        return sorted(Path(p) for p in glob.glob(str(root / "**" / "*.jsonl"), recursive=True))


def _trace_id(records: Sequence[Mapping[str, Any]]) -> str:
    for rec in records:
        sid = rec.get("sessionId")
        if sid:
            return str(sid)
    return "unknown-claude-code-session"


def _parent(rec: Mapping[str, Any]) -> str | None:
    parent = rec.get("parentUuid")
    return str(parent) if parent else None


def _line_metadata(rec: Mapping[str, Any]) -> dict[str, Any]:
    meta = {k: rec.get(k) for k in ("cwd", "gitBranch", "version", "sessionId") if rec.get(k) is not None}
    return meta


def _total_tokens(usage: Mapping[str, Any]) -> int | None:
    inp = _input_tokens(usage)
    out = usage.get("output_tokens")
    if inp is None and out is None:
        return None
    return (inp or 0) + (int(out) if out is not None else 0)


def _epoch() -> datetime:
    """Stable fallback when a transcript line carries no timestamp."""
    return datetime.fromtimestamp(0, tz=UTC)
