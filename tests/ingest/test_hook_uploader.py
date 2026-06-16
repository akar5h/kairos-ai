"""Tests for hook_uploader.py (F1.2).

DB-requiring tests are guarded by ``_skip_no_db`` (same pattern as
tests/loop/test_spans_migration.py).  Non-DB tests exercise the spool
parsing, corrupt-line skip, and .done rename logic using tmp_path.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

_DSN = os.environ.get("KAIROS_PG_DSN", "").strip()
_DB_AVAILABLE = bool(_DSN)

_skip_no_db = pytest.mark.skipif(
    not _DB_AVAILABLE,
    reason="KAIROS_PG_DSN not set — kairos-pg container not reachable in this environment",
)

_MIGRATIONS_DIR = Path(__file__).parent.parent.parent / "migrations"


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _make_post_tool_use_record(session_id: str, tool_use_id: str | None = None) -> dict[str, object]:
    return {
        "session_id": session_id,
        "event_name": "PostToolUse",
        "tool_use_id": tool_use_id or uuid.uuid4().hex,
        "tool_name": "Bash",
        "tool_input_redacted": {"command": "ls -la"},
        "tool_output": "file1\nfile2",
        "is_error": False,
        "permission_mode": "default",
        "agent_id": None,
        "agent_type": None,
        "payload_redacted": {"hook_event_name": "PostToolUse", "session_id": session_id},
        "occurred_at": datetime.now(tz=UTC).isoformat(),
    }


# ── Migration file tests ───────────────────────────────────────────────────────


class TestHookEventsMigrationFile:
    """0011_hook_events.sql must exist and contain expected DDL."""

    def test_file_exists(self) -> None:
        assert (_MIGRATIONS_DIR / "0011_hook_events.sql").exists()

    def test_ddl_keywords(self) -> None:
        sql = (_MIGRATIONS_DIR / "0011_hook_events.sql").read_text()
        keywords = (
            "CREATE TABLE IF NOT EXISTS hook_events",
            "PRIMARY KEY",
            "session_id",
            "seq",
            "tool_use_id",
            "payload_redacted",
            "jsonb",
        )
        for keyword in keywords:
            assert keyword in sql, f"Expected keyword not found in migration: {keyword!r}"

    def test_index_present(self) -> None:
        sql = (_MIGRATIONS_DIR / "0011_hook_events.sql").read_text()
        assert "hook_events_session_tool_use_idx" in sql


# ── Uploader unit tests (no DB) ───────────────────────────────────────────────


class TestDrainSpoolNoDB:
    """Spool parsing and drained-file handling — no Postgres required."""

    def test_empty_spool_dir_returns_zero(self, tmp_path: Path) -> None:
        """drain_spool on an empty dir returns 0 without error."""
        # We need a DSN but won't hit the DB (no files to drain).
        # Use a placeholder; no connection will be attempted.
        from kairos.ingest.hook_uploader import drain_spool

        result = drain_spool(tmp_path, dsn="postgresql://placeholder/placeholder")
        assert result == 0

    def test_missing_spool_dir_returns_zero(self, tmp_path: Path) -> None:
        from kairos.ingest.hook_uploader import drain_spool

        missing = tmp_path / "no_such_dir"
        result = drain_spool(missing, dsn="postgresql://placeholder/placeholder")
        assert result == 0

    def test_record_to_row_shape(self) -> None:
        """_record_to_row produces a dict with expected keys and Jsonb wrappers."""
        from psycopg.types.json import Jsonb

        from kairos.ingest.hook_uploader import _record_to_row

        rec = _make_post_tool_use_record("sess-abc", tool_use_id="tid-123")
        row = _record_to_row(rec, seq=1)

        assert row["session_id"] == "sess-abc"
        assert row["seq"] == 1
        assert row["tool_use_id"] == "tid-123"
        assert row["event_name"] == "PostToolUse"
        assert row["tool_name"] == "Bash"
        assert isinstance(row["tool_input_redacted"], Jsonb)
        assert isinstance(row["payload_redacted"], Jsonb)
        assert isinstance(row["occurred_at"], datetime)

    def test_record_to_row_null_tool_use_id(self) -> None:
        """tool_use_id absent (SessionStart/End) → None in row."""
        from kairos.ingest.hook_uploader import _record_to_row

        rec: dict[str, object] = {
            "session_id": "sess-start",
            "event_name": "SessionStart",
            "tool_use_id": None,
            "tool_name": None,
            "tool_input_redacted": None,
            "tool_output": None,
            "is_error": None,
            "permission_mode": "default",
            "agent_id": None,
            "agent_type": None,
            "payload_redacted": {},
            "occurred_at": datetime.now(tz=UTC).isoformat(),
        }
        row = _record_to_row(rec, seq=1)
        assert row["tool_use_id"] is None
        assert row["tool_name"] is None

    def test_parse_occurred_at_fallback(self) -> None:
        """Malformed occurred_at falls back to now() without raising."""
        from kairos.ingest.hook_uploader import _parse_occurred_at

        result = _parse_occurred_at("not-a-date")
        assert isinstance(result, datetime)
        assert result.tzinfo is not None

        result2 = _parse_occurred_at(None)
        assert isinstance(result2, datetime)


# ── Idle-drain guard tests (no DB) ────────────────────────────────────────────


def _make_session_end_record(session_id: str) -> dict[str, object]:
    return {
        "session_id": session_id,
        "event_name": "SessionEnd",
        "tool_use_id": None,
        "tool_name": None,
        "tool_input_redacted": None,
        "tool_output": None,
        "is_error": None,
        "permission_mode": "default",
        "agent_id": None,
        "agent_type": None,
        "payload_redacted": {},
        "occurred_at": datetime.now(tz=UTC).isoformat(),
    }


def _write_spool(spool_dir: Path, session_id: str, records: list[dict]) -> Path:
    """Write records as JSONL to a spool file and return the path."""
    spool_file = spool_dir / f"{session_id}.jsonl"
    with spool_file.open("w") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
    return spool_file


class TestIsDrainable:
    """Unit-level tests for _is_drainable logic."""

    def test_session_end_present_is_drainable_even_if_fresh(self, tmp_path: Path) -> None:
        """A file with SessionEnd is always drainable, regardless of mtime."""
        from kairos.ingest.hook_uploader import _is_drainable

        session_id = f"test-end-{uuid.uuid4().hex[:8]}"
        spool_file = _write_spool(
            tmp_path,
            session_id,
            [
                _make_post_tool_use_record(session_id),
                _make_session_end_record(session_id),
            ],
        )
        # Use a "now" that is just 1 second after mtime — file would be "active"
        # without the SessionEnd guard.
        now = spool_file.stat().st_mtime + 1.0
        records = [
            _make_post_tool_use_record(session_id),
            _make_session_end_record(session_id),
        ]
        assert _is_drainable(spool_file, records, idle_seconds=90, _now=now)

    def test_stale_file_no_session_end_is_drainable(self, tmp_path: Path) -> None:
        """A file older than idle_seconds with no SessionEnd is drainable."""
        from kairos.ingest.hook_uploader import _is_drainable

        session_id = f"test-stale-{uuid.uuid4().hex[:8]}"
        spool_file = _write_spool(
            tmp_path, session_id, [_make_post_tool_use_record(session_id)]
        )
        # Set mtime to 200 seconds ago.
        import os
        old_mtime = spool_file.stat().st_mtime - 200
        os.utime(spool_file, (old_mtime, old_mtime))

        records = [_make_post_tool_use_record(session_id)]
        now = spool_file.stat().st_mtime + 200  # 200 seconds after old mtime
        assert _is_drainable(spool_file, records, idle_seconds=90, _now=now)

    def test_fresh_file_no_session_end_is_not_drainable(self, tmp_path: Path) -> None:
        """A freshly modified file with no SessionEnd must be skipped."""
        from kairos.ingest.hook_uploader import _is_drainable

        session_id = f"test-fresh-{uuid.uuid4().hex[:8]}"
        spool_file = _write_spool(
            tmp_path, session_id, [_make_post_tool_use_record(session_id)]
        )
        # "now" is only 10 seconds after mtime — well within idle_seconds=90.
        now = spool_file.stat().st_mtime + 10.0
        records = [_make_post_tool_use_record(session_id)]
        assert not _is_drainable(spool_file, records, idle_seconds=90, _now=now)

    def test_has_session_end_helper(self) -> None:
        from kairos.ingest.hook_uploader import _has_session_end

        session_id = "sess-abc"
        records_with_end = [
            _make_post_tool_use_record(session_id),
            _make_session_end_record(session_id),
        ]
        records_without_end = [_make_post_tool_use_record(session_id)]
        assert _has_session_end(records_with_end)
        assert not _has_session_end(records_without_end)
        assert not _has_session_end([])


class TestDrainFileIdleGuard:
    """Tests for _drain_file skipping active files (no DB required)."""

    def test_active_file_skipped_returns_zero_not_renamed(self, tmp_path: Path) -> None:
        """Active spool file (fresh mtime, no SessionEnd) → 0 rows, file NOT renamed."""
        from unittest.mock import MagicMock

        from kairos.ingest.hook_uploader import _drain_file

        session_id = f"test-active-{uuid.uuid4().hex[:8]}"
        spool_file = _write_spool(
            tmp_path, session_id, [_make_post_tool_use_record(session_id)]
        )
        # "now" is 10s after mtime → within idle window.
        now = spool_file.stat().st_mtime + 10.0

        mock_conn = MagicMock()
        result = _drain_file(spool_file, mock_conn, idle_seconds=90, _now=now)

        assert result == 0, "Active file should return 0 rows"
        # File must NOT be renamed to .done.
        assert spool_file.exists(), "Active spool file must not be renamed"
        done_file = spool_file.with_suffix(spool_file.suffix + ".done")
        assert not done_file.exists(), ".done file must not be created for active file"
        # DB must not have been touched.
        mock_conn.cursor.assert_not_called()

    def test_session_end_file_drained_regardless_of_mtime(self, tmp_path: Path) -> None:
        """File with SessionEnd is sent to DB even if freshly modified.

        Skips DB calls but verifies _mark_done (rename) occurs when
        _is_drainable returns True for a SessionEnd file.
        We mock psycopg at the cursor level to avoid a real DB connection.
        """
        from unittest.mock import MagicMock

        from kairos.ingest.hook_uploader import _drain_file

        session_id = f"test-end-drain-{uuid.uuid4().hex[:8]}"
        records = [
            _make_post_tool_use_record(session_id),
            _make_session_end_record(session_id),
        ]
        spool_file = _write_spool(tmp_path, session_id, records)
        # "now" is only 5s after mtime — active by mtime, but has SessionEnd.
        now = spool_file.stat().st_mtime + 5.0

        # Mock a conn that pretends _max_seq_for_session returns 0.
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = (0,)
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cursor
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        result = _drain_file(spool_file, mock_conn, idle_seconds=90, _now=now)

        assert result == 2, "Both records should be uploaded"
        done_file = spool_file.with_suffix(spool_file.suffix + ".done")
        assert not spool_file.exists(), "Drained file must be renamed to .done"
        assert done_file.exists(), ".done file must exist after drain"

    def test_stale_file_no_session_end_drained(self, tmp_path: Path) -> None:
        """File older than idle_seconds (no SessionEnd) is drained."""
        import os
        from unittest.mock import MagicMock

        from kairos.ingest.hook_uploader import _drain_file

        session_id = f"test-stale-drain-{uuid.uuid4().hex[:8]}"
        records = [_make_post_tool_use_record(session_id)]
        spool_file = _write_spool(tmp_path, session_id, records)

        # Back-date mtime to 200 seconds ago.
        old_mtime = spool_file.stat().st_mtime - 200
        os.utime(spool_file, (old_mtime, old_mtime))
        now = old_mtime + 200  # 200s later → well past idle_seconds=90

        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = (0,)
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cursor
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        result = _drain_file(spool_file, mock_conn, idle_seconds=90, _now=now)

        assert result == 1
        done_file = spool_file.with_suffix(spool_file.suffix + ".done")
        assert done_file.exists()


# ── DB round-trip tests ───────────────────────────────────────────────────────


@_skip_no_db
class TestDrainSpoolDB:
    """Round-trip: spool file → hook_events rows → read back."""

    def _ensure_migrations(self) -> None:
        from kairos.loop import db

        db.apply_migrations()

    def test_migration_applies(self) -> None:
        self._ensure_migrations()
        from kairos.loop import db

        with db.get_connection() as conn:
            rows = conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            ).fetchall()
        found = {r[0] for r in rows}
        assert "hook_events" in found

    def test_index_present(self) -> None:
        self._ensure_migrations()
        from kairos.loop import db

        with db.get_connection() as conn:
            rows = conn.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'hook_events'"
            ).fetchall()
        index_names = {r[0] for r in rows}
        assert "hook_events_session_tool_use_idx" in index_names

    def test_round_trip_single_file(self, tmp_path: Path) -> None:
        """Write a spool file, drain it, verify rows land in hook_events."""
        self._ensure_migrations()
        from kairos.ingest.hook_uploader import drain_spool
        from kairos.loop import db

        session_id = f"test-{uuid.uuid4().hex[:8]}"
        tool_use_id = uuid.uuid4().hex

        # Write two records to a spool file.
        spool_file = tmp_path / f"{session_id}.jsonl"
        records = [
            _make_post_tool_use_record(session_id, tool_use_id=tool_use_id),
            {
                "session_id": session_id,
                "event_name": "SessionStart",
                "tool_use_id": None,
                "tool_name": None,
                "tool_input_redacted": None,
                "tool_output": None,
                "is_error": None,
                "permission_mode": "default",
                "agent_id": None,
                "agent_type": None,
                "payload_redacted": {},
                "occurred_at": datetime.now(tz=UTC).isoformat(),
            },
            _make_session_end_record(session_id),
        ]
        with spool_file.open("w") as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")

        n = drain_spool(tmp_path, dsn=_DSN)
        assert n == 3

        # Spool file should be renamed to .done.
        assert not spool_file.exists()
        assert (tmp_path / f"{session_id}.jsonl.done").exists()

        # Rows should be in hook_events.
        with db.get_connection() as conn:
            rows = conn.execute(
                "SELECT session_id, tool_use_id, event_name FROM hook_events "
                "WHERE session_id = %s ORDER BY seq",
                (session_id,),
            ).fetchall()

        assert len(rows) == 3
        # The uploader assigns seq in order of iteration.
        event_names = [r[2] for r in rows]
        assert "PostToolUse" in event_names
        assert "SessionStart" in event_names
        assert "SessionEnd" in event_names

        # Verify tool_use_id stored correctly.
        tool_row = next(r for r in rows if r[2] == "PostToolUse")
        assert tool_row[1] == tool_use_id

        # Cleanup.
        with db.get_connection() as conn:
            conn.execute("DELETE FROM hook_events WHERE session_id = %s", (session_id,))
            conn.commit()

    def test_done_file_not_re_uploaded(self, tmp_path: Path) -> None:
        """A .jsonl.done file is not re-processed on a second drain_spool call."""
        self._ensure_migrations()
        from kairos.ingest.hook_uploader import drain_spool
        from kairos.loop import db

        session_id = f"test-done-{uuid.uuid4().hex[:8]}"
        spool_file = tmp_path / f"{session_id}.jsonl"
        rec = _make_post_tool_use_record(session_id)
        end_rec = _make_session_end_record(session_id)
        spool_file.write_text(json.dumps(rec) + "\n" + json.dumps(end_rec) + "\n")

        n1 = drain_spool(tmp_path, dsn=_DSN)
        assert n1 == 2

        # Second call — .done file exists, no .jsonl files remain.
        n2 = drain_spool(tmp_path, dsn=_DSN)
        assert n2 == 0

        # Cleanup.
        with db.get_connection() as conn:
            conn.execute("DELETE FROM hook_events WHERE session_id = %s", (session_id,))
            conn.commit()

    def test_corrupt_lines_skipped(self, tmp_path: Path) -> None:
        """Corrupt JSONL lines are skipped; valid lines still upload."""
        self._ensure_migrations()
        from kairos.ingest.hook_uploader import drain_spool
        from kairos.loop import db

        session_id = f"test-corrupt-{uuid.uuid4().hex[:8]}"
        spool_file = tmp_path / f"{session_id}.jsonl"

        good = _make_post_tool_use_record(session_id)
        end_rec = _make_session_end_record(session_id)
        with spool_file.open("w") as fh:
            fh.write("NOT JSON\n")
            fh.write(json.dumps(good) + "\n")
            fh.write("{broken\n")
            fh.write(json.dumps(end_rec) + "\n")

        n = drain_spool(tmp_path, dsn=_DSN)
        assert n == 2  # good PostToolUse + SessionEnd; corrupt lines skipped

        with db.get_connection() as conn:
            rows = conn.execute(
                "SELECT session_id FROM hook_events WHERE session_id = %s",
                (session_id,),
            ).fetchall()
        assert len(rows) == 2

        # Cleanup.
        with db.get_connection() as conn:
            conn.execute("DELETE FROM hook_events WHERE session_id = %s", (session_id,))
            conn.commit()
