"""Tests for src/kairos/readers/transcript_join.py.

Covers:
  - tool_errors_from_transcript: is_error=true → step ERROR, clean → OK
  - ordinal alignment correctness (k-th same-tool step ↔ k-th same-tool call)
  - non-claude_code trace untouched (phoenix.spans_to_envelope + is_claude_code guard)
  - missing-transcript graceful no-op (returns {})
  - absent session.id graceful no-op (returns {})
  - error_count reflects corrections via _correct_tool_errors_from_transcript
"""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest  # noqa: TC002

from kairos.models.enums import StepStatus, StepStatusSource, StepType
from kairos.models.trace import Step
from kairos.readers.transcript_join import (
    _align_is_errors,
    _parse_transcript,
    _TranscriptCall,
    _window_calls,
    tool_errors_from_transcript,
)

# ── helpers ────────────────────────────────────────────────────────────────────

_T0 = datetime(2026, 6, 10, 8, 0, 0, tzinfo=UTC)


def _step(
    index: int,
    tool: str | None,
    step_type: StepType = StepType.TOOL_CALL,
    status: StepStatus = StepStatus.OK,
    status_source: StepStatusSource = StepStatusSource.ATTR_SUCCESS,
) -> Step:
    return Step(
        step_index=index,
        step_type=step_type,
        tool_name=tool,
        status=status,
        status_source=status_source,
    )


def _call(
    name: str,
    is_error: bool = False,
    offset_s: int = 0,
) -> _TranscriptCall:
    return _TranscriptCall(
        name=name,
        is_error=is_error,
        ts=_T0 + timedelta(seconds=offset_s),
    )


def _write_jsonl(lines: list[dict[str, Any]]) -> Path:
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode="w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")
    return Path(f.name)


def _cc_transcript_lines(
    *,
    tool_name: str = "Edit",
    is_error: bool = False,
    ts: str = "2026-06-10T08:00:01.000Z",
) -> list[dict[str, Any]]:
    """Minimal valid session JSONL with one tool_use / tool_result pair."""
    return [
        {
            "type": "assistant",
            "timestamp": ts,
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu_test_01",
                        "name": tool_name,
                        "input": {"file_path": "/tmp/x.py"},
                    }
                ]
            },
        },
        {
            "type": "user",
            "timestamp": "2026-06-10T08:00:02.000Z",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_test_01",
                        "is_error": is_error,
                        "content": "<tool_use_error>File has been modified since read</tool_use_error>"
                        if is_error
                        else "OK",
                    }
                ]
            },
        },
    ]


# ── _parse_transcript ─────────────────────────────────────────────────────────


class TestParseTranscript:
    def test_clean_call_is_error_false(self) -> None:
        path = _write_jsonl(_cc_transcript_lines(tool_name="Edit", is_error=False))
        try:
            calls = _parse_transcript(path)
            assert len(calls) == 1
            assert calls[0].name == "Edit"
            assert calls[0].is_error is False
        finally:
            path.unlink(missing_ok=True)

    def test_error_call_is_error_true(self) -> None:
        path = _write_jsonl(_cc_transcript_lines(tool_name="Edit", is_error=True))
        try:
            calls = _parse_transcript(path)
            assert len(calls) == 1
            assert calls[0].is_error is True
        finally:
            path.unlink(missing_ok=True)

    def test_multiple_tools_in_order(self) -> None:
        lines: list[dict[str, Any]] = [
            {
                "type": "assistant",
                "timestamp": "2026-06-10T08:00:00.000Z",
                "message": {
                    "content": [
                        {"type": "tool_use", "id": "tu_1", "name": "Read", "input": {"file_path": "/f"}},
                        {"type": "tool_use", "id": "tu_2", "name": "Edit", "input": {"file_path": "/f"}},
                    ]
                },
            },
            {
                "type": "user",
                "timestamp": "2026-06-10T08:00:01.000Z",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": "tu_1", "is_error": False, "content": "ok"},
                        {"type": "tool_result", "tool_use_id": "tu_2", "is_error": True, "content": "err"},
                    ]
                },
            },
        ]
        path = _write_jsonl(lines)
        try:
            calls = _parse_transcript(path)
            assert len(calls) == 2
            assert calls[0].name == "Read"
            assert calls[0].is_error is False
            assert calls[1].name == "Edit"
            assert calls[1].is_error is True
        finally:
            path.unlink(missing_ok=True)

    def test_corrupt_line_skipped(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode="w", encoding="utf-8") as f:
            f.write("NOT JSON\n")
            f.write(
                json.dumps(
                    {
                        "type": "assistant",
                        "timestamp": "2026-06-10T08:00:00.000Z",
                        "message": {"content": [{"type": "tool_use", "id": "t", "name": "Bash", "input": {}}]},
                    }
                )
                + "\n"
            )
        path = Path(f.name)
        try:
            calls = _parse_transcript(path)
            assert len(calls) == 1
        finally:
            path.unlink(missing_ok=True)

    def test_missing_file_returns_empty(self) -> None:
        calls = _parse_transcript(Path("/tmp/nonexistent_session_xyz.jsonl"))
        assert calls == []


# ── _window_calls ─────────────────────────────────────────────────────────────


class TestWindowCalls:
    def test_calls_inside_window_kept(self) -> None:
        calls = [_call("Bash", offset_s=0), _call("Bash", offset_s=30)]
        out = _window_calls(calls, _T0, _T0 + timedelta(seconds=30))
        assert len(out) == 2

    def test_calls_outside_window_dropped(self) -> None:
        calls = [
            _call("Bash", offset_s=-300),
            _call("Bash", offset_s=10),
            _call("Bash", offset_s=600),
        ]
        out = _window_calls(calls, _T0, _T0 + timedelta(seconds=60))
        assert len(out) == 1
        assert out[0].ts == _T0 + timedelta(seconds=10)

    def test_pad_includes_edges(self) -> None:
        """±60s pad: a call 50s before trace start is still in."""
        calls = [_call("Bash", offset_s=-50)]
        out = _window_calls(calls, _T0, _T0 + timedelta(seconds=10))
        assert len(out) == 1

    def test_timestampless_calls_dropped(self) -> None:
        call = _TranscriptCall(name="Bash", is_error=False, ts=None)
        out = _window_calls([call], _T0, _T0 + timedelta(seconds=60))
        assert out == []

    def test_unknown_window_passes_all(self) -> None:
        calls = [_call("Bash", offset_s=99999)]
        assert _window_calls(calls, None, None) == calls


# ── _align_is_errors ──────────────────────────────────────────────────────────


class TestAlignIsErrors:
    def test_kth_error_step_matches_kth_call(self) -> None:
        steps = [_step(0, "Edit"), _step(1, "Edit")]
        calls = [_call("Edit", is_error=True), _call("Edit", is_error=False)]
        errors = _align_is_errors(steps, calls)
        assert errors == {0: True}  # only first Edit is error; second is clean

    def test_clean_step_not_in_errors(self) -> None:
        steps = [_step(0, "Bash")]
        calls = [_call("Bash", is_error=False)]
        errors = _align_is_errors(steps, calls)
        assert errors == {}

    def test_never_matches_across_names(self) -> None:
        steps = [_step(0, "Bash")]
        calls = [_call("Edit", is_error=True)]  # Edit error but step is Bash
        errors = _align_is_errors(steps, calls)
        assert errors == {}

    def test_llm_steps_excluded(self) -> None:
        steps = [
            _step(0, None, step_type=StepType.LLM),
            _step(1, "Edit"),
            _step(2, None, step_type=StepType.LLM),
        ]
        calls = [_call("Edit", is_error=True)]
        errors = _align_is_errors(steps, calls)
        assert 0 not in errors
        assert 2 not in errors
        assert errors == {1: True}

    def test_more_steps_than_calls_no_correction(self) -> None:
        steps = [_step(0, "Write"), _step(1, "Write")]
        calls = [_call("Write", is_error=True)]  # only one call
        errors = _align_is_errors(steps, calls)
        # First step has error; second step has no matching call → no error entry
        assert errors == {0: True}
        assert 1 not in errors

    def test_interleaved_tools_ordinal_correct(self) -> None:
        steps = [_step(0, "Bash"), _step(1, "Edit"), _step(2, "Bash"), _step(3, "Edit")]
        calls = [
            _call("Bash", is_error=False),
            _call("Edit", is_error=True),
            _call("Bash", is_error=True),
            _call("Edit", is_error=False),
        ]
        errors = _align_is_errors(steps, calls)
        # step 0 (1st Bash) → clean; step 1 (1st Edit) → error;
        # step 2 (2nd Bash) → error; step 3 (2nd Edit) → clean
        assert errors == {1: True, 2: True}


# ── tool_errors_from_transcript (full pipeline) ───────────────────────────────


class TestToolErrorsFromTranscript:
    def test_no_session_id_returns_empty(self) -> None:
        """When no span carries session.id, gracefully return {}."""

        class _FakeSpan:
            attributes: dict[str, Any] = {}
            start_time = 1_000_000_000_000
            end_time = 2_000_000_000_000

        steps = [_step(0, "Edit")]
        errors = tool_errors_from_transcript([_FakeSpan()], steps)
        assert errors == {}

    def test_missing_transcript_returns_empty(self) -> None:
        """session.id present but no file found → {}."""

        class _FakeSpan:
            attributes: dict[str, Any] = {"session.id": "nonexistent-session-xyz-99999"}
            start_time = 1_000_000_000_000
            end_time = 2_000_000_000_000

        steps = [_step(0, "Edit")]
        errors = tool_errors_from_transcript([_FakeSpan()], steps)
        assert errors == {}

    def test_error_step_found_in_real_transcript(self, tmp_path: Path) -> None:
        """Full pipeline: transcript with is_error=true → {step_index: True}."""
        session_id = "test-session-full-pipeline-01"
        project_dir = tmp_path / "projects" / "test-project"
        project_dir.mkdir(parents=True)
        transcript_path = project_dir / f"{session_id}.jsonl"

        ts_base = "2026-06-10T08:00:01.000Z"
        lines = _cc_transcript_lines(tool_name="Edit", is_error=True, ts=ts_base)
        transcript_path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")

        span_start_ns = int(datetime(2026, 6, 10, 8, 0, 0, tzinfo=UTC).timestamp() * 1e9)
        span_end_ns = int(datetime(2026, 6, 10, 8, 0, 10, tzinfo=UTC).timestamp() * 1e9)

        class _FakeSpan:
            attributes: dict[str, Any] = {"session.id": session_id}
            start_time = span_start_ns
            end_time = span_end_ns

        steps = [_step(0, "Edit")]

        import kairos.readers.transcript_join as tj

        original_glob = tj.TRANSCRIPT_GLOB
        tj.TRANSCRIPT_GLOB = str(project_dir / "{session_id}.jsonl")
        try:
            errors = tool_errors_from_transcript([_FakeSpan()], steps)
        finally:
            tj.TRANSCRIPT_GLOB = original_glob

        assert errors == {0: True}

    def test_clean_step_not_corrected(self, tmp_path: Path) -> None:
        """Transcript clean (is_error=false) → errors dict is empty."""
        session_id = "test-session-clean-01"
        project_dir = tmp_path / "projects" / "proj"
        project_dir.mkdir(parents=True)
        transcript_path = project_dir / f"{session_id}.jsonl"

        ts_base = "2026-06-10T08:00:01.000Z"
        lines = _cc_transcript_lines(tool_name="Bash", is_error=False, ts=ts_base)
        transcript_path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")

        span_start_ns = int(datetime(2026, 6, 10, 8, 0, 0, tzinfo=UTC).timestamp() * 1e9)
        span_end_ns = int(datetime(2026, 6, 10, 8, 0, 10, tzinfo=UTC).timestamp() * 1e9)

        class _FakeSpan:
            attributes: dict[str, Any] = {"session.id": session_id}
            start_time = span_start_ns
            end_time = span_end_ns

        steps = [_step(0, "Bash")]

        import kairos.readers.transcript_join as tj

        original_glob = tj.TRANSCRIPT_GLOB
        tj.TRANSCRIPT_GLOB = str(project_dir / "{session_id}.jsonl")
        try:
            errors = tool_errors_from_transcript([_FakeSpan()], steps)
        finally:
            tj.TRANSCRIPT_GLOB = original_glob

        assert errors == {}


# ── phoenix.spans_to_envelope integration ────────────────────────────────────
#
# These tests use the _correct_tool_errors_from_transcript path end-to-end
# through spans_to_envelope by monkeypatching tool_errors_from_transcript.


def _cc_tool_span(tool_name: str, success: bool) -> dict[str, Any]:
    """Build a minimal claude_code-shaped span set for one tool step."""
    return {
        "name": "claude_code.tool",
        "context": {"trace_id": "abcdef1234567890abcdef1234567890", "span_id": "bbbbbbbbbbbbbbbb"},
        "parent_id": "aaaaaaaaaaaaaaaa",
        "start_time": "2026-06-10T08:00:01.000000+00:00",
        "end_time": "2026-06-10T08:00:02.000000+00:00",
        "status_code": "UNSET",
        "status_message": "",
        "attributes": {
            "span.type": "tool",
            "tool_name": tool_name,
            "success": success,
            "session.id": "test-session-integration",
        },
        "events": [],
    }


def _cc_interaction_span() -> dict[str, Any]:
    return {
        "name": "claude_code.interaction",
        "context": {"trace_id": "abcdef1234567890abcdef1234567890", "span_id": "aaaaaaaaaaaaaaaa"},
        "parent_id": None,
        "start_time": "2026-06-10T08:00:00.000000+00:00",
        "end_time": "2026-06-10T08:00:10.000000+00:00",
        "status_code": "UNSET",
        "status_message": "",
        "attributes": {"span.type": "interaction", "user_prompt": "do it"},
        "events": [],
    }


class TestPhoenixSpansToEnvelopeTranscriptCorrection:
    def test_is_error_true_flips_ok_step_to_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Transcript marks Edit is_error=true → step status flips to ERROR."""
        from kairos.readers.phoenix import spans_to_envelope

        # Monkeypatch tool_errors_from_transcript to simulate transcript finding an error
        def _fake_errors(spans: list[Any], steps: list[Any]) -> dict[int, bool]:
            # Return the first TOOL_CALL step as an error
            for s in steps:
                if s.step_type == StepType.TOOL_CALL:
                    return {s.step_index: True}
            return {}

        monkeypatch.setattr(
            "kairos.readers.phoenix.tool_errors_from_transcript",
            _fake_errors,
        )

        spans = [
            _cc_interaction_span(),
            _cc_tool_span("Edit", success=True),  # emitter says success=True (the bug)
        ]
        env = spans_to_envelope(spans)
        tool_steps = [s for s in env.steps if s.step_type is StepType.TOOL_CALL]
        assert len(tool_steps) == 1
        assert tool_steps[0].status is StepStatus.ERROR
        assert env.error_count == 1

    def test_clean_transcript_leaves_ok_step(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Transcript marks step clean → status stays OK."""
        monkeypatch.setattr(
            "kairos.readers.phoenix.tool_errors_from_transcript",
            lambda spans, steps: {},  # no errors
        )

        from kairos.readers.phoenix import spans_to_envelope

        spans = [
            _cc_interaction_span(),
            _cc_tool_span("Bash", success=True),
        ]
        env = spans_to_envelope(spans)
        tool_steps = [s for s in env.steps if s.step_type is StepType.TOOL_CALL]
        assert tool_steps[0].status is StepStatus.OK
        assert env.error_count == 0

    def test_non_claude_code_trace_not_corrected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Non-claude_code trace: _correct_tool_errors_from_transcript never called."""
        call_count = {"n": 0}

        def _counting_fake(spans: list[Any], steps: list[Any]) -> dict[int, bool]:
            call_count["n"] += 1
            return {}

        monkeypatch.setattr(
            "kairos.readers.phoenix.tool_errors_from_transcript",
            _counting_fake,
        )

        from kairos.readers.phoenix import spans_to_envelope

        # Generic OTel trace — no claude_code.* spans
        spans = [
            {
                "name": "kairos.task",
                "context": {"trace_id": "1111111111111111111111111111dddd", "span_id": "1111111111111111"},
                "parent_id": None,
                "start_time": "2026-06-10T08:00:00.000000+00:00",
                "end_time": "2026-06-10T08:00:10.000000+00:00",
                "status_code": "UNSET",
                "status_message": "",
                "attributes": {"kairos.agent.name": "generic"},
                "events": [],
            },
            {
                "name": "tool.submit",
                "context": {"trace_id": "1111111111111111111111111111dddd", "span_id": "2222222222222222"},
                "parent_id": "1111111111111111",
                "start_time": "2026-06-10T08:00:01.000000+00:00",
                "end_time": "2026-06-10T08:00:02.000000+00:00",
                "status_code": "UNSET",
                "status_message": "",
                "attributes": {"gen_ai.tool.name": "submit"},
                "events": [],
            },
        ]
        env = spans_to_envelope(spans)
        # tool_errors_from_transcript must not have been called (not a CC trace)
        assert call_count["n"] == 0
        tool_steps = [s for s in env.steps if s.step_type is StepType.TOOL_CALL]
        assert tool_steps[0].status is StepStatus.OK

    def test_error_count_reflects_correction(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """error_count is updated when corrections flip steps."""
        monkeypatch.setattr(
            "kairos.readers.phoenix.tool_errors_from_transcript",
            lambda spans, steps: {s.step_index: True for s in steps if s.step_type is StepType.TOOL_CALL},
        )

        from kairos.readers.phoenix import spans_to_envelope

        spans = [
            _cc_interaction_span(),
            _cc_tool_span("Write", success=True),
        ]
        env = spans_to_envelope(spans)
        assert env.error_count == 1

    def test_already_error_step_not_double_flipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A step already ERROR (from execution-child success=False) stays ERROR."""
        monkeypatch.setattr(
            "kairos.readers.phoenix.tool_errors_from_transcript",
            lambda spans, steps: {s.step_index: True for s in steps if s.step_type is StepType.TOOL_CALL},
        )

        from kairos.readers.phoenix import spans_to_envelope

        spans = [
            _cc_interaction_span(),
            _cc_tool_span("Write", success=False),  # already ERROR via execution child
        ]
        env = spans_to_envelope(spans)
        tool_steps = [s for s in env.steps if s.step_type is StepType.TOOL_CALL]
        assert tool_steps[0].status is StepStatus.ERROR
        assert env.error_count == 1


# ── Args enrichment (PART 1 & 2: reader populates tool_args from transcript) ──


class TestToolArgsFromTranscript:
    """Tests for the new tool_args_from_transcript public API and
    _enrich_tool_args_from_transcript in phoenix.py.
    """

    def test_no_session_id_returns_empty(self) -> None:
        """No session.id on spans → empty dict, no crash."""
        from kairos.readers.transcript_join import tool_args_from_transcript

        class _FakeSpan:
            attributes: dict[str, Any] = {}
            start_time = 1_000_000_000_000
            end_time = 2_000_000_000_000

        steps = [_step(0, "Edit")]
        result = tool_args_from_transcript([_FakeSpan()], steps)
        assert result == {}

    def test_missing_transcript_returns_empty(self) -> None:
        """session.id present but no file found → {}."""
        from kairos.readers.transcript_join import tool_args_from_transcript

        class _FakeSpan:
            attributes: dict[str, Any] = {"session.id": "nonexistent-session-args-99999"}
            start_time = 1_000_000_000_000
            end_time = 2_000_000_000_000

        steps = [_step(0, "Edit")]
        result = tool_args_from_transcript([_FakeSpan()], steps)
        assert result == {}

    def test_args_populated_from_transcript(self, tmp_path: Path) -> None:
        """Full pipeline: transcript carries input args → step gets tool_args."""
        from kairos.readers.transcript_join import tool_args_from_transcript

        session_id = "test-session-args-01"
        project_dir = tmp_path / "projects" / "test-project"
        project_dir.mkdir(parents=True)
        transcript_path = project_dir / f"{session_id}.jsonl"

        # Build a transcript with a tool_use that has input args
        lines: list[dict[str, Any]] = [
            {
                "type": "assistant",
                "timestamp": "2026-06-10T08:00:01.000Z",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tu_args_01",
                            "name": "Edit",
                            "input": {"file_path": "/tmp/foo.py", "old_string": "x", "new_string": "y"},
                        }
                    ]
                },
            },
            {
                "type": "user",
                "timestamp": "2026-06-10T08:00:02.000Z",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu_args_01",
                            "is_error": False,
                            "content": "OK",
                        }
                    ]
                },
            },
        ]
        transcript_path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")

        span_start_ns = int(datetime(2026, 6, 10, 8, 0, 0, tzinfo=UTC).timestamp() * 1e9)
        span_end_ns = int(datetime(2026, 6, 10, 8, 0, 10, tzinfo=UTC).timestamp() * 1e9)

        class _FakeSpan:
            attributes: dict[str, Any] = {"session.id": session_id}
            start_time = span_start_ns
            end_time = span_end_ns

        steps = [_step(0, "Edit")]

        import kairos.readers.transcript_join as tj

        original_glob = tj.TRANSCRIPT_GLOB
        tj.TRANSCRIPT_GLOB = str(project_dir / "{session_id}.jsonl")
        try:
            result = tool_args_from_transcript([_FakeSpan()], steps)
        finally:
            tj.TRANSCRIPT_GLOB = original_glob

        assert 0 in result
        assert result[0]["file_path"] == "/tmp/foo.py"
        assert result[0]["old_string"] == "x"

    def test_secret_redacted_in_args(self, tmp_path: Path) -> None:
        """Bearer token in Bash command args is redacted."""
        from kairos.readers.transcript_join import tool_args_from_transcript

        session_id = "test-session-args-redact-01"
        project_dir = tmp_path / "projects" / "p"
        project_dir.mkdir(parents=True)
        transcript_path = project_dir / f"{session_id}.jsonl"

        lines_: list[dict[str, Any]] = [
            {
                "type": "assistant",
                "timestamp": "2026-06-10T08:00:01.000Z",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tu_sec_01",
                            "name": "Bash",
                            "input": {"command": "curl -H 'Authorization: Bearer sk-secret123456789012' http://api"},
                        }
                    ]
                },
            },
            {
                "type": "user",
                "timestamp": "2026-06-10T08:00:02.000Z",
                "message": {
                    "content": [{"type": "tool_result", "tool_use_id": "tu_sec_01", "is_error": False, "content": "ok"}]
                },
            },
        ]
        transcript_path.write_text("\n".join(json.dumps(line) for line in lines_) + "\n", encoding="utf-8")

        span_start_ns = int(datetime(2026, 6, 10, 8, 0, 0, tzinfo=UTC).timestamp() * 1e9)
        span_end_ns = int(datetime(2026, 6, 10, 8, 0, 10, tzinfo=UTC).timestamp() * 1e9)

        class _FakeSpan:
            attributes: dict[str, Any] = {"session.id": session_id}
            start_time = span_start_ns
            end_time = span_end_ns

        steps = [_step(0, "Bash")]

        import kairos.readers.transcript_join as tj

        original_glob = tj.TRANSCRIPT_GLOB
        tj.TRANSCRIPT_GLOB = str(project_dir / "{session_id}.jsonl")
        try:
            result = tool_args_from_transcript([_FakeSpan()], steps)
        finally:
            tj.TRANSCRIPT_GLOB = original_glob

        assert 0 in result
        cmd = result[0].get("command", "")
        # Bearer token and sk- key must be redacted
        assert "Bearer" not in cmd or "[REDACTED]" in cmd
        assert "sk-secret" not in cmd

    def test_phoenix_enriches_tool_args_on_cc_trace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """spans_to_envelope populates step.tool_args via _enrich_tool_args_from_transcript."""
        from kairos.readers.phoenix import spans_to_envelope

        test_args = {"file_path": "/foo/bar.py", "old_string": "x"}

        def _fake_args(spans: list[Any], steps: list[Any]) -> dict[int, dict[str, Any]]:
            # Return args for every TOOL_CALL step
            return {s.step_index: test_args for s in steps if s.step_type is StepType.TOOL_CALL}

        monkeypatch.setattr("kairos.readers.phoenix.tool_args_from_transcript", _fake_args)
        monkeypatch.setattr(
            "kairos.readers.phoenix.tool_errors_from_transcript",
            lambda spans, steps: {},
        )

        spans = [
            _cc_interaction_span(),
            _cc_tool_span("Edit", success=True),
        ]
        env = spans_to_envelope(spans)
        tool_steps = [s for s in env.steps if s.step_type is StepType.TOOL_CALL]
        assert len(tool_steps) == 1
        assert tool_steps[0].tool_args == test_args
        assert tool_steps[0].tool_args_normalized is not None

    def test_phoenix_does_not_overwrite_existing_args(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If a step already has tool_args from the span, transcript args do NOT overwrite."""
        from kairos.readers.phoenix import spans_to_envelope

        monkeypatch.setattr(
            "kairos.readers.phoenix.tool_args_from_transcript",
            lambda spans, steps: {
                s.step_index: {"file_path": "/TRANSCRIPT"} for s in steps if s.step_type is StepType.TOOL_CALL
            },
        )
        monkeypatch.setattr(
            "kairos.readers.phoenix.tool_errors_from_transcript",
            lambda spans, steps: {},
        )

        # Build a span that already has args set via input.value
        span_with_args = dict(_cc_tool_span("Edit", success=True))
        span_with_args["attributes"] = dict(span_with_args.get("attributes", {}))
        span_with_args["attributes"]["input.value"] = '{"file_path": "/SPAN_ARG.py"}'

        spans = [_cc_interaction_span(), span_with_args]
        env = spans_to_envelope(spans)
        tool_steps = [s for s in env.steps if s.step_type is StepType.TOOL_CALL]
        assert len(tool_steps) == 1
        # Span-sourced args take precedence; transcript should not overwrite
        assert tool_steps[0].tool_args is not None
        assert tool_steps[0].tool_args.get("file_path") == "/SPAN_ARG.py"

    def test_non_cc_trace_not_enriched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Non-claude_code trace: _enrich_tool_args_from_transcript never called."""
        call_count = {"n": 0}

        def _counting_fake(spans: list[Any], steps: list[Any]) -> dict[int, dict[str, Any]]:
            call_count["n"] += 1
            return {}

        monkeypatch.setattr("kairos.readers.phoenix.tool_args_from_transcript", _counting_fake)
        monkeypatch.setattr(
            "kairos.readers.phoenix.tool_errors_from_transcript",
            lambda spans, steps: {},
        )

        from kairos.readers.phoenix import spans_to_envelope

        spans = [
            {
                "name": "kairos.task",
                "context": {"trace_id": "1111111111111111111111111111eeee", "span_id": "1111111111111111"},
                "parent_id": None,
                "start_time": "2026-06-10T08:00:00.000000+00:00",
                "end_time": "2026-06-10T08:00:10.000000+00:00",
                "status_code": "UNSET",
                "status_message": "",
                "attributes": {"kairos.agent.name": "generic"},
                "events": [],
            },
            {
                "name": "tool.submit",
                "context": {"trace_id": "1111111111111111111111111111eeee", "span_id": "2222222222222222"},
                "parent_id": "1111111111111111",
                "start_time": "2026-06-10T08:00:01.000000+00:00",
                "end_time": "2026-06-10T08:00:02.000000+00:00",
                "status_code": "UNSET",
                "status_message": "",
                "attributes": {"gen_ai.tool.name": "submit"},
                "events": [],
            },
        ]
        spans_to_envelope(spans)
        assert call_count["n"] == 0
