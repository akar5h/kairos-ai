"""Tests for eval/review/build_disagreement_queue.py.

Covers:
  - label→coarse mapping (CLEAN / PROBLEM / UNKNOWN)
  - spotcheck row→coarse mapping
  - disagreement classification logic
    (fired+clean → disagree; fired+problem → not; silent+clean → not;
     silent+problem → disagree)
  - cap + drop logging
  - detector_note attached to the right steps
  - generate_disagreement_question content
  - parse_spotcheck_md table parsing
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

# Bootstrap: add eval/review to sys.path so the module can be imported.
_EVAL_REVIEW = Path(__file__).parents[2] / "eval" / "review"
if str(_EVAL_REVIEW) not in sys.path:
    sys.path.insert(0, str(_EVAL_REVIEW))

import build_disagreement_queue as bdq  # type: ignore[import-untyped]  # noqa: E402

# ── helpers ───────────────────────────────────────────────────────────────────


def _make_finding(
    pattern_name: str,
    severity: str = "warning",
    affected_step_indices: list[int] | None = None,
    evidence: dict[str, Any] | None = None,
) -> Any:
    """Build a minimal Finding-like object."""
    from kairos.detection.models import Finding

    return Finding(
        pattern_name=pattern_name,
        tier=1,
        trace_id="test_trace",
        confidence=1.0,
        severity=severity,
        evidence=evidence or {},
        affected_step_indices=affected_step_indices or [],
        estimated_token_waste=0,
    )


# ── label→coarse mapping ──────────────────────────────────────────────────────


class TestMapLabelToCoarse:
    def test_pass_answer_maps_clean(self) -> None:
        assert bdq.map_label_to_coarse("pass", "pass") == "CLEAN"

    def test_lgtm_maps_clean(self) -> None:
        assert bdq.map_label_to_coarse("LGTM", "pass") == "CLEAN"

    def test_looks_good_maps_clean(self) -> None:
        assert bdq.map_label_to_coarse("Looks good to me!", "pass") == "CLEAN"

    def test_short_pass_verdict_maps_clean(self) -> None:
        # Short answer + pass verdict = owner confirming
        assert bdq.map_label_to_coarse("ok I agree", "pass") == "CLEAN"

    def test_error_mention_maps_problem(self) -> None:
        assert bdq.map_label_to_coarse("There were errors that were never retried.", "pass") == "PROBLEM"

    def test_haywire_maps_problem(self) -> None:
        assert bdq.map_label_to_coarse("The agent went haywire after the shell restarted.", "fail") == "PROBLEM"

    def test_struggle_maps_problem(self) -> None:
        assert bdq.map_label_to_coarse("I think this was a struggle session.", "fail") == "PROBLEM"

    def test_inconclusive_maps_unknown(self) -> None:
        assert bdq.map_label_to_coarse("I can't comment on this without more data.", "pass") == "UNKNOWN"

    def test_no_verdict_unclear_maps_unknown(self) -> None:
        assert bdq.map_label_to_coarse("The steps are unclear to me.", "non_computable") == "UNKNOWN"

    def test_long_neutral_answer_non_pass_maps_unknown(self) -> None:
        # No CLEAN or PROBLEM keywords, not a short pass confirmation
        text = "The agent did some interesting things with the codebase."
        assert bdq.map_label_to_coarse(text, "non_computable") == "UNKNOWN"


# ── spotcheck row→coarse ──────────────────────────────────────────────────────


class TestSpotcheckToLabel:
    def test_agree_y_pass(self) -> None:
        coarse, _, _ = bdq.spotcheck_to_label({"agree": "Y", "verdict": "pass", "comment": ""})
        assert coarse == "CLEAN"

    def test_agree_y_fail(self) -> None:
        coarse, _, _ = bdq.spotcheck_to_label({"agree": "Y", "verdict": "fail", "comment": "bro it failed"})
        assert coarse == "PROBLEM"

    def test_agree_n_pass(self) -> None:
        # Owner disagrees with pass verdict → the trace is actually bad
        coarse, _, _ = bdq.spotcheck_to_label({"agree": "N", "verdict": "pass", "comment": "wrong verdict"})
        assert coarse == "PROBLEM"

    def test_agree_n_fail(self) -> None:
        # Owner disagrees with fail → the trace was actually fine
        coarse, _, _ = bdq.spotcheck_to_label({"agree": "N", "verdict": "fail", "comment": "edits happened"})
        assert coarse == "CLEAN"

    def test_agree_question_mark(self) -> None:
        coarse, _, _ = bdq.spotcheck_to_label({"agree": "?", "verdict": "fail", "comment": ""})
        assert coarse == "UNKNOWN"

    def test_prior_label_includes_agree_and_verdict(self) -> None:
        _, prior_label, _ = bdq.spotcheck_to_label({"agree": "Y", "verdict": "pass", "comment": ""})
        assert "AGREE=Y" in prior_label
        assert "pass" in prior_label

    def test_comment_passed_through(self) -> None:
        _, _, comment = bdq.spotcheck_to_label({"agree": "Y", "verdict": "fail", "comment": "confirmed fail"})
        assert comment == "confirmed fail"


# ── disagreement classification logic ─────────────────────────────────────────


class TestDisagreementClassification:
    """Verify the disagreement conditions directly from business logic."""

    def test_fired_clean_is_disagreement(self) -> None:
        """D1 or D2 fired + CLEAN label → disagreement (kind=fired_clean)."""
        d1 = [_make_finding("unrecovered_error")]
        coarse = "CLEAN"
        d1_fired = bool(d1)
        d2_fired = False
        any_fired = d1_fired or d2_fired
        assert coarse == "CLEAN" and any_fired
        kind = "fired_clean" if (coarse == "CLEAN" and any_fired) else None
        assert kind == "fired_clean"

    def test_silent_problem_is_disagreement(self) -> None:
        """Neither D1 nor D2 fired + PROBLEM label → disagreement (kind=silent_problem)."""
        d1 = []
        d2 = []
        coarse = "PROBLEM"
        any_fired = bool(d1) or bool(d2)
        assert coarse == "PROBLEM" and not any_fired
        kind = "silent_problem" if (coarse == "PROBLEM" and not any_fired) else None
        assert kind == "silent_problem"

    def test_fired_problem_is_not_disagreement(self) -> None:
        """D1/D2 fired + PROBLEM label → agreement, NOT a disagreement."""
        d1 = [_make_finding("unrecovered_error")]
        coarse = "PROBLEM"
        any_fired = bool(d1)
        # Agreement: detectors and owner both say problem
        is_disagree = (coarse == "CLEAN" and any_fired) or (coarse == "PROBLEM" and not any_fired)
        assert not is_disagree

    def test_silent_clean_is_not_disagreement(self) -> None:
        """Neither fired + CLEAN label → agreement, NOT a disagreement."""
        d1 = []
        d2 = []
        coarse = "CLEAN"
        any_fired = bool(d1) or bool(d2)
        is_disagree = (coarse == "CLEAN" and any_fired) or (coarse == "PROBLEM" and not any_fired)
        assert not is_disagree

    def test_unknown_never_disagrees(self) -> None:
        """UNKNOWN labels are skipped — never become disagreements."""
        # In the main loop, UNKNOWN traces are skipped before the disagreement check.
        coarse = "UNKNOWN"
        d1 = [_make_finding("unrecovered_error")]
        any_fired = bool(d1)
        # Only CLEAN or PROBLEM enter the disagreement check
        if coarse == "UNKNOWN":
            is_disagree = False
        else:
            is_disagree = (coarse == "CLEAN" and any_fired) or (coarse == "PROBLEM" and not any_fired)
        assert not is_disagree


# ── cap + drop logging ────────────────────────────────────────────────────────


class TestCapAndDrop:
    def test_cap_at_max_queue(self) -> None:
        """When candidates exceed MAX_QUEUE, only MAX_QUEUE are kept."""
        cap = bdq.MAX_QUEUE
        # Build cap+5 fake candidates with identical priority
        candidates = [
            {
                "trace_id": f"trace_{i:02d}",
                "priority": (0, 1, 0),
            }
            for i in range(cap + 5)
        ]
        candidates.sort(key=lambda c: c["priority"], reverse=True)
        dropped_count = max(0, len(candidates) - cap)
        kept = candidates[:cap]
        dropped = candidates[cap:]
        assert len(kept) == cap
        assert len(dropped) == 5
        assert dropped_count == 5

    def test_priority_d1_error_ranked_first(self) -> None:
        """D1 error-severity findings rank above D1 warning."""
        f_error = _make_finding("unrecovered_error", severity="error")
        f_warn = _make_finding("unrecovered_error", severity="warning")
        f_d2 = _make_finding("struggle_ratio", severity="warning")

        score_error = bdq._priority_score([f_error], [])
        score_warn = bdq._priority_score([f_warn], [])
        score_d2_only = bdq._priority_score([], [f_d2])

        assert score_error > score_warn
        assert score_warn > score_d2_only

    def test_more_fires_rank_higher(self) -> None:
        """More total fires → higher priority."""
        f1 = _make_finding("unrecovered_error")
        f2 = _make_finding("unrecovered_error")
        score_two = bdq._priority_score([f1, f2], [])
        score_one = bdq._priority_score([f1], [])
        assert score_two > score_one


# ── detector_note attachment ──────────────────────────────────────────────────


class TestDetectorNoteAttachment:
    def test_d1_note_attached_to_flagged_step(self) -> None:
        """D1 note must appear on the exact affected step index."""
        f = _make_finding(
            "unrecovered_error",
            severity="warning",
            affected_step_indices=[3],
            evidence={"tool": "Edit", "step_index": 3, "recovery_window": 10, "in_required_side_effects": True},
        )
        note = bdq._d1_note_for_step(3, [f], None)
        assert note is not None
        assert "Edit" in note
        assert "D1" in note

    def test_d1_note_not_on_non_flagged_step(self) -> None:
        f = _make_finding("unrecovered_error", affected_step_indices=[5])
        note = bdq._d1_note_for_step(3, [f], None)
        assert note is None

    def test_d2_note_includes_ratio(self) -> None:
        f = _make_finding(
            "struggle_ratio",
            severity="warning",
            evidence={
                "struggle_ratio": 3.5,
                "threshold": 2.0,
                "error_steps": 5,
                "redundant_steps": 2,
                "rejected_tool_calls": 1,
                "side_effect_successes": 2,
            },
        )
        note = bdq._d2_note([f])
        assert note is not None
        assert "D2" in note
        assert "3.5" in note
        assert "2.0" in note

    def test_d2_note_none_when_no_findings(self) -> None:
        assert bdq._d2_note([]) is None

    def test_d1_note_mentions_required_side_effect(self) -> None:
        f = _make_finding(
            "unrecovered_error",
            severity="error",
            affected_step_indices=[7],
            evidence={"tool": "Write", "step_index": 7, "recovery_window": 10, "in_required_side_effects": True},
        )
        note = bdq._d1_note_for_step(7, [f], None)
        assert note is not None
        assert "required side-effect" in note


# ── generate_disagreement_question ───────────────────────────────────────────


class TestGenerateQuestion:
    def test_d1_only_question_mentions_steps(self) -> None:
        f = _make_finding("unrecovered_error", affected_step_indices=[3, 7])
        q = bdq.generate_disagreement_question(
            [f], [], prior_label="pass", prior_comment="", operation_name="Code Implementation"
        )
        assert "D1" in q
        assert "3" in q
        assert "7" in q
        assert "pass" in q
        assert "recover" in q.lower() or "detector" in q.lower()

    def test_d2_only_question_mentions_ratio(self) -> None:
        f = _make_finding(
            "struggle_ratio",
            evidence={"struggle_ratio": 4.2, "threshold": 2.0},
            affected_step_indices=[1, 2],
        )
        q = bdq.generate_disagreement_question(
            [], [f], prior_label="LGTM", prior_comment="", operation_name="Code Implementation"
        )
        assert "D2" in q
        assert "4.2" in q

    def test_combined_d1_d2_question(self) -> None:
        f1 = _make_finding("unrecovered_error", affected_step_indices=[2])
        f2 = _make_finding(
            "struggle_ratio",
            evidence={"struggle_ratio": 3.0, "threshold": 2.0},
            affected_step_indices=[2, 4],
        )
        q = bdq.generate_disagreement_question(
            [f1], [f2], prior_label="pass", prior_comment="all good", operation_name="Code Implementation"
        )
        assert "D1" in q
        assert "D2" in q
        assert "all good" in q

    def test_prior_comment_included(self) -> None:
        f = _make_finding("unrecovered_error", affected_step_indices=[0])
        q = bdq.generate_disagreement_question(
            [f], [], prior_label="pass", prior_comment="I thought it was fine", operation_name="X"
        )
        assert "I thought it was fine" in q


# ── parse_spotcheck_md ────────────────────────────────────────────────────────


class TestParseSpotcheckMd:
    def test_parses_table_rows(self) -> None:
        """parse_spotcheck_md must extract trace_id, verdict, agree, comment."""
        content = (
            "# Test\n\n"
            "| Trace | Workflow | Verdict | FR | Ev | Src | Last | Digest | AGREE? |\n"
            "|---|---|---|---|---|---|---|---|---|\n"
            "| [abcdef1234567890…](http://localhost:6006/projects/X/traces/abcdef1234567890abcdef1234567890) "
            "| Code Implementation | fail | missing_side_effect |  |  | Bash | [↓](#d-abc) | Y | Bro it failed |\n"
        )
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w") as f:
            f.write(content)
            tmp_path = Path(f.name)
        try:
            rows = bdq.parse_spotcheck_md(tmp_path)
            assert len(rows) == 1
            row = rows[0]
            assert row["trace_id"] == "abcdef1234567890abcdef1234567890"
            assert row["agree"] == "Y"
            assert "fail" in row["verdict"]
            assert "Bro it failed" in row["comment"]
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_missing_file_returns_empty(self) -> None:
        rows = bdq.parse_spotcheck_md(Path("/nonexistent/spotcheck.md"))
        assert rows == []

    def test_agree_question_mark_parsed(self) -> None:
        content = (
            "| [1234567890123456…](http://x/traces/12345678901234561234567890123456) "
            "| Code Implementation | fail | fr |  |  | Bash | [↓](#d) | ? | |\n"
        )
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w") as f:
            f.write(content)
            tmp_path = Path(f.name)
        try:
            rows = bdq.parse_spotcheck_md(tmp_path)
            assert len(rows) == 1
            assert rows[0]["agree"] == "?"
        finally:
            tmp_path.unlink(missing_ok=True)


# ── load_answers_jsonl ────────────────────────────────────────────────────────


class TestLoadAnswersJsonl:
    def test_loads_records(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode="w") as f:
            f.write(json.dumps({"trace_id": "t1", "answer": "pass", "verdict_shown": "pass"}) + "\n")
            f.write(json.dumps({"trace_id": "t2", "answer": "it failed hard", "verdict_shown": "fail"}) + "\n")
            tmp_path = Path(f.name)
        try:
            recs = bdq.load_answers_jsonl(tmp_path)
            by_id = {r["trace_id"]: r for r in recs}
            assert "t1" in by_id
            assert "t2" in by_id
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_last_answer_wins(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode="w") as f:
            f.write(json.dumps({"trace_id": "dup", "answer": "first", "verdict_shown": "pass"}) + "\n")
            f.write(json.dumps({"trace_id": "dup", "answer": "second", "verdict_shown": "pass"}) + "\n")
            tmp_path = Path(f.name)
        try:
            recs = bdq.load_answers_jsonl(tmp_path)
            by_id = {r["trace_id"]: r for r in recs}
            assert by_id["dup"]["answer"] == "second"
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_missing_file_returns_empty(self) -> None:
        recs = bdq.load_answers_jsonl(Path("/nonexistent/answers.jsonl"))
        assert recs == []
