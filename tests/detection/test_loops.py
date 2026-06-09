"""Tests for Tier 1 Pattern 2: Loop detector (period-1 only)."""

from __future__ import annotations

from kairos.detection.loops import loop_assertion, loop_guard
from kairos.models.enums import StepStatus, StepType
from kairos.models.trace import Step, TraceEnvelope


def _make_trace(
    trace_id: str,
    tool_specs: list[tuple[str, str | None, StepStatus]],
) -> TraceEnvelope:
    """Build a trace from (tool_name, tool_output, status) tuples."""
    steps = [
        Step(
            step_index=i,
            step_type=StepType.TOOL_CALL,
            tool_name=name,
            tool_output=output,
            status=status,
        )
        for i, (name, output, status) in enumerate(tool_specs)
    ]
    return TraceEnvelope(trace_id=trace_id, steps=steps)


# ---------------------------------------------------------------------------
# Guard tests
# ---------------------------------------------------------------------------


class TestLoopGuard:
    def test_guard_below_median(self) -> None:
        trace = _make_trace("t1", [("a", None, StepStatus.OK)] * 5)
        assert loop_guard(trace, cluster_median_steps=10) is False

    def test_guard_above_median(self) -> None:
        trace = _make_trace("t2", [("a", None, StepStatus.OK)] * 15)
        assert loop_guard(trace, cluster_median_steps=10) is True

    def test_guard_equal_to_median(self) -> None:
        trace = _make_trace("t3", [("a", None, StepStatus.OK)] * 10)
        assert loop_guard(trace, cluster_median_steps=10) is False


# ---------------------------------------------------------------------------
# Assertion tests (period-1 only)
# ---------------------------------------------------------------------------


class TestLoopAssertion:
    def test_finds_period1_loop(self) -> None:
        # ["a","a","a"] — period=1, repeats=3, same output
        specs: list[tuple[str, str | None, StepStatus]] = [
            ("a", "out", StepStatus.OK),
            ("a", "out", StepStatus.OK),
            ("a", "out", StepStatus.OK),
        ]
        trace = _make_trace("loop1", specs)
        findings = loop_assertion(trace, min_repeats=3)
        assert len(findings) == 1
        f = findings[0]
        assert f.evidence["period"] == 1
        assert f.evidence["repeats"] == 3
        assert f.pattern_name == "loop_detected"

    def test_no_loop_when_outputs_change(self) -> None:
        # Same tool but outputs differ → legitimate iteration, not a loop
        specs: list[tuple[str, str | None, StepStatus]] = [
            ("a", "page1", StepStatus.OK),
            ("a", "page2", StepStatus.OK),
            ("a", "page3", StepStatus.OK),
        ]
        trace = _make_trace("iter1", specs)
        findings = loop_assertion(trace, min_repeats=3)
        assert len(findings) == 0

    def test_stuck_loop_all_errors(self) -> None:
        # Same tool, same output, all errors → stuck_loop
        specs: list[tuple[str, str | None, StepStatus]] = [
            ("a", "err", StepStatus.ERROR),
            ("a", "err", StepStatus.ERROR),
            ("a", "err", StepStatus.ERROR),
        ]
        trace = _make_trace("stuck1", specs)
        findings = loop_assertion(trace, min_repeats=3)
        assert len(findings) == 1
        assert findings[0].evidence["classification"] == "stuck_loop"
        assert findings[0].pattern_name == "stuck_loop"
        assert findings[0].severity == "critical"

    def test_min_repeats_respected(self) -> None:
        # Only 2 repeats — below min_repeats=3
        specs: list[tuple[str, str | None, StepStatus]] = [
            ("a", "out", StepStatus.OK),
            ("a", "out", StepStatus.OK),
        ]
        trace = _make_trace("short1", specs)
        findings = loop_assertion(trace, min_repeats=3)
        assert len(findings) == 0

    def test_no_loop_short_trace(self) -> None:
        trace = _make_trace("short2", [("x", "out", StepStatus.OK)] * 2)
        findings = loop_assertion(trace, min_repeats=3)
        assert len(findings) == 0

    def test_period2_not_detected(self) -> None:
        # ["a","b","a","b","a","b"] — period-2 pattern. Period-1 detector
        # sees two separate runs of length 1 each, neither meets min_repeats=3.
        specs: list[tuple[str, str | None, StepStatus]] = [
            ("a", "out", StepStatus.OK),
            ("b", "out", StepStatus.OK),
            ("a", "out", StepStatus.OK),
            ("b", "out", StepStatus.OK),
            ("a", "out", StepStatus.OK),
            ("b", "out", StepStatus.OK),
        ]
        trace = _make_trace("period2", specs)
        findings = loop_assertion(trace, min_repeats=3)
        assert len(findings) == 0

    def test_longer_run_detected(self) -> None:
        # 6 consecutive same-tool calls with same output → 1 finding
        specs: list[tuple[str, str | None, StepStatus]] = [
            ("a", "stuck", StepStatus.OK),
        ] * 6
        trace = _make_trace("long_run", specs)
        findings = loop_assertion(trace, min_repeats=3)
        assert len(findings) == 1
        assert findings[0].evidence["repeats"] == 6
        assert findings[0].evidence["period"] == 1

    def test_two_distinct_loops(self) -> None:
        # ["a","a","a","b","b","b"] → two separate period-1 loops
        specs: list[tuple[str, str | None, StepStatus]] = [
            ("a", "out", StepStatus.OK),
            ("a", "out", StepStatus.OK),
            ("a", "out", StepStatus.OK),
            ("b", "out", StepStatus.OK),
            ("b", "out", StepStatus.OK),
            ("b", "out", StepStatus.OK),
        ]
        trace = _make_trace("two_loops", specs)
        findings = loop_assertion(trace, min_repeats=3)
        assert len(findings) == 2
        tools = {f.evidence["pattern"][0] for f in findings}
        assert tools == {"a", "b"}

    def test_loop_with_progress_in_between_not_flagged(self) -> None:
        # ["a","a","b","a","a"] — two separate runs of "a" (len=2), neither meets min_repeats=3
        specs: list[tuple[str, str | None, StepStatus]] = [
            ("a", "out", StepStatus.OK),
            ("a", "out", StepStatus.OK),
            ("b", "other", StepStatus.OK),
            ("a", "out", StepStatus.OK),
            ("a", "out", StepStatus.OK),
        ]
        trace = _make_trace("interspersed", specs)
        findings = loop_assertion(trace, min_repeats=3)
        assert len(findings) == 0

    def test_none_output_treated_as_identical(self) -> None:
        # All None outputs → outputs are "identical" → loop should fire
        specs: list[tuple[str, str | None, StepStatus]] = [
            ("a", None, StepStatus.OK),
            ("a", None, StepStatus.OK),
            ("a", None, StepStatus.OK),
        ]
        trace = _make_trace("none_out", specs)
        findings = loop_assertion(trace, min_repeats=3)
        assert len(findings) == 1
