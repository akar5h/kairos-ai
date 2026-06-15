"""transcript_join.py — Live-path transcript enrichment: is_error correction + args.

Extracts ``tool_result.is_error`` flags and ``tool_use.input`` args from the
Claude Code session transcript and returns mappings of trace tool-step indices.

This is the SINGLE SOURCE OF TRUTH for that alignment logic; eval/review/
transcript_align.py covers the same ground for the review-app digest path and
MUST NOT be changed by this module (no import of eval code from src/).

Provenance note (TESTBED SCOPE):
    This module is a testbed-scoped enrichment only.  The durable fix is
    emitter-side: the OTel emitter (Claude Code tracer) should set
    ``success=false`` on ``tool.execution`` spans when the tool_result carries
    ``is_error: true`` AND populate tool args on the span.  Flag for the
    emitter-side roadmap item.  Until that fix ships, this module corrects
    live-Phoenix analysis in-process.

Public API::

    from kairos.readers.transcript_join import (
        tool_errors_from_transcript,
        tool_args_from_transcript,
    )

    # Returns {step_index: True} for every tool step the transcript marks
    # is_error=true.  Returns {} on any failure (missing transcript, no
    # session.id, unmatched window) — never raises.
    errors = tool_errors_from_transcript(wrapped_spans, steps)

    # Returns {step_index: dict} with REDACTED tool_use.input args for each
    # aligned tool step.  Returns {} on any failure — never raises.
    args = tool_args_from_transcript(wrapped_spans, steps)

Design: pure / filesystem-read (no network); always degrades gracefully.

Security: Bash command args are redacted to remove secrets (tokens, keys, etc.)
before populating step.tool_args.  See _redact_args().
"""

from __future__ import annotations

import glob
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kairos.log import get_logger

if TYPE_CHECKING:
    from kairos.models.trace import Step

logger = get_logger(__name__)

# ── Secret-redaction patterns ────────────────────────────────────────────────
# Applied to string values inside Bash command args to strip common secret shapes.
# Pattern list is additive; order does not matter.

_SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}", re.ASCII),           # OpenAI / Anthropic sk- keys
    re.compile(r"ghp_[A-Za-z0-9]{36}", re.ASCII),              # GitHub PAT
    re.compile(r"AKIA[0-9A-Z]{16}", re.ASCII),                 # AWS access key
    re.compile(r"Bearer\s+\S+", re.IGNORECASE),                # Bearer tokens
    re.compile(r"-----BEGIN [A-Z ]+-----.*?-----END [A-Z ]+-----", re.DOTALL),  # PEM certs
    # JWT: three base64url segments joined by dots
    re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+", re.ASCII),
    # DB URLs with embedded credentials: scheme://user:pass@host/...
    re.compile(
        r"(postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://[^:\s]+:[^@\s]+@\S+",
        re.IGNORECASE,
    ),
    # Assignment-style secrets: KEY=value or KEY: value (case-insensitive key match)
    re.compile(
        r"(?i)(api[_-]?key|secret|token|password|passwd|pwd|access[_-]?key)"
        r"(?:\s*[=:]\s*)\S+",
    ),
    # GitHub fine-grained PAT
    re.compile(r"github_pat_[A-Za-z0-9_]{22,}", re.ASCII),
    # Slack tokens
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}", re.ASCII),
]

_REDACTED = "[REDACTED]"

# ── Constants ─────────────────────────────────────────────────────────────────

TRANSCRIPT_GLOB = str(Path.home() / ".claude" / "projects" / "*" / "{session_id}.jsonl")

WINDOW_PAD_SECONDS = 60

# ── Transcript model ──────────────────────────────────────────────────────────


@dataclass
class _TranscriptCall:
    """Transcript tool invocation — name, is_error flag, and input args."""

    name: str
    is_error: bool
    ts: datetime | None
    args: dict[str, Any] = field(default_factory=dict)
    """tool_use.input dict — populated when the transcript carries it; empty otherwise."""


# ── Secret redaction ─────────────────────────────────────────────────────────


def _redact_value(v: Any) -> Any:
    """Recursively redact secrets from arg values."""
    if isinstance(v, str):
        result = v
        for pat in _SECRET_PATTERNS:
            result = pat.sub(_REDACTED, result)
        return result
    if isinstance(v, dict):
        return {k: _redact_value(val) for k, val in v.items()}
    if isinstance(v, list):
        return [_redact_value(item) for item in v]
    return v


def _redact_args(args: dict[str, Any]) -> dict[str, Any]:
    """Return a new dict with secret strings replaced by ``[REDACTED]``."""
    return {k: _redact_value(v) for k, v in args.items()}


# ── Locate transcript ─────────────────────────────────────────────────────────


def _find_transcript(session_id: str) -> Path | None:
    """Locate the Claude Code session transcript for a session id, or None."""
    pattern = TRANSCRIPT_GLOB.format(session_id=glob.escape(session_id))
    matches = glob.glob(pattern)
    return Path(matches[0]) if matches else None


# ── Parse timestamp ────────────────────────────────────────────────────────────


def _parse_ts(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


# ── Parse transcript ───────────────────────────────────────────────────────────


def _parse_transcript(path: Path) -> list[_TranscriptCall]:
    """Parse a session JSONL into an ordered list of _TranscriptCalls.

    Extracts name, is_error, and input args from tool_use / tool_result pairs.
    Order of appearance == execution order.
    """
    calls: list[_TranscriptCall] = []
    # Pending tool_use waiting for its tool_result: {tool_use_id: _TranscriptCall}
    pending: dict[str, _TranscriptCall] = {}

    try:
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
                        # Extract and redact the input args.
                        raw_input = item.get("input")
                        args = _redact_args(raw_input) if isinstance(raw_input, dict) else {}
                        call = _TranscriptCall(
                            name=str(item.get("name", "?")),
                            is_error=False,  # updated when result arrives
                            ts=ts,
                            args=args,
                        )
                        calls.append(call)
                        use_id = item.get("id")
                        if isinstance(use_id, str):
                            pending[use_id] = call
                    elif item.get("type") == "tool_result":
                        use_id = item.get("tool_use_id")
                        if isinstance(use_id, str) and use_id in pending:
                            pending[use_id].is_error = bool(item.get("is_error"))
    except OSError as exc:
        logger.warning(
            "transcript_join.read_error",
            path=str(path),
            error=str(exc),
        )
        return []

    return calls


# ── Window ────────────────────────────────────────────────────────────────────


def _window_calls(
    calls: list[_TranscriptCall],
    start: datetime | None,
    end: datetime | None,
    pad_seconds: int = WINDOW_PAD_SECONDS,
) -> list[_TranscriptCall]:
    """Filter calls to [start − pad, end + pad].

    A session spans multiple traces; the window isolates calls belonging to
    this trace.  Calls without a timestamp are dropped — cannot be placed.
    If start or end is unknown, no filtering — better a loose alignment.
    """
    if start is None or end is None:
        return calls
    pad = timedelta(seconds=pad_seconds)
    lo, hi = start - pad, end + pad
    return [c for c in calls if c.ts is not None and lo <= c.ts <= hi]


# ── Ordinal alignment (is_error extraction + args extraction) ────────────────


def _align_is_errors(
    steps: list[Step],
    calls: list[_TranscriptCall],
) -> dict[int, bool]:
    """Align trace tool steps to transcript calls by ordinal occurrence per tool name.

    Same semantics as eval/review/transcript_align.align_steps: k-th step
    named X ↔ k-th call named X in the window.  NEVER matches across names.
    Steps with no matching call are omitted (caller treats absent as no correction).

    Returns {step_index: is_error} ONLY for steps where is_error=True — callers
    only need the error set; absence means no correction.
    """
    from kairos.models.enums import StepType  # local import keeps module import-light

    calls_by_name: dict[str, list[_TranscriptCall]] = {}
    for call in calls:
        calls_by_name.setdefault(call.name, []).append(call)

    counters: dict[str, int] = {}
    errors: dict[int, bool] = {}

    for step in steps:
        if step.step_type != StepType.TOOL_CALL or not step.tool_name:
            continue
        name = step.tool_name
        k = counters.get(name, 0)
        counters[name] = k + 1
        pool = calls_by_name.get(name, [])
        if k < len(pool) and pool[k].is_error:
            errors[step.step_index] = True

    return errors


def _align_args(
    steps: list[Step],
    calls: list[_TranscriptCall],
) -> dict[int, dict[str, Any]]:
    """Align trace tool steps to transcript calls by ordinal and return their args.

    Same ordinal-per-tool-name alignment as ``_align_is_errors``.  Returns
    {step_index: args_dict} for EVERY aligned step that has a non-empty args dict
    in the transcript.  Steps with no match or with an empty args dict are omitted.

    Never raises.
    """
    from kairos.models.enums import StepType  # local import keeps module import-light

    calls_by_name: dict[str, list[_TranscriptCall]] = {}
    for call in calls:
        calls_by_name.setdefault(call.name, []).append(call)

    counters: dict[str, int] = {}
    result: dict[int, dict[str, Any]] = {}

    for step in steps:
        if step.step_type != StepType.TOOL_CALL or not step.tool_name:
            continue
        name = step.tool_name
        k = counters.get(name, 0)
        counters[name] = k + 1
        pool = calls_by_name.get(name, [])
        if k < len(pool) and pool[k].args:
            result[step.step_index] = pool[k].args

    return result


# ── Session-id extraction from spans ─────────────────────────────────────────


def _session_id_from_spans(spans: list[Any]) -> str | None:
    """Extract session.id from any span's attributes (first match wins)."""
    for span in spans:
        attrs = getattr(span, "attributes", None)
        if not isinstance(attrs, dict):
            continue
        sid = attrs.get("session.id")
        if isinstance(sid, str) and sid:
            return sid
    return None


# ── Time range from spans ─────────────────────────────────────────────────────


def _trace_time_range(spans: list[Any]) -> tuple[datetime | None, datetime | None]:
    """Return (min_start, max_end) across all spans as aware datetimes."""
    start_ns: int | None = None
    end_ns: int | None = None
    for span in spans:
        s = getattr(span, "start_time", None)
        e = getattr(span, "end_time", None)
        if isinstance(s, int) and s > 0:
            start_ns = s if start_ns is None else min(start_ns, s)
        if isinstance(e, int) and e > 0:
            end_ns = e if end_ns is None else max(end_ns, e)

    start_dt = datetime.fromtimestamp(start_ns / 1e9, tz=UTC) if start_ns is not None else None
    end_dt = datetime.fromtimestamp(end_ns / 1e9, tz=UTC) if end_ns is not None else None
    return start_dt, end_dt


# ── Internal: shared transcript fetch ────────────────────────────────────────


def _fetch_windowed_calls(
    spans: list[Any],
    steps: list[Step],
) -> list[_TranscriptCall]:
    """Shared pipeline: locate transcript, parse, window, and return calls.

    Returns an empty list on ANY failure (missing session.id, transcript not
    found, no window match, parse error) — caller must treat empty as "no
    data available".  Never raises.
    """
    session_id = _session_id_from_spans(spans)
    if not session_id:
        logger.debug(
            "transcript_join.no_session_id",
            hint="session.id absent on all spans — skipping transcript enrichment",
        )
        return []

    path = _find_transcript(session_id)
    if path is None:
        logger.debug(
            "transcript_join.transcript_not_found",
            session_id=session_id,
        )
        return []

    calls = _parse_transcript(path)
    if not calls:
        logger.debug(
            "transcript_join.no_calls_parsed",
            session_id=session_id,
            path=str(path),
        )
        return []

    start_dt, end_dt = _trace_time_range(spans)
    return _window_calls(calls, start_dt, end_dt)


# ── Public API ────────────────────────────────────────────────────────────────


def tool_errors_from_transcript(
    spans: list[Any],
    steps: list[Step],
) -> dict[int, bool]:
    """Return {step_index: True} for every tool step the transcript marks is_error=true.

    Pipeline:
      1. Extract session.id from span attributes.
      2. Locate transcript file (``~/.claude/projects/*/<session_id>.jsonl``).
      3. Parse tool_use / tool_result pairs.
      4. Window to trace time range (±60s pad).
      5. Align by ordinal per tool name; extract is_error=True entries only.

    Returns an empty dict on ANY failure (missing session.id, transcript not
    found, no window match, parse error) — caller must treat empty as "no
    correction available", not as "all steps clean".

    Never raises.  Logs a debug line when degrading gracefully.
    """
    windowed = _fetch_windowed_calls(spans, steps)
    if not windowed:
        return {}
    return _align_is_errors(steps, windowed)


def tool_args_from_transcript(
    spans: list[Any],
    steps: list[Step],
) -> dict[int, dict[str, Any]]:
    """Return {step_index: args_dict} with redacted tool_use.input args per aligned step.

    Uses the SAME ordinal-per-tool-name, time-windowed alignment as
    ``tool_errors_from_transcript`` — reuses ``_fetch_windowed_calls`` so the
    transcript is read only once per call site when callers invoke both.

    Args are redacted by ``_redact_args`` before being returned — secrets
    (API keys, Bearer tokens, etc.) in Bash commands are stripped.

    Returns an empty dict on ANY failure (missing session.id, transcript not
    found, no window match, parse error, no args in transcript).

    Never raises.
    """
    windowed = _fetch_windowed_calls(spans, steps)
    if not windowed:
        return {}
    return _align_args(steps, windowed)
