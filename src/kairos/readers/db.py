"""DB-backed span reader — reads raw spans from Postgres, returns TraceEnvelope (F1.1).

Replaces the Phoenix HTTP fetch with a local Postgres read. The span rows
are converted back to the same duck-typed ``_PhoenixSpan`` shape that
``spans_to_envelope`` already accepts, so no changes to the analysis path
are needed.

Public API::

    from kairos.readers.db import fetch_spans_from_db, fetch_envelope_from_db

    spans   = fetch_spans_from_db("0123...", dsn="postgresql://...")
    envelope = fetch_envelope_from_db("0123...", dsn="postgresql://...")
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import psycopg
from psycopg.rows import dict_row

from kairos.readers.phoenix import _PhoenixSpan, _SpanContext, _SpanEvent, _SpanParent, _SpanResource, _SpanStatus

if TYPE_CHECKING:
    from kairos.models.trace import TraceEnvelope

from opentelemetry.trace import StatusCode  # noqa: TC002

_STATUS_MAP: dict[str, StatusCode] = {
    "OK": StatusCode.OK,
    "ERROR": StatusCode.ERROR,
    "UNSET": StatusCode.UNSET,
}

_DT_EPOCH_NS_FACTOR = 1_000_000_000


def _db_row_to_span(row: dict[str, Any]) -> _PhoenixSpan:
    """Convert a ``spans`` DB row back to the duck-typed ``_PhoenixSpan`` shape.

    Mirrors ``_phoenix_dict_to_span`` so ``spans_to_envelope`` sees an
    identical surface regardless of whether spans came from Phoenix or Postgres.
    """
    trace_id_hex: str = row.get("trace_id") or "0" * 32
    span_id_hex: str = row.get("span_id") or "0" * 16
    parent_hex: str | None = row.get("parent_span_id")

    status_str = (row.get("status_code") or "UNSET").upper()
    status_code = _STATUS_MAP.get(status_str, StatusCode.UNSET)

    raw_events: list[dict[str, Any]] = row.get("events") or []
    events = [
        _SpanEvent(name=ev.get("name", ""), attributes=dict(ev.get("attributes") or {}))
        for ev in raw_events
    ]

    # Timestamps stored as timestamptz; psycopg returns datetime objects.
    import datetime  # noqa: PLC0415

    def _dt_to_ns(dt: Any) -> int:
        if dt is None:
            return 0
        if isinstance(dt, datetime.datetime):
            return int(dt.timestamp() * _DT_EPOCH_NS_FACTOR)
        # Fallback: already an int (nanoseconds).
        return int(dt)

    resource_attrs: dict[str, Any] = dict(row.get("resource") or {})

    return _PhoenixSpan(
        name=row.get("name", ""),
        attributes=dict(row.get("attributes") or {}),
        context=_SpanContext(
            trace_id=int(trace_id_hex.replace("-", ""), 16),
            span_id=int(span_id_hex.replace("-", ""), 16),
        ),
        parent=_SpanParent(span_id=int(parent_hex.replace("-", ""), 16)) if parent_hex else None,
        start_time=_dt_to_ns(row.get("start_time")),
        end_time=_dt_to_ns(row.get("end_time")),
        status=_SpanStatus(status_code=status_code),
        events=events,
        resource=_SpanResource(attributes=resource_attrs),
    )


def fetch_spans_from_db(trace_id: str, dsn: str) -> list[_PhoenixSpan]:
    """Read all spans for ``trace_id`` from the ``spans`` table.

    Returns a list of ``_PhoenixSpan`` objects in the same duck-typed shape
    that ``spans_to_envelope`` accepts (ordered by ``start_time`` ascending).
    """
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        rows = conn.execute(
            "SELECT trace_id, span_id, parent_span_id, name, "
            "       start_time, end_time, status_code, "
            "       attributes, events, resource "
            "FROM spans WHERE trace_id = %s ORDER BY start_time ASC",
            (trace_id,),
        ).fetchall()

    return [_db_row_to_span(row) for row in rows]


def fetch_envelope_from_db(
    trace_id: str,
    dsn: str,
    *,
    correlation_key_attr: str | None = None,
) -> TraceEnvelope:
    """Fetch spans from DB and return a ``TraceEnvelope``.

    Drop-in replacement for ``PhoenixReader.fetch_envelope()`` — reads from
    Postgres instead of Phoenix HTTP.
    """
    from kairos.readers.phoenix import spans_to_envelope  # noqa: PLC0415

    spans = fetch_spans_from_db(trace_id, dsn)
    return spans_to_envelope(spans, correlation_key_attr=correlation_key_attr)
