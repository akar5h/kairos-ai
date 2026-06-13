"""Tests for Tier 1.5 session-quality detectors.

Covers:
  D1 unrecovered_error — fire, no-fire, jaccard threshold, session-restart
  D2 struggle_ratio    — fire, no-fire, breakdown components
  D3 coordination_waste — fire (repeat), fire (curl), no-fire, severity scaling
  D4 work_to_talk_ratio — fire, no-fire, op-exempt
  LEARN                — candidate logic, < EXPECT_MIN_N abstention
"""

from __future__ import annotations

from kairos.detection.session_quality import (
    EXPECT_MIN_N,
    STRUGGLE_T,
    WTT_T,
    ExpectationMissCandidate,
    detect_coordination_waste,
    detect_session_quality,
    detect_struggle_ratio,
    detect_unrecovered_error,
    detect_work_to_talk_ratio,
    learn_tool_expectations,
)
from kairos.models.enums import StepStatus, StepType
from kairos.models.trace import Step, TraceEnvelope
from kairos.taxonomy.business_context import BusinessOperation

# ── Helpers ────────────────────────────────────────────────────────────────────


def _step(
    index: int,
    tool_name: str | None = None,
    status: StepStatus = StepStatus.OK,
    step_type: StepType = StepType.TOOL_CALL,
    tool_args: dict | None = None,
    tool_args_normalized: dict | None = None,
    tool_output: str | None = None,
    error_message: str | None = None,
    total_tokens: int | None = None,
) -> Step:
    return Step(
        step_index=index,
        step_type=step_type,
        tool_name=tool_name,
        status=status,
        tool_args=tool_args,
        tool_args_normalized=tool_args_normalized,
        tool_output=tool_output,
        error_message=error_message,
        total_tokens=total_tokens,
    )


def _llm_step(index: int, total_tokens: int = 1000) -> Step:
    return Step(
        step_index=index,
        step_type=StepType.LLM,
        total_tokens=total_tokens,
    )


def _make_trace(trace_id: str, steps: list[Step], total_tokens: int = 0) -> TraceEnvelope:
    return TraceEnvelope(trace_id=trace_id, steps=steps, total_tokens=total_tokens)


def _code_op(
    name: str = "Code Implementation",
    required: list[str] | None = None,
    expected: list[str] | None = None,
    side_effect_match: str = "any",
) -> BusinessOperation:
    return BusinessOperation(
        name=name,
        description="test op",
        required_side_effect_tools=required or ["Edit", "Write"],
        expected_tools=expected or ["Read", "Edit", "Write", "Bash"],
        side_effect_match=side_effect_match,
    )


# ── D1 — unrecovered_error ────────────────────────────────────────────────────


class TestUnrecoveredError:
    def test_no_fire_clean_trace(self) -> None:
        """No ERROR steps → no findings."""
        trace = _make_trace("t1", [
            _step(0, "Edit", StepStatus.OK, tool_args={"file_path": "/a.py"}),
            _step(1, "Bash", StepStatus.OK, tool_args={"command": "ls"}),
        ])
        assert detect_unrecovered_error(trace) == []

    def test_fire_unrecovered_error(self) -> None:
        """ERROR with no later same-tool similar-arg call → fires."""
        trace = _make_trace("t2", [
            _step(0, "Edit", StepStatus.ERROR, tool_args={"file_path": "/a.py"},
                  error_message="File not found"),
            _step(1, "Bash", StepStatus.OK, tool_args={"command": "ls"}),
        ])
        findings = detect_unrecovered_error(trace)
        assert len(findings) == 1
        assert findings[0].pattern_name == "unrecovered_error"
        assert findings[0].evidence["tool"] == "Edit"

    def test_no_fire_when_recovered_same_tool_same_args(self) -> None:
        """ERROR followed by near-identical args call → recovered, no finding."""
        trace = _make_trace("t3", [
            _step(0, "Edit", StepStatus.ERROR, tool_args={"file_path": "/a.py", "old_string": "foo"}),
            _step(1, "Edit", StepStatus.OK, tool_args={"file_path": "/a.py", "old_string": "foo"}),
        ])
        assert detect_unrecovered_error(trace) == []

    def test_no_fire_different_args_not_recovery(self) -> None:
        """ERROR on /a.py; retry on /b.py (jaccard low) → NOT a recovery, fires."""
        a_args = {"file_path": "/a.py", "old_string": "foo", "new_string": "bar"}
        b_args = {"file_path": "/b.py", "old_string": "zzz", "new_string": "yyy"}
        trace = _make_trace("t4", [
            _step(0, "Edit", StepStatus.ERROR, tool_args=a_args),
            _step(1, "Edit", StepStatus.OK, tool_args=b_args),
        ])
        # Different file, different args → jaccard will be very low → unrecovered
        findings = detect_unrecovered_error(trace)
        assert len(findings) == 1

    def test_severity_error_for_required_side_effect(self) -> None:
        """Tool in required_side_effect_tools → severity error."""
        op = _code_op(required=["Edit", "Write"])
        trace = _make_trace("t5", [
            _step(0, "Edit", StepStatus.ERROR, tool_args={"file_path": "/a.py"}),
        ])
        findings = detect_unrecovered_error(trace, operation=op)
        assert findings[0].severity == "error"

    def test_severity_warning_for_non_required_tool(self) -> None:
        """Tool NOT in required_side_effect_tools → severity warning."""
        op = _code_op(required=["Write"])  # Edit not in required
        trace = _make_trace("t6", [
            _step(0, "Edit", StepStatus.ERROR, tool_args={"file_path": "/a.py"}),
        ])
        findings = detect_unrecovered_error(trace, operation=op)
        assert findings[0].severity == "warning"

    def test_session_restart_not_counted_as_recovery(self) -> None:
        """Recovery after a session-restart boundary is NOT counted → fires."""
        trace = _make_trace("t7", [
            _step(0, "Edit", StepStatus.ERROR, tool_args={"file_path": "/a.py"}),
            # Bash restart boundary
            _step(1, "Bash", StepStatus.OK,
                  tool_args={"command": "cat ~/.claude/system_prompt.txt"}),
            # Same tool after restart: should NOT count as recovery
            _step(2, "Edit", StepStatus.OK, tool_args={"file_path": "/a.py"}),
        ])
        findings = detect_unrecovered_error(trace, recovery_window=10)
        assert len(findings) == 1

    def test_recovery_within_window(self) -> None:
        """Recovery step within RECOVERY_WINDOW → no finding."""
        args = {"file_path": "/a.py", "old_string": "x", "new_string": "y"}
        steps = [_step(0, "Edit", StepStatus.ERROR, tool_args=args)]
        # Add 3 unrelated steps, then recovery within window=10
        for i in range(1, 4):
            steps.append(_step(i, "Bash", StepStatus.OK, tool_args={"command": "ls"}))
        steps.append(_step(4, "Edit", StepStatus.OK, tool_args=args))
        trace = _make_trace("t8", steps)
        assert detect_unrecovered_error(trace, recovery_window=10) == []

    def test_recovery_outside_window_fires(self) -> None:
        """Recovery step BEYOND RECOVERY_WINDOW → fires (window = 2)."""
        args = {"file_path": "/a.py", "old_string": "x", "new_string": "y"}
        steps = [_step(0, "Edit", StepStatus.ERROR, tool_args=args)]
        for i in range(1, 5):
            steps.append(_step(i, "Bash", StepStatus.OK, tool_args={"command": "ls"}))
        steps.append(_step(5, "Edit", StepStatus.OK, tool_args=args))
        trace = _make_trace("t9", steps)
        findings = detect_unrecovered_error(trace, recovery_window=2)
        assert len(findings) == 1


# ── D2 — struggle_ratio ───────────────────────────────────────────────────────


class TestStruggleRatio:
    def test_no_fire_clean_trace(self) -> None:
        """Clean trace with no errors or redundant → no finding."""
        trace = _make_trace("s1", [
            _step(0, "Edit", StepStatus.OK, tool_args={"f": "a"}),
            _step(1, "Write", StepStatus.OK, tool_args={"f": "b"}),
        ])
        assert detect_struggle_ratio(trace) == []

    def test_fire_high_error_ratio(self) -> None:
        """Many errors, few successes → fires."""
        steps = [_step(i, "Bash", StepStatus.ERROR, tool_args={"command": f"c{i}"})
                 for i in range(10)]
        steps.append(_step(10, "Edit", StepStatus.OK, tool_args={"f": "x"}))
        trace = _make_trace("s2", steps)
        findings = detect_struggle_ratio(trace, struggle_t=2.0)
        assert len(findings) == 1
        assert findings[0].evidence["struggle_ratio"] >= 2.0
        assert findings[0].severity == "warning"

    def test_evidence_breakdown(self) -> None:
        """Finding evidence contains the churn breakdown."""
        steps = [
            _step(0, "Bash", StepStatus.ERROR, tool_args={"command": "a"}),
            _step(1, "Bash", StepStatus.ERROR, tool_args={"command": "b"}),
            _step(2, "Bash", StepStatus.ERROR, tool_args={"command": "c"}),
            _step(3, "Edit", StepStatus.OK, tool_args={"f": "x"}),
        ]
        trace = _make_trace("s3", steps)
        findings = detect_struggle_ratio(trace, struggle_t=1.5)
        assert len(findings) == 1
        ev = findings[0].evidence
        assert "error_steps" in ev
        assert "redundant_steps" in ev
        assert "side_effect_successes" in ev

    def test_tool_use_error_counted_as_rejected(self) -> None:
        """Rejected tool calls (tool_use_error in error_message) counted."""
        steps = [
            _step(0, "Edit", StepStatus.ERROR,
                  error_message="<tool_use_error>File not read yet</tool_use_error>",
                  tool_args={"f": "a"}),
            _step(1, "Edit", StepStatus.OK, tool_args={"f": "b"}),
        ]
        trace = _make_trace("s4", steps)
        # rejected_tool_calls=1, error_steps=1 → total=2, side_effect=1 → ratio=2
        findings = detect_struggle_ratio(trace, struggle_t=1.5)
        assert findings[0].evidence["rejected_tool_calls"] == 1

    def test_no_fire_below_threshold(self) -> None:
        """Ratio below threshold → no finding."""
        steps = [_step(i, "Edit", StepStatus.OK, tool_args={"f": f"{i}"}) for i in range(5)]
        trace = _make_trace("s5", steps)
        assert detect_struggle_ratio(trace, struggle_t=STRUGGLE_T) == []


# ── D3 — coordination_waste ───────────────────────────────────────────────────


class TestCoordinationWaste:
    def test_no_fire_clean_trace(self) -> None:
        """Varied tool calls, no curl → no finding."""
        trace = _make_trace("c1", [
            _step(0, "Edit", StepStatus.OK, tool_args={"f": "a"}),
            _step(1, "Write", StepStatus.OK, tool_args={"f": "b"}),
            _step(2, "Bash", StepStatus.OK, tool_args={"command": "ls"}),
        ])
        assert detect_coordination_waste(trace, repeat_t=3, curl_t=0.7) == []

    def test_fire_repeat_identical_args(self) -> None:
        """3+ identical-arg calls of same tool → fires."""
        args = {"command": "curl http://api/status"}
        steps = [
            _step(i, "Bash", StepStatus.OK, tool_args=args, tool_args_normalized=args)
            for i in range(4)
        ]
        trace = _make_trace("c2", steps)
        findings = detect_coordination_waste(trace, repeat_t=3, curl_t=0.99)
        assert len(findings) == 1
        assert findings[0].evidence["repeat_violations"]

    def test_fire_curl_fraction(self) -> None:
        """Bash curl fraction above threshold → fires."""
        steps = [
            _step(0, "Bash", StepStatus.OK, tool_args={"command": "curl http://api/foo"}),
            _step(1, "Bash", StepStatus.OK, tool_args={"command": "curl http://api/bar"}),
            _step(2, "Bash", StepStatus.OK, tool_args={"command": "curl http://api/baz"}),
            _step(3, "Bash", StepStatus.OK, tool_args={"command": "ls"}),
        ]
        trace = _make_trace("c3", steps)
        findings = detect_coordination_waste(trace, repeat_t=99, curl_t=0.7)
        assert len(findings) == 1
        assert findings[0].evidence["curl_fraction"] >= 0.7

    def test_severity_info_at_threshold(self) -> None:
        """Just at threshold with non-curl args → info severity.

        Use args without 'curl' so the curl-fraction branch doesn't push
        severity to warning; only the repeat-count branch fires at count=3
        which is exactly threshold (not 2×), so severity stays info.
        """
        args = {"command": "git status --short"}
        steps = [
            _step(i, "Bash", StepStatus.OK, tool_args=args, tool_args_normalized=args)
            for i in range(3)
        ]
        trace = _make_trace("c4", steps)
        findings = detect_coordination_waste(trace, repeat_t=3, curl_t=0.99)
        # Count=3 at threshold (< 2×threshold=6), curl_fraction=0.0 → info
        assert len(findings) == 1
        assert findings[0].severity == "info"

    def test_severity_warning_at_double_threshold(self) -> None:
        """Double the threshold count → warning severity."""
        args = {"command": "curl http://api/status"}
        steps = [
            _step(i, "Bash", StepStatus.OK, tool_args=args, tool_args_normalized=args)
            for i in range(6)
        ]
        trace = _make_trace("c5", steps)
        findings = detect_coordination_waste(trace, repeat_t=3, curl_t=0.99)
        assert findings[0].severity == "warning"

    def test_different_args_no_repeat_violation(self) -> None:
        """Same tool but different args → no repeat violation."""
        steps = [
            _step(i, "Bash", StepStatus.OK, tool_args={"command": f"ls /dir{i}"})
            for i in range(5)
        ]
        trace = _make_trace("c6", steps)
        findings = detect_coordination_waste(trace, repeat_t=3, curl_t=0.99)
        assert not findings


# ── D4 — work_to_talk_ratio ───────────────────────────────────────────────────


class TestWorkToTalkRatio:
    def test_no_fire_op_exempt_research(self) -> None:
        """Codebase Research op → D4 exempt, never fires."""
        op = BusinessOperation(
            name="Codebase Research",
            description="research",
            required_side_effect_tools=["Read"],
            expected_tools=["Read", "Grep"],
        )
        trace = _make_trace("w1", [
            _llm_step(0, total_tokens=10000),
            _step(1, "Read", StepStatus.OK),
        ])
        assert detect_work_to_talk_ratio(trace, operation=op) == []

    def test_no_fire_op_exempt_coordination(self) -> None:
        """Paperclip Coordination op → D4 exempt."""
        op = BusinessOperation(
            name="Paperclip Coordination",
            description="coord",
            required_side_effect_tools=["Skill"],
            expected_tools=["Bash", "Skill"],
        )
        trace = _make_trace("w2", [
            _llm_step(0, total_tokens=5000),
        ])
        assert detect_work_to_talk_ratio(trace, operation=op) == []

    def test_fire_low_ratio(self) -> None:
        """Very high tokens, no side effects → fires."""
        trace = _make_trace(
            "w3",
            [
                _llm_step(0, total_tokens=100_000),
                _step(1, "Bash", StepStatus.OK),
            ],
            total_tokens=100_000,
        )
        op = _code_op()
        findings = detect_work_to_talk_ratio(trace, operation=op, wtt_t=WTT_T)
        # ratio = 0 side_effect_successes / 100 (100k tokens / 1000) = 0.0
        assert len(findings) == 1
        assert findings[0].severity == "info"

    def test_no_fire_good_ratio(self) -> None:
        """Many side effects, few tokens → no finding."""
        steps = [
            _llm_step(0, total_tokens=100),
        ]
        for i in range(1, 11):
            steps.append(_step(i, "Edit", StepStatus.OK, tool_args={"f": f"{i}"}))
        trace = _make_trace("w4", steps)
        op = _code_op()
        assert detect_work_to_talk_ratio(trace, operation=op, wtt_t=WTT_T) == []

    def test_no_op_uses_all_ok_steps(self) -> None:
        """No operation → side_effect_successes = count of all OK tool steps."""
        steps = [
            _llm_step(0, total_tokens=200_000),
            _step(1, "Bash", StepStatus.OK),
        ]
        trace = _make_trace("w5", steps, total_tokens=200_000)
        findings = detect_work_to_talk_ratio(trace, operation=None, wtt_t=WTT_T)
        # 1 success / 200 = 0.005 < WTT_T=0.05 → fires
        assert len(findings) == 1

    def test_evidence_contains_ratio(self) -> None:
        """Evidence dict contains wtt_ratio and threshold."""
        steps = [
            _llm_step(0, total_tokens=100_000),
        ]
        trace = _make_trace("w6", steps, total_tokens=100_000)
        op = _code_op()
        findings = detect_work_to_talk_ratio(trace, operation=op, wtt_t=WTT_T)
        assert "wtt_ratio" in findings[0].evidence
        assert "threshold" in findings[0].evidence


# ── LEARN stage ────────────────────────────────────────────────────────────────


class TestLearnToolExpectations:
    def _clean_trace(self, trace_id: str, tools: list[str]) -> TraceEnvelope:
        """Make a trace where all listed tools succeed (clean for Code Implementation)."""
        steps = [_step(i, t, StepStatus.OK, tool_args={"f": str(i)})
                 for i, t in enumerate(tools)]
        return _make_trace(trace_id, steps)

    def test_abstain_below_expect_min_n(self) -> None:
        """Fewer than EXPECT_MIN_N clean traces → abstain, no candidates."""
        op = _code_op(required=["Edit"], expected=["Edit", "Read"])
        traces = [self._clean_trace(f"t{i}", ["Edit", "Read"]) for i in range(EXPECT_MIN_N - 1)]
        result = learn_tool_expectations(traces, op, expect_min_n=EXPECT_MIN_N)
        assert result.abstained is True
        assert result.candidates == []
        assert result.abstain_reason is not None

    def test_abstain_reason_mentions_n(self) -> None:
        """Abstain reason names the count and threshold."""
        op = _code_op()
        result = learn_tool_expectations([], op, expect_min_n=5)
        assert "0" in result.abstain_reason

    def test_no_candidates_when_all_traces_have_tool(self) -> None:
        """All clean traces have all expected tools → no candidates."""
        op = _code_op(required=["Edit"], expected=["Edit", "Read"])
        traces = [self._clean_trace(f"t{i}", ["Edit", "Read"])
                  for i in range(EXPECT_MIN_N + 1)]
        result = learn_tool_expectations(traces, op, expect_t=0.9, expect_min_n=EXPECT_MIN_N)
        assert not result.abstained
        assert result.candidates == []

    def test_candidate_emitted_for_missing_tool(self) -> None:
        """A trace missing a near-universal tool → expectation-miss candidate.

        Setup: 10 clean traces all have both Edit and Read (Read presence=1.0 >=0.9).
        The test trace has Edit but not Read — it becomes an expectation-miss candidate.
        The test trace itself is NOT counted as clean (it has no side-effect success for
        the required tool OR we pass it separately as a non-clean candidate trace).
        """
        op = _code_op(required=["Edit"], expected=["Edit", "Read"])
        # 10 clean traces all have both tools — Read presence rate = 1.0
        clean = [self._clean_trace(f"clean{i}", ["Edit", "Read"]) for i in range(10)]
        # 1 additional trace with Edit (clean) but missing Read
        # It IS clean (Edit present = side-effect satisfied) but missing Read
        missing_read = self._clean_trace("miss1", ["Edit"])
        result = learn_tool_expectations(
            clean + [missing_read], op, expect_t=0.9, expect_min_n=5
        )
        assert not result.abstained
        # Read presence = 10/11 ≈ 0.909 >= 0.9 → expected
        # miss1 has no OK Read call → candidate
        candidates = [c for c in result.candidates if c.trace_id == "miss1"]
        assert any(c.missing_tool == "Read" for c in candidates)

    def test_presence_rate_computed_correctly(self) -> None:
        """Presence rate = fraction of clean traces with at least one OK call."""
        op = _code_op(required=["Edit"], expected=["Edit", "Read"])
        # 8 clean traces: 6 have Read, 2 don't (but have Edit so still clean)
        traces = []
        for i in range(6):
            traces.append(self._clean_trace(f"t{i}", ["Edit", "Read"]))
        for i in range(6, 8):
            traces.append(self._clean_trace(f"t{i}", ["Edit"]))
        result = learn_tool_expectations(traces, op, expect_t=0.9, expect_min_n=5)
        # Read present in 6/8 = 0.75 clean traces → below expect_t=0.9 → not expected → no miss
        read_rate = result.tool_presence_rates.get("Read", 0.0)
        assert abs(read_rate - 6 / 8) < 0.01

    def test_candidate_fields_populated(self) -> None:
        """Candidate has all required fields."""
        op = _code_op(required=["Edit"], expected=["Edit", "Read"])
        clean = [self._clean_trace(f"c{i}", ["Edit", "Read"]) for i in range(6)]
        missing = _make_trace("m1", [_step(0, "Edit", StepStatus.OK, tool_args={"f": "a"})])
        result = learn_tool_expectations(clean + [missing], op, expect_t=0.9, expect_min_n=5)
        for c in result.candidates:
            assert isinstance(c, ExpectationMissCandidate)
            assert c.workflow_name == op.name
            assert 0.0 < c.presence_rate <= 1.0
            assert c.clean_trace_count >= EXPECT_MIN_N


# ── Orchestrator ───────────────────────────────────────────────────────────────


class TestDetectSessionQuality:
    def test_returns_list_of_findings(self) -> None:
        """Orchestrator returns a flat list."""
        trace = _make_trace("orch1", [
            _step(0, "Edit", StepStatus.OK, tool_args={"f": "a"}),
        ])
        result = detect_session_quality([trace])
        assert isinstance(result, list)

    def test_empty_trace_no_findings(self) -> None:
        """Empty trace → no findings from any detector."""
        trace = _make_trace("orch2", [])
        assert detect_session_quality([trace]) == []

    def test_multiple_traces_aggregated(self) -> None:
        """Findings from multiple traces are concatenated."""
        t1 = _make_trace("orch3a", [
            _step(0, "Edit", StepStatus.ERROR, tool_args={"f": "a"}),
        ])
        t2 = _make_trace("orch3b", [
            _step(0, "Edit", StepStatus.ERROR, tool_args={"f": "b"}),
        ])
        findings = detect_session_quality([t1, t2])
        trace_ids = {f.trace_id for f in findings}
        assert "orch3a" in trace_ids
        assert "orch3b" in trace_ids


# ── D2 — F10 arg-similarity fix ───────────────────────────────────────────────


class TestD2ArgsRequiredForRedundancy:
    """D2 must NOT count consecutive same-tool pairs as redundant when args are absent
    (the F10 over-fire: 56 distinct Bash commands looked identical without real args).
    """

    def test_sequential_distinct_bash_commands_not_redundant(self) -> None:
        """56 sequential Bash steps with no args → redundant_steps == 0."""
        from kairos.detection.session_quality import _count_redundant_steps
        steps = [
            _step(i, "Bash", StepStatus.OK)  # no tool_args → empty
            for i in range(56)
        ]
        assert _count_redundant_steps(steps) == 0

    def test_sequential_same_tool_different_args_not_redundant(self) -> None:
        """Same tool, different args per step → not redundant."""
        from kairos.detection.session_quality import _count_redundant_steps
        steps = [
            _step(i, "Bash", StepStatus.OK, tool_args={"command": f"ls /dir{i}"})
            for i in range(10)
        ]
        assert _count_redundant_steps(steps) == 0

    def test_true_identical_arg_repeats_are_redundant(self) -> None:
        """Same tool, identical args → counted as redundant."""
        from kairos.detection.session_quality import _count_redundant_steps
        args = {"command": "curl http://api/status"}
        steps = [
            _step(i, "Bash", StepStatus.OK, tool_args=args)
            for i in range(4)
        ]
        # 3 consecutive pairs of (Bash,args)~(Bash,args) → 3 redundant
        assert _count_redundant_steps(steps) == 3

    def test_post_error_retry_not_redundant(self) -> None:
        """Error step followed by same-tool same-args step → NOT redundant (retry)."""
        from kairos.detection.session_quality import _count_redundant_steps
        args = {"file_path": "/a.py"}
        steps = [
            _step(0, "Edit", StepStatus.ERROR, tool_args=args),
            _step(1, "Edit", StepStatus.OK, tool_args=args),
        ]
        assert _count_redundant_steps(steps) == 0

    def test_struggle_ratio_no_fire_on_empty_args_bash_heavy(self) -> None:
        """Bash-heavy trace with absent args must NOT fire D2 (the 6a90e914 fix)."""
        # Simulate 83 Bash steps (like 6a90e914) with no args — all distinct commands
        # that we can't see because the transcript is missing.
        steps = [_step(i, "Bash", StepStatus.OK) for i in range(83)]
        steps.append(_step(83, "Edit", StepStatus.OK))  # 1 side effect
        trace = _make_trace("bash_heavy", steps)
        findings = detect_struggle_ratio(trace, struggle_t=2.0)
        # Without real args, redundant_steps=0 → struggle = 0/84 = 0.0 < 2.0 → no fire
        assert findings == []


# ── D1 — empty-args safe degradation ─────────────────────────────────────────


class TestD1EmptyArgsFallback:
    """D1 must degrade safely when args are absent (no transcript): use status-based
    recovery instead of firing on every error.
    """

    def test_no_args_ok_retry_counts_as_recovery(self) -> None:
        """No args on either side: a later OK call of same tool → recovered, no fire."""
        trace = _make_trace("d1_fallback1", [
            _step(0, "Edit", StepStatus.ERROR),   # no args
            _step(1, "Edit", StepStatus.OK),      # no args, but OK → recovery
        ])
        assert detect_unrecovered_error(trace) == []

    def test_no_args_no_ok_retry_fires(self) -> None:
        """No args, error step with no same-tool OK follow-up → fires."""
        trace = _make_trace("d1_fallback2", [
            _step(0, "Edit", StepStatus.ERROR),
            _step(1, "Bash", StepStatus.OK),
        ])
        findings = detect_unrecovered_error(trace)
        assert len(findings) == 1
        assert findings[0].evidence["tool"] == "Edit"

    def test_with_real_args_uses_jaccard_not_status(self) -> None:
        """When args present, jaccard must pass — OK retry with different args NOT recovery."""
        a_args = {"file_path": "/a.py", "old_string": "foo", "new_string": "bar"}
        b_args = {"file_path": "/b.py", "old_string": "zzz", "new_string": "yyy"}
        trace = _make_trace("d1_fallback3", [
            _step(0, "Edit", StepStatus.ERROR, tool_args=a_args),
            _step(1, "Edit", StepStatus.OK, tool_args=b_args),  # args differ → not recovery
        ])
        findings = detect_unrecovered_error(trace)
        assert len(findings) == 1


# ── D3 — empty-args F10 guard ─────────────────────────────────────────────────


class TestD3EmptyArgsGuard:
    """D3 must NOT collapse all empty-arg Bash calls into the same key and false-fire."""

    def test_empty_args_excluded_from_repeat_count(self) -> None:
        """Many Bash steps with no args → no repeat violation."""
        steps = [_step(i, "Bash", StepStatus.OK) for i in range(10)]
        trace = _make_trace("d3_empty1", steps)
        findings = detect_coordination_waste(trace, repeat_t=3, curl_t=0.99)
        assert findings == []

    def test_real_args_still_fire_repeat(self) -> None:
        """Steps with real identical args → repeat still fires."""
        args = {"command": "git status"}
        steps = [
            _step(i, "Bash", StepStatus.OK, tool_args=args, tool_args_normalized=args)
            for i in range(4)
        ]
        trace = _make_trace("d3_empty2", steps)
        findings = detect_coordination_waste(trace, repeat_t=3, curl_t=0.99)
        assert len(findings) == 1
        assert findings[0].evidence["repeat_violations"]

    def test_mixed_empty_and_real_args_only_real_counted(self) -> None:
        """Mix: some steps with args, some without → only args-bearing steps in count."""
        args = {"command": "git status"}
        steps = (
            [_step(i, "Bash", StepStatus.OK) for i in range(5)]  # no args
            + [_step(i + 5, "Bash", StepStatus.OK, tool_args=args, tool_args_normalized=args)
               for i in range(2)]  # real args but only 2 → below repeat_t=3
        )
        trace = _make_trace("d3_empty3", steps)
        findings = detect_coordination_waste(trace, repeat_t=3, curl_t=0.99)
        assert findings == []
