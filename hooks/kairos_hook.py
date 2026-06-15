#!/usr/bin/env python3
"""kairos_hook.py — Claude Code hook entrypoint for Kairos (F1.2).

Receives a hook event JSON payload on STDIN, redacts secrets from
tool_input, and appends one JSON line to a per-session spool file at
~/.kairos/spool/<session_id>.jsonl.

ALWAYS exits 0.  Any internal error is silently swallowed so this hook
can never disrupt the user's Claude Code session.  exit(2) would block
the tool — we never use it.

Environment variables consumed (NOT inherited OTEL_* vars — those are
not propagated to hook subprocesses):
  KAIROS_HOOK_DISABLED   — if set (any value), no-op exit 0.
  KAIROS_SPOOL_DIR       — override spool root (default ~/.kairos/spool).

Settings stanza to wire this hook in ~/.claude/settings.json
(install.sh / F1.3 will write this automatically):

    {
      "hooks": {
        "PostToolUse": [
          {
            "matcher": "",
            "hooks": [
              {
                "type": "command",
                "command": "/path/to/python3 /path/to/hooks/kairos_hook.py"
              }
            ]
          }
        ],
        "PostToolUseFailure": [
          {
            "matcher": "",
            "hooks": [
              {
                "type": "command",
                "command": "/path/to/python3 /path/to/hooks/kairos_hook.py"
              }
            ]
          }
        ],
        "SessionStart": [
          {
            "matcher": "",
            "hooks": [
              {
                "type": "command",
                "command": "/path/to/python3 /path/to/hooks/kairos_hook.py"
              }
            ]
          }
        ],
        "SessionEnd": [
          {
            "matcher": "",
            "hooks": [
              {
                "type": "command",
                "command": "/path/to/python3 /path/to/hooks/kairos_hook.py"
              }
            ]
          }
        ]
      }
    }

Spool format (one JSON line per event):
  {
    "session_id": "...",
    "event_name": "PostToolUse",
    "tool_use_id": "...",          # may be absent for SessionStart/End
    "tool_name": "...",            # PostToolUse / PostToolUseFailure only
    "tool_input_redacted": {...},  # redacted tool_input dict
    "tool_output": "...",          # string (PostToolUse / Failure only)
    "is_error": false,
    "permission_mode": "...",
    "agent_id": "...",
    "agent_type": "...",
    "payload_redacted": {...},     # full redacted payload — no data loss
    "occurred_at": "2026-06-16T..."
  }
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# ── Secret-redaction patterns ─────────────────────────────────────────────────
# IDENTICAL patterns to kairos.readers.transcript_join._SECRET_PATTERNS.
# Kept stdlib-only so this script runs without installing the package.

_SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}", re.ASCII),           # OpenAI / Anthropic sk- keys
    re.compile(r"ghp_[A-Za-z0-9]{36}", re.ASCII),              # GitHub PAT
    re.compile(r"AKIA[0-9A-Z]{16}", re.ASCII),                 # AWS access key
    re.compile(r"Bearer\s+\S+", re.IGNORECASE),                # Bearer tokens
    re.compile(r"-----BEGIN [A-Z ]+-----.*?-----END [A-Z ]+-----", re.DOTALL),  # PEM certs
    re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+", re.ASCII),  # JWT
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


def _redact_value(v: Any) -> Any:
    """Recursively redact secrets from a value (str/dict/list pass-through)."""
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


def _redact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a new dict with all string values recursively redacted."""
    return {k: _redact_value(v) for k, v in payload.items()}


# ── Spool helpers ─────────────────────────────────────────────────────────────


def _spool_dir() -> Path:
    override = os.environ.get("KAIROS_SPOOL_DIR", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".kairos" / "spool"


def _append_spool(session_id: str, record: dict[str, Any]) -> None:
    """Atomically-enough append one JSON line to ~/.kairos/spool/<session_id>.jsonl.

    Uses line-buffered write + flush + fsync to make partial writes unlikely.
    The spool directory is created if absent.
    """
    spool = _spool_dir()
    spool.mkdir(parents=True, exist_ok=True)
    spool_file = spool / f"{session_id}.jsonl"
    line = json.dumps(record, default=str) + "\n"
    with spool_file.open("a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())


# ── Main ──────────────────────────────────────────────────────────────────────


def _process(raw: str) -> None:
    """Parse raw stdin JSON, build redacted record, append to spool."""
    try:
        payload: dict[str, Any] = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return  # malformed input — silently drop

    if not isinstance(payload, dict):
        return

    session_id: str = str(payload.get("session_id") or "unknown")
    event_name: str = str(payload.get("hook_event_name") or "")

    # Redact before any persistence.
    redacted_payload = _redact_payload(payload)

    tool_input_raw = payload.get("tool_input")
    tool_input_redacted: dict[str, Any] | None = None
    if isinstance(tool_input_raw, dict):
        tool_input_redacted = _redact_value(tool_input_raw)

    tool_output_raw = payload.get("tool_output")
    tool_output: str | None = None
    if tool_output_raw is not None:
        tool_output = _redact_value(str(tool_output_raw))

    record: dict[str, Any] = {
        "session_id": session_id,
        "event_name": event_name,
        "tool_use_id": payload.get("tool_use_id"),
        "tool_name": payload.get("tool_name"),
        "tool_input_redacted": tool_input_redacted,
        "tool_output": tool_output,
        "is_error": payload.get("is_error"),
        "permission_mode": payload.get("permission_mode"),
        "agent_id": payload.get("agent_id"),
        "agent_type": payload.get("agent_type"),
        "payload_redacted": redacted_payload,
        "occurred_at": datetime.now(tz=UTC).isoformat(),
    }

    _append_spool(session_id, record)


def main() -> None:
    # Owner guard — set KAIROS_HOOK_DISABLED to disable without uninstalling.
    if os.environ.get("KAIROS_HOOK_DISABLED"):
        sys.exit(0)

    try:
        raw = sys.stdin.read()
        _process(raw)
    except Exception:  # noqa: BLE001,S110 — swallow everything; never break CC session
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
