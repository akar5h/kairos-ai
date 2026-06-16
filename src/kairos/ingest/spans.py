"""Persist raw OTLP spans to the Postgres ``spans`` table (F1.1).

Accepts spans in the duck-typed shape produced by ``_phoenix_dict_to_span``
(i.e. ``_PhoenixSpan`` dataclass instances) OR plain Phoenix span dicts.
Uses psycopg3, DSN supplied by caller (never reads env directly — caller
handles that via ``kairos.loop.db._dsn()`` or passes KAIROS_PG_DSN explicitly).

Public API::

    from kairos.ingest.spans import persist_spans

    n = persist_spans(spans, dsn="postgresql://...")
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb


def _ns_to_dt(ns: int) -> datetime:
    """Convert nanoseconds-since-epoch to a timezone-aware UTC datetime."""
    return datetime.fromtimestamp(ns / 1_000_000_000, tz=UTC)


def _span_to_row(span: Any) -> dict[str, Any]:
    """Extract DB column values from a duck-typed span (``_PhoenixSpan`` shape).

    Accepts both ``_PhoenixSpan`` dataclass objects and raw Phoenix dicts.
    """
    if isinstance(span, dict):
        # Raw Phoenix dict — extract directly.
        ctx = span.get("context") or {}
        trace_id: str = ctx.get("trace_id") or ""
        span_id: str = ctx.get("span_id") or ""
        parent_span_id: str | None = span.get("parent_id")
        name: str = span.get("name", "")
        start_ns: int = 0
        end_ns: int = 0
        raw_start = span.get("start_time")
        raw_end = span.get("end_time")
        if raw_start:
            from datetime import datetime as _dt  # noqa: PLC0415

            dt = _dt.fromisoformat(raw_start)
            start_ns = int(dt.timestamp() * 1_000_000_000)
        if raw_end:
            from datetime import datetime as _dt  # noqa: PLC0415

            dt = _dt.fromisoformat(raw_end)
            end_ns = int(dt.timestamp() * 1_000_000_000)
        status_code: str | None = span.get("status_code")
        attributes: dict[str, Any] = dict(span.get("attributes") or {})
        raw_events = span.get("events") or []
        events: list[dict[str, Any]] = [
            {"name": ev.get("name", ""), "attributes": dict(ev.get("attributes") or {})}
            for ev in raw_events
        ]
        resource: dict[str, Any] = {}
    else:
        # _PhoenixSpan dataclass (duck-typed OTel shape).
        ctx = getattr(span, "context", None)
        trace_id = f"{ctx.trace_id:032x}" if ctx is not None else ""
        span_id = f"{ctx.span_id:016x}" if ctx is not None else ""
        parent = getattr(span, "parent", None)
        parent_span_id = f"{parent.span_id:016x}" if parent is not None else None
        name = getattr(span, "name", "")
        start_ns = getattr(span, "start_time", 0) or 0
        end_ns = getattr(span, "end_time", 0) or 0
        status_obj = getattr(span, "status", None)
        status_code = status_obj.status_code.name if status_obj is not None else None
        attributes = dict(getattr(span, "attributes", None) or {})
        raw_span_events = getattr(span, "events", None) or []
        events = [
            {"name": getattr(ev, "name", ""), "attributes": dict(getattr(ev, "attributes", None) or {})}
            for ev in raw_span_events
        ]
        resource_obj = getattr(span, "resource", None)
        resource = dict(getattr(resource_obj, "attributes", None) or {}) if resource_obj is not None else {}

    start_dt = _ns_to_dt(start_ns) if start_ns else None
    end_dt = _ns_to_dt(end_ns) if end_ns else None

    # Extract session_id from the session.id attribute (set by CC exporter).
    # None when the span carries no session context.
    session_id: str | None = attributes.get("session.id") or None

    return {
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": parent_span_id,
        "name": name,
        "start_time": start_dt,
        "end_time": end_dt,
        "status_code": status_code,
        "attributes": Jsonb(attributes),
        "events": Jsonb(events),
        "resource": Jsonb(resource),
        "session_id": session_id,
    }


_UPSERT_SQL = """
INSERT INTO spans
    (trace_id, span_id, parent_span_id, name,
     start_time, end_time, status_code,
     attributes, events, resource, source, session_id)
VALUES
    (%(trace_id)s, %(span_id)s, %(parent_span_id)s, %(name)s,
     %(start_time)s, %(end_time)s, %(status_code)s,
     %(attributes)s, %(events)s, %(resource)s, %(source)s, %(session_id)s)
ON CONFLICT (trace_id, span_id) DO UPDATE SET
    parent_span_id = EXCLUDED.parent_span_id,
    name           = EXCLUDED.name,
    start_time     = EXCLUDED.start_time,
    end_time       = EXCLUDED.end_time,
    status_code    = EXCLUDED.status_code,
    attributes     = EXCLUDED.attributes,
    events         = EXCLUDED.events,
    resource       = EXCLUDED.resource,
    source         = EXCLUDED.source,
    session_id     = EXCLUDED.session_id,
    ingested_at    = now()
"""


def persist_spans(spans: list[Any], dsn: str, *, source: str | None = None) -> int:
    """Upsert ``spans`` into the ``spans`` table.

    Args:
        spans: List of duck-typed span objects (``_PhoenixSpan``) or raw
               Phoenix span dicts.
        dsn:   libpq connection string.
        source: Optional label for the ingestion source (e.g. ``"otlp_grpc"``).

    Returns:
        Number of rows upserted.
    """
    if not spans:
        return 0

    rows = [_span_to_row(s) for s in spans]
    for row in rows:
        row["source"] = source

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.executemany(_UPSERT_SQL, rows)
        conn.commit()

    return len(rows)
