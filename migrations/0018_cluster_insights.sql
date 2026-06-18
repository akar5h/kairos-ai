-- Migration 0018: cluster_insights table for P4.1 semantic cluster labeling.
--
-- Stores LLM-generated insight records per cluster_key.
-- One row per labeling call; multiple rows per cluster_key are allowed
-- (e.g. after weekly re-labeling), ordered by created_at DESC.
-- approved_at NULL = pending human review (or auto-approved if auto_approve=true).

CREATE TABLE cluster_insights (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    cluster_key     text NOT NULL,
    pattern_name    text,
    description     text,
    discriminator_hint text,
    root_cause      text,
    confidence      float,
    is_coherent     boolean,
    auto_approve    boolean DEFAULT false,
    approved_at     timestamptz,
    approved_by     text,
    model_used      text,
    created_at      timestamptz DEFAULT now()
);

CREATE INDEX ON cluster_insights (cluster_key);
CREATE INDEX ON cluster_insights (approved_at) WHERE approved_at IS NULL;
