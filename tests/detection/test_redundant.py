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


class TestRedundantF10Guard:
    """F10 guard: args-absent pairs must never produce a finding.

    Live claude_code.tool spans carry no tool_args/tool_output; without args
    evidence jaccard_dict_similarity returns 1.0 for None/None and ∅/∅, which
    historically produced a 642-finding flood at confidence 1.0.
    """

    def _make_trace_no_args(
        self,
        trace_id: str,
        tools: list[tuple[str, StepStatus, int]],
    ) -> TraceEnvelope:
        """Build trace from (tool_name, status, total_tokens) — NO args on any step."""
        steps = [
            Step(
                step_index=i,
                step_type=StepType.TOOL_CALL,
                tool_name=name,
                tool_args=None,
                tool_args_normalized=None,
                status=status,
                total_tokens=tokens,
            )
            for i, (name, status, tokens) in enumerate(tools)
        ]
        return TraceEnvelope(trace_id=trace_id, steps=steps)

    def test_args_absent_both_silent(self) -> None:
        # Both steps have no args → skip entirely, no finding
        trace = self._make_trace_no_args(
            "f10_absent_both",
            [
                ("Read", StepStatus.OK, 500),
                ("Read", StepStatus.OK, 500),
            ],
        )
        findings = redundant_assertion(trace)
        assert len(findings) == 0

    def test_args_absent_empty_dict_silent(self) -> None:
        # Empty dicts are falsy — also skipped (same as None)
        steps = [
            Step(
                step_index=0,
                step_type=StepType.TOOL_CALL,
                tool_name="Edit",
                tool_args={},
                tool_args_normalized={},
                status=StepStatus.OK,
                total_tokens=500,
            ),
            Step(
                step_index=1,
                step_type=StepType.TOOL_CALL,
                tool_name="Edit",
                tool_args={},
                tool_args_normalized={},
                status=StepStatus.OK,
                total_tokens=500,
            ),
        ]
        trace = TraceEnvelope(trace_id="f10_empty_dicts", steps=steps)
        findings = redundant_assertion(trace)
        assert len(findings) == 0

    def test_args_present_pair_fires(self) -> None:
        # When at least one side has args, similarity runs and fires above threshold
        args = {"file_path": "/src/foo.py", "old_str": "x", "new_str": "y"}
        trace = _make_trace(
            "f10_args_present",
            [
                ("Edit", args, StepStatus.OK, 500),
                ("Edit", args, StepStatus.OK, 500),
            ],
        )
        findings = redundant_assertion(trace)
        assert len(findings) == 1

    def test_args_mixed_one_absent_silent(self) -> None:
        # One step has args, the other doesn't — mixed → skip (both must be truthy).
        # Actually per spec: "if not args_a and not args_b" — only skip when BOTH absent.
        # When one has args, similarity runs normally. Let's verify the mixed case
        # where curr has args but nxt doesn't: Jaccard(dict, None) = 0.0 → below threshold.
        steps = [
            Step(
                step_index=0,
                step_type=StepType.TOOL_CALL,
                tool_name="Bash",
                tool_args={"cmd": "ls"},
                tool_args_normalized={"cmd": "ls"},
                status=StepStatus.OK,
                total_tokens=500,
            ),
            Step(
                step_index=1,
                step_type=StepType.TOOL_CALL,
                tool_name="Bash",
                tool_args=None,
                tool_args_normalized=None,
                status=StepStatus.OK,
                total_tokens=500,
            ),
        ]
        trace = TraceEnvelope(trace_id="f10_mixed", steps=steps)
        findings = redundant_assertion(trace)
        # Jaccard({cmd:ls}, None) = 0.0 → below 0.85 threshold → no finding
        assert len(findings) == 0
