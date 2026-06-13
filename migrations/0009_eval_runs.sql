-- Migration 0009: eval_runs — Kairos self-improvement time series
--
-- Stores one row per eval_run execution (before or after a change).
-- panel is the full MetricPanel JSON: outcome metrics, per-detector metrics,
-- aggregates. No raw tool outputs or PII — aggregated metrics only.
--
-- Idempotent: CREATE TABLE IF NOT EXISTS + ON CONFLICT (run_id) DO NOTHING
-- in store.py ensures re-running a migration is safe.

CREATE TABLE IF NOT EXISTS eval_runs (
    run_id      text        PRIMARY KEY,        -- sha256(ref_full|corpus_hash|config_hash|ts)[:32]
    ref         text        NOT NULL,           -- git ref evaluated (may be short SHA, branch, tag)
    ref_full    text        NOT NULL,           -- resolved full 40-char SHA
    corpus_hash text        NOT NULL,           -- stable ruler hash (corpus_hash field)
    config_hash text,                           -- sha256 of context.yaml used; NULL if not tracked
    k           int         NOT NULL DEFAULT 2, -- number of evaluation runs (nondeterminism check)
    panel       jsonb       NOT NULL,           -- MetricPanel.to_dict() serialized
    verdict     text        NOT NULL,           -- "PASS" | "REGRESSED" | "NONDETERMINISM_ERROR" | "run"
    ts          timestamptz NOT NULL DEFAULT now()
);

-- Index for corpus_hash queries (compare across refs at same corpus version)
CREATE INDEX IF NOT EXISTS eval_runs_corpus_hash_idx ON eval_runs (corpus_hash, ts DESC);

-- Index for ref queries (find all runs at a given ref)
CREATE INDEX IF NOT EXISTS eval_runs_ref_idx ON eval_runs (ref, ts DESC);
