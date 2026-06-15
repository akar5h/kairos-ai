"""Tests for migration 0010_spans.sql.

DB-requiring tests are guarded by ``_skip_no_db`` (same pattern as test_db.py).
Unit test verifies the SQL file exists and has the expected DDL keywords.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

_DSN = os.environ.get("KAIROS_PG_DSN", "").strip()
_DB_AVAILABLE = bool(_DSN)

_skip_no_db = pytest.mark.skipif(
    not _DB_AVAILABLE,
    reason="KAIROS_PG_DSN not set — kairos-pg container not reachable in this environment",
)

_MIGRATIONS_DIR = Path(__file__).parent.parent.parent / "migrations"


class TestSpansMigrationFile:
    """0010_spans.sql must exist and contain the expected DDL."""

    def test_file_exists(self) -> None:
        assert (_MIGRATIONS_DIR / "0010_spans.sql").exists()

    def test_ddl_keywords(self) -> None:
        sql = (_MIGRATIONS_DIR / "0010_spans.sql").read_text()
        keywords = ("CREATE TABLE IF NOT EXISTS spans", "PRIMARY KEY", "trace_id", "span_id", "attributes", "jsonb")
        for keyword in keywords:
            assert keyword in sql, f"Expected keyword not found in migration: {keyword!r}"


@_skip_no_db
class TestSpansMigrationApplies:
    """0010_spans.sql applies cleanly against a live kairos-pg."""

    def test_migration_idempotent(self) -> None:
        from kairos.loop import db

        applied_1 = db.apply_migrations()
        applied_2 = db.apply_migrations()

        # Second run must be a no-op.
        assert applied_2 == []
        assert isinstance(applied_1, list)

    def test_spans_table_present(self) -> None:
        from kairos.loop import db

        db.apply_migrations()

        with db.get_connection() as conn:
            rows = conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            ).fetchall()
        found = {row[0] for row in rows}
        assert "spans" in found, f"'spans' table missing; found: {found}"

    def test_spans_index_present(self) -> None:
        from kairos.loop import db

        db.apply_migrations()

        with db.get_connection() as conn:
            rows = conn.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'spans'"
            ).fetchall()
        index_names = {row[0] for row in rows}
        assert "spans_trace_id_idx" in index_names, f"Index missing; found: {index_names}"

    def test_insert_and_delete(self) -> None:
        """Verify the table accepts a row with all expected columns."""
        from kairos.loop import db

        db.apply_migrations()

        tid = uuid.uuid4().hex
        sid = uuid.uuid4().hex[:16]

        with db.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO spans
                    (trace_id, span_id, name, start_time, attributes, events, resource)
                VALUES (%s, %s, %s, now(), '{}', '[]', '{}')
                """,
                (tid, sid, "test.span"),
            )
            conn.commit()

            row = conn.execute(
                "SELECT trace_id, span_id, name FROM spans WHERE trace_id = %s",
                (tid,),
            ).fetchone()

        assert row is not None
        assert row[0] == tid
        assert row[1] == sid
        assert row[2] == "test.span"

        # Cleanup.
        with db.get_connection() as conn:
            conn.execute("DELETE FROM spans WHERE trace_id = %s", (tid,))
            conn.commit()
