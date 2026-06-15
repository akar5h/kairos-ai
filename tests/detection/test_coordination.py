"""Tests for detect_coordination_context — coordination-context classifier.

Covers:
  - fires on user_input marker phrase (case-insensitive)
  - fires on Skill:paperclip tool signature
  - fires on Bash:PAPERCLIP_API arg substring
  - does NOT fire when markers + tools are both empty (feature off)
  - does NOT fire on a clean coding trace with none of the signals
  - severity is always "info"
  - pattern_name is "coordination_context"
"""

from __future__ import annotations

from kairos.detection.coordination import detect_coordination_context
from kairos.models.enums import StepStatus, StepType
from kairos.models.trace import Step, TraceEnvelope

# ── Helpers (mirror the pattern from test_session_quality.py) ─────────────────

_MARKERS = ["wake payload", "resume delta", "heartbeat"]
_TOOLS = ["Skill:paperclip", "Bash:PAPERCLIP_API", "Bash:paperclip"]


def _step(
    index: int,
    tool_name: str | None = None,
    status: StepStatus = StepStatus.OK,
    step_type: StepType = StepType.TOOL_CALL,
    tool_args: dict | None = None,
    tool_args_normalized: dict | None = None,
) -> Step:
    return Step(
        step_index=index,
        step_type=step_type,
        tool_name=tool_name,
        status=status,
        tool_args=tool_args,
        tool_args_normalized=tool_args_normalized,
    )


def _make_trace(
    trace_id: str,
    steps: list[Step],
    user_input: str | None = None,
) -> TraceEnvelope:
    return TraceEnvelope(trace_id=trace_id, steps=steps, user_input=user_input)


# ── Feature-off guard ─────────────────────────────────────────────────────────


class TestFeatureOff:
    def test_both_empty_returns_none(self) -> None:
        """When markers and tools are both empty, feature is off — always None."""
        trace = _make_trace(
            "t0",
            steps=[_step(0, "Skill", tool_args={"args": "paperclip"})],
            user_input="Paperclip Wake Payload: heartbeat session for issue #42.",
        )
        result = detect_coordination_context(trace, markers=[], tools=[])
        assert result is None

    def test_only_markers_empty_tools_still_returns_none(self) -> None:
        """Markers empty + tools empty → None, even if user_input has a keyword."""
        trace = _make_trace("t1", steps=[], user_input="wake payload")
        result = detect_coordination_context(trace, markers=[], tools=[])
        assert result is None


# ── Marker firing (user_input) ────────────────────────────────────────────────


class TestMarkerFiring:
    def test_fires_on_wake_payload_exact_case(self) -> None:
        """Marker 'wake payload' fires when user_input contains it verbatim."""
        trace = _make_trace(
            "t2",
            steps=[_step(0, "Read", tool_args={"file_path": "/src/foo.py"})],
            user_input="Paperclip Wake Payload: heartbeat scoped to issue #7.",
        )
        finding = detect_coordination_context(trace, markers=_MARKERS, tools=_TOOLS)
        assert finding is not None
        assert finding.pattern_name == "coordination_context"
        assert finding.severity == "info"
        assert finding.trace_id == "t2"
        assert finding.evidence["matched_marker"] == "wake payload"
        assert finding.evidence["match_location"] == "task text"

    def test_fires_on_wake_payload_upper_case(self) -> None:
        """Marker matching is case-insensitive."""
        trace = _make_trace(
            "t3",
            steps=[],
            user_input="WAKE PAYLOAD received from scheduler.",
        )
        finding = detect_coordination_context(trace, markers=_MARKERS, tools=_TOOLS)
        assert finding is not None
        assert finding.evidence["matched_marker"] == "wake payload"

    def test_fires_on_resume_delta(self) -> None:
        """'resume delta' marker fires correctly."""
        trace = _make_trace(
            "t4",
            steps=[],
            user_input="Paperclip Resume Delta: pick up from where you left off.",
        )
        finding = detect_coordination_context(trace, markers=_MARKERS, tools=_TOOLS)
        assert finding is not None
        assert finding.evidence["matched_marker"] == "resume delta"

    def test_fires_on_heartbeat(self) -> None:
        """'heartbeat' marker fires correctly."""
        trace = _make_trace(
            "t5",
            steps=[],
            user_input="Continue your heartbeat session for issue #15.",
        )
        finding = detect_coordination_context(trace, markers=_MARKERS, tools=_TOOLS)
        assert finding is not None
        assert finding.evidence["matched_marker"] == "heartbeat"

    def test_marker_match_takes_priority_over_tool_match(self) -> None:
        """When task text matches AND a step matches, the marker (task text) fires first."""
        trace = _make_trace(
            "t6",
            steps=[_step(5, "Skill", tool_args={"args": "paperclip"})],
            user_input="wake payload: issue #99",
        )
        finding = detect_coordination_context(trace, markers=_MARKERS, tools=_TOOLS)
        assert finding is not None
        assert finding.evidence["match_location"] == "task text"


# ── Tool signature firing ─────────────────────────────────────────────────────


class TestToolFiring:
    def test_fires_on_skill_paperclip(self) -> None:
        """Skill tool with 'paperclip' in args matches Skill:paperclip signature."""
        trace = _make_trace(
            "t7",
            steps=[
                _step(0, "Read", tool_args={"file_path": "/src/main.py"}),
                _step(1, "Skill", tool_args={"skill": "paperclip", "action": "checkout"}),
            ],
            user_input="Implement the new feature on branch dev.",
        )
        finding = detect_coordination_context(trace, markers=_MARKERS, tools=_TOOLS)
        assert finding is not None
        assert finding.pattern_name == "coordination_context"
        assert finding.severity == "info"
        assert finding.evidence["matched_tool_signature"] == "Skill:paperclip"
        assert finding.evidence["match_location"] == "step 1"
        assert finding.affected_step_indices == [1]

    def test_fires_on_bash_paperclip_api(self) -> None:
        """Bash step with PAPERCLIP_API in args matches Bash:PAPERCLIP_API signature."""
        trace = _make_trace(
            "t8",
            steps=[
                _step(
                    2,
                    "Bash",
                    tool_args={"command": 'curl -X POST "$PAPERCLIP_API_URL/api/issues/checkout"'},
                ),
            ],
            user_input="Check what tasks are pending.",
        )
        finding = detect_coordination_context(trace, markers=_MARKERS, tools=_TOOLS)
        assert finding is not None
        assert finding.evidence["matched_tool_signature"] == "Bash:PAPERCLIP_API"
        assert finding.evidence["match_location"] == "step 2"

    def test_fires_on_bash_paperclip_lowercase_in_args(self) -> None:
        """Bash:paperclip signature fires on a Bash command referencing 'paperclip'."""
        trace = _make_trace(
            "t9",
            steps=[
                _step(
                    3,
                    "Bash",
                    tool_args={"command": "python3 -c 'import paperclip; paperclip.status()'"},
                ),
            ],
            user_input="Run the analysis.",
        )
        finding = detect_coordination_context(trace, markers=_MARKERS, tools=_TOOLS)
        assert finding is not None
        # "PAPERCLIP_API" is not in args but "paperclip" is — Bash:paperclip fires.
        assert finding.evidence["matched_tool_signature"] == "Bash:paperclip"

    def test_fires_using_tool_args_normalized_when_available(self) -> None:
        """Detector uses tool_args_normalized over tool_args (matching session_quality pattern)."""
        trace = _make_trace(
            "t10",
            steps=[
                _step(
                    4,
                    "Skill",
                    # tool_args has no signal, tool_args_normalized has the substring
                    tool_args={"x": "irrelevant"},
                    tool_args_normalized={"skill": "paperclip"},
                ),
            ],
            user_input="Do some coding.",
        )
        finding = detect_coordination_context(trace, markers=_MARKERS, tools=_TOOLS)
        assert finding is not None
        assert finding.evidence["matched_tool_signature"] == "Skill:paperclip"

    def test_bare_tool_name_sig_no_substring(self) -> None:
        """A bare 'ToolName' signature (no colon) matches on tool_name only."""
        trace = _make_trace(
            "t11",
            steps=[_step(0, "Skill", tool_args={"skill": "unrelated_thing"})],
            user_input="Normal task.",
        )
        finding = detect_coordination_context(
            trace,
            markers=[],
            tools=["Skill"],  # no substring requirement
        )
        assert finding is not None
        assert finding.evidence["matched_tool_signature"] == "Skill"


# ── No-fire cases ─────────────────────────────────────────────────────────────


class TestNoFire:
    def test_clean_coding_trace_no_signal(self) -> None:
        """A genuine coding trace with no coordination signals returns None."""
        trace = _make_trace(
            "t12",
            steps=[
                _step(0, "Read", tool_args={"file_path": "/src/api.py"}),
                _step(1, "Edit", tool_args={"file_path": "/src/api.py", "old": "x", "new": "y"}),
                _step(2, "Bash", tool_args={"command": "pytest -q"}),
            ],
            user_input="Fix the null pointer exception in api.py line 42.",
        )
        result = detect_coordination_context(trace, markers=_MARKERS, tools=_TOOLS)
        assert result is None

    def test_no_fire_when_user_input_none(self) -> None:
        """user_input=None with no matching steps → no finding."""
        trace = _make_trace(
            "t13",
            steps=[_step(0, "Read", tool_args={"file_path": "/src/app.py"})],
            user_input=None,
        )
        result = detect_coordination_context(trace, markers=_MARKERS, tools=_TOOLS)
        assert result is None

    def test_no_fire_skill_wrong_tool_name(self) -> None:
        """'Skill:paperclip' sig must match tool_name == 'Skill' exactly — 'Agent' doesn't match."""
        trace = _make_trace(
            "t14",
            steps=[_step(0, "Agent", tool_args={"args": "paperclip"})],
            user_input="Delegate work.",
        )
        result = detect_coordination_context(trace, markers=_MARKERS, tools=_TOOLS)
        assert result is None

    def test_no_fire_bash_unrelated_args(self) -> None:
        """Bash step whose args have no PAPERCLIP substring → no finding."""
        trace = _make_trace(
            "t15",
            steps=[
                _step(0, "Bash", tool_args={"command": "git status"}),
                _step(1, "Bash", tool_args={"command": "npm test"}),
            ],
            user_input="Run CI checks.",
        )
        result = detect_coordination_context(trace, markers=_MARKERS, tools=_TOOLS)
        assert result is None

    def test_no_fire_empty_steps(self) -> None:
        """Trace with no steps and no matching user_input → None."""
        trace = _make_trace("t16", steps=[], user_input="Implement auth flow.")
        result = detect_coordination_context(trace, markers=_MARKERS, tools=_TOOLS)
        assert result is None


# ── Finding field assertions ───────────────────────────────────────────────────


class TestFindingFields:
    def test_severity_always_info(self) -> None:
        """coordination_context findings are always severity='info'."""
        trace = _make_trace("t17", steps=[], user_input="wake payload for issue 3")
        finding = detect_coordination_context(trace, markers=_MARKERS, tools=[])
        assert finding is not None
        assert finding.severity == "info"

    def test_confidence_is_1_on_marker_match(self) -> None:
        """Marker match is deterministic → confidence 1.0."""
        trace = _make_trace("t18", steps=[], user_input="Paperclip heartbeat session")
        finding = detect_coordination_context(trace, markers=["heartbeat"], tools=[])
        assert finding is not None
        assert finding.confidence == 1.0

    def test_confidence_is_1_on_tool_match(self) -> None:
        """Tool match is deterministic → confidence 1.0."""
        trace = _make_trace(
            "t19",
            steps=[_step(0, "Skill", tool_args={"skill": "paperclip"})],
            user_input="Do work",
        )
        finding = detect_coordination_context(trace, markers=[], tools=["Skill:paperclip"])
        assert finding is not None
        assert finding.confidence == 1.0

    def test_estimated_token_waste_is_zero(self) -> None:
        """Coordination flag is not a waste signal — token waste is 0."""
        trace = _make_trace("t20", steps=[], user_input="resume delta: pick up work")
        finding = detect_coordination_context(trace, markers=_MARKERS, tools=[])
        assert finding is not None
        assert finding.estimated_token_waste == 0
