-- Migration 0013: session_id — surface session hierarchy on spans
--
-- Adds a session_id column to spans and backfills it from the
-- attributes->>'session.id' JSONB field that the CC exporter embeds.
-- New ingestion populates session_id directly (ingest/spans.py).
--
-- Indexes added:
--   spans_session_id_idx         — B-tree on session_id (primary list/group key)
--   spans_tool_name_expr_idx     — B-tree expression index on attributes->>'tool_name'
--   spans_attrs_trgm_gin_idx     — GIN trigram on attributes::text (content search)
--                                  Requires pg_trgm; skipped gracefully if absent.
--
-- Content-search at v1 scale falls back to a sequential scan when the trigram
-- index is absent.  Adding pg_trgm and re-running the migration (it is
-- idempotent via IF NOT EXISTS) will enable index-accelerated ILIKE.

-- 1. Add column (idempotent via DO block)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'spans' AND column_name = 'session_id'
    ) THEN
        ALTER TABLE spans ADD COLUMN session_id text;
    END IF;
END;
$$;

-- 2. Backfill existing rows that have session.id in attributes.
UPDATE spans
SET session_id = attributes->>'session.id'
WHERE session_id IS NULL
  AND attributes->>'session.id' IS NOT NULL;

-- 3. B-tree index on session_id (primary session-level read pattern).
CREATE INDEX IF NOT EXISTS spans_session_id_idx ON spans (session_id);

-- 4. Expression index on tool_name attribute (tool-filter queries).
CREATE INDEX IF NOT EXISTS spans_tool_name_expr_idx
    ON spans ((attributes->>'tool_name'));

-- 5. GIN trigram index on attributes::text (content search acceleration).
--    Skipped gracefully when pg_trgm is not installed.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_available_extensions WHERE name = 'pg_trgm'
    ) THEN
        CREATE EXTENSION IF NOT EXISTS pg_trgm;
        -- CREATE INDEX IF NOT EXISTS is not supported inside DO blocks for
        -- concurrent-creation; use a regular CREATE INDEX CONCURRENTLY
        -- outside. Inside a migration block we use non-concurrent creation.
        IF NOT EXISTS (
            SELECT 1 FROM pg_indexes
            WHERE tablename = 'spans'
              AND indexname = 'spans_attrs_trgm_gin_idx'
        ) THEN
            EXECUTE 'CREATE INDEX spans_attrs_trgm_gin_idx'
                    ' ON spans USING GIN (CAST(attributes AS text) gin_trgm_ops)';
        END IF;
    ELSE
        RAISE NOTICE
            'pg_trgm not available — spans_attrs_trgm_gin_idx skipped; '
            'content-search will use sequential scan. '
            'Install pg_trgm and re-run migration to add the index.';
    END IF;
END;
$$;
