"""Tests for redundant execution detector (guard + assertion)."""

from __future__ import annotations

from kairos.detection.redundant import redundant_assertion, redundant_guard
from kairos.models.enums import StepStatus, StepType
from kairos.models.trace import Step, TraceEnvelope


def _make_trace(
    trace_id: str,
    tools_with_args: list[tuple[str, dict, StepStatus, int]],
) -> TraceEnvelope:
    """Build trace from list of (tool_name, args_dict, status, total_tokens) tuples."""
    steps = [
        Step(
            step_index=i,
            step_type=StepType.TOOL_CALL,
            tool_name=name,
            tool_args=args,
            tool_args_normalized=args,
            status=status,
            total_tokens=tokens,
        )
        for i, (name, args, status, tokens) in enumerate(tools_with_args)
    ]
    return TraceEnvelope(trace_id=trace_id, steps=steps)


class TestRedundantGuard:
    """Tests for redundant_guard."""

    def test_guard_no_consecutive(self) -> None:
        trace = _make_trace(
            "t1",
            [
                ("a", {}, StepStatus.OK, 100),
                ("b", {}, StepStatus.OK, 100),
                ("c", {}, StepStatus.OK, 100),
            ],
        )
        assert redundant_guard(trace) is False

    def test_guard_consecutive(self) -> None:
        trace = _make_trace(
            "t2",
            [
                ("a", {}, StepStatus.OK, 100),
                ("b", {}, StepStatus.OK, 100),
                ("b", {}, StepStatus.OK, 100),
                ("c", {}, StepStatus.OK, 100),
            ],
        )
        assert redundant_guard(trace) is True

    def test_guard_single_step(self) -> None:
        trace = _make_trace(
            "t3",
            [
                ("a", {}, StepStatus.OK, 100),
            ],
        )
        assert redundant_guard(trace) is False

    def test_guard_empty(self) -> None:
        trace = _make_trace("t4", [])
        assert redundant_guard(trace) is False


class TestRedundantAssertion:
    """Tests for redundant_assertion."""

    def test_assertion_finds_redundant_pair(self) -> None:
        args = {"query": "openai pricing", "limit": 10}
        trace = _make_trace(
            "t5",
            [
                ("search_web", args, StepStatus.OK, 500),
                ("search_web", args, StepStatus.OK, 500),
            ],
        )
        findings = redundant_assertion(trace)
        assert len(findings) == 1
        f = findings[0]
        assert f.pattern_name == "redundant_execution"
        assert f.confidence >= 0.99
        assert f.evidence["tool"] == "search_web"

    def test_assertion_skips_retry_after_error(self) -> None:
        args = {"query": "openai pricing"}
        trace = _make_trace(
            "t6",
            [
                ("search_web", args, StepStatus.ERROR, 500),
                ("search_web", args, StepStatus.OK, 500),
            ],
        )
        findings = redundant_assertion(trace)
        assert len(findings) == 0

    def test_assertion_above_threshold(self) -> None:
        a = {"query": "openai pricing", "limit": 10, "page": 1}
        b = {"query": "openai pricing", "limit": 10, "page": 1}
        trace = _make_trace(
            "t7",
            [
                ("search_web", a, StepStatus.OK, 500),
                ("search_web", b, StepStatus.OK, 500),
            ],
        )
        findings = redundant_assertion(trace)
        assert len(findings) == 1

    def test_assertion_below_threshold(self) -> None:
        a = {"query": "openai pricing", "limit": 10}
        b = {"query": "competitor analysis", "max_results": 50, "region": "us"}
        trace = _make_trace(
            "t8",
            [
                ("search_web", a, StepStatus.OK, 500),
                ("search_web", b, StepStatus.OK, 500),
            ],
        )
        findings = redundant_assertion(trace)
        assert len(findings) == 0

    def test_assertion_multiple_pairs(self) -> None:
        args = {"query": "test"}
        trace = _make_trace(
            "t9",
            [
                ("search_web", args, StepStatus.OK, 500),
                ("search_web", args, StepStatus.OK, 500),
                ("search_web", args, StepStatus.OK, 500),
            ],
        )
        findings = redundant_assertion(trace)
        assert len(findings) == 2

    def test_assertion_estimates_token_waste(self) -> None:
        args = {"query": "test"}
        trace = _make_trace(
            "t10",
            [
                ("search_web", args, StepStatus.OK, 1000),
                ("search_web", args, StepStatus.OK, 5000),
            ],
        )
        findings = redundant_assertion(trace)
        assert len(findings) == 1
        assert findings[0].estimated_token_waste == 5000

    def test_assertion_different_tools(self) -> None:
        args = {"query": "test"}
        trace = _make_trace(
            "t11",
            [
                ("search_web", args, StepStatus.OK, 500),
                ("fetch_page", args, StepStatus.OK, 500),
            ],
        )
        findings = redundant_assertion(trace)
        assert len(findings) == 0
