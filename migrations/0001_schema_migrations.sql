-- Migration 0001: schema_migrations tracking table.
-- Applied by src/kairos/loop/db.py before any other migration.
-- This table records which migration files have been applied so
-- the runner is idempotent: re-running never double-applies.

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     text        PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now()
);
