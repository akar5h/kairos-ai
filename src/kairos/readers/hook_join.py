"""hook_join.py — Enrich envelope steps from hook_events via session+ordinal join (F1.2b).

Mirrors the ordinal-per-tool-name alignment used by ``transcript_join.py`` but
sources truth from the ``hook_events`` Postgres table instead of local JSONL
transcript files.  Works for remote sessions without local transcripts.

**Why no time-window guard?**
transcript_join uses a ±60 s window because a session JSONL spans multiple
traces in sequence — without windowing, calls from earlier/later traces would
bleed in.  hook_events rows are already per-session; the (session_id, seq)
primary key naturally scopes them to one session.  A single session may still
span multiple traces, but a given trace covers a contiguous ordinal slice —
ordinal alignment within that slice is sufficient; no time filter needed.

**Alignment rule:**
Within a session_id, for each tool_name, match the Nth envelope tool-step of
that name to the Nth hook_events row of that name (ordered by seq).  Unmatched
steps (no corresponding hook row) are left untouched.

**Why not reuse phoenix._correct_tool_errors_from_transcript / _enrich_…?**
Those functions accept ``wrapped: list[Any]`` (raw spans) and internally call
``transcript_join.tool_errors_from_transcript`` / ``tool_args_from_transcript``,
which re-extract session.id and locate the filesystem transcript.  They cannot
be redirected to a DB source without breaking their signature.  The patch logic
is mirrored minimally here (same fields, same provenance rules).

Public API::

    from kairos.readers.hook_join import (
        HookEventRow,
        fetch_hook_events_for_session,
        enrich_envelope_with_hooks,
    )
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kairos.log import get_logger

logger = get_logger(__name__)

# Event names that carry tool data (PostToolUse = success, PostToolUseFailure = error).
_TOOL_EVENT_NAMES = ("PostToolUse", "PostToolUseFailure")


# ── Data model ────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class HookEventRow:
    """One row from ``hook_events`` for a tool invocation."""

    session_id: str
    seq: int
    tool_name: str
    is_error: bool
    tool_input_redacted: dict[str, Any] | None
    """Redacted tool_use.input (already scrubbed by the hook before DB insert)."""
    tool_output: str | None


# ── DB fetch ──────────────────────────────────────────────────────────────────


def fetch_hook_events_for_session(session_id: str, dsn: str) -> list[HookEventRow]:
    """Fetch all PostToolUse / PostToolUseFailure rows for ``session_id``, ordered by seq.

    Filters to event_name IN ('PostToolUse', 'PostToolUseFailure') — only those
    rows carry tool_name / is_error / tool_input_redacted / tool_output.

    Returns an empty list on DB error (logs warning, never raises).
    """
    import psycopg  # local import — callers without psycopg don't pay the import
    from psycopg.rows import dict_row

    try:
        with psycopg.connect(dsn, row_factory=dict_row) as conn:
            rows = conn.execute(
                "SELECT session_id, seq, tool_name, is_error, "
                "       tool_input_redacted, tool_output "
                "FROM hook_events "
                "WHERE session_id = %s "
                "  AND event_name = ANY(%s) "
                "ORDER BY seq ASC",
                (session_id, list(_TOOL_EVENT_NAMES)),
            ).fetchall()
    except Exception:
        logger.warning(
            "hook_join.fetch_error",
            session_id=session_id,
            exc_info=True,
        )
        return []

    result: list[HookEventRow] = []
    for row in rows:
        tool_name = row.get("tool_name")
        if not isinstance(tool_name, str) or not tool_name:
            continue  # no tool name → skip (shouldn't happen for PostToolUse rows)
        raw_input = row.get("tool_input_redacted")
        tool_input: dict[str, Any] | None = raw_input if isinstance(raw_input, dict) else None
        result.append(
            HookEventRow(
                session_id=str(row["session_id"]),
                seq=int(row["seq"]),
                tool_name=tool_name,
                is_error=bool(row.get("is_error")),
                tool_input_redacted=tool_input,
                tool_output=row.get("tool_output"),
            )
        )

    return result


# ── Ordinal alignment ─────────────────────────────────────────────────────────


def _align_hook_events(
    steps: list[Any],  # list[Step]
    hook_rows: list[HookEventRow],
) -> dict[int, HookEventRow]:
    """Align envelope tool-steps to hook rows by ordinal per tool name.

    k-th envelope step named X ↔ k-th hook_events row named X (by seq).
    Returns {step_index: HookEventRow} for every aligned pair.  Steps with
    no corresponding hook row are absent from the result.
    """
    from kairos.models.enums import StepType  # local import

    # Group hook rows by tool name, preserving seq order.
    rows_by_name: dict[str, list[HookEventRow]] = {}
    for row in hook_rows:
        rows_by_name.setdefault(row.tool_name, []).append(row)

    counters: dict[str, int] = {}
    aligned: dict[int, HookEventRow] = {}

    for step in steps:
        if step.step_type != StepType.TOOL_CALL or not step.tool_name:
            continue
        name = step.tool_name
        k = counters.get(name, 0)
        counters[name] = k + 1
        pool = rows_by_name.get(name, [])
        if k < len(pool):
            aligned[step.step_index] = pool[k]

    return aligned


# ── Envelope patch ────────────────────────────────────────────────────────────


def enrich_envelope_with_hooks(
    envelope: Any,  # TraceEnvelope
    dsn: str,
) -> Any:  # TraceEnvelope
    """Enrich ``envelope`` tool steps with truth from hook_events rows.

    Patch logic (mirrors phoenix._correct_tool_errors_from_transcript +
    _enrich_tool_args_from_transcript, same provenance rules):

    * is_error=True → step.status = StepStatus.ERROR.  Steps already ERROR
      are not touched.  If step had status_source=NONE, stamp ATTR_SUCCESS
      (same provenance decision as the transcript path — the structured
      is_error field is the signal; ATTR_SUCCESS records it came from a
      structured source, not a textual scan).
    * tool_input_redacted → step.tool_args + step.tool_args_normalized
      (via normalize_args).  Only written when step.tool_args is currently
      empty (None or {}) — never overwrites existing args.
    * tool_output → step.tool_output.  Only written when step.tool_output
      is currently empty (None or "").  hook_events stores the real output.
    * Recomputes envelope.error_count after patching.

    Session-id extraction: reads from ``envelope.metadata["session_id"]``
    (populated by _collect_kairos_metadata via session.id span attribute).
    Falls back to ``envelope.session_id`` if metadata path is absent.

    Returns the same envelope object (mutated in place for step fields;
    error_count recomputed).  On any failure (no session_id, DB error, no
    rows) returns envelope unchanged.  Never raises.
    """
    from kairos.models.enums import StepStatus, StepStatusSource, StepType
    from kairos.normalization.arg_normalizer import normalize_args

    # ── Resolve session_id ────────────────────────────────────────────────
    session_id: str | None = None

    # Prefer metadata["session_id"] — that's where spans_to_envelope puts it.
    meta = getattr(envelope, "metadata", None)
    if isinstance(meta, dict):
        raw = meta.get("session_id")
        if isinstance(raw, str) and raw:
            session_id = raw

    # Fallback: envelope.session_id field (may be populated by other paths).
    if not session_id:
        raw2 = getattr(envelope, "session_id", None)
        if isinstance(raw2, str) and raw2:
            session_id = raw2

    if not session_id:
        logger.debug(
            "hook_join.no_session_id",
            trace_id=getattr(envelope, "trace_id", None),
            hint="session_id absent from envelope metadata and envelope.session_id — skipping hook enrichment",
        )
        return envelope

    # ── Fetch hook rows ───────────────────────────────────────────────────
    hook_rows = fetch_hook_events_for_session(session_id, dsn)
    if not hook_rows:
        logger.debug(
            "hook_join.no_hook_rows",
            session_id=session_id,
            trace_id=getattr(envelope, "trace_id", None),
        )
        return envelope

    # ── Align ─────────────────────────────────────────────────────────────
    steps = list(getattr(envelope, "steps", []) or [])
    if not steps:
        return envelope

    aligned = _align_hook_events(steps, hook_rows)
    if not aligned:
        logger.debug(
            "hook_join.no_aligned_steps",
            session_id=session_id,
            trace_id=getattr(envelope, "trace_id", None),
        )
        return envelope

    # ── Patch steps ───────────────────────────────────────────────────────
    corrected = 0
    enriched_args = 0
    enriched_output = 0

    for step in steps:
        if step.step_type != StepType.TOOL_CALL:
            continue
        row = aligned.get(step.step_index)
        if row is None:
            continue

        # is_error → status correction (same rule as transcript path).
        if row.is_error and step.status is not StepStatus.ERROR:
            step.status = StepStatus.ERROR
            if step.status_source is StepStatusSource.NONE:
                step.status_source = StepStatusSource.ATTR_SUCCESS
            corrected += 1

        # tool_args enrichment — only if step currently empty.
        if row.tool_input_redacted and not step.tool_args:
            step.tool_args = row.tool_input_redacted
            step.tool_args_normalized = normalize_args(row.tool_input_redacted)
            enriched_args += 1

        # tool_output enrichment — only if step currently empty.
        if row.tool_output and not step.tool_output:
            step.tool_output = row.tool_output
            enriched_output += 1

    # Recompute error_count whenever any correction was made.
    if corrected:
        envelope.error_count = sum(1 for s in steps if s.status is StepStatus.ERROR)

    logger.info(
        "hook_join.enriched",
        session_id=session_id,
        trace_id=getattr(envelope, "trace_id", None),
        hook_rows=len(hook_rows),
        aligned=len(aligned),
        corrected=corrected,
        enriched_args=enriched_args,
        enriched_output=enriched_output,
    )

    return envelope
