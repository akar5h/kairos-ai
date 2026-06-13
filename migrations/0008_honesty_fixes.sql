-- Migration 0008: three honesty fixes for nightly_rollup (Day 11).
--
-- Fix 1 — outcome_rate is now nullable.
--   The "unmapped" pseudo-workflow has NO contract to pass/fail.
--   Storing outcome_rate=0.0 for unmapped rows was a lie ("everything failed").
--   NULL explicitly means "no contract, no rate" and is excluded from all
--   outcome aggregations and dashboard outcome plots.
--
-- Fix 2 — coordination_waste_rate → coordination_waste_per_trace.
--   The old column name implied a 0–1 rate, but the stored values were the
--   average number of coordination_waste findings per trace in the cell
--   (values of 3.0, 6.0, 13.0 appeared in the live series — clearly not
--   a fraction).  The rename to coordination_waste_per_trace makes the
--   semantics honest: mean coordination_waste findings per trace.
--   Rationale for choice (a) over (b): (a) is a simple rename with zero
--   information loss; (b) would require discarding the magnitude information
--   and mis-characterise cells with many coordination events as equivalent
--   to cells with just one.  Rename is cheaper and more honest.
--
-- Fix 3 (schema side) — no column change needed for agent bucketing.
--   UUID agent names are bucketed to "paperclip-claude-other" at write time
--   in persist.py; the column type (text) is unchanged.

-- Fix 1: make outcome_rate nullable.
ALTER TABLE nightly_rollup ALTER COLUMN outcome_rate DROP NOT NULL;
ALTER TABLE nightly_rollup ALTER COLUMN outcome_rate DROP DEFAULT;

-- Fix 2: rename the column.
ALTER TABLE nightly_rollup
    RENAME COLUMN coordination_waste_rate TO coordination_waste_per_trace;
