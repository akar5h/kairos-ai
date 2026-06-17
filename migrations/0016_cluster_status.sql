-- Migration 0016: add lifecycle status to discovery_queue clusters.
-- Each cluster_key gets a single status row; status transitions are:
--   open (default) → resolved → regressed (back to open possible via re-open)
ALTER TABLE discovery_queue
    ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'open'
    CHECK (status IN ('open', 'resolved', 'regressed'));

ALTER TABLE discovery_queue
    ADD COLUMN IF NOT EXISTS status_updated_at timestamptz;

CREATE INDEX IF NOT EXISTS discovery_queue_status_idx ON discovery_queue (status);
