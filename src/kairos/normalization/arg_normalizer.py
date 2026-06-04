"""Canonicalize tool arguments for comparison.

Normalizes tool args by sorting keys, lowercasing strings, stripping
ephemeral fields and patterns (UUIDs, unix timestamps). Does NOT mutate input.
"""

from __future__ import annotations

import re
from typing import Any, cast

# Fields to strip before comparison (ephemeral, non-semantic)
STRIP_FIELDS: set[str] = {
    "timestamp",
    "ts",
    "created_at",
    "updated_at",
    "request_id",
    "req_id",
    "trace_id",
    "span_id",
    "session_id",
    "session_token",
    "nonce",
    "idempotency_key",
    "x-request-id",
    "x-trace-id",
}

# Patterns to strip from string values
STRIP_PATTERNS: list[re.Pattern[str]] = [
    re.compile(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        re.I,
    ),
    re.compile(r"\d{10,13}"),  # Unix timestamps (10 or 13 digit)
]


def normalize_args(args: dict[str, Any] | None) -> dict[str, Any] | None:
    """Canonicalize tool arguments for Jaccard comparison.

    Returns a new dict (does not mutate input).
    """
    if args is None:
        return None
    return cast("dict[str, Any]", _normalize_value(args))


def _normalize_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _normalize_value(v) for k, v in sorted(value.items()) if k.lower() not in STRIP_FIELDS}
    if isinstance(value, list):
        return [_normalize_value(item) for item in value]
    if isinstance(value, str):
        normalized = value.lower().strip()
        for pattern in STRIP_PATTERNS:
            normalized = pattern.sub("", normalized)
        return normalized.strip()
    if isinstance(value, (int, float, bool)):
        return value
    return str(value).lower()
