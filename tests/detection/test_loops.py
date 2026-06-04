"""Tests for Tier 1 Pattern 2: Loop detector."""

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
# Assertion tests
# ---------------------------------------------------------------------------


class TestLoopAssertion:
    def test_finds_simple_loop(self) -> None:
        # ["a","b","a","b","a","b"] — period=2, repeats=3, same outputs
        specs: list[tuple[str, str | None, StepStatus]] = [
            ("a", "out", StepStatus.OK),
            ("b", "out", StepStatus.OK),
            ("a", "out", StepStatus.OK),
            ("b", "out", StepStatus.OK),
            ("a", "out", StepStatus.OK),
            ("b", "out", StepStatus.OK),
        ]
        trace = _make_trace("loop1", specs)
        findings = loop_assertion(trace, min_repeats=3)
        assert len(findings) == 1
        f = findings[0]
        assert f.evidence["period"] == 2
        assert f.evidence["repeats"] == 3

    def test_no_loop_when_outputs_change(self) -> None:
        # Same tool pattern but each output differs → legitimate iteration
        specs: list[tuple[str, str | None, StepStatus]] = [
            ("a", "page1", StepStatus.OK),
            ("b", "res1", StepStatus.OK),
            ("a", "page2", StepStatus.OK),
            ("b", "res2", StepStatus.OK),
            ("a", "page3", StepStatus.OK),
            ("b", "res3", StepStatus.OK),
        ]
        trace = _make_trace("iter1", specs)
        findings = loop_assertion(trace, min_repeats=3)
        assert len(findings) == 0

    def test_stuck_loop_all_errors(self) -> None:
        specs: list[tuple[str, str | None, StepStatus]] = [
            ("a", "err", StepStatus.ERROR),
            ("b", "err", StepStatus.ERROR),
            ("a", "err", StepStatus.ERROR),
            ("b", "err", StepStatus.ERROR),
            ("a", "err", StepStatus.ERROR),
            ("b", "err", StepStatus.ERROR),
        ]
        trace = _make_trace("stuck1", specs)
        findings = loop_assertion(trace, min_repeats=3)
        assert len(findings) == 1
        assert findings[0].evidence["classification"] == "stuck_loop"

    def test_min_repeats_respected(self) -> None:
        # Only 2 repeats of period 2 — below min_repeats=3
        specs: list[tuple[str, str | None, StepStatus]] = [
            ("a", "out", StepStatus.OK),
            ("b", "out", StepStatus.OK),
            ("a", "out", StepStatus.OK),
            ("b", "out", StepStatus.OK),
        ]
        trace = _make_trace("short1", specs)
        findings = loop_assertion(trace, min_repeats=3)
        assert len(findings) == 0

    def test_no_loop_short_trace(self) -> None:
        # 4 steps, period=2 can only repeat 2x → below min_repeats=3
        specs: list[tuple[str, str | None, StepStatus]] = [
            ("x", "out", StepStatus.OK),
            ("y", "out", StepStatus.OK),
            ("x", "out", StepStatus.OK),
            ("y", "out", StepStatus.OK),
        ]
        trace = _make_trace("short2", specs)
        findings = loop_assertion(trace, min_repeats=3)
        assert len(findings) == 0

    def test_longer_period(self) -> None:
        # ["a","b","c","a","b","c","a","b","c"] → period=3, repeats=3
        specs: list[tuple[str, str | None, StepStatus]] = [
            ("a", "out", StepStatus.OK),
            ("b", "out", StepStatus.OK),
            ("c", "out", StepStatus.OK),
            ("a", "out", StepStatus.OK),
            ("b", "out", StepStatus.OK),
            ("c", "out", StepStatus.OK),
            ("a", "out", StepStatus.OK),
            ("b", "out", StepStatus.OK),
            ("c", "out", StepStatus.OK),
        ]
        trace = _make_trace("long_period", specs)
        findings = loop_assertion(trace, min_repeats=3)
        assert len(findings) == 1
        assert findings[0].evidence["period"] == 3
        assert findings[0].evidence["repeats"] == 3

    def test_prefers_longest_period(self) -> None:
        # ["a","a","a","a","a","a"] matches period=1 (x6) AND period=2 (x3)
        # Should return period=2 (longest) since we iterate longest-first
        # and consumed indices prevent shorter re-detection.
        specs: list[tuple[str, str | None, StepStatus]] = [
            ("a", "out", StepStatus.OK),
            ("a", "out", StepStatus.OK),
            ("a", "out", StepStatus.OK),
            ("a", "out", StepStatus.OK),
            ("a", "out", StepStatus.OK),
            ("a", "out", StepStatus.OK),
        ]
        trace = _make_trace("prefer_long", specs)
        findings = loop_assertion(trace, min_repeats=3)
        assert len(findings) >= 1
        # The first (and ideally only) finding should have the longest period
        assert findings[0].evidence["period"] == 2
