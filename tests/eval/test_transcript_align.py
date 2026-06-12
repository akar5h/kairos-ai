"""Tests for eval/review/transcript_align.py.

Covers:
  - ordinal per-tool-name matching (k-th Bash step ↔ k-th Bash tool_use)
  - window filtering (±60s pad, drops out-of-window and timestamp-less calls)
  - no-match fallback (never guesses across tool names)
  - redaction applied to every digest
  - transcript parsing (tool_use/tool_result joined on tool_use_id)
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Bootstrap: add eval/review to sys.path so the modules can be imported.
_EVAL_REVIEW = Path(__file__).parents[2] / "eval" / "review"
if str(_EVAL_REVIEW) not in sys.path:
    sys.path.insert(0, str(_EVAL_REVIEW))

import transcript_align as ta  # type: ignore[import-untyped]  # noqa: E402

from kairos.models.enums import StepStatus, StepStatusSource, StepType  # noqa: E402
from kairos.models.trace import Step  # noqa: E402

# ── helpers ───────────────────────────────────────────────────────────────────

_T0 = datetime(2026, 6, 10, 7, 0, 0, tzinfo=UTC)


def _step(index: int, tool: str | None, step_type: StepType = StepType.TOOL_CALL) -> Step:
    return Step(
        step_index=index,
        step_type=step_type,
        tool_name=tool,
        status=StepStatus.OK,
        status_source=StepStatusSource.NONE,
    )


def _call(name: str, command: str = "", offset_s: int = 0, output: str | None = None) -> ta.TranscriptCall:
    return ta.TranscriptCall(
        name=name,
        tool_input={"command": command} if command else {},
        ts=_T0 + timedelta(seconds=offset_s),
        output=output,
    )


# ── ordinal matching ──────────────────────────────────────────────────────────


class TestOrdinalMatching:
    def test_kth_step_matches_kth_call(self) -> None:
        steps = [_step(0, "Bash"), _step(1, "Bash"), _step(2, "Bash")]
        calls = [_call("Bash", "echo one"), _call("Bash", "echo two"), _call("Bash", "echo three")]
        aligned = ta.align_steps(steps, calls)
        assert aligned[0] is calls[0]
        assert aligned[1] is calls[1]
        assert aligned[2] is calls[2]

    def test_interleaved_tools_match_per_name(self) -> None:
        steps = [_step(0, "Bash"), _step(1, "Read"), _step(2, "Bash"), _step(3, "Read")]
        calls = [
            _call("Bash", "first bash"),
            _call("Read"),
            _call("Bash", "second bash"),
            _call("Read"),
        ]
        aligned = ta.align_steps(steps, calls)
        assert aligned[0] is calls[0]
        assert aligned[1] is calls[1]
        assert aligned[2] is calls[2]
        assert aligned[3] is calls[3]

    def test_llm_steps_excluded(self) -> None:
        steps = [_step(0, None, StepType.LLM), _step(1, "Bash"), _step(2, None, StepType.LLM)]
        calls = [_call("Bash", "only bash")]
        aligned = ta.align_steps(steps, calls)
        assert 0 not in aligned
        assert 2 not in aligned
        assert aligned[1] is calls[0]

    def test_more_steps_than_calls_yields_none(self) -> None:
        steps = [_step(0, "Bash"), _step(1, "Bash")]
        calls = [_call("Bash", "only one")]
        aligned = ta.align_steps(steps, calls)
        assert aligned[0] is calls[0]
        assert aligned[1] is None


# ── no-match fallback ─────────────────────────────────────────────────────────


class TestNoMatchFallback:
    def test_never_matches_across_names(self) -> None:
        """A Bash step must never pick up a Read call, even if it's the only one."""
        steps = [_step(0, "Bash")]
        calls = [_call("Read")]
        aligned = ta.align_steps(steps, calls)
        assert aligned[0] is None

    def test_empty_calls_all_none(self) -> None:
        steps = [_step(0, "Bash"), _step(1, "Edit")]
        aligned = ta.align_steps(steps, [])
        assert aligned == {0: None, 1: None}

    def test_align_trace_no_session_id_returns_empty(self) -> None:
        steps = [_step(0, "Bash")]
        assert ta.align_trace_to_transcript(steps, None, _T0, _T0) == {}

    def test_align_trace_missing_transcript_returns_empty(self) -> None:
        steps = [_step(0, "Bash")]
        assert ta.align_trace_to_transcript(steps, "no-such-session-id-zzz", _T0, _T0) == {}


# ── window filtering ──────────────────────────────────────────────────────────


class TestWindowFiltering:
    def test_calls_inside_window_kept(self) -> None:
        calls = [_call("Bash", offset_s=0), _call("Bash", offset_s=30)]
        out = ta.window_calls(calls, _T0, _T0 + timedelta(seconds=30))
        assert len(out) == 2

    def test_calls_outside_window_dropped(self) -> None:
        calls = [
            _call("Bash", "before", offset_s=-300),
            _call("Bash", "during", offset_s=10),
            _call("Bash", "after", offset_s=600),
        ]
        out = ta.window_calls(calls, _T0, _T0 + timedelta(seconds=60))
        assert len(out) == 1
        assert out[0].tool_input["command"] == "during"

    def test_pad_includes_edges(self) -> None:
        """±60s pad: a call 50s before trace start is still in."""
        calls = [_call("Bash", "early", offset_s=-50)]
        out = ta.window_calls(calls, _T0, _T0 + timedelta(seconds=10))
        assert len(out) == 1

    def test_timestampless_calls_dropped(self) -> None:
        call = ta.TranscriptCall(name="Bash", tool_input={}, ts=None)
        out = ta.window_calls([call], _T0, _T0 + timedelta(seconds=60))
        assert out == []

    def test_unknown_window_passes_all(self) -> None:
        calls = [_call("Bash", offset_s=99999)]
        assert ta.window_calls(calls, None, None) == calls


# ── digests + redaction ───────────────────────────────────────────────────────


class TestDigestRedaction:
    def test_args_digest_redacts_token_assignment(self) -> None:
        call = _call("Bash", 'curl -H "Authorization: Bearer abc123def456ghi789jkl012" $URL')
        digest = ta.call_args_digest(call)
        assert "abc123def456ghi789jkl012" not in digest
        assert "[REDACTED]" in digest

    def test_args_digest_redacts_jwt(self) -> None:
        jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4"
        call = _call("Bash", f"export X={jwt}")
        digest = ta.call_args_digest(call)
        assert "eyJhbGciOiJ" not in digest

    def test_args_digest_redacts_ghp_token(self) -> None:
        call = _call("Bash", "git push https://ghp_abcdefghij1234567890ABCDEFGHIJ@github.com/x/y")
        digest = ta.call_args_digest(call)
        assert "ghp_abcdefghij" not in digest

    def test_args_digest_redacts_aws_key(self) -> None:
        call = _call("Bash", "aws configure set aws_access_key_id AKIAIOSFODNN7EXAMPLE")
        digest = ta.call_args_digest(call)
        assert "AKIAIOSFODNN7EXAMPLE" not in digest

    def test_output_digest_redacts(self) -> None:
        call = _call("Bash", "env", output="PAPERCLIP_API_KEY=supersecretvalue123\nother line")
        digest = ta.call_output_digest(call)
        assert "supersecretvalue123" not in digest

    def test_output_digest_first_meaningful_line(self) -> None:
        call = _call("Bash", "ls", output="\n\n  \nfirst real line\nsecond line")
        assert ta.call_output_digest(call) == "first real line"

    def test_args_digest_length_capped(self) -> None:
        call = _call("Bash", "x" * 500)
        digest = ta.call_args_digest(call)
        assert len(digest) <= ta.ARGS_DIGEST_CHARS + 1  # +1 for ellipsis char

    def test_output_digest_length_capped(self) -> None:
        call = _call("Bash", "ls", output="y" * 1000)
        digest = ta.call_output_digest(call)
        assert len(digest) <= ta.OUTPUT_DIGEST_CHARS + 1

    def test_preferred_field_per_tool(self) -> None:
        call = ta.TranscriptCall(
            name="Read",
            tool_input={"file_path": "/repo/main.py", "limit": 100},
            ts=_T0,
        )
        assert ta.call_args_digest(call) == "/repo/main.py"

    def test_grep_uses_pattern(self) -> None:
        call = ta.TranscriptCall(name="Grep", tool_input={"pattern": "def main", "path": "/x"}, ts=_T0)
        assert ta.call_args_digest(call) == "def main"

    def test_clean_command_untouched(self) -> None:
        call = _call("Bash", "git status && git log --oneline -5")
        assert ta.call_args_digest(call) == "git status && git log --oneline -5"


# ── transcript parsing ────────────────────────────────────────────────────────


class TestParseTranscript:
    def _write_jsonl(self, lines: list[dict]) -> Path:
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode="w") as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")
        return Path(f.name)

    def test_tool_use_and_result_joined_by_id(self) -> None:
        path = self._write_jsonl(
            [
                {
                    "type": "assistant",
                    "timestamp": "2026-06-10T07:00:00.000Z",
                    "message": {
                        "content": [
                            {"type": "tool_use", "id": "tu_1", "name": "Bash", "input": {"command": "ls"}}
                        ]
                    },
                },
                {
                    "type": "user",
                    "timestamp": "2026-06-10T07:00:01.000Z",
                    "message": {
                        "content": [
                            {"type": "tool_result", "tool_use_id": "tu_1", "content": "file_a\nfile_b"}
                        ]
                    },
                },
            ]
        )
        try:
            calls = ta.parse_transcript(path)
            assert len(calls) == 1
            assert calls[0].name == "Bash"
            assert calls[0].tool_input == {"command": "ls"}
            assert calls[0].output == "file_a\nfile_b"
            assert calls[0].is_error is False
            assert calls[0].ts is not None
        finally:
            path.unlink(missing_ok=True)

    def test_error_result_flagged(self) -> None:
        path = self._write_jsonl(
            [
                {
                    "type": "assistant",
                    "timestamp": "2026-06-10T07:00:00.000Z",
                    "message": {
                        "content": [{"type": "tool_use", "id": "tu_1", "name": "Bash", "input": {"command": "x"}}]
                    },
                },
                {
                    "type": "user",
                    "timestamp": "2026-06-10T07:00:01.000Z",
                    "message": {
                        "content": [
                            {"type": "tool_result", "tool_use_id": "tu_1", "is_error": True, "content": "boom"}
                        ]
                    },
                },
            ]
        )
        try:
            calls = ta.parse_transcript(path)
            assert calls[0].is_error is True
            assert calls[0].output == "boom"
        finally:
            path.unlink(missing_ok=True)

    def test_list_content_flattened(self) -> None:
        path = self._write_jsonl(
            [
                {
                    "type": "assistant",
                    "timestamp": "2026-06-10T07:00:00.000Z",
                    "message": {
                        "content": [{"type": "tool_use", "id": "tu_1", "name": "Read", "input": {"file_path": "/f"}}]
                    },
                },
                {
                    "type": "user",
                    "timestamp": "2026-06-10T07:00:01.000Z",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "tu_1",
                                "content": [{"type": "text", "text": "line one"}, {"type": "text", "text": "line two"}],
                            }
                        ]
                    },
                },
            ]
        )
        try:
            calls = ta.parse_transcript(path)
            assert calls[0].output == "line one\nline two"
        finally:
            path.unlink(missing_ok=True)

    def test_corrupt_lines_skipped(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode="w") as f:
            f.write("NOT JSON\n")
            f.write(
                json.dumps(
                    {
                        "type": "assistant",
                        "timestamp": "2026-06-10T07:00:00.000Z",
                        "message": {"content": [{"type": "tool_use", "id": "t", "name": "Bash", "input": {}}]},
                    }
                )
                + "\n"
            )
        path = Path(f.name)
        try:
            calls = ta.parse_transcript(path)
            assert len(calls) == 1
        finally:
            path.unlink(missing_ok=True)


# ── end-to-end through build_step_list ────────────────────────────────────────


class TestBuildStepListIntegration:
    def test_no_match_marker_on_unmatched_tool_step(self) -> None:
        import build_queue as bq  # noqa: PLC0415

        steps = [_step(0, "Bash")]
        entries, _ = bq.build_step_list(steps, None, transcript_map={0: None})
        assert entries[0]["args_digest"] == ta.NO_MATCH

    def test_matched_step_gets_transcript_digests(self) -> None:
        import build_queue as bq  # noqa: PLC0415

        call = _call("Bash", "git status", output="On branch main")
        steps = [_step(0, "Bash")]
        entries, _ = bq.build_step_list(steps, None, transcript_map={0: call})
        assert entries[0]["args_digest"] == "git status"
        assert entries[0]["output_digest"] == "On branch main"

    def test_collapsed_run_digests_from_transcript(self) -> None:
        import build_queue as bq  # noqa: PLC0415

        steps = [_step(i, "Bash") for i in range(4)]
        tmap = {i: _call("Bash", f"cmd number {i}") for i in range(4)}
        _, collapsed = bq.build_step_list(steps, None, transcript_map=tmap)
        assert len(collapsed) == 1
        assert collapsed[0]["first_args_digest"] == "cmd number 0"
        assert collapsed[0]["last_args_digest"] == "cmd number 3"

    def test_digest_redacted_through_build_step_list(self) -> None:
        import build_queue as bq  # noqa: PLC0415

        call = _call("Bash", 'curl -H "Authorization: Bearer secrettokenvalue12345"')
        steps = [_step(0, "Bash")]
        entries, _ = bq.build_step_list(steps, None, transcript_map={0: call})
        assert "secrettokenvalue12345" not in entries[0]["args_digest"]
