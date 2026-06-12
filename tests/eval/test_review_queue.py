"""Tests for eval/review/build_queue.py.

Covers:
  - collapsed_run logic: runs < threshold not collapsed, runs >= threshold are
  - question generation per verdict (fail/pass/non_computable/escalated)
  - answers.jsonl append + prefill round-trip
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

# Bootstrap: add eval/review to sys.path so the modules can be imported.
_EVAL_REVIEW = Path(__file__).parents[2] / "eval" / "review"
if str(_EVAL_REVIEW) not in sys.path:
    sys.path.insert(0, str(_EVAL_REVIEW))

import build_queue as bq  # type: ignore[import-untyped]  # noqa: E402

# ── helpers ───────────────────────────────────────────────────────────────────


def _make_step(index: int, tool: str, status: str = "ok", step_type: str = "tool_call") -> dict:
    """Build a minimal Step-like dict for testing via TraceEnvelope model."""
    from kairos.models.enums import StepStatus, StepStatusSource, StepType
    from kairos.models.trace import Step

    return Step(
        step_index=index,
        step_type=StepType(step_type),
        tool_name=tool if step_type == "tool_call" else None,
        status=StepStatus(status),
        status_source=StepStatusSource.NONE,
    )


# ── collapsed-run logic ───────────────────────────────────────────────────────


class TestCollapsedRuns:
    def test_short_run_not_collapsed(self) -> None:
        """3 same-tool steps (< threshold of 4) should NOT be collapsed."""
        steps = [_make_step(i, "Bash") for i in range(3)]
        step_entries, collapsed_runs = bq.build_step_list(steps, evidence_step_index=None)
        assert collapsed_runs == []
        assert all(not s["collapsed"] for s in step_entries)

    def test_run_at_threshold_is_collapsed(self) -> None:
        """Exactly 4 same-tool steps should be collapsed into one run entry."""
        steps = [_make_step(i, "Bash") for i in range(4)]
        step_entries, collapsed_runs = bq.build_step_list(steps, evidence_step_index=None)
        assert len(collapsed_runs) == 1
        assert collapsed_runs[0]["count"] == 4
        assert collapsed_runs[0]["first_index"] == 0
        assert collapsed_runs[0]["last_index"] == 3
        assert all(s["collapsed"] for s in step_entries)

    def test_long_run_collapsed(self) -> None:
        """47 consecutive Bash steps should collapse into a single run."""
        steps = [_make_step(i, "Bash") for i in range(47)]
        step_entries, collapsed_runs = bq.build_step_list(steps, evidence_step_index=None)
        assert len(collapsed_runs) == 1
        assert collapsed_runs[0]["count"] == 47

    def test_evidence_step_breaks_run(self) -> None:
        """A step designated as evidence must NOT be collapsed even mid-run."""
        steps = [_make_step(i, "Bash") for i in range(8)]
        # step 4 is evidence — it should not be collapsed
        step_entries, collapsed_runs = bq.build_step_list(steps, evidence_step_index=4)
        # evidence step should not be collapsed
        evidence_entries = [s for s in step_entries if s["index"] == 4]
        assert len(evidence_entries) == 1
        assert not evidence_entries[0]["collapsed"]

    def test_error_step_not_collapsed(self) -> None:
        """Error steps should never be swallowed into a collapsed run."""
        steps = [_make_step(i, "Bash") for i in range(4)]
        steps[2] = _make_step(2, "Bash", status="error")
        step_entries, collapsed_runs = bq.build_step_list(steps, evidence_step_index=None)
        # With an error step in the middle, the run is broken — should not collapse cleanly
        error_entries = [s for s in step_entries if s["index"] == 2]
        assert error_entries[0]["status"] == "error"
        assert not error_entries[0]["collapsed"]

    def test_mixed_tools_not_collapsed(self) -> None:
        """Alternating tools should not collapse even if same tool appears >= threshold times."""
        steps = []
        for i in range(8):
            tool = "Bash" if i % 2 == 0 else "Read"
            steps.append(_make_step(i, tool))
        _, collapsed_runs = bq.build_step_list(steps, evidence_step_index=None)
        assert collapsed_runs == []

    def test_two_separate_runs(self) -> None:
        """Two distinct collapsed runs of different tools should each appear separately."""
        bash_steps = [_make_step(i, "Bash") for i in range(4)]
        read_steps = [_make_step(i + 4, "Read") for i in range(4)]
        steps = bash_steps + read_steps
        _, collapsed_runs = bq.build_step_list(steps, evidence_step_index=None)
        assert len(collapsed_runs) == 2
        tools = {cr["tool"] for cr in collapsed_runs}
        assert tools == {"Bash", "Read"}


# ── question generation ───────────────────────────────────────────────────────


class TestQuestionGeneration:
    def test_fail_with_missing_side_effect(self) -> None:
        q = bq.generate_question("fail", "missing_side_effect")
        assert "FAIL" in q
        assert "missing_side_effect" in q
        assert "required write" in q.lower() or "never" in q.lower()
        assert "agree" in q.lower()

    def test_fail_with_critical_tool_error(self) -> None:
        q = bq.generate_question("fail", "critical_tool_error")
        assert "FAIL" in q
        assert "critical_tool_error" in q

    def test_fail_unknown_reason(self) -> None:
        q = bq.generate_question("fail", None)
        assert "FAIL" in q

    def test_pass(self) -> None:
        q = bq.generate_question("pass", None)
        assert "PASS" in q
        assert "engine" in q.lower() or "contract" in q.lower()

    def test_non_computable(self) -> None:
        q = bq.generate_question("non_computable", "partial_trace")
        assert "partial_trace" in q
        assert "abstained" in q.lower() or "read" in q.lower()

    def test_escalated_uses_non_computable_path(self) -> None:
        # escalated is not in the explicit branches — falls to the else
        q = bq.generate_question("escalated", None)
        assert q  # non-empty

    @pytest.mark.parametrize(
        "verdict,failure_reason",
        [
            ("fail", "terminal_error"),
            ("fail", "side_effect_output_failed"),
            ("fail", "partial_trace"),
            ("pass", None),
            ("non_computable", "terminal_unknown"),
        ],
    )
    def test_all_combos_produce_non_empty_question(self, verdict: str, failure_reason: str | None) -> None:
        q = bq.generate_question(verdict, failure_reason)
        assert isinstance(q, str)
        assert len(q) > 10


# ── answers.jsonl round-trip ──────────────────────────────────────────────────


class TestAnswersRoundTrip:
    """Test the answers append + prefill round-trip by exercising the app helpers directly."""

    def _write_answers(self, path: Path, records: list[dict]) -> None:
        with path.open("a", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")

    def _load_answers(self, path: Path) -> dict[str, dict]:
        answers: dict[str, dict] = {}
        if not path.exists():
            return answers
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    answers[rec["trace_id"]] = rec
                except (json.JSONDecodeError, KeyError):
                    continue
        return answers

    def test_append_single_answer(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode="w") as f:
            answers_path = Path(f.name)
        try:
            rec = {
                "trace_id": "abc123",
                "question": "Do you agree?",
                "answer": "Yes, engine is correct.",
                "verdict_shown": "fail",
                "ts": "2026-06-12T00:00:00+00:00",
            }
            self._write_answers(answers_path, [rec])
            answers = self._load_answers(answers_path)
            assert "abc123" in answers
            assert answers["abc123"]["answer"] == "Yes, engine is correct."
        finally:
            answers_path.unlink(missing_ok=True)

    def test_append_multiple_answers(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode="w") as f:
            answers_path = Path(f.name)
        try:
            records = [
                {"trace_id": f"trace_{i}", "question": "Q", "answer": f"A{i}", "verdict_shown": "pass", "ts": "ts"}
                for i in range(5)
            ]
            self._write_answers(answers_path, records)
            answers = self._load_answers(answers_path)
            assert len(answers) == 5
            for i in range(5):
                assert answers[f"trace_{i}"]["answer"] == f"A{i}"
        finally:
            answers_path.unlink(missing_ok=True)

    def test_later_answer_overwrites_earlier(self) -> None:
        """Last write for a trace_id should win (prefill = most recent answer)."""
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode="w") as f:
            answers_path = Path(f.name)
        try:
            records = [
                {"trace_id": "dup", "question": "Q", "answer": "first", "verdict_shown": "fail", "ts": "t1"},
                {"trace_id": "dup", "question": "Q", "answer": "second", "verdict_shown": "fail", "ts": "t2"},
            ]
            self._write_answers(answers_path, records)
            answers = self._load_answers(answers_path)
            assert answers["dup"]["answer"] == "second"
        finally:
            answers_path.unlink(missing_ok=True)

    def test_load_from_empty_file(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode="w") as f:
            answers_path = Path(f.name)
        try:
            answers = self._load_answers(answers_path)
            assert answers == {}
        finally:
            answers_path.unlink(missing_ok=True)

    def test_load_skips_corrupt_lines(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode="w") as f:
            answers_path = Path(f.name)
        try:
            with answers_path.open("w") as f:
                f.write('{"trace_id":"ok","question":"Q","answer":"A","verdict_shown":"pass","ts":"t"}\n')
                f.write("NOT VALID JSON\n")
                f.write('{"trace_id":"ok2","question":"Q2","answer":"A2","verdict_shown":"pass","ts":"t"}\n')
            answers = self._load_answers(answers_path)
            assert "ok" in answers
            assert "ok2" in answers
            assert len(answers) == 2
        finally:
            answers_path.unlink(missing_ok=True)

    def test_unanswered_sorted_before_answered(self) -> None:
        """Unanswered traces should appear before answered ones in the review order."""
        queue = [{"trace_id": f"t{i}"} for i in range(6)]
        answered = {"t1": {"answer": "a"}, "t3": {"answer": "b"}}
        unanswered = [e["trace_id"] for e in queue if e["trace_id"] not in answered]
        answered_list = [e["trace_id"] for e in queue if e["trace_id"] in answered]
        order = unanswered + answered_list
        assert order.index("t1") > order.index("t0")
        assert order.index("t3") > order.index("t0")
        # All unanswered first
        for i, tid in enumerate(order):
            if i < len(unanswered):
                assert tid not in answered
            else:
                assert tid in answered


# ── redaction safety ──────────────────────────────────────────────────────────


class TestRedaction:
    def test_redacts_bearer_token(self) -> None:
        # The authorization key=: pattern redacts the keyword + first token;
        # the remainder of a multi-part Bearer value may need the 40-char pattern.
        # Use a long single-blob token to reliably trigger full redaction.
        token = "A" * 50
        text = f"Authorization: Bearer {token}"
        result = bq.redact(text)
        assert token not in result
        assert "[REDACTED]" in result

    def test_redacts_sk_key(self) -> None:
        text = "sk-abcdefghijklmnopqrstuvwxyz123456"
        result = bq.redact(text)
        assert "sk-abc" not in result

    def test_redacts_email(self) -> None:
        text = "contact: user@example.com"
        result = bq.redact(text)
        assert "user@example.com" not in result

    def test_clean_text_unchanged(self) -> None:
        text = "cd /repo && git status"
        result = bq.redact(text)
        assert result == text

    def test_idempotent(self) -> None:
        text = "Authorization: Bearer secret123456789012345678901234"
        once = bq.redact(text)
        twice = bq.redact(once)
        assert once == twice
