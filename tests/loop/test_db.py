"""Tests for src/kairos/loop/db.py — connection helper and migration runner.

Tests that require a live kairos-pg instance are guarded by a skip marker:
if KAIROS_PG_DSN is not set (CI without the container), they emit a clean
SKIP with a reason string.  When the container IS reachable (local dev, this
sprint), all tests run and must pass.
"""

from __future__ import annotations

import os
import uuid
from datetime import date
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DSN = os.environ.get("KAIROS_PG_DSN", "").strip()
_DB_AVAILABLE = bool(_DSN)

_skip_no_db = pytest.mark.skipif(
    not _DB_AVAILABLE,
    reason="KAIROS_PG_DSN not set — kairos-pg container not reachable in this environment",
)


# ---------------------------------------------------------------------------
# Unit tests (no DB required)
# ---------------------------------------------------------------------------


class TestGetConnectionRaisesWithoutDSN:
    """db.get_connection() must fail fast when KAIROS_PG_DSN is absent."""

    def test_raises_runtime_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("KAIROS_PG_DSN", raising=False)
        from kairos.loop import db

        with pytest.raises(RuntimeError, match="KAIROS_PG_DSN"):
            db.get_connection()

    def test_raises_on_empty_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KAIROS_PG_DSN", "   ")
        from kairos.loop import db

        with pytest.raises(RuntimeError, match="KAIROS_PG_DSN"):
            db.get_connection()


class TestApplyMigrationsNoFilesRaises:
    """apply_migrations() raises when the directory contains no *.sql files."""

    def test_raises_file_not_found(self, tmp_path: Path) -> None:
        from kairos.loop import db

        empty_dir = tmp_path / "empty_migrations"
        empty_dir.mkdir()

        with pytest.raises(FileNotFoundError, match="No \\*.sql"):
            db.apply_migrations(migrations_dir=empty_dir)


# ---------------------------------------------------------------------------
# Integration tests (require live kairos-pg)
# ---------------------------------------------------------------------------


@_skip_no_db
class TestConnectionHelper:
    """get_connection() opens a working connection when DSN is set."""

    def test_ping(self) -> None:
        from kairos.loop import db

        with db.get_connection() as conn:
            row = conn.execute("SELECT 1 AS one").fetchone()
        assert row is not None
        assert row[0] == 1


@_skip_no_db
class TestMigrationIdempotency:
    """apply_migrations() is idempotent: applying twice yields no error."""

    def test_apply_twice(self) -> None:
        from kairos.loop import db

        # First application (may already be applied from a prior run — that's fine).
        applied_1 = db.apply_migrations()
        # Second application — nothing new should be applied.
        applied_2 = db.apply_migrations()

        assert applied_2 == [], f"Expected no new migrations on second run, got {applied_2}"
        # Either first run applied something or the DB was already current.
        assert isinstance(applied_1, list)

    def test_all_tables_present(self) -> None:
        """After migrations all five spec tables must exist in the kairos DB."""
        from kairos.loop import db

        db.apply_migrations()

        expected_tables = {
            "findings",
            "nightly_rollup",
            "labels",
            "expectations",
            "discovery_queue",
            "schema_migrations",
        }
        with db.get_connection() as conn:
            rows = conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'").fetchall()
        found = {row[0] for row in rows}
        missing = expected_tables - found
        assert not missing, f"Tables missing after migration: {missing}"


@_skip_no_db
class TestFindingsRoundTrip:
    """Round-trip INSERT + SELECT on the findings table."""

    def test_insert_and_select(self) -> None:
        from kairos.loop import db

        db.apply_migrations()

        night = date(2026, 6, 13)
        trace_id = f"test-trace-{uuid.uuid4().hex[:8]}"
        detector = "test_detector"

        with db.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO findings
                    (night_id, trace_id, unit_id, workflow, agent,
                     detector, severity, evidence_steps, tokens, struggle,
                     outcome, config_hash)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (night_id, trace_id, detector) DO UPDATE
                    SET ingested_at = now()
                """,
                (
                    night,
                    trace_id,
                    "unit-1",
                    "test_workflow",
                    "test_agent",
                    detector,
                    "warning",
                    [1, 3, 5],
                    42,
                    0.25,
                    "pass",
                    "abc123",
                ),
            )
            conn.commit()

            row = conn.execute(
                "SELECT trace_id, detector, severity, evidence_steps, tokens, struggle "
                "FROM findings WHERE night_id = %s AND trace_id = %s AND detector = %s",
                (night, trace_id, detector),
            ).fetchone()

        assert row is not None
        assert row[0] == trace_id
        assert row[1] == detector
        assert row[2] == "warning"
        assert list(row[3]) == [1, 3, 5]
        assert row[4] == 42
        assert abs(row[5] - 0.25) < 1e-6

        # Cleanup — keep the test DB tidy.
        with db.get_connection() as conn:
            conn.execute(
                "DELETE FROM findings WHERE night_id = %s AND trace_id = %s",
                (night, trace_id),
            )
            conn.commit()


@_skip_no_db
class TestNightlyRollupRoundTrip:
    """Round-trip INSERT + SELECT on the nightly_rollup table."""

    def test_insert_and_select(self) -> None:
        from kairos.loop import db

        db.apply_migrations()

        night = date(2026, 6, 13)
        workflow = f"test_workflow_{uuid.uuid4().hex[:6]}"
        agent = "test_agent"

        with db.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO nightly_rollup
                    (night_id, workflow, agent, units, traces, outcome_rate,
                     struggle_p50, struggle_p90, coordination_waste_per_trace,
                     tokens_per_unit, finding_counts, config_hash)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (night_id, workflow, agent) DO UPDATE
                    SET units = EXCLUDED.units
                """,
                (
                    night,
                    workflow,
                    agent,
                    10,
                    15,
                    0.80,
                    0.12,
                    0.45,
                    0.05,  # coordination_waste_per_trace (renamed from coordination_waste_rate)
                    1200.5,
                    '{"unrecovered_error": 3, "struggle_ratio": 1}',
                    "def456",
                ),
            )
            conn.commit()

            row = conn.execute(
                "SELECT workflow, units, outcome_rate, finding_counts "
                "FROM nightly_rollup WHERE night_id = %s AND workflow = %s AND agent = %s",
                (night, workflow, agent),
            ).fetchone()

        assert row is not None
        assert row[0] == workflow
        assert row[1] == 10
        assert abs(row[2] - 0.80) < 1e-6
        assert row[3] == {"unrecovered_error": 3, "struggle_ratio": 1}

        # Cleanup.
        with db.get_connection() as conn:
            conn.execute(
                "DELETE FROM nightly_rollup WHERE night_id = %s AND workflow = %s",
                (night, workflow),
            )
            conn.commit()


@_skip_no_db
class TestMigrationIdempotencyOnFreshSchema:
    """Applying migrations to a DB that already has the tables is a no-op."""

    def test_no_error_on_existing_tables(self) -> None:
        """CREATE TABLE IF NOT EXISTS in migrations means re-running never crashes."""
        from kairos.loop import db

        # Run three times — all must succeed.
        for _ in range(3):
            db.apply_migrations()

        with db.get_connection() as conn:
            count = conn.execute("SELECT count(*) FROM schema_migrations").fetchone()
        assert count is not None
        assert count[0] >= 6  # 0001 through 0006
