-- Migration 0010: spans — raw OTLP span storage
--
-- Stores raw OTel spans as they arrive so analysis can read from Postgres
-- instead of fetching from Phoenix at analysis time (F1.1).
-- attributes/events/resource are JSONB so all OTel attribute types round-trip
-- faithfully through the _db_row_to_span adapter in readers/db.py.
--
-- Idempotent: CREATE TABLE IF NOT EXISTS + ON CONFLICT (trace_id, span_id) DO UPDATE
-- in ingest/spans.py ensures re-ingesting a trace is safe.

CREATE TABLE IF NOT EXISTS spans (
    trace_id       text        NOT NULL,
    span_id        text        NOT NULL,
    parent_span_id text,
    name           text        NOT NULL,
    start_time     timestamptz NOT NULL,
    end_time       timestamptz,
    status_code    text,
    attributes     jsonb       NOT NULL DEFAULT '{}',
    events         jsonb       NOT NULL DEFAULT '[]',
    resource       jsonb       NOT NULL DEFAULT '{}',
    source         text,
    ingested_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (trace_id, span_id)
);

-- Index for trace-level lookups (the primary read pattern)
CREATE INDEX IF NOT EXISTS spans_trace_id_idx ON spans (trace_id);
