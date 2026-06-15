"""hook_uploader.py — Drain hook spool files → hook_events table (F1.2).

Reads per-session JSONL spool files written by hooks/kairos_hook.py and
upserts rows into the ``hook_events`` Postgres table (migration 0011).

Public API::

    from kairos.ingest.hook_uploader import drain_spool

    n = drain_spool(spool_dir=Path("~/.kairos/spool"), dsn="postgresql://...")

Design notes
------------
* **seq per session** — each spool file carries events in arrival order;
  we assign a monotone seq = (max existing seq for this session) + 1 for
  each new event.  This makes the uploader safe to run multiple times on
  a growing spool file.
* **corrupt-line skip** — malformed JSON lines are counted and skipped;
  they never crash the uploader.
* **drained-file handling** — after all rows from a file are successfully
  inserted, the file is atomically renamed to ``<name>.done``.  Re-running
  the uploader will not re-upload `.done` files.
* **DSN never hardcoded** — caller supplies it; see kairos.loop.db._dsn().
* Uses psycopg3, mirrors the upsert pattern in ingest/spans.py.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

logger = logging.getLogger(__name__)

_UPSERT_SQL = """
INSERT INTO hook_events
    (session_id, seq, tool_use_id, event_name, tool_name,
     tool_input_redacted, tool_output, is_error,
     permission_mode, agent_id, agent_type,
     payload_redacted, occurred_at)
VALUES
    (%(session_id)s, %(seq)s, %(tool_use_id)s, %(event_name)s, %(tool_name)s,
     %(tool_input_redacted)s, %(tool_output)s, %(is_error)s,
     %(permission_mode)s, %(agent_id)s, %(agent_type)s,
     %(payload_redacted)s, %(occurred_at)s)
ON CONFLICT (session_id, seq) DO UPDATE SET
    tool_use_id         = EXCLUDED.tool_use_id,
    event_name          = EXCLUDED.event_name,
    tool_name           = EXCLUDED.tool_name,
    tool_input_redacted = EXCLUDED.tool_input_redacted,
    tool_output         = EXCLUDED.tool_output,
    is_error            = EXCLUDED.is_error,
    permission_mode     = EXCLUDED.permission_mode,
    agent_id            = EXCLUDED.agent_id,
    agent_type          = EXCLUDED.agent_type,
    payload_redacted    = EXCLUDED.payload_redacted,
    occurred_at         = EXCLUDED.occurred_at,
    ingested_at         = now()
"""


def _parse_occurred_at(raw: Any) -> datetime:
    """Parse an ISO-8601 string to an aware datetime; fall back to now()."""
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            pass
    return datetime.now(tz=UTC)


def _record_to_row(record: dict[str, Any], seq: int) -> dict[str, Any]:
    """Map a spool record dict to a hook_events DB row dict."""
    tool_input = record.get("tool_input_redacted")
    payload = record.get("payload_redacted") or {}

    return {
        "session_id": str(record.get("session_id") or "unknown"),
        "seq": seq,
        "tool_use_id": record.get("tool_use_id") or None,
        "event_name": str(record.get("event_name") or ""),
        "tool_name": record.get("tool_name") or None,
        "tool_input_redacted": Jsonb(tool_input) if isinstance(tool_input, dict) else None,
        "tool_output": record.get("tool_output") or None,
        "is_error": record.get("is_error"),
        "permission_mode": record.get("permission_mode") or None,
        "agent_id": record.get("agent_id") or None,
        "agent_type": record.get("agent_type") or None,
        "payload_redacted": Jsonb(payload if isinstance(payload, dict) else {}),
        "occurred_at": _parse_occurred_at(record.get("occurred_at")),
    }


def _max_seq_for_session(conn: psycopg.Connection[Any], session_id: str) -> int:
    """Return the highest seq already uploaded for this session_id (or 0)."""
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) FROM hook_events WHERE session_id = %s",
        (session_id,),
    ).fetchone()
    return int(row[0]) if row else 0


def _drain_file(path: Path, conn: psycopg.Connection[Any]) -> int:
    """Upload all rows from one spool file and rename it to .done.

    Corrupt / unparseable lines are skipped with a warning log.
    Returns the count of rows successfully upserted.
    """
    records: list[dict[str, Any]] = []
    skipped = 0

    with path.open(encoding="utf-8", errors="replace") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                logger.warning(
                    "hook_uploader.corrupt_line",
                    extra={"path": str(path), "lineno": lineno},
                )
                skipped += 1
                continue
            if not isinstance(rec, dict):
                skipped += 1
                continue
            records.append(rec)

    if skipped:
        logger.warning(
            "hook_uploader.skipped_lines",
            extra={"path": str(path), "count": skipped},
        )

    if not records:
        # Nothing to upload — still mark done so we don't re-scan.
        _mark_done(path)
        return 0

    # Group by session_id (spool files are per-session, but be defensive).
    by_session: dict[str, list[dict[str, Any]]] = {}
    for rec in records:
        sid = str(rec.get("session_id") or "unknown")
        by_session.setdefault(sid, []).append(rec)

    total = 0
    with conn.cursor() as cur:
        for session_id, session_records in by_session.items():
            seq = _max_seq_for_session(conn, session_id)
            rows = []
            for rec in session_records:
                seq += 1
                rows.append(_record_to_row(rec, seq))
            cur.executemany(_UPSERT_SQL, rows)
            total += len(rows)
    conn.commit()

    _mark_done(path)
    return total


def _mark_done(path: Path) -> None:
    """Atomically rename spool file to <name>.done."""
    done_path = path.with_suffix(path.suffix + ".done")
    path.rename(done_path)


def drain_spool(spool_dir: Path, dsn: str) -> int:
    """Drain all pending spool files in ``spool_dir`` into the ``hook_events`` table.

    Args:
        spool_dir: Directory containing ``<session_id>.jsonl`` spool files.
                   Files already renamed to ``.jsonl.done`` are skipped.
        dsn:       libpq connection string (never read from env here — caller
                   is responsible for supplying it from KAIROS_PG_DSN).

    Returns:
        Total number of rows upserted across all files this call.
    """
    spool_path = Path(spool_dir).expanduser()
    if not spool_path.exists():
        return 0

    files = sorted(spool_path.glob("*.jsonl"))
    if not files:
        return 0

    total = 0
    with psycopg.connect(dsn) as conn:
        for file in files:
            try:
                total += _drain_file(file, conn)
            except Exception:
                logger.exception(
                    "hook_uploader.file_error",
                    extra={"path": str(file)},
                )
                # Skip this file; don't let one bad file abort the whole drain.

    return total
