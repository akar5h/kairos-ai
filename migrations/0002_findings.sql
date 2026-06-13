-- Migration 0002: raw findings table.
--
-- Grows with traffic; partition by night_id when it gets large.
-- Idempotent upsert key: (night_id, trace_id, detector) — re-running a
-- night never double-counts (ledger-cursor discipline).
--
-- config_hash discipline (baseline_break):
--   Deltas MUST only be computed within a single config_hash.
--   A config change writes a baseline_break row in nightly_rollup so the
--   dashboard shows a visible discontinuity, never a fake trend.
--   persist.py enforces this contract before any INSERT.
--
-- Security contract (enforced by persist.py, Day 10):
--   evidence_steps holds step indices only (int[]), never raw tool output.
--   tokens is a scalar count.
--   No PII or secret material is written here; full tool outputs are
--   redacted before this table is touched.

CREATE TABLE IF NOT EXISTS findings (
    night_id        date            NOT NULL,
    trace_id        text            NOT NULL,
    unit_id         text            NOT NULL,
    workflow        text            NOT NULL,
    agent           text            NOT NULL,
    detector        text            NOT NULL,
    severity        text            NOT NULL,
    evidence_steps  int[]           NOT NULL DEFAULT '{}',
    tokens          int             NOT NULL DEFAULT 0,
    struggle        real            NOT NULL DEFAULT 0.0,
    outcome         text            NOT NULL,
    config_hash     text            NOT NULL,
    ingested_at     timestamptz     NOT NULL DEFAULT now(),
    PRIMARY KEY (night_id, trace_id, detector)
);
