-- Migration 0011: hook_events — CC hook capture storage (F1.2)
--
-- Stores Claude Code hook events (PostToolUse, PostToolUseFailure,
-- SessionStart, SessionEnd) as they arrive via the kairos_hook.py spool
-- and hook_uploader.py drain.  Full redacted payload is retained in
-- payload_redacted so no data is lost; structured columns allow targeted
-- queries without JSON extraction.
--
-- Primary key (session_id, seq) uses a surrogate sequence per session
-- because tool_use_id is absent for SessionStart / SessionEnd events.
--
-- Index on (session_id, tool_use_id) is the future span-join key:
-- envelopes will look up hook_events rows by tool_use_id to enrich
-- OTel spans with real args / outputs / is_error.

CREATE TABLE IF NOT EXISTS hook_events (
    session_id          text        NOT NULL,
    seq                 bigint      NOT NULL,
    tool_use_id         text,
    event_name          text        NOT NULL,
    tool_name           text,
    tool_input_redacted jsonb,
    tool_output         text,
    is_error            boolean,
    permission_mode     text,
    agent_id            text,
    agent_type          text,
    payload_redacted    jsonb       NOT NULL DEFAULT '{}',
    occurred_at         timestamptz NOT NULL,
    ingested_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, seq)
);

-- Join key to OTel spans: look up by (session_id, tool_use_id).
CREATE INDEX IF NOT EXISTS hook_events_session_tool_use_idx
    ON hook_events (session_id, tool_use_id)
    WHERE tool_use_id IS NOT NULL;
