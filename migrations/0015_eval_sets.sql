-- Migration 0015: eval_sets table for P3.2 cluster → eval-set generation.
CREATE TABLE IF NOT EXISTS eval_sets (
    eval_set_id         text PRIMARY KEY,
    cluster_key         text NOT NULL,
    detector_version    text NOT NULL,
    frozen_at           timestamptz NOT NULL,
    held_in             jsonb NOT NULL,
    held_out            jsonb NOT NULL,
    discriminator_type  text NOT NULL,
    discriminator_config jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS eval_sets_cluster_key_idx ON eval_sets (cluster_key);
