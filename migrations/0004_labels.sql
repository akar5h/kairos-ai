-- Migration 0004: owner labels table (the flywheel's memory).
--
-- Stores owner-supplied labels from the review app (Day 12+).
-- label_class: the detector class or discovery cluster being labeled.
-- verdict: 'tp' | 'fp' | 'fn' — ground truth for precision measurement.

CREATE TABLE IF NOT EXISTS labels (
    id          text        PRIMARY KEY,
    trace_id    text        NOT NULL,
    question    text        NOT NULL,
    answer      text        NOT NULL,
    verdict     text        NOT NULL,
    label_class text        NOT NULL,
    ts          timestamptz NOT NULL DEFAULT now()
);
