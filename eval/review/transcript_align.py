"""transcript_align.py — Align trace steps to Claude Code session-transcript tool calls.

Phoenix spans are skeletons (tool_name + timing + success only — no args or
outputs, the F10 emitter limitation). The session transcript at
``~/.claude/projects/*/<session_id>.jsonl`` carries the real ``tool_use``
inputs and ``tool_result`` outputs. This module:

  1. locates the transcript via the ``session.id`` attribute on tool spans,
  2. parses tool_use / tool_result pairs (joined on ``tool_use_id``),
  3. windows the transcript to the trace's time range (±60s pad — one
     session spans multiple traces/heartbeats),
  4. aligns trace steps to transcript calls by ORDINAL OCCURRENCE PER TOOL
     NAME (k-th Bash step in trace ↔ k-th Bash tool_use in window).

Every digest string passes redaction before it leaves this module.
Security requirement, not a nice-to-have: live Bash args carry tokens.
"""

from __future__ import annotations

import glob
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kairos.models.trace import Step

# ── Constants ─────────────────────────────────────────────────────────────────

TRANSCRIPT_GLOB = "/Users/akarshgajbhiye/.claude/projects/*/{session_id}.jsonl"

ARGS_DIGEST_CHARS = 160
OUTPUT_DIGEST_CHARS = 240
WINDOW_PAD_SECONDS = 60

NO_MATCH = "(no transcript match)"

# Preferred arg field per tool — pick the field a human judges from.
_ARG_KEY_BY_TOOL: dict[str, str] = {
    "Bash": "command",
    "Read": "file_path",
    "Edit": "file_path",
    "Write": "file_path",
    "NotebookEdit": "notebook_path",
    "Grep": "pattern",
    "Glob": "pattern",
    "Skill": "skill",
    "Agent": "prompt",
    "Task": "prompt",
    "WebFetch": "url",
    "WebSearch": "query",
}

# Generic fallback order when the tool isn't in the table.
_ARG_KEY_FALLBACK: tuple[str, ...] = ("command", "file_path", "skill", "prompt", "pattern", "query", "url")

# ── Redaction ─────────────────────────────────────────────────────────────────
# Base patterns identical to scripts/export_spotcheck.py, extended with
# provider-token shapes (GitHub PAT, Slack, AWS, JWT, PEM blocks).
# Aggressive by design: a false redaction is cheap, a leaked credential is not.

REDACTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Bearer-with-value FIRST: "Authorization: Bearer <tok>" must consume the
    # token, not stop at the word "Bearer" (gap found in the export_spotcheck
    # ordering, where the keyword pattern ate "Bearer" and left the value).
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/$-]{4,}=*"), "[REDACTED]"),
    (
        re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization|bearer)\s*[=:]\s*(bearer\s+)?\S+"),
        "[REDACTED]",
    ),
    (re.compile(r"sk-[A-Za-z0-9-]{20,}"), "[REDACTED]"),
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "[REDACTED]"),
    (re.compile(r"gho_[A-Za-z0-9]{20,}"), "[REDACTED]"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "[REDACTED]"),
    (re.compile(r"xox[bpoas]-[A-Za-z0-9-]{10,}"), "[REDACTED]"),
    (re.compile(r"AKIA[A-Z0-9]{16}"), "[REDACTED]"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}(\.[A-Za-z0-9_-]+)?"), "[REDACTED]"),  # JWT
    (re.compile(r"-----BEGIN[A-Z ]*-----(.|\n)*?-----END[A-Z ]*-----"), "[REDACTED]"),
    (re.compile(r"-----BEGIN[A-Z ]*-----\S*"), "[REDACTED]"),  # PEM header remnant on one line
    (re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"), "[REDACTED]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[REDACTED]"),
    (re.compile(r"postgres(ql)?://\S+"), "[REDACTED]"),
]


def redact(text: str) -> str:
    """Apply all redaction patterns. Idempotent; safe on already-clean text."""
    for pattern, replacement in REDACTION_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _one_line(text: str, limit: int) -> str:
    """Collapse whitespace/newlines and truncate to limit chars."""
    flat = " ".join(text.split())
    return flat[:limit] + "…" if len(flat) > limit else flat


# ── Transcript model ──────────────────────────────────────────────────────────


@dataclass
class TranscriptCall:
    """One tool invocation reconstructed from the session transcript."""

    name: str
    tool_input: Any
    ts: datetime | None
    output: str | None = None
    is_error: bool = False


# ── Locate + parse ────────────────────────────────────────────────────────────


def find_transcript(session_id: str) -> Path | None:
    """Locate the Claude Code session transcript for a session id, or None."""
    matches = glob.glob(TRANSCRIPT_GLOB.format(session_id=glob.escape(session_id)))
    return Path(matches[0]) if matches else None


def _parse_ts(raw: Any) -> datetime | None:
    """Parse a transcript line timestamp (ISO-8601, may end in Z)."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _result_text(content: Any) -> str:
    """Flatten a tool_result content payload (str or list of text blocks) to str."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return str(content) if content is not None else ""


def parse_transcript(path: Path) -> list[TranscriptCall]:
    """Parse a session jsonl into an ordered list of TranscriptCalls.

    Assistant messages carry ``tool_use`` blocks {id, name, input}; the
    following user message carries matching ``tool_result`` blocks joined
    on ``tool_use_id``. Order of appearance == execution order.
    """
    calls: list[TranscriptCall] = []
    by_use_id: dict[str, TranscriptCall] = {}

    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            msg = rec.get("message")
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            ts = _parse_ts(rec.get("timestamp"))
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "tool_use":
                    call = TranscriptCall(
                        name=str(item.get("name", "?")),
                        tool_input=item.get("input"),
                        ts=ts,
                    )
                    calls.append(call)
                    use_id = item.get("id")
                    if isinstance(use_id, str):
                        by_use_id[use_id] = call
                elif item.get("type") == "tool_result":
                    use_id = item.get("tool_use_id")
                    if isinstance(use_id, str) and use_id in by_use_id:
                        target = by_use_id[use_id]
                        target.output = _result_text(item.get("content"))
                        target.is_error = bool(item.get("is_error"))

    return calls


# ── Windowing ─────────────────────────────────────────────────────────────────


def window_calls(
    calls: list[TranscriptCall],
    start: datetime | None,
    end: datetime | None,
    pad_seconds: int = WINDOW_PAD_SECONDS,
) -> list[TranscriptCall]:
    """Filter calls to [start − pad, end + pad].

    A session spans multiple traces/heartbeats; the window isolates the
    calls belonging to THIS trace. Calls without a timestamp are dropped
    (cannot be placed). If start or end is unknown, no filtering happens —
    better a loose alignment than none.
    """
    if start is None or end is None:
        return calls
    pad = timedelta(seconds=pad_seconds)
    lo, hi = start - pad, end + pad
    return [c for c in calls if c.ts is not None and lo <= c.ts <= hi]


# ── Ordinal alignment ─────────────────────────────────────────────────────────


def align_steps(steps: list[Step], calls: list[TranscriptCall]) -> dict[int, TranscriptCall | None]:
    """Align trace tool steps to transcript calls by ordinal occurrence per tool name.

    k-th step named X in the trace ↔ k-th call named X in the window. Both
    sequences come from the same execution, so per-name order matches even
    when interleaved with other tools. NEVER matches across names; a step
    with no k-th same-name call maps to None.

    Returns {step_index: TranscriptCall | None} for TOOL_CALL steps only.
    """
    from kairos.models.enums import StepType  # local import keeps module import-light for tests

    calls_by_name: dict[str, list[TranscriptCall]] = {}
    for call in calls:
        calls_by_name.setdefault(call.name, []).append(call)

    counters: dict[str, int] = {}
    aligned: dict[int, TranscriptCall | None] = {}

    for step in steps:
        if step.step_type != StepType.TOOL_CALL or not step.tool_name:
            continue
        name = step.tool_name
        k = counters.get(name, 0)
        counters[name] = k + 1
        pool = calls_by_name.get(name, [])
        aligned[step.step_index] = pool[k] if k < len(pool) else None

    return aligned


# ── Digest builders ───────────────────────────────────────────────────────────


def call_args_digest(call: TranscriptCall, limit: int = ARGS_DIGEST_CHARS) -> str:
    """Redacted one-line summary of a call's input (≤``limit`` chars).

    Picks the meaningful field per tool (command for Bash, file_path for
    Read/Edit/Write, pattern for Grep, …); falls back to compact JSON.
    """
    inp = call.tool_input
    if not isinstance(inp, dict):
        return redact(_one_line(str(inp), limit)) if inp is not None else ""
    preferred = _ARG_KEY_BY_TOOL.get(call.name)
    keys = (preferred, *_ARG_KEY_FALLBACK) if preferred else _ARG_KEY_FALLBACK
    for key in keys:
        if key and inp.get(key):
            return redact(_one_line(str(inp[key]), limit))
    if not inp:
        return ""
    return redact(_one_line(json.dumps(inp, default=str), limit))


def call_output_digest(call: TranscriptCall, limit: int = OUTPUT_DIGEST_CHARS) -> str:
    """Redacted first meaningful line of a call's tool_result (≤``limit`` chars)."""
    if not call.output:
        return ""
    for line in str(call.output).splitlines():
        if line.strip():
            return redact(_one_line(line, limit))
    return ""


# ── One-shot convenience ──────────────────────────────────────────────────────


def align_trace_to_transcript(
    steps: list[Step],
    session_id: str | None,
    start: datetime | None,
    end: datetime | None,
) -> dict[int, TranscriptCall | None]:
    """Full pipeline: locate transcript → parse → window → align.

    Returns an empty dict when the transcript is unresolvable (no session id
    or file not found) — callers then fall back to ``(no transcript match)``.
    """
    if not session_id:
        return {}
    path = find_transcript(session_id)
    if path is None:
        return {}
    calls = parse_transcript(path)
    windowed = window_calls(calls, start, end)
    return align_steps(steps, windowed)


# ── Full-content builders (labeling view) ─────────────────────────────────────
# The digest builders above one-line + truncate hard (160/240 chars) for the
# compact timeline. These return the FULL redacted content, multi-line, with a
# head+tail cap so a 50 KB test log doesn't bloat the queue JSON.

FULL_CONTENT_CHARS = 4000


def _head_tail(text: str, limit: int) -> str:
    """Keep first + last ``limit//2`` chars, eliding the middle (with a marker).

    Failures usually show at the tail (stack trace, final error) and the head
    (the command/intent), so both ends are preserved.
    """
    if len(text) <= limit:
        return text
    half = limit // 2
    elided = text[half:-half]
    n_lines = elided.count("\n") + 1
    return f"{text[:half]}\n…({n_lines} lines elided)…\n{text[-half:]}"


def call_full_input(call: TranscriptCall, limit: int = FULL_CONTENT_CHARS) -> str:
    """Full redacted, multi-line tool input (≤``limit`` chars, head+tail capped).

    Prefers the meaningful field per tool (full value, not one-lined); falls
    back to indented JSON of the whole input dict.
    """
    inp = call.tool_input
    if inp is None:
        return ""
    if isinstance(inp, dict):
        preferred = _ARG_KEY_BY_TOOL.get(call.name)
        keys = (preferred, *_ARG_KEY_FALLBACK) if preferred else _ARG_KEY_FALLBACK
        rendered: str | None = None
        for key in keys:
            if key and inp.get(key):
                rendered = str(inp[key])
                break
        if rendered is None:
            rendered = json.dumps(inp, indent=2, default=str) if inp else ""
    else:
        rendered = str(inp)
    return redact(_head_tail(rendered, limit))


def call_full_output(call: TranscriptCall, limit: int = FULL_CONTENT_CHARS) -> str:
    """Full redacted, multi-line tool_result (≤``limit`` chars, head+tail capped)."""
    if not call.output:
        return ""
    return redact(_head_tail(str(call.output), limit))


# ── Conversation frame (task / intent / interrupts / surface) ─────────────────
# Tool I/O alone can't answer "mechanical or failure?". The frame around the
# tools — what the user asked, whether the user interrupted, whether it was an
# API error vs an agent error — is the signal that resolves the ambiguity.

_FRAME_TEXT_CHARS = 600


@dataclass
class FrameEvent:
    """One non-tool conversational event from the transcript.

    ``kind`` ∈ {"user", "interrupt", "assistant_text", "api_error"}.
    ``text`` is already redacted and one-lined (≤``_FRAME_TEXT_CHARS``).
    """

    kind: str
    ts: datetime | None
    text: str


@dataclass
class TranscriptFrame:
    """Session-level context + the ordered non-tool event stream."""

    cwd: str | None = None
    git_branch: str | None = None
    version: str | None = None
    model: str | None = None
    permission_mode: str | None = None
    events: list[FrameEvent] = field(default_factory=list)


def _msg_text(msg: Any) -> str:
    """Flatten a message's text content (str or list of blocks) to a string."""
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return "\n".join(parts)
    return ""


def _is_command_noise(text: str) -> bool:
    """True for slash-command / local-command meta lines that aren't real turns."""
    stripped = text.lstrip()
    if stripped.startswith(("<command-name>", "<local-command", "<command-message>")):
        return True
    return bool(stripped.startswith("/") and len(stripped.split()) <= 2)


def parse_frame(path: Path) -> TranscriptFrame:
    """Single walk of a session jsonl → session context + non-tool events.

    Tool calls are intentionally NOT duplicated here — they come from the
    TranscriptCall path. Every emitted ``text`` is redacted.
    """
    frame = TranscriptFrame()

    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue

            # Session context — first non-null wins.
            if frame.cwd is None and rec.get("cwd"):
                frame.cwd = str(rec["cwd"])
            if frame.git_branch is None and rec.get("gitBranch"):
                frame.git_branch = str(rec["gitBranch"])
            if frame.version is None and rec.get("version"):
                frame.version = str(rec["version"])
            if frame.permission_mode is None and rec.get("permissionMode"):
                frame.permission_mode = str(rec["permissionMode"])

            ts = _parse_ts(rec.get("timestamp"))
            rec_type = rec.get("type")
            msg = rec.get("message")
            if frame.model is None and isinstance(msg, dict) and msg.get("model"):
                frame.model = str(msg["model"])

            # API error markers — infra failure, distinct from agent failure.
            if rec.get("isApiErrorMessage"):
                status = rec.get("apiErrorStatus") or ""
                frame.events.append(FrameEvent("api_error", ts, redact(f"API error {status}".strip())))
                continue

            if rec.get("isMeta"):
                continue

            text = _msg_text(msg)
            if not text.strip():
                continue

            if rec_type == "user":
                if _is_command_noise(text):
                    continue
                kind = (
                    "interrupt"
                    if ("interrupted by user" in text.lower() or rec.get("interruptedMessageId"))
                    else "user"
                )
                frame.events.append(FrameEvent(kind, ts, redact(_one_line(text, _FRAME_TEXT_CHARS))))
            elif rec_type == "assistant":
                frame.events.append(
                    FrameEvent("assistant_text", ts, redact(_one_line(text, _FRAME_TEXT_CHARS)))
                )

    return frame


def window_frame_events(
    events: list[FrameEvent],
    start: datetime | None,
    end: datetime | None,
    pad_seconds: int = WINDOW_PAD_SECONDS,
) -> list[FrameEvent]:
    """Filter frame events to the trace window [start − pad, end + pad].

    Mirrors ``window_calls``: when start or end is unknown, no filtering (a
    loose frame beats none); events without a ts are dropped only when the
    window is defined.
    """
    if start is None or end is None:
        return events
    pad = timedelta(seconds=pad_seconds)
    lo, hi = start - pad, end + pad
    return [e for e in events if e.ts is not None and lo <= e.ts <= hi]


def first_assistant_text_after(events: list[FrameEvent], ts: datetime | None) -> str | None:
    """First assistant_text event chronologically after ``ts`` (the reaction)."""
    if ts is None:
        return None
    for e in events:
        if e.kind == "assistant_text" and e.ts is not None and e.ts > ts:
            return e.text
    return None


def attach_reactions(
    step_entries: list[dict[str, Any]],
    transcript_map: dict[int, TranscriptCall | None],
    windowed_events: list[FrameEvent],
    *,
    restart_indices: frozenset[int] | None = None,
    max_chars: int = 400,
) -> None:
    """Mutate step dicts in place: attach the agent's reaction after a failure.

    For each error step (``status=="error"`` or ``is_error_struct``) — plus any
    restart-boundary step when ``restart_indices`` is given — set ``reaction``
    to the first assistant_text turn that followed it. This is the strongest
    haywire-vs-recovered tell: what the agent SAID it would do next.
    """
    restart = restart_indices or frozenset()
    for se in step_entries:
        idx = se.get("index")
        interesting = se.get("status") == "error" or se.get("is_error_struct") or idx in restart
        if not interesting:
            continue
        call = transcript_map.get(idx) if idx is not None else None
        ts = call.ts if call is not None else None
        reaction = first_assistant_text_after(windowed_events, ts)
        if reaction:
            se["reaction"] = reaction[:max_chars]


def load_windowed_frame(
    session_id: str | None,
    start: datetime | None,
    end: datetime | None,
) -> tuple[TranscriptFrame | None, list[FrameEvent]]:
    """Locate → parse → window the conversation frame for a trace.

    Returns ``(None, [])`` when the transcript is unresolvable.
    """
    if not session_id:
        return None, []
    path = find_transcript(session_id)
    if path is None:
        return None, []
    frame = parse_frame(path)
    return frame, window_frame_events(frame.events, start, end)


def build_conversation(
    session_id: str | None,
    start: datetime | None,
    end: datetime | None,
    *,
    restart_indices: list[int] | None = None,  # noqa: ARG001 — reserved, not used here
    max_items: int = 400,
) -> list[dict[str, Any]]:
    """Merge windowed frame events and tool calls into ONE time-ordered conversation.

    Each item is a dict with ``kind`` ∈
    {"user","interrupt","assistant_text","api_error","tool","truncated"}.

    Frame events carry:
        kind, ts_offset_s, text   (text already redacted by parse_frame)

    Tool-call items carry:
        kind="tool", ts_offset_s, tool, input_full, output_full, is_error

    Items without a timestamp sort to the end (stable). The raw ``ts``
    datetime is dropped before returning (not JSON-friendly). If the list
    exceeds ``max_items`` the tail is dropped and a final
    ``{"kind":"truncated","dropped":N}`` item is appended.

    Returns [] when the transcript cannot be located.
    restart_indices is accepted but intentionally not used: restart marking
    belongs to the step-timeline path (which has step-index context).
    """
    if not session_id:
        return []
    path = find_transcript(session_id)
    if path is None:
        return []

    frame = parse_frame(path)
    calls = parse_transcript(path)

    windowed_events = window_frame_events(frame.events, start, end)
    windowed_calls = window_calls(calls, start, end)

    def _offset(ts: datetime | None) -> float | None:
        if ts is None or start is None:
            return None
        return round((ts - start).total_seconds(), 1)

    items: list[tuple[datetime | None, dict[str, Any]]] = []

    for ev in windowed_events:
        items.append(
            (
                ev.ts,
                {
                    "kind": ev.kind,
                    "ts_offset_s": _offset(ev.ts),
                    "text": ev.text,  # already redacted by parse_frame
                },
            )
        )

    for call in windowed_calls:
        items.append(
            (
                call.ts,
                {
                    "kind": "tool",
                    "ts_offset_s": _offset(call.ts),
                    "tool": call.name,
                    "input_full": call_full_input(call),
                    "output_full": call_full_output(call),
                    "is_error": bool(call.is_error),
                },
            )
        )

    # Sort: items with a ts sort by ts; None-ts items sort to the end (stable).
    items.sort(key=lambda pair: (pair[0] is None, pair[0]))

    result = [item for _, item in items]

    if len(result) > max_items:
        dropped = len(result) - max_items
        result = result[:max_items]
        result.append({"kind": "truncated", "dropped": dropped})

    return result


def entry_frame_fields(
    frame: TranscriptFrame | None,
    windowed: list[FrameEvent],
    trace_start: datetime | None,
    *,
    max_user_events: int = 30,
    task_chars: int = 800,
) -> dict[str, Any]:
    """Build the entry-level frame fields: task, surface, user_events.

    ``task`` is the first windowed user/interrupt turn (the originating
    request). ``user_events`` excludes assistant_text (that feeds per-step
    reactions, not the spine).
    """
    if frame is None:
        return {"task": "", "surface": {}, "user_events": []}

    def _offset(ev: FrameEvent) -> float | None:
        if ev.ts is None or trace_start is None:
            return None
        return round((ev.ts - trace_start).total_seconds(), 1)

    task = ""
    for ev in windowed:
        if ev.kind in ("user", "interrupt"):
            task = ev.text[:task_chars]
            break

    user_events = [
        {"kind": ev.kind, "ts_offset_s": _offset(ev), "text": ev.text}
        for ev in windowed
        if ev.kind in ("user", "interrupt", "api_error")
    ][:max_user_events]

    surface = {
        "cwd": redact(frame.cwd) if frame.cwd else None,
        "git_branch": frame.git_branch,
        "version": frame.version,
        "model": frame.model,
        "permission_mode": frame.permission_mode,
    }

    return {"task": task, "surface": surface, "user_events": user_events}
