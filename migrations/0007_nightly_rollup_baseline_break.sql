-- Migration 0007: add baseline_break flag to nightly_rollup.
--
-- When config_hash changes between consecutive nights, persist.py writes a
-- sentinel row with baseline_break=true so the Day-11 dashboard renders a
-- visible vertical discontinuity rule, never drawing a misleading trend line
-- across the hash boundary.
--
-- Deltas MUST only be computed within a single config_hash.  This column
-- carries the discontinuity signal at the row level so callers can filter it.

ALTER TABLE nightly_rollup
    ADD COLUMN IF NOT EXISTS baseline_break bool NOT NULL DEFAULT false;
