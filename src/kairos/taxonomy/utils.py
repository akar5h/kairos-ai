"""Shared taxonomy utilities."""

from __future__ import annotations

from typing import TYPE_CHECKING

from kairos.models.enums import StepStatus

if TYPE_CHECKING:
    from kairos.models.trace import TraceEnvelope


def required_tool_coverage(trace: TraceEnvelope, expected_tools: list[str]) -> float:
    """Fraction of expected_tools with at least one successful step in the trace."""
    if not expected_tools:
        return 1.0
    successful = {
        step.tool_name
        for step in trace.steps
        if step.tool_name is not None and step.status == StepStatus.OK and not step.error_message
    }
    hit = sum(1 for t in expected_tools if t in successful)
    return hit / len(expected_tools)
