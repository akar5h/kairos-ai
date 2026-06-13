-- Migration 0006: discovery queue table.
--
-- Holds anomaly candidates and expectation-miss candidates surfaced
-- by the DISCOVER stage for owner labeling.
-- Discovery never fires findings itself (unlabeled = unmeasured).
-- kind: 'anomaly' | 'expectation_miss'
-- features (jsonb): the feature vector that triggered surfacing
--   (tool-sequence shape, token z, latency z, etc.).
-- labeled=false means awaiting owner review in the review app.

CREATE TABLE IF NOT EXISTS discovery_queue (
    id          text        PRIMARY KEY,
    night_id    date        NOT NULL,
    kind        text        NOT NULL,
    trace_id    text        NOT NULL,
    features    jsonb       NOT NULL DEFAULT '{}',
    labeled     bool        NOT NULL DEFAULT false
);
