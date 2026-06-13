"""Kairos Postgres connection helper and migration runner.

Single source of truth for all Kairos-produced data (findings, nightly_rollup,
labels, expectations, discovery_queue).  See migrations/ for the schema.

Security contract (enforced here and by persist.py):
  - DSN is read exclusively from the KAIROS_PG_DSN environment variable.
    No credentials appear in source.
  - This store holds REDACTED evidence only.  persist.py is responsible for
    stripping raw tool output before any INSERT; evidence_steps contains step
    indices (int[]), never full tool outputs or secrets.
  - The kairos-pg container is bound to 127.0.0.1:5434 only (not 0.0.0.0).

Usage::

    from kairos.loop.db import get_connection, apply_migrations

    apply_migrations()                  # idempotent; safe to call every start-up
    with get_connection() as conn:
        conn.execute("SELECT 1")
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import psycopg
from psycopg import Connection

if TYPE_CHECKING:
    from collections.abc import Generator

# Migrations directory is always relative to this package's repo root.
_MIGRATIONS_DIR = Path(__file__).parent.parent.parent.parent / "migrations"


def _dsn() -> str:
    """Return the Postgres DSN from the environment.

    Raises ``RuntimeError`` if KAIROS_PG_DSN is not set — fail loudly so
    the operator knows immediately why the loop cannot start.
    """
    dsn = os.environ.get("KAIROS_PG_DSN", "").strip()
    if not dsn:
        raise RuntimeError(
            "KAIROS_PG_DSN is not set. "
            "Set it to a libpq connection string, e.g. "
            "postgresql://kairos:secret@127.0.0.1:5434/kairos"
        )
    return dsn


def get_connection() -> Connection[tuple[object, ...]]:
    """Open and return a new psycopg connection.

    The caller is responsible for closing or using it as a context manager::

        with get_connection() as conn:
            conn.execute(...)

    Returns a row_factory=None (tuples) connection; callers that want dicts
    can call ``psycopg.rows.dict_row`` themselves.
    """
    return psycopg.connect(_dsn())


def iter_connection() -> Generator[Connection[tuple[object, ...]]]:
    """Yield a single connection and close it on exit (for use in ``with`` blocks)."""
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()


def apply_migrations(migrations_dir: Path | None = None) -> list[str]:
    """Apply all pending migrations in ``migrations/`` in filename order.

    Idempotent: migrations already recorded in ``schema_migrations`` are
    skipped.  Safe to call on every application start-up.

    The ``schema_migrations`` tracking table is created first (its own SQL
    file is ``0001_schema_migrations.sql``); if it already exists the
    ``CREATE TABLE IF NOT EXISTS`` is a no-op.

    Args:
        migrations_dir: Override the default ``<repo_root>/migrations/``
            path.  Useful in tests.

    Returns:
        List of migration version strings that were applied this call
        (empty list if everything was already up to date).
    """
    mdir = migrations_dir or _MIGRATIONS_DIR
    sql_files = sorted(mdir.glob("*.sql"))
    if not sql_files:
        raise FileNotFoundError(f"No *.sql migration files found in {mdir}")

    applied_this_run: list[str] = []

    with get_connection() as conn:
        # Bootstrap: create the tracking table before we try to query it.
        # This is safe to run even when it already exists.
        bootstrap_file = mdir / "0001_schema_migrations.sql"
        if bootstrap_file.exists():
            conn.execute(bootstrap_file.read_text())
            conn.commit()

        # Fetch already-applied versions.
        rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
        already_applied = {row[0] for row in rows}

        for sql_file in sql_files:
            version = sql_file.name  # e.g. "0002_findings.sql"
            if version in already_applied:
                continue

            conn.execute(sql_file.read_text())
            conn.execute(
                "INSERT INTO schema_migrations (version) VALUES (%s) "
                "ON CONFLICT (version) DO NOTHING",
                (version,),
            )
            conn.commit()
            applied_this_run.append(version)

    return applied_this_run
