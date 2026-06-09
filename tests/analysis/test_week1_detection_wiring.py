"""Red-phase tests for the Week 1 detection adapter pattern.

These tests prove that ``detect_tier1`` can be called with a workflow-scoped
median step count. No production change lands in Day 3 — this just pins the
adapter shape that ``week1_pipeline.py`` (Day 5) will use.
"""

from __future__ import annotations

import statistics
from typing import Any

from kairos.detection.models import Finding
from kairos.detection.runner import detect_tier1
from kairos.models.enums import StepStatus, StepType, TerminalStatus
from kairos.models.trace import Step, TraceEnvelope


def _step(
    i: int,
    tool: str,
    *,
    status: StepStatus = StepStatus.OK,
    tool_args: dict[str, Any] | None = None,
    tool_output: str | None = "ok",
) -> Step:
    return Step(
        step_index=i,
        step_type=StepType.TOOL_CALL,
        tool_name=tool,
        tool_args=tool_args if tool_args is not None else {"stub": True},
        tool_args_normalized=tool_args if tool_args is not None else {"stub": True},
        tool_output=tool_output,
        status=status,
    )


def _linear_trace(trace_id: str, step_count: int) -> TraceEnvelope:
    steps = [_step(i, f"tool_{i}") for i in range(step_count)]
    return TraceEnvelope(
        trace_id=trace_id,
        user_input="go",
        steps=steps,
        terminal_status=TerminalStatus.COMPLETED,
    )


def _looping_trace(trace_id: str) -> TraceEnvelope:
    """A looping trace: 3+ repeats of the same (A, B) pair with identical outputs."""
    # period-1 loop: same tool repeated 4x with identical output
    steps: list[Step] = [
        _step(i, "tool_a", tool_args={"cycle": i}, tool_output="stuck")
        for i in range(4)
    ]
    return TraceEnvelope(
        trace_id=trace_id,
        user_input="stuck",
        steps=steps,
        terminal_status=TerminalStatus.COMPLETED,
    )


class TestDetectTier1WorkflowAdapter:
    """Pin the adapter shape Day 5 will use."""

    def test_cluster_median_steps_computed_from_workflow_cohort(self) -> None:
        step_counts = [2, 4, 6, 8, 10]
        assert statistics.median(step_counts) == 6

    def test_detect_tier1_callable_with_workflow_median(self) -> None:
        clean_a = _linear_trace("clean-a", 3)
        clean_b = _linear_trace("clean-b", 3)
        looper = _looping_trace("looper")
        traces = [clean_a, clean_b, looper]
        step_counts = [t.step_count for t in traces]
        median_steps = statistics.median(step_counts)

        findings = detect_tier1(traces, cluster_median_steps=median_steps)

        assert isinstance(findings, list)
        for f in findings:
            assert isinstance(f, Finding)
        # The looping trace must produce at least one finding.
        looper_findings = [f for f in findings if f.trace_id == "looper"]
        assert len(looper_findings) >= 1

    def test_detect_tier1_with_empty_traces_returns_empty_list(self) -> None:
        findings = detect_tier1([], cluster_median_steps=0)
        assert findings == []
