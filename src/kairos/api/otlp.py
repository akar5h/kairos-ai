"""OTLP/HTTP ingest router for Kairos (F1.4).

CC exports OTLP to ``http://localhost:4318`` with protocol ``http/protobuf``
(set by install.sh).  The CC exporter POSTs a protobuf-encoded
``ExportTraceServiceRequest`` to ``/v1/traces`` with
``Content-Type: application/x-protobuf``.

This router receives that payload, maps OTLP ResourceSpans → _PhoenixSpan,
and calls ``persist_spans`` to write to Postgres.

Key design decisions
--------------------
- Never 500 on a malformed batch.  A telemetry receiver must never break the
  producer (mirrors the hook's ethos).  Log + skip bad spans, persist what's
  valid, return partial-success.
- Protobuf AND JSON are supported (``application/json`` for compatibility).
- AnyValue flattening: string/int/double/bool unwrapped; array recursed;
  kvlist recursed to dict; bytes hex-encoded; missing oneof → None.
- OTLP trace_id/span_id come as raw bytes (16 and 8 bytes respectively);
  _SpanContext expects ints (the _span_to_row formatter renders them as hex).
- Resource attributes live on ResourceSpans.resource and are carried on the
  _SpanResource passed to _PhoenixSpan.
- Span events (name + attributes) are fully preserved; they carry
  tool.output content when OTEL_LOG_TOOL_CONTENT=1.
"""

from __future__ import annotations

import logging
from typing import Any

import fastapi
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from opentelemetry.proto.trace.v1.trace_pb2 import Span as OtlpSpan
from opentelemetry.proto.trace.v1.trace_pb2 import Status as OtlpStatus
from opentelemetry.trace import StatusCode

from kairos.ingest.spans import persist_spans
from kairos.loop.db import _dsn
from kairos.readers.phoenix import (
    _PhoenixSpan,
    _SpanContext,
    _SpanEvent,
    _SpanParent,
    _SpanResource,
    _SpanStatus,
)

logger = logging.getLogger(__name__)

router = fastapi.APIRouter()

# Content-Type values for OTLP/HTTP
_CT_PROTO = "application/x-protobuf"
_CT_JSON = "application/json"

# OTLP Status.StatusCode → OpenTelemetry SDK StatusCode
_OTLP_STATUS_MAP: dict[int, StatusCode] = {
    OtlpStatus.STATUS_CODE_UNSET: StatusCode.UNSET,
    OtlpStatus.STATUS_CODE_OK: StatusCode.OK,
    OtlpStatus.STATUS_CODE_ERROR: StatusCode.ERROR,
}


def _flatten_any_value(av: Any) -> Any:
    """Flatten an OTLP ``AnyValue`` proto to a plain Python value.

    Recursively handles array_value and kvlist_value.
    Missing oneof (empty AnyValue) returns None.
    """
    which = av.WhichOneof("value")
    if which == "string_value":
        return av.string_value
    if which == "int_value":
        return av.int_value
    if which == "double_value":
        return av.double_value
    if which == "bool_value":
        return av.bool_value
    if which == "bytes_value":
        return av.bytes_value.hex()
    if which == "array_value":
        return [_flatten_any_value(v) for v in av.array_value.values]
    if which == "kvlist_value":
        return {kv.key: _flatten_any_value(kv.value) for kv in av.kvlist_value.values}
    return None


def _flatten_attributes(kv_list: Any) -> dict[str, Any]:
    """Convert a repeated ``KeyValue`` proto list to a plain dict."""
    return {kv.key: _flatten_any_value(kv.value) for kv in kv_list}


def _otlp_span_to_phoenix(
    otlp_span: OtlpSpan,
    resource_attrs: dict[str, Any],
) -> _PhoenixSpan:
    """Map a single OTLP ``Span`` + its resource attributes to a ``_PhoenixSpan``.

    Bytes IDs
    ---------
    OTLP trace_id is 16 bytes; span_id is 8 bytes.  We convert via
    ``int.from_bytes(b, 'big')`` — _span_to_row formats them as 32/16-char
    hex strings with ``f"{ctx.trace_id:032x}"`` / ``f"{ctx.span_id:016x}"``.

    Resource attributes
    -------------------
    Carried on the _SpanResource so the DB ``resource`` column is populated.
    genai_mapping currently ignores resource, but F1.5+ may use it.
    """
    # IDs: bytes → int (big-endian, matching hex representation)
    trace_id_int = int.from_bytes(otlp_span.trace_id, "big") if otlp_span.trace_id else 0
    span_id_int = int.from_bytes(otlp_span.span_id, "big") if otlp_span.span_id else 0
    parent_span_id_int = (
        int.from_bytes(otlp_span.parent_span_id, "big")
        if otlp_span.parent_span_id
        else None
    )

    # Status
    otlp_code = otlp_span.status.code if otlp_span.HasField("status") else OtlpStatus.STATUS_CODE_UNSET
    sdk_code = _OTLP_STATUS_MAP.get(otlp_code, StatusCode.UNSET)
    status_msg = otlp_span.status.message or None

    # Attributes
    span_attrs = _flatten_attributes(otlp_span.attributes)

    # Events (preserve fully — may carry tool.output content)
    events = [
        _SpanEvent(
            name=ev.name,
            attributes=_flatten_attributes(ev.attributes),
        )
        for ev in otlp_span.events
    ]

    return _PhoenixSpan(
        name=otlp_span.name,
        attributes=span_attrs,
        context=_SpanContext(trace_id=trace_id_int, span_id=span_id_int),
        parent=_SpanParent(span_id=parent_span_id_int) if parent_span_id_int is not None else None,
        start_time=otlp_span.start_time_unix_nano,
        end_time=otlp_span.end_time_unix_nano,
        status=_SpanStatus(status_code=sdk_code, description=status_msg),
        events=events,
        resource=_SpanResource(attributes=resource_attrs),
    )


def _decode_request(body: bytes, content_type: str) -> ExportTraceServiceRequest | None:
    """Decode the raw HTTP body into an ``ExportTraceServiceRequest``.

    Supports ``application/x-protobuf`` (primary) and ``application/json``
    (fallback for some CC configs).  Returns None on decode failure.
    """
    req = ExportTraceServiceRequest()
    try:
        if content_type.startswith(_CT_JSON):
            from google.protobuf import json_format  # type: ignore[import-untyped]  # noqa: PLC0415
            json_format.Parse(body, req)
        else:
            # Default: treat as protobuf even if Content-Type is unexpected.
            req.ParseFromString(body)
    except Exception:
        logger.exception("otlp.decode_failed content_type=%s body_len=%d", content_type, len(body))
        return None
    return req


def _map_resource_spans(req: ExportTraceServiceRequest) -> tuple[list[_PhoenixSpan], int]:
    """Map all ResourceSpans in a request to _PhoenixSpan instances.

    Returns ``(mapped_spans, skip_count)`` — spans that raised during mapping
    are counted in skip_count so the partial-success response can report them.
    """
    mapped: list[_PhoenixSpan] = []
    skipped = 0

    for rs in req.resource_spans:
        resource_attrs = _flatten_attributes(rs.resource.attributes) if rs.HasField("resource") else {}
        for ss in rs.scope_spans:
            for otlp_span in ss.spans:
                try:
                    phoenix_span = _otlp_span_to_phoenix(otlp_span, resource_attrs)
                    mapped.append(phoenix_span)
                except Exception:
                    logger.exception(
                        "otlp.span_map_failed span_id=%s",
                        otlp_span.span_id.hex() if otlp_span.span_id else "?",
                    )
                    skipped += 1

    return mapped, skipped


@router.post("/v1/traces", tags=["otlp"])
async def ingest_traces(request: fastapi.Request) -> fastapi.Response:
    """OTLP/HTTP trace ingest endpoint.

    Accepts ``application/x-protobuf`` (primary) and ``application/json``.
    Never returns 5xx — logs errors, persists what's valid, returns
    ``ExportTraceServiceResponse`` (empty partial-success on any failure).

    Returns 200 with a serialized ``ExportTraceServiceResponse``.
    """
    body = await request.body()
    content_type = request.headers.get("content-type", _CT_PROTO)

    # Decode
    req = _decode_request(body, content_type)
    if req is None:
        # Malformed body — return empty partial-success, nothing persisted.
        logger.warning("otlp.malformed_body content_type=%s body_len=%d", content_type, len(body))
        resp_proto = ExportTraceServiceResponse()
        return fastapi.Response(
            content=resp_proto.SerializeToString(),
            media_type=_CT_PROTO,
        )

    # Map
    mapped_spans, skipped = _map_resource_spans(req)

    # Persist
    persisted = 0
    if mapped_spans:
        try:
            dsn = _dsn()
            persisted = persist_spans(mapped_spans, dsn, source="otlp_http")
        except RuntimeError:
            # KAIROS_PG_DSN not set — log but do not crash the producer.
            logger.warning("otlp.dsn_not_set — spans not persisted; set KAIROS_PG_DSN")
        except Exception:
            logger.exception("otlp.persist_failed span_count=%d", len(mapped_spans))

    logger.info(
        "otlp.ingest_traces total=%d mapped=%d persisted=%d skipped=%d",
        sum(len(ss.spans) for rs in req.resource_spans for ss in rs.scope_spans),
        len(mapped_spans),
        persisted,
        skipped,
    )

    resp_proto = ExportTraceServiceResponse()
    return fastapi.Response(
        content=resp_proto.SerializeToString(),
        media_type=_CT_PROTO,
    )
