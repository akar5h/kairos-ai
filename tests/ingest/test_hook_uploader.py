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
        ]
        with spool_file.open("w") as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")

        n = drain_spool(tmp_path, dsn=_DSN)
        assert n == 2

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

        assert len(rows) == 2
        # The uploader assigns seq in order of iteration; SessionStart was second.
        event_names = [r[2] for r in rows]
        assert "PostToolUse" in event_names
        assert "SessionStart" in event_names

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
        spool_file.write_text(json.dumps(rec) + "\n")

        n1 = drain_spool(tmp_path, dsn=_DSN)
        assert n1 == 1

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
        with spool_file.open("w") as fh:
            fh.write("NOT JSON\n")
            fh.write(json.dumps(good) + "\n")
            fh.write("{broken\n")

        n = drain_spool(tmp_path, dsn=_DSN)
        assert n == 1  # only the good line

        with db.get_connection() as conn:
            rows = conn.execute(
                "SELECT session_id FROM hook_events WHERE session_id = %s",
                (session_id,),
            ).fetchall()
        assert len(rows) == 1

        # Cleanup.
        with db.get_connection() as conn:
            conn.execute("DELETE FROM hook_events WHERE session_id = %s", (session_id,))
            conn.commit()
