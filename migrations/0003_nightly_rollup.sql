-- Migration 0003: nightly rollup time-series table.
--
-- Small table — one row per (night_id, workflow, agent).
-- This is what the dashboard reads; kept deliberately narrow.
--
-- config_hash discipline (baseline_break):
--   When config_hash changes between two consecutive nights,
--   the loop runner writes a sentinel row with a 'baseline_break'
--   marker so the dashboard renders a vertical discontinuity rule
--   instead of drawing a misleading trend line across the boundary.
--   finding_counts (jsonb) maps detector → count for that night.

CREATE TABLE IF NOT EXISTS nightly_rollup (
    night_id                date    NOT NULL,
    workflow                text    NOT NULL,
    agent                   text    NOT NULL,
    units                   int     NOT NULL DEFAULT 0,
    traces                  int     NOT NULL DEFAULT 0,
    outcome_rate            real    NOT NULL DEFAULT 0.0,
    struggle_p50            real    NOT NULL DEFAULT 0.0,
    struggle_p90            real    NOT NULL DEFAULT 0.0,
    coordination_waste_rate real    NOT NULL DEFAULT 0.0,
    tokens_per_unit         real    NOT NULL DEFAULT 0.0,
    finding_counts          jsonb   NOT NULL DEFAULT '{}',
    config_hash             text    NOT NULL,
    PRIMARY KEY (night_id, workflow, agent)
);
