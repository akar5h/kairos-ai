"""Tests for blocked_on_user session-end detection (Day 3 — terminal mapping).

Spec requirement (sprint-exec-1-truth.md §5):
  Conservative: HUMAN_ESCALATION only when the literal final span of the trace
  (by end_time, excluding task root) is blocked_on_user AND no llm_request
  follows it.

W3 test matrix entry:
  blocked_on_user session → HUMAN_ESCALATION, pass-eligible, escalation_rate counts
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from kairos.models.enums import TerminalStatus
from kairos.readers.phoenix import _is_session_end_blocked_on_user, _phoenix_dict_to_span, spans_to_envelope

_FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _make_span(
    name: str,
    start_ns: int,
    end_ns: int,
    *,
    parent_id: str | None = None,
    attrs: dict[str, Any] | None = None,
) -> Any:
    """Build a minimal Phoenix-dict span and convert to _PhoenixSpan."""
    d: dict[str, Any] = {
        "name": name,
        "attributes": attrs or {},
        "context": {
            "trace_id": "a" * 32,
            "span_id": f"{abs(hash(name + str(start_ns))):016x}"[:16],
        },
        "parent_id": parent_id,
        "status_code": "UNSET",
        "status_message": None,
        "start_time": _ns_to_iso(start_ns),
        "end_time": _ns_to_iso(end_ns),
        "events": [],
    }
    return _phoenix_dict_to_span(d)


def _ns_to_iso(ns: int) -> str:
    from datetime import UTC, datetime

    dt = datetime.fromtimestamp(ns / 1e9, tz=UTC)
    return dt.isoformat()


# ─────────────────── _is_session_end_blocked_on_user ────────────────────


# Use realistic nanosecond timestamps (2026-01-01 epoch region)
_T0 = 1_750_000_000_000_000_000  # ns baseline


def test_blocked_on_user_as_final_span_is_detected() -> None:
    """Session ends with blocked_on_user as the last non-task span."""
    interaction = _make_span("claude_code.interaction", _T0, _T0 + 5_000_000_000, attrs={"span.type": "interaction"})
    llm1 = _make_span("claude_code.llm_request", _T0 + 100_000_000, _T0 + 2_000_000_000)
    blocked = _make_span("claude_code.tool.blocked_on_user", _T0 + 2_100_000_000, _T0 + 2_500_000_000)

    # blocked ends AFTER llm1 ends AND no llm_request starts after blocked.end_time
    assert _is_session_end_blocked_on_user([interaction, llm1, blocked]) is True


def test_blocked_on_user_followed_by_llm_is_not_session_end() -> None:
    """Mid-trace blocked_on_user (permission phase) — llm_request follows it."""
    interaction = _make_span("claude_code.interaction", _T0, _T0 + 10_000_000_000, attrs={"span.type": "interaction"})
    blocked = _make_span("claude_code.tool.blocked_on_user", _T0 + 2_000_000_000, _T0 + 2_500_000_000)
    llm_after = _make_span(
        "claude_code.llm_request", _T0 + 3_000_000_000, _T0 + 4_000_000_000
    )  # starts AFTER blocked.end

    assert _is_session_end_blocked_on_user([interaction, blocked, llm_after]) is False


def test_no_blocked_on_user_returns_false() -> None:
    """Normal trace without blocked_on_user."""
    interaction = _make_span("claude_code.interaction", _T0, _T0 + 5_000_000_000, attrs={"span.type": "interaction"})
    llm = _make_span("claude_code.llm_request", _T0 + 100_000_000, _T0 + 2_000_000_000)
    tool = _make_span(
        "claude_code.tool", _T0 + 2_100_000_000, _T0 + 2_500_000_000, attrs={"span.type": "tool", "tool_name": "Read"}
    )

    assert _is_session_end_blocked_on_user([interaction, llm, tool]) is False


def test_empty_span_list_returns_false() -> None:
    assert _is_session_end_blocked_on_user([]) is False


def test_only_task_span_returns_false() -> None:
    interaction = _make_span("claude_code.interaction", _T0, _T0 + 5_000_000_000, attrs={"span.type": "interaction"})
    assert _is_session_end_blocked_on_user([interaction]) is False


# ─────────────────── spans_to_envelope terminal mapping ─────────────────


def test_spans_to_envelope_human_escalation_terminal_status() -> None:
    """When session ends blocked, envelope has HUMAN_ESCALATION terminal status."""
    # Build a minimal trace that ends on blocked_on_user.
    interaction_dict: dict[str, Any] = {
        "name": "claude_code.interaction",
        "attributes": {"span.type": "interaction", "kairos.agent.name": "test-agent"},
        "context": {"trace_id": "b" * 32, "span_id": "a" * 16},
        "parent_id": None,
        "status_code": "UNSET",
        "status_message": None,
        "start_time": _ns_to_iso(1_000_000_000),
        "end_time": _ns_to_iso(5_000_000_000),
        "events": [],
    }
    llm_dict: dict[str, Any] = {
        "name": "claude_code.llm_request",
        "attributes": {"span.type": "llm_request", "gen_ai.system": "anthropic", "gen_ai.request.model": "claude"},
        "context": {"trace_id": "b" * 32, "span_id": "b" * 16},
        "parent_id": "a" * 16,
        "status_code": "UNSET",
        "status_message": None,
        "start_time": _ns_to_iso(1_100_000_000),
        "end_time": _ns_to_iso(2_000_000_000),
        "events": [],
    }
    blocked_dict: dict[str, Any] = {
        "name": "claude_code.tool.blocked_on_user",
        "attributes": {"span.type": "tool.blocked_on_user"},
        "context": {"trace_id": "b" * 32, "span_id": "c" * 16},
        "parent_id": "a" * 16,
        "status_code": "UNSET",
        "status_message": None,
        "start_time": _ns_to_iso(2_100_000_000),
        "end_time": _ns_to_iso(2_500_000_000),
        "events": [],
    }

    envelope = spans_to_envelope([interaction_dict, llm_dict, blocked_dict])
    assert envelope.terminal_status == TerminalStatus.HUMAN_ESCALATION


def test_spans_to_envelope_mid_trace_blocked_stays_completed() -> None:
    """Mid-trace blocked_on_user does NOT override terminal status."""
    interaction_dict: dict[str, Any] = {
        "name": "claude_code.interaction",
        "attributes": {"span.type": "interaction", "kairos.agent.name": "test-agent"},
        "context": {"trace_id": "c" * 32, "span_id": "a" * 16},
        "parent_id": None,
        "status_code": "UNSET",
        "status_message": None,
        "start_time": _ns_to_iso(1_000_000_000),
        "end_time": _ns_to_iso(10_000_000_000),
        "events": [],
    }
    blocked_dict: dict[str, Any] = {
        "name": "claude_code.tool.blocked_on_user",
        "attributes": {"span.type": "tool.blocked_on_user"},
        "context": {"trace_id": "c" * 32, "span_id": "b" * 16},
        "parent_id": "a" * 16,
        "status_code": "UNSET",
        "status_message": None,
        "start_time": _ns_to_iso(2_000_000_000),
        "end_time": _ns_to_iso(2_500_000_000),
        "events": [],
    }
    llm_after_dict: dict[str, Any] = {
        "name": "claude_code.llm_request",
        "attributes": {"span.type": "llm_request", "gen_ai.system": "anthropic", "gen_ai.request.model": "claude"},
        "context": {"trace_id": "c" * 32, "span_id": "c" * 16},
        "parent_id": "a" * 16,
        "status_code": "UNSET",
        "status_message": None,
        "start_time": _ns_to_iso(3_000_000_000),  # starts AFTER blocked ended
        "end_time": _ns_to_iso(4_000_000_000),
        "events": [],
    }

    envelope = spans_to_envelope([interaction_dict, blocked_dict, llm_after_dict])
    # A subsequent llm_request exists → not a session-end block → COMPLETED (default)
    assert envelope.terminal_status == TerminalStatus.COMPLETED


def test_real_fixture_trace_is_not_session_blocked() -> None:
    """The real claude_code_trace.json fixture ends with llm_request → not HUMAN_ESCALATION.

    In the fixture: blocked_on_user has the earliest timestamps but the trace ends with
    a second llm_request (end_turn). The conservative check must correctly NOT flag it.
    """
    raw = json.loads((_FIXTURE_DIR / "claude_code_trace.json").read_text())
    envelope = spans_to_envelope(raw)
    # The fixture trace ends with llm_request (stop_reason=end_turn) — should be COMPLETED.
    assert envelope.terminal_status == TerminalStatus.COMPLETED
