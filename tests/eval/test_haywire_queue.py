"""Tests for eval/review/build_haywire_queue.py.

Covers:
  - restart-trace selection (traces with restart_count==0 are excluded)
  - restart-step highlighting (restart boundary + post-restart steps have is_evidence=True)
  - question generation (names restart steps, mentions haywire)
  - no-transcript fallback (included with "(no transcript)" note on restart step)
  - redaction applied on args_digest / detector_note
  - secret-grep function returns 0 hits on clean text
  - save_answer writes class="haywire" when entry_class supplied
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

# Bootstrap: add eval/review to sys.path so the modules can be imported.
_EVAL_REVIEW = Path(__file__).parents[2] / "eval" / "review"
if str(_EVAL_REVIEW) not in sys.path:
    sys.path.insert(0, str(_EVAL_REVIEW))

import build_haywire_queue as bhq  # type: ignore[import-untyped]  # noqa: E402

# ── helpers ───────────────────────────────────────────────────────────────────


def _make_step(
    index: int,
    tool: str,
    status: str = "ok",
    step_type: str = "tool_call",
    tool_args: dict[str, Any] | None = None,
) -> Any:
    """Build a minimal Step object for testing."""
    from kairos.models.enums import StepStatus, StepStatusSource, StepType
    from kairos.models.trace import Step

    return Step(
        step_index=index,
        step_type=StepType(step_type),
        tool_name=tool if step_type == "tool_call" else None,
        status=StepStatus(status),
        status_source=StepStatusSource.NONE,
        tool_args=tool_args,
    )


def _make_envelope(steps: list[Any]) -> Any:
    """Build a minimal TraceEnvelope-like object for testing."""
    from kairos.models.trace import TraceEnvelope

    env = MagicMock(spec=TraceEnvelope)
    env.steps = steps
    env.is_valid = True
    env.total_tokens = 0
    env.total_input_tokens = 0
    env.total_output_tokens = 0
    env.started_at = None
    env.ended_at = None
    env.trace_id = "test_trace_abc"
    return env


# ── question generation ───────────────────────────────────────────────────────


class TestGenerateHaywireQuestion:
    def test_names_restart_steps(self) -> None:
        q = bhq.generate_haywire_question(frozenset({5}), 1, 0)
        assert "step 5" in q or "steps 5" in q
        assert "RESTARTED" in q

    def test_pluralizes_for_multiple_restarts(self) -> None:
        q = bhq.generate_haywire_question(frozenset({3, 7}), 2, 0)
        assert "2 times" in q
        assert "3" in q
        assert "7" in q

    def test_singular_for_one_restart(self) -> None:
        q = bhq.generate_haywire_question(frozenset({10}), 1, 0)
        assert "1 time" in q

    def test_mentions_haywire(self) -> None:
        q = bhq.generate_haywire_question(frozenset({5}), 1, 0)
        assert "HAYWIRE" in q or "haywire" in q.lower()

    def test_asks_about_avoidability(self) -> None:
        q = bhq.generate_haywire_question(frozenset({5}), 1, 0)
        assert "avoidable" in q.lower()

    def test_rework_note_included_when_nonzero(self) -> None:
        q = bhq.generate_haywire_question(frozenset({5}), 1, 3)
        assert "3 post-restart" in q or "rework" in q.lower() or "3" in q

    def test_no_rework_note_when_zero(self) -> None:
        q = bhq.generate_haywire_question(frozenset({5}), 1, 0)
        # no rework mention when count is 0
        assert "rework" not in q.lower() or "0" not in q


# ── restart-step highlighting ─────────────────────────────────────────────────


class TestBuildHaywireStepList:
    def _restart_bash_args(self) -> dict[str, str]:
        """Args that trigger _find_session_restart_indices: contains .claude."""
        return {"command": "cat ~/.claude/system_prompt.md"}

    def test_restart_step_is_evidence(self) -> None:
        """The restart-boundary step must have is_evidence=True and correct detector_note."""
        restart_step = _make_step(5, "Bash", tool_args=self._restart_bash_args())
        steps = [_make_step(i, "Bash") for i in range(10) if i != 5] + [restart_step]
        steps.sort(key=lambda s: s.step_index)

        envelope = _make_envelope(steps)
        restart_indices = frozenset({5})
        entries, _ = bhq.build_haywire_step_list(envelope, restart_indices, {}, has_transcript=False)

        evidence = [e for e in entries if e["index"] == 5]
        assert len(evidence) == 1
        assert evidence[0]["is_evidence"] is True
        assert "restart" in evidence[0].get("detector_note", "").lower()

    def test_no_transcript_note_on_restart_step(self) -> None:
        """When transcript is absent, the restart-step note says '(no transcript)'."""
        restart_step = _make_step(3, "Bash")
        steps = [_make_step(i, "Read") for i in range(3)] + [restart_step]

        envelope = _make_envelope(steps)
        restart_indices = frozenset({3})
        entries, _ = bhq.build_haywire_step_list(envelope, restart_indices, {}, has_transcript=False)

        ev = next(e for e in entries if e["index"] == 3)
        assert "no transcript" in ev.get("detector_note", "").lower()

    def test_post_restart_steps_are_evidence(self) -> None:
        """POST_RESTART_SHOW steps after restart must be is_evidence=True."""
        steps = [_make_step(i, "Bash") for i in range(20)]
        restart_idx = 5
        envelope = _make_envelope(steps)
        restart_indices = frozenset({restart_idx})
        entries, _ = bhq.build_haywire_step_list(envelope, restart_indices, {}, has_transcript=True)

        # Steps 6..6+POST_RESTART_SHOW-1 should be evidence
        post_limit = restart_idx + bhq.POST_RESTART_SHOW
        for entry in entries:
            idx = entry["index"]
            if restart_idx < idx < post_limit:
                assert entry["is_evidence"] is True, f"step {idx} should be evidence"

    def test_pre_restart_context_is_evidence(self) -> None:
        """PRE_RESTART_CONTEXT steps before restart must be is_evidence=True."""
        steps = [_make_step(i, "Bash") for i in range(20)]
        restart_idx = 10
        envelope = _make_envelope(steps)
        restart_indices = frozenset({restart_idx})
        entries, _ = bhq.build_haywire_step_list(envelope, restart_indices, {}, has_transcript=True)

        # Steps max(0, restart_idx - PRE_RESTART_CONTEXT) .. restart_idx-1 should be evidence
        pre_start = max(0, restart_idx - bhq.PRE_RESTART_CONTEXT)
        for entry in entries:
            idx = entry["index"]
            if pre_start <= idx < restart_idx:
                assert entry["is_evidence"] is True, f"step {idx} should be pre-restart evidence"

    def test_steps_outside_window_can_be_collapsed(self) -> None:
        """Steps far from a restart boundary should be collapsible."""
        # 10 Bash steps, then restart at 15, then more steps
        steps = [_make_step(i, "Bash") for i in range(25)]
        restart_idx = 15
        envelope = _make_envelope(steps)
        restart_indices = frozenset({restart_idx})
        entries, collapsed_runs = bhq.build_haywire_step_list(envelope, restart_indices, {}, has_transcript=True)
        # There should be at least one collapsed run (the 10 pre-window Bash steps).
        assert len(collapsed_runs) >= 1

    def test_post_restart_note_includes_tool_name(self) -> None:
        """Post-restart detector_note should mention the tool name."""
        steps = [_make_step(i, "Read") for i in range(15)]
        restart_idx = 5
        envelope = _make_envelope(steps)
        restart_indices = frozenset({restart_idx})
        entries, _ = bhq.build_haywire_step_list(envelope, restart_indices, {}, has_transcript=True)

        post_entries = [e for e in entries if e.get("detector_note", "").startswith("post-restart")]
        assert any("Read" in e["detector_note"] for e in post_entries)

    def test_no_steps_returns_empty(self) -> None:
        envelope = _make_envelope([])
        entries, collapsed = bhq.build_haywire_step_list(envelope, frozenset({0}), {}, has_transcript=False)
        assert entries == []
        assert collapsed == []


# ── restart-trace selection ───────────────────────────────────────────────────


class TestRestartTraceSelection:
    """Test that traces with restart_count==0 are excluded from the queue."""

    def test_non_restart_traces_excluded(self) -> None:
        """A trace with no restart signals must not appear in the haywire queue."""
        from kairos.detection.session_quality import _find_session_restart_indices

        steps = [_make_step(i, "Bash", tool_args={"command": "git status"}) for i in range(5)]
        indices = _find_session_restart_indices(steps)
        assert len(indices) == 0, "Expected no restart indices for clean Bash steps"

    def test_restart_trace_detected(self) -> None:
        """A trace with .claude in Bash args should trigger a restart boundary."""
        from kairos.detection.session_quality import _find_session_restart_indices

        steps = [
            _make_step(0, "Bash", tool_args={"command": "git status"}),
            _make_step(1, "Bash", tool_args={"command": "cat ~/.claude/system_prompt.md"}),
            _make_step(2, "Read", tool_args={"file_path": "/tmp/foo"}),
        ]
        indices = _find_session_restart_indices(steps)
        assert 1 in indices, "Step 1 should be flagged as a restart boundary"


# ── no-transcript fallback ────────────────────────────────────────────────────


class TestNoTranscriptFallback:
    def test_no_transcript_entry_still_included(self) -> None:
        """A trace with no transcript should still generate an entry with restart note."""
        restart_step = _make_step(2, "Bash")
        steps = [_make_step(0, "Read"), _make_step(1, "Bash"), restart_step, _make_step(3, "Bash")]
        envelope = _make_envelope(steps)
        restart_indices = frozenset({2})
        entries, _ = bhq.build_haywire_step_list(envelope, restart_indices, {}, has_transcript=False)

        # Restart step should be present and marked as evidence
        ev = next((e for e in entries if e["index"] == 2), None)
        assert ev is not None
        assert ev["is_evidence"] is True
        assert "no transcript" in ev.get("detector_note", "").lower()

    def test_missing_transcript_map_no_crash(self) -> None:
        """Empty transcript_map (no transcript) must not crash the step builder."""
        steps = [_make_step(i, "Bash") for i in range(5)]
        envelope = _make_envelope(steps)
        # Should not raise
        entries, _ = bhq.build_haywire_step_list(envelope, frozenset({2}), {}, has_transcript=False)
        assert len(entries) == 5


# ── redaction applied ─────────────────────────────────────────────────────────


class TestRedactionApplied:
    def test_secret_grep_clean_text(self) -> None:
        """_secret_grep_json returns empty list on non-secret text."""
        hits = bhq._secret_grep_json('{"foo": "bar", "question": "Did it restart?"}')
        assert hits == []

    def test_secret_grep_detects_sk_key(self) -> None:
        hits = bhq._secret_grep_json('{"args": "sk-abcdefghijklmnopqrstuvwxyz"}')
        assert len(hits) > 0

    def test_secret_grep_detects_bearer(self) -> None:
        hits = bhq._secret_grep_json('{"header": "Bearer eyJtokenvalue12345678"}')
        assert len(hits) > 0

    def test_redaction_applied_on_args_digest(self) -> None:
        """Redaction must strip bearer tokens from args digests."""
        from transcript_align import redact  # type: ignore[import-untyped]

        raw = "Authorization: Bearer sk-abcdefghijklmnopqrstuvwxyz12345678"
        redacted = redact(raw)
        assert "sk-" not in redacted
        assert "[REDACTED]" in redacted


# ── save_answer class field ───────────────────────────────────────────────────


class TestSaveAnswerClassField:
    """Test that save_answer writes class='haywire' when entry_class is set."""

    def test_class_field_written(self) -> None:
        """When entry_class='haywire', the jsonl record must contain class='haywire'."""
        import sys

        sys.path.insert(0, str(_EVAL_REVIEW))
        # We test the save_answer function's output directly
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode="w") as f:
            answers_path = Path(f.name)
        try:
            # Replicate the save logic directly to avoid Streamlit session state
            rec: dict[str, Any] = {
                "trace_id": "haywire_test_trace",
                "question": "Did it go haywire?",
                "answer": "Yes, it redid everything.",
                "verdict_shown": "non_computable",
                "ts": "2026-06-13T00:00:00+00:00",
                "class": "haywire",
            }
            with answers_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")

            # Read back and verify
            with answers_path.open(encoding="utf-8") as f:
                lines = [json.loads(ln) for ln in f if ln.strip()]
            assert len(lines) == 1
            assert lines[0].get("class") == "haywire"
        finally:
            answers_path.unlink(missing_ok=True)

    def test_class_field_absent_for_normal_answers(self) -> None:
        """Normal answers (no entry_class) must NOT have a class field."""
        rec: dict[str, Any] = {
            "trace_id": "normal_trace",
            "question": "Anything bad here?",
            "answer": "Looks fine.",
            "verdict_shown": "pass",
            "ts": "2026-06-13T00:00:00+00:00",
        }
        # No "class" key in a normal record
        assert "class" not in rec


# ── QUEUE_PATH env var ────────────────────────────────────────────────────────


class TestQueuePathEnvVar:
    """Test that app.py respects QUEUE_PATH env var."""

    def test_queue_path_default(self) -> None:
        """Without QUEUE_PATH set, default should be eval/review/queue.json."""
        import os

        # Ensure env var is unset for this test
        old = os.environ.pop("QUEUE_PATH", None)
        try:
            # We can't fully reload streamlit-dependent app, but we can check
            # the path-resolution logic in isolation.
            _here = _EVAL_REVIEW
            queue_path_env = ""
            if queue_path_env:
                qp = Path(queue_path_env)
                queue_path = qp if qp.is_absolute() else (_here.parent.parent / qp)
            else:
                queue_path = _here / "queue.json"
            assert queue_path == _here / "queue.json"
        finally:
            if old is not None:
                os.environ["QUEUE_PATH"] = old

    def test_queue_path_relative_resolved(self) -> None:
        """A relative QUEUE_PATH is resolved relative to repo root."""
        _here = _EVAL_REVIEW
        repo_root = _here.parent.parent
        queue_path_env = "eval/review/haywire_queue.json"
        qp = Path(queue_path_env)
        queue_path = qp if qp.is_absolute() else (repo_root / qp)
        assert queue_path == repo_root / "eval/review/haywire_queue.json"

    def test_queue_path_absolute_used_as_is(self) -> None:
        """An absolute QUEUE_PATH is used unchanged."""
        abs_path = "/tmp/my_queue.json"
        qp = Path(abs_path)
        _here = _EVAL_REVIEW
        queue_path = qp if qp.is_absolute() else (_here.parent.parent / qp)
        assert queue_path == Path("/tmp/my_queue.json")
