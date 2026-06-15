"""Coordination-context classifier — deterministic, no LLM, no network.

Flags traces that are control-plane coordination sessions (e.g. scheduler
heartbeats, inbox-poll re-entries, token re-derivation) rather than genuine
agent-quality work.  A Finding with severity ``"info"`` is returned when the
trace's user_input matches any configured marker phrase OR any step matches a
configured tool signature.

The detector is generic: the marker strings and tool signatures live in
``BusinessContext.coordination_markers`` / ``BusinessContext.coordination_tools``
(config/context.yaml).  No coordination-framework names are hardcoded here.

Tool signature matching semantics
----------------------------------
Each entry in ``coordination_tools`` is one of:
  - ``"ToolName"``            — step.tool_name == ToolName (exact, case-sensitive).
  - ``"ToolName:substring"``  — tool_name matches AND the step's combined args
                                string contains ``substring`` (case-insensitive).

The "combined args string" is built from ``step.tool_args_normalized or
step.tool_args`` by joining all string values with a space.  This matches the
convention used by other detectors in this package (session_quality.py).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kairos.detection.models import Finding

if TYPE_CHECKING:
    from kairos.models.trace import Step, TraceEnvelope


def _args_text(step: Step) -> str:
    """Return a single lowercased string of all string values in the step's args."""
    args = step.tool_args_normalized or step.tool_args
    if not args:
        return ""
    return " ".join(v for v in args.values() if isinstance(v, str)).lower()


def _matches_tool_sig(step: Step, signature: str) -> bool:
    """Return True when ``step`` matches the tool signature string.

    Signature format: ``"ToolName"`` or ``"ToolName:substring"``.
    Matching is exact on tool_name (case-sensitive) and case-insensitive on
    the substring portion.
    """
    if ":" in signature:
        tool_name, substring = signature.split(":", 1)
        if step.tool_name != tool_name:
            return False
        return substring.lower() in _args_text(step)
    else:
        return step.tool_name == signature


def detect_coordination_context(
    envelope: TraceEnvelope,
    *,
    markers: list[str],
    tools: list[str],
) -> Finding | None:
    """Return a Finding when the trace is a coordination-context session.

    Returns ``None`` when:
      - Both ``markers`` and ``tools`` are empty (feature off — backward-compatible).
      - Neither marker phrase nor tool signature fires on the trace.

    The Finding severity is always ``"info"`` — this is a classification flag,
    not an alarm.  Evidence names the specific marker/tool that fired and its
    location (``"task text"`` or step index).
    """
    if not markers and not tools:
        return None

    user_input_lower = (envelope.user_input or "").lower()

    # Check marker phrases against task text.
    for marker in markers:
        if marker.lower() in user_input_lower:
            return Finding(
                pattern_name="coordination_context",
                tier=1,
                trace_id=envelope.trace_id,
                confidence=1.0,
                severity="info",
                evidence={
                    "matched_marker": marker,
                    "match_location": "task text",
                    "user_input_excerpt": (envelope.user_input or "")[:200],
                },
                affected_step_indices=[],
                estimated_token_waste=0,
            )

    # Check tool signatures against all steps.
    for step in envelope.steps:
        if step.tool_name is None:
            continue
        for sig in tools:
            if _matches_tool_sig(step, sig):
                return Finding(
                    pattern_name="coordination_context",
                    tier=1,
                    trace_id=envelope.trace_id,
                    confidence=1.0,
                    severity="info",
                    evidence={
                        "matched_tool_signature": sig,
                        "match_location": f"step {step.step_index}",
                        "tool_name": step.tool_name,
                        "args_excerpt": _args_text(step)[:200],
                    },
                    affected_step_indices=[step.step_index],
                    estimated_token_waste=0,
                )

    return None
