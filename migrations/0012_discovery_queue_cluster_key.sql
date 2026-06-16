-- Migration 0012: add cluster_key to discovery_queue.
--
-- The cluster_key is computed by loop/discover.py (_cluster_key fn) but was
-- only stored in the JSON emit, not in Postgres.  The read API (F2.1) needs
-- to GROUP BY cluster_key in Postgres so we add the column here.
--
-- Backfill: existing rows get cluster_key = 'unknown' (safe sentinel).
-- Going-forward: loop/discover.py _persist_candidates_pg stores the real key.

ALTER TABLE discovery_queue
    ADD COLUMN IF NOT EXISTS cluster_key text NOT NULL DEFAULT 'unknown';

-- Index for the cluster aggregation query (GROUP BY cluster_key).
CREATE INDEX IF NOT EXISTS discovery_queue_cluster_key_idx
    ON discovery_queue (cluster_key);
