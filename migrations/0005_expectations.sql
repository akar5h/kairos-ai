-- Migration 0005: learned expectations table.
--
-- Stores per-workflow tool presence rates learned from clean traces.
-- Expectations are LEARNED, never declared (no config burden).
-- confirmed=true means an owner label validated this expectation.
-- Rows with confirmed=false are candidates surfaced for labeling.
--
-- Populated by the LEARN stage of the nightly loop (Day 10+).
-- Zero user declaration, zero config conflict — the doubt-driven-development
-- silent-skip pattern is caught this way, not by hand-written rules.

CREATE TABLE IF NOT EXISTS expectations (
    workflow        text    NOT NULL,
    tool            text    NOT NULL,
    presence_rate   real    NOT NULL DEFAULT 0.0,
    confirmed       bool    NOT NULL DEFAULT false,
    first_seen      date    NOT NULL,
    PRIMARY KEY (workflow, tool)
);
