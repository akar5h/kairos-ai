"""Tests for Tier 1 detection runner."""

from __future__ import annotations

from kairos.detection.runner import detect_tier1
from kairos.models.enums import StepStatus, StepType
from kairos.models.trace import Step, TraceEnvelope


def _make_trace(
    trace_id: str,
    tool_specs: list[tuple[str, str | None, dict | None, StepStatus]],
) -> TraceEnvelope:
    """Build a trace from (tool_name, tool_output, tool_args, status) tuples."""
    steps = [
        Step(
            step_index=i,
            step_type=StepType.TOOL_CALL,
            tool_name=name,
            tool_output=output,
            tool_args=args,
            status=status,
        )
        for i, (name, output, args, status) in enumerate(tool_specs)
    ]
    return TraceEnvelope(trace_id=trace_id, steps=steps)


class TestDetectTier1:
    def test_runs_both_patterns(self) -> None:
        """Trace with a redundant pair AND a loop → findings from both."""
        same_args = {"q": "hello"}
        specs: list[tuple[str, str | None, dict | None, StepStatus]] = [
            # Redundant pair (consecutive same tool + same args)
            ("search", "res", same_args, StepStatus.OK),
            ("search", "res", same_args, StepStatus.OK),
            # Loop: period-1, tool "a" repeated 3x with same output
            ("a", "out", None, StepStatus.OK),
            ("a", "out", None, StepStatus.OK),
            ("a", "out", None, StepStatus.OK),
        ]
        trace = _make_trace("both1", specs)
        # Set median low so loop guard passes (5 steps, median=4)
        findings = detect_tier1([trace], cluster_median_steps=4)
        patterns = {f.pattern_name for f in findings}
        assert "redundant_execution" in patterns
        assert "loop_detected" in patterns or "stuck_loop" in patterns

    def test_skips_guarded_traces(self) -> None:
        """Short trace with no consecutive tools → empty."""
        specs: list[tuple[str, str | None, dict | None, StepStatus]] = [
            ("a", "out", None, StepStatus.OK),
            ("b", "out", None, StepStatus.OK),
        ]
        trace = _make_trace("skip1", specs)
        findings = detect_tier1([trace], cluster_median_steps=10)
        assert findings == []

    def test_empty_input(self) -> None:
        findings = detect_tier1([], cluster_median_steps=10)
        assert findings == []
