-- Migration 0014: relax NOT NULL on optional label columns.
--
-- The POST /v1/labels write contract (F2.4) allows question, verdict, and
-- label_class to be null (only trace_id and answer are required). The original
-- 0004 schema declared them NOT NULL, which would reject those inserts.
-- DROP NOT NULL is idempotent in Postgres (no-op if already nullable).

ALTER TABLE labels ALTER COLUMN question DROP NOT NULL;
ALTER TABLE labels ALTER COLUMN verdict DROP NOT NULL;
ALTER TABLE labels ALTER COLUMN label_class DROP NOT NULL;
