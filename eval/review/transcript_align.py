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
from dataclasses import dataclass
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
