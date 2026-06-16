"""Tests for src/kairos/readers/hook_join.py (F1.2b).

Unit tests (no DB) cover:
  - Ordinal alignment: Nth step named X ↔ Nth hook row named X
  - is_error=True → step.status ERROR + status_source preserved/stamped
  - tool_input_redacted → tool_args + tool_args_normalized populated
  - tool_output → step.tool_output populated
  - error_count recomputed after corrections
  - Step 2 untouched when only step 1 has an error hook row
  - Multiple tool names interleaved → each aligns within its own name-ordinal
  - More steps than hook rows → extras untouched, no crash
  - More hook rows than steps → extras silently ignored, no crash
  - No session_id on envelope → returns unchanged, no crash
  - enrich_hooks=False in fetch_envelope_from_db → hook_join never called

Integration tests (require KAIROS_PG_DSN):
  - Persist hook_events rows, build envelope, enrich, assert patches applied
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest

from kairos.models.enums import StepStatus, StepStatusSource, StepType
from kairos.models.trace import Step, TraceEnvelope
from kairos.readers.hook_join import (
    HookEventRow,
    _align_hook_events,
    enrich_envelope_with_hooks,
)

_DSN = os.environ.get("KAIROS_PG_DSN", "").strip()
_DB_AVAILABLE = bool(_DSN)

_skip_no_db = pytest.mark.skipif(
    not _DB_AVAILABLE,
    reason="KAIROS_PG_DSN not set — kairos-pg container not reachable in this environment",
)

# ── Helpers ────────────────────────────────────────────────────────────────────

# Trace clusters: traceA around _T_A, traceB ~10 min later (well outside ±60 s pad).
_T_A = datetime(2026, 6, 10, 8, 0, 0, tzinfo=UTC)
_T_B = datetime(2026, 6, 10, 8, 10, 0, tzinfo=UTC)


def _step(
    index: int,
    tool: str,
    status: StepStatus = StepStatus.OK,
    status_source: StepStatusSource = StepStatusSource.ATTR_SUCCESS,
    tool_args: dict[str, Any] | None = None,
    tool_output: str | None = None,
    started_at: datetime | None = _T_A,
    ended_at: datetime | None = _T_A,
) -> Step:
    return Step(
        step_index=index,
        step_type=StepType.TOOL_CALL,
        tool_name=tool,
        status=status,
        status_source=status_source,
        tool_args=tool_args,
        tool_output=tool_output,
        started_at=started_at,
        ended_at=ended_at,
    )


def _llm_step(index: int) -> Step:
    return Step(
        step_index=index,
        step_type=StepType.LLM,
    )


def _hook_row(
    tool_name: str,
    is_error: bool = False,
    seq: int = 1,
    tool_input: dict[str, Any] | None = None,
    tool_output: str | None = None,
    occurred_at: datetime | None = _T_A,
) -> HookEventRow:
    return HookEventRow(
        session_id="test-session",
        seq=seq,
        tool_name=tool_name,
        is_error=is_error,
        tool_input_redacted=tool_input or {"command": f"cmd-{seq}"},
        tool_output=tool_output or f"output-{seq}",
        occurred_at=occurred_at,
    )


def _envelope(
    steps: list[Step],
    session_id: str | None = "test-session",
) -> TraceEnvelope:
    """Build a minimal TraceEnvelope with optional session_id in metadata.

    started_at/ended_at fall back from the steps' timestamps; we also stamp
    envelope-level bounds so the trace window is always derivable.
    """
    meta: dict[str, Any] | None = {"session_id": session_id} if session_id else None
    starts = [s.started_at for s in steps if s.started_at is not None]
    ends = [s.ended_at for s in steps if s.ended_at is not None]
    return TraceEnvelope(
        trace_id="aabbccdd" * 4,
        steps=steps,
        metadata=meta,
        error_count=sum(1 for s in steps if s.status is StepStatus.ERROR),
        started_at=min(starts) if starts else _T_A,
        ended_at=max(ends) if ends else _T_A,
    )


# ── Unit tests: _align_hook_events ────────────────────────────────────────────


class TestAlignHookEvents:
    def test_single_match(self) -> None:
        steps = [_step(0, "Bash")]
        rows = [_hook_row("Bash", seq=1)]
        aligned = _align_hook_events(steps, rows)
        assert aligned == {0: rows[0]}

    def test_ordinal_per_name_two_bash(self) -> None:
        steps = [_step(0, "Bash"), _step(1, "Bash")]
        rows = [_hook_row("Bash", seq=1), _hook_row("Bash", seq=2)]
        aligned = _align_hook_events(steps, rows)
        assert aligned[0] is rows[0]
        assert aligned[1] is rows[1]

    def test_interleaved_tools(self) -> None:
        steps = [_step(0, "Bash"), _step(1, "Edit"), _step(2, "Bash"), _step(3, "Edit")]
        rows = [
            _hook_row("Bash", seq=1),
            _hook_row("Edit", seq=2),
            _hook_row("Bash", seq=3),
            _hook_row("Edit", seq=4),
        ]
        aligned = _align_hook_events(steps, rows)
        # Bash steps get Bash rows (ordinal 0→row[0], ordinal 1→row[2])
        assert aligned[0].seq == 1  # Bash row seq=1
        assert aligned[2].seq == 3  # Bash row seq=3
        # Edit steps get Edit rows (ordinal 0→row[1], ordinal 1→row[3])
        assert aligned[1].seq == 2  # Edit row seq=2
        assert aligned[3].seq == 4  # Edit row seq=4

    def test_more_steps_than_rows(self) -> None:
        steps = [_step(0, "Bash"), _step(1, "Bash"), _step(2, "Bash")]
        rows = [_hook_row("Bash", seq=1)]  # only one row
        aligned = _align_hook_events(steps, rows)
        assert 0 in aligned
        assert 1 not in aligned
        assert 2 not in aligned

    def test_more_rows_than_steps(self) -> None:
        steps = [_step(0, "Bash")]
        rows = [_hook_row("Bash", seq=1), _hook_row("Bash", seq=2), _hook_row("Bash", seq=3)]
        aligned = _align_hook_events(steps, rows)
        assert len(aligned) == 1
        assert aligned[0].seq == 1  # only first row used

    def test_non_tool_steps_skipped(self) -> None:
        steps = [_llm_step(0), _step(1, "Bash")]
        rows = [_hook_row("Bash", seq=1)]
        aligned = _align_hook_events(steps, rows)
        assert 0 not in aligned
        assert 1 in aligned

    def test_empty_steps_and_rows(self) -> None:
        assert _align_hook_events([], []) == {}

    def test_no_matching_tool_name(self) -> None:
        steps = [_step(0, "Write")]
        rows = [_hook_row("Bash", seq=1)]
        aligned = _align_hook_events(steps, rows)
        assert aligned == {}


# ── Unit tests: enrich_envelope_with_hooks ───────────────────────────────────


class TestEnrichEnvelopeWithHooks:
    """Pure-unit tests: inject a fake hook-event list via monkeypatch."""

    def _enrich(
        self,
        envelope: TraceEnvelope,
        hook_rows: list[HookEventRow],
        monkeypatch: pytest.MonkeyPatch,
    ) -> TraceEnvelope:
        """Monkeypatch fetch_hook_events_for_session, call enrich, return envelope."""
        with patch(
            "kairos.readers.hook_join.fetch_hook_events_for_session",
            return_value=hook_rows,
        ):
            return enrich_envelope_with_hooks(envelope, dsn="fake://")

    def test_is_error_patches_step_to_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        steps = [_step(0, "Bash"), _step(1, "Bash")]
        env = _envelope(steps)
        rows = [
            _hook_row("Bash", is_error=True, seq=1),
            _hook_row("Bash", is_error=False, seq=2),
        ]
        enriched = self._enrich(env, rows, monkeypatch)
        assert enriched.steps[0].status is StepStatus.ERROR
        assert enriched.steps[1].status is StepStatus.OK

    def test_step2_untouched_when_step1_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        steps = [_step(0, "Bash"), _step(1, "Bash")]
        env = _envelope(steps)
        rows = [_hook_row("Bash", is_error=True, seq=1), _hook_row("Bash", is_error=False, seq=2)]
        enriched = self._enrich(env, rows, monkeypatch)
        s1 = enriched.steps[1]
        assert s1.status is StepStatus.OK
        assert s1.status_source is StepStatusSource.ATTR_SUCCESS

    def test_tool_args_populated_from_hook(self, monkeypatch: pytest.MonkeyPatch) -> None:
        steps = [_step(0, "Bash")]
        env = _envelope(steps)
        rows = [_hook_row("Bash", seq=1, tool_input={"command": "echo hello"})]
        enriched = self._enrich(env, rows, monkeypatch)
        assert enriched.steps[0].tool_args == {"command": "echo hello"}
        assert enriched.steps[0].tool_args_normalized is not None

    def test_tool_output_populated_from_hook(self, monkeypatch: pytest.MonkeyPatch) -> None:
        steps = [_step(0, "Bash")]
        env = _envelope(steps)
        rows = [_hook_row("Bash", seq=1, tool_output="hello world")]
        enriched = self._enrich(env, rows, monkeypatch)
        assert enriched.steps[0].tool_output == "hello world"

    def test_error_count_recomputed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        steps = [_step(0, "Bash"), _step(1, "Bash")]
        env = _envelope(steps)
        assert env.error_count == 0
        rows = [_hook_row("Bash", is_error=True, seq=1), _hook_row("Bash", is_error=False, seq=2)]
        enriched = self._enrich(env, rows, monkeypatch)
        assert enriched.error_count == 1

    def test_existing_args_not_overwritten(self, monkeypatch: pytest.MonkeyPatch) -> None:
        existing_args = {"command": "original"}
        steps = [_step(0, "Bash", tool_args=existing_args)]
        env = _envelope(steps)
        rows = [_hook_row("Bash", seq=1, tool_input={"command": "hook-provided"})]
        enriched = self._enrich(env, rows, monkeypatch)
        # Existing args must NOT be overwritten.
        assert enriched.steps[0].tool_args == existing_args

    def test_existing_error_status_not_overwritten(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A step already ERROR is not touched by the hook correction."""
        steps = [_step(0, "Bash", status=StepStatus.ERROR, status_source=StepStatusSource.OTEL_STATUS)]
        env = _envelope(steps, session_id="s1")
        rows = [_hook_row("Bash", is_error=True, seq=1)]
        enriched = self._enrich(env, rows, monkeypatch)
        # status_source must remain OTEL_STATUS (not stamped to ATTR_SUCCESS).
        assert enriched.steps[0].status_source is StepStatusSource.OTEL_STATUS

    def test_status_source_none_stamped_to_attr_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        steps = [_step(0, "Bash", status=StepStatus.OK, status_source=StepStatusSource.NONE)]
        env = _envelope(steps)
        rows = [_hook_row("Bash", is_error=True, seq=1)]
        enriched = self._enrich(env, rows, monkeypatch)
        assert enriched.steps[0].status is StepStatus.ERROR
        assert enriched.steps[0].status_source is StepStatusSource.ATTR_SUCCESS

    def test_no_session_id_returns_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        steps = [_step(0, "Bash")]
        env = _envelope(steps, session_id=None)
        # fetch should never be called.
        with patch(
            "kairos.readers.hook_join.fetch_hook_events_for_session",
            side_effect=AssertionError("should not be called"),
        ):
            enriched = enrich_envelope_with_hooks(env, dsn="fake://")
        assert enriched.steps[0].status is StepStatus.OK

    def test_interleaved_tools_aligned_independently(self, monkeypatch: pytest.MonkeyPatch) -> None:
        steps = [_step(0, "Bash"), _step(1, "Edit"), _step(2, "Bash")]
        env = _envelope(steps)
        rows = [
            _hook_row("Bash", is_error=True, seq=1),  # Bash ordinal 0 → step 0
            _hook_row("Edit", is_error=False, seq=2),  # Edit ordinal 0 → step 1
            _hook_row("Bash", is_error=False, seq=3),  # Bash ordinal 1 → step 2
        ]
        enriched = self._enrich(env, rows, monkeypatch)
        assert enriched.steps[0].status is StepStatus.ERROR  # Bash 0 → error
        assert enriched.steps[1].status is StepStatus.OK  # Edit 0 → ok
        assert enriched.steps[2].status is StepStatus.OK  # Bash 1 → ok

    def test_more_steps_than_rows_no_crash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        steps = [_step(0, "Bash"), _step(1, "Bash"), _step(2, "Bash")]
        env = _envelope(steps)
        rows = [_hook_row("Bash", is_error=True, seq=1)]  # only one row
        enriched = self._enrich(env, rows, monkeypatch)
        assert enriched.steps[0].status is StepStatus.ERROR
        # Steps 1 and 2 have no matching row → untouched
        assert enriched.steps[1].status is StepStatus.OK
        assert enriched.steps[2].status is StepStatus.OK

    def test_more_rows_than_steps_no_crash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        steps = [_step(0, "Bash")]
        env = _envelope(steps)
        rows = [
            _hook_row("Bash", is_error=True, seq=1),
            _hook_row("Bash", is_error=True, seq=2),  # extra row, no step to match
        ]
        enriched = self._enrich(env, rows, monkeypatch)
        assert enriched.steps[0].status is StepStatus.ERROR  # first aligned
        # No crash.


class TestTraceWindowing:
    """Time-window guard prevents cross-trace bleed in multi-trace sessions."""

    def _enrich(
        self,
        envelope: TraceEnvelope,
        hook_rows: list[HookEventRow],
    ) -> TraceEnvelope:
        with patch(
            "kairos.readers.hook_join.fetch_hook_events_for_session",
            return_value=hook_rows,
        ):
            return enrich_envelope_with_hooks(envelope, dsn="fake://")

    def test_cross_trace_bleed_prevented(self) -> None:
        """REGRESSION: traceB's Bash must align to traceB's hook row, NOT traceA's.

        Session has TWO traces:
          traceA: Bash#1 (args A-bash, error), Read#1 (args A-read)
          traceB: Bash#2 (args B-bash, ok)
        The whole-session pool, ordered by seq, is [Bash#1, Read#1, Bash#2].
        When enriching traceB (whose only Bash is ordinal 0), a naive
        full-pool alignment would grab pool's Bash ordinal 0 = Bash#1 (traceA).
        The time window must scope the pool to traceB's cluster first, so
        traceB's Bash aligns to Bash#2.
        """
        # All three hook rows for the whole session, ordered by seq.
        session_rows = [
            _hook_row(
                "Bash",
                is_error=True,
                seq=1,
                tool_input={"command": "A-bash"},
                tool_output="out-A-bash",
                occurred_at=_T_A,
            ),
            _hook_row(
                "Read",
                is_error=False,
                seq=2,
                tool_input={"file_path": "A-read"},
                tool_output="out-A-read",
                occurred_at=_T_A + timedelta(seconds=1),
            ),
            _hook_row(
                "Bash",
                is_error=False,
                seq=3,
                tool_input={"command": "B-bash"},
                tool_output="out-B-bash",
                occurred_at=_T_B,
            ),
        ]

        # traceB envelope: a single Bash step in traceB's time cluster.
        b_step = _step(0, "Bash", started_at=_T_B, ended_at=_T_B)
        env_b = _envelope([b_step])

        enriched = self._enrich(env_b, session_rows)

        # traceB's Bash must get traceB's row (B-bash, ok) — NOT traceA's
        # Bash#1 (A-bash, error).
        assert enriched.steps[0].tool_args == {"command": "B-bash"}
        assert enriched.steps[0].tool_output == "out-B-bash"
        assert enriched.steps[0].status is StepStatus.OK  # NOT ERROR from traceA

    def test_trace_a_still_aligns_to_its_own_rows(self) -> None:
        """The earlier trace (A) aligns to its own rows when enriched."""
        session_rows = [
            _hook_row(
                "Bash",
                is_error=True,
                seq=1,
                tool_input={"command": "A-bash"},
                tool_output="out-A-bash",
                occurred_at=_T_A,
            ),
            _hook_row(
                "Read",
                is_error=False,
                seq=2,
                tool_input={"file_path": "A-read"},
                tool_output="out-A-read",
                occurred_at=_T_A + timedelta(seconds=1),
            ),
            _hook_row(
                "Bash",
                is_error=False,
                seq=3,
                tool_input={"command": "B-bash"},
                tool_output="out-B-bash",
                occurred_at=_T_B,
            ),
        ]
        a_bash = _step(0, "Bash", started_at=_T_A, ended_at=_T_A)
        a_read = _step(1, "Read", started_at=_T_A + timedelta(seconds=1), ended_at=_T_A + timedelta(seconds=1))
        env_a = _envelope([a_bash, a_read])

        enriched = self._enrich(env_a, session_rows)

        assert enriched.steps[0].tool_args == {"command": "A-bash"}
        assert enriched.steps[0].status is StepStatus.ERROR  # traceA's Bash failed
        assert enriched.steps[1].tool_args == {"file_path": "A-read"}

    def test_no_window_skips_enrichment(self) -> None:
        """No usable timestamps anywhere → skip (never align against full pool)."""
        step = _step(0, "Bash", started_at=None, ended_at=None)
        # Build envelope WITHOUT started_at/ended_at fallback.
        env = TraceEnvelope(
            trace_id="aabbccdd" * 4,
            steps=[step],
            metadata={"session_id": "test-session"},
            started_at=None,
            ended_at=None,
        )
        rows = [_hook_row("Bash", is_error=True, seq=1, tool_input={"command": "x"})]
        enriched = self._enrich(env, rows)
        # Skipped → untouched.
        assert enriched.steps[0].status is StepStatus.OK
        assert enriched.steps[0].tool_args is None

    def test_envelope_timestamp_fallback_used(self) -> None:
        """When steps lack timestamps but the envelope has them, window still works."""
        step = _step(0, "Bash", started_at=None, ended_at=None)
        env = TraceEnvelope(
            trace_id="aabbccdd" * 4,
            steps=[step],
            metadata={"session_id": "test-session"},
            started_at=_T_A,
            ended_at=_T_A,
        )
        rows = [_hook_row("Bash", is_error=True, seq=1, tool_input={"command": "x"}, occurred_at=_T_A)]
        enriched = self._enrich(env, rows)
        assert enriched.steps[0].status is StepStatus.ERROR

    def test_row_without_occurred_at_dropped(self) -> None:
        """A hook row missing occurred_at cannot be windowed → not aligned."""
        step = _step(0, "Bash", started_at=_T_A, ended_at=_T_A)
        env = _envelope([step])
        rows = [_hook_row("Bash", is_error=True, seq=1, occurred_at=None)]
        enriched = self._enrich(env, rows)
        # Dropped by window filter → step untouched.
        assert enriched.steps[0].status is StepStatus.OK


class TestFetchEnvelopeFromDbEnrichHooks:
    """Verify the enrich_hooks routing in fetch_envelope_from_db.

    Default is now True (hook-truth by default). Callers can still pass
    enrich_hooks=False to read the RAW (un-enriched) OTel envelope.
    """

    def test_enrich_hooks_false_does_not_call_hook_join(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Explicit enrich_hooks=False (raw path) must never call hook_join."""
        from kairos.readers.db import fetch_envelope_from_db

        with (
            patch("kairos.readers.db.fetch_spans_from_db", return_value=[]),
            patch("kairos.readers.phoenix.spans_to_envelope") as mock_s2e,
            patch(
                "kairos.readers.hook_join.enrich_envelope_with_hooks",
                side_effect=AssertionError("must not be called"),
            ),
        ):
            mock_s2e.return_value = TraceEnvelope(trace_id="", is_valid=False)
            fetch_envelope_from_db("abc" * 10, "fake://", enrich_hooks=False)
            # No AssertionError → hook_join was not invoked.

    def test_default_calls_hook_join_and_returns_enriched(self) -> None:
        """The NEW default (no kwarg) routes through enrich_envelope_with_hooks.

        The hook says is_error=True, so the enriched envelope's step shows ERROR
        — proving hook-truth is the default, not the raw OTel value.
        """
        from kairos.readers.db import fetch_envelope_from_db

        # Raw (pre-enrich) envelope: one Bash step reported OK by raw OTel.
        raw_env = _envelope([_step(0, "Bash", status=StepStatus.OK)])
        # Enriched envelope: same step corrected to ERROR by the hook.
        enriched_env = _envelope([_step(0, "Bash", status=StepStatus.ERROR)])
        enriched_env.error_count = 1

        with (
            patch("kairos.readers.db.fetch_spans_from_db", return_value=[]),
            patch("kairos.readers.phoenix.spans_to_envelope", return_value=raw_env),
            patch(
                "kairos.readers.hook_join.enrich_envelope_with_hooks",
                return_value=enriched_env,
            ) as mock_enrich,
        ):
            # No enrich_hooks kwarg → uses the new default (True).
            result = fetch_envelope_from_db("abc" * 10, "fake://")

        mock_enrich.assert_called_once()
        assert result.steps[0].status is StepStatus.ERROR
        assert result.error_count == 1


# ── Integration tests (require KAIROS_PG_DSN) ────────────────────────────────


@_skip_no_db
class TestHookJoinIntegration:
    """Round-trip: persist hook_events → build envelope → enrich → assert patches."""

    def _cleanup(self, session_id: str) -> None:
        import psycopg

        with psycopg.connect(_DSN) as conn:
            conn.execute("DELETE FROM hook_events WHERE session_id = %s", (session_id,))
            conn.commit()

    def _insert_hook_rows(
        self,
        session_id: str,
        rows: list[dict[str, Any]],
    ) -> None:
        import psycopg
        from psycopg.types.json import Jsonb

        with psycopg.connect(_DSN) as conn:
            for i, row in enumerate(rows, 1):
                conn.execute(
                    "INSERT INTO hook_events "
                    "  (session_id, seq, event_name, tool_name, is_error, "
                    "   tool_input_redacted, tool_output, occurred_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (session_id, seq) DO NOTHING",
                    (
                        session_id,
                        i,
                        row.get("event_name", "PostToolUse"),
                        row.get("tool_name"),
                        row.get("is_error", False),
                        Jsonb(row.get("tool_input_redacted") or {}),
                        row.get("tool_output"),
                        row.get("occurred_at", _T_A),
                    ),
                )
            conn.commit()

    def test_is_error_patched_via_db(self) -> None:
        from kairos.loop import db as loop_db

        loop_db.apply_migrations()

        session_id = f"test-hook-{uuid.uuid4().hex[:8]}"
        try:
            self._insert_hook_rows(
                session_id,
                [
                    {
                        "event_name": "PostToolUseFailure",
                        "tool_name": "Bash",
                        "is_error": True,
                        "tool_input_redacted": {"command": "bad-cmd"},
                        "tool_output": "error: command not found",
                    }
                ],
            )

            from kairos.readers.hook_join import fetch_hook_events_for_session

            fetched = fetch_hook_events_for_session(session_id, _DSN)
            assert len(fetched) == 1
            assert fetched[0].is_error is True
            assert fetched[0].tool_name == "Bash"
            assert fetched[0].tool_input_redacted == {"command": "bad-cmd"}
            assert fetched[0].tool_output == "error: command not found"

            # Build a stub envelope with a Bash step and a session_id in metadata.
            bash_step = _step(0, "Bash")
            env = _envelope([bash_step], session_id=session_id)

            enriched = enrich_envelope_with_hooks(env, _DSN)
            assert enriched.steps[0].status is StepStatus.ERROR
            assert enriched.steps[0].tool_args == {"command": "bad-cmd"}
            assert enriched.steps[0].tool_output == "error: command not found"
            assert enriched.error_count == 1
        finally:
            self._cleanup(session_id)

    def test_session_start_end_rows_ignored(self) -> None:
        """SessionStart / SessionEnd rows must not appear in fetch results."""
        from kairos.loop import db as loop_db

        loop_db.apply_migrations()

        session_id = f"test-hook-{uuid.uuid4().hex[:8]}"
        try:
            self._insert_hook_rows(
                session_id,
                [
                    {"event_name": "SessionStart", "tool_name": None, "is_error": None},
                    {
                        "event_name": "PostToolUse",
                        "tool_name": "Edit",
                        "is_error": False,
                        "tool_input_redacted": {"file_path": "/tmp/x.py"},
                        "tool_output": "ok",
                    },
                    {"event_name": "SessionEnd", "tool_name": None, "is_error": None},
                ],
            )

            from kairos.readers.hook_join import fetch_hook_events_for_session

            fetched = fetch_hook_events_for_session(session_id, _DSN)
            # Only PostToolUse / PostToolUseFailure rows — SessionStart / SessionEnd filtered.
            assert len(fetched) == 1
            assert fetched[0].tool_name == "Edit"
        finally:
            self._cleanup(session_id)
