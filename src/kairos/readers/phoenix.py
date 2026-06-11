"""Phoenix reader — pulls OTel spans from a Phoenix server, returns TraceEnvelope.

Architecture: Kairos sits on top of OpenTelemetry. Hosts emit OTel spans
via OpenLLMetry / OpenInference / raw OTel; Phoenix (or any OTel backend)
stores them. This reader queries Phoenix for spans by trace_id, converts
each span via ``genai_mapping``, hands the resulting events to
``LiveNormalizer``, and returns a ``TraceEnvelope`` ready for analysis.

Phoenix span dict shape (from arize-phoenix-client) is mapped onto an
OTel-ReadableSpan-like adapter so the existing ``genai_mapping`` pure
functions work unchanged.

Public API::

    from kairos.readers.phoenix import PhoenixReader

    reader = PhoenixReader(endpoint="http://localhost:6006")
    envelope = reader.fetch_envelope("0123456789abcdef0123456789abcdef")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from opentelemetry.trace import StatusCode  # noqa: TC002 — runtime use in adapter

from kairos.log import get_logger
from kairos.models.trace import TraceEnvelope
from kairos.normalization.events import AnyEvent  # noqa: TC001
from kairos.normalization.live_normalizer import LiveNormalizer
from kairos.readers.genai_mapping import (
    classify_span,
    span_to_llm_call,
    span_to_retrieval,
    span_to_tool_call,
    span_to_trace_end,
    span_to_trace_start,
)

logger = get_logger(__name__)

# arize-phoenix-client.get_spans paginates internally via cursor until
# all matching spans are fetched or the limit is reached. 100_000 covers
# any realistic trace; raise further with PhoenixReader(span_limit=N).
_DEFAULT_LIMIT: int = 100_000
_DEFAULT_PROJECT: str = "default"


# ───────────────────────── Phoenix-span adapter ─────────────────────────


@dataclass
class _SpanContext:
    trace_id: int
    span_id: int


@dataclass
class _SpanParent:
    span_id: int


@dataclass
class _SpanStatus:
    status_code: StatusCode
    description: str | None = None


@dataclass
class _SpanEvent:
    name: str
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass
class _SpanResource:
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass
class _PhoenixSpan:
    """ReadableSpan-shaped adapter over a Phoenix span dict.

    The ``genai_mapping`` functions duck-type ``span.attributes``,
    ``span.context.trace_id``, ``span.parent.span_id``, etc. We expose
    the same surface so they work unchanged.
    """

    name: str
    attributes: dict[str, Any]
    context: _SpanContext
    parent: _SpanParent | None
    start_time: int  # nanoseconds since epoch
    end_time: int
    status: _SpanStatus
    events: list[_SpanEvent]
    resource: _SpanResource


_STATUS_MAP: dict[str, StatusCode] = {
    "OK": StatusCode.OK,
    "ERROR": StatusCode.ERROR,
    "UNSET": StatusCode.UNSET,
}


def _iso_to_ns(iso: str) -> int:
    """Convert a Phoenix ISO-8601 timestamp string to nanoseconds-since-epoch."""
    dt = datetime.fromisoformat(iso)
    return int(dt.timestamp() * 1_000_000_000)


def _phoenix_dict_to_span(d: dict[str, Any]) -> _PhoenixSpan:
    """Wrap a Phoenix span dict in a ReadableSpan-shaped adapter."""
    ctx = d.get("context") or {}
    trace_id_hex = ctx.get("trace_id") or "0" * 32
    span_id_hex = ctx.get("span_id") or "0" * 16
    parent_hex = d.get("parent_id")

    status_str = (d.get("status_code") or "UNSET").upper()
    status_code = _STATUS_MAP.get(status_str, StatusCode.UNSET)
    description = d.get("status_message") or None

    raw_events = d.get("events") or []
    events = [_SpanEvent(name=ev.get("name", ""), attributes=dict(ev.get("attributes") or {})) for ev in raw_events]

    return _PhoenixSpan(
        name=d.get("name", ""),
        attributes=dict(d.get("attributes") or {}),
        context=_SpanContext(
            trace_id=int(trace_id_hex, 16),
            span_id=int(span_id_hex, 16),
        ),
        parent=_SpanParent(span_id=int(parent_hex, 16)) if parent_hex else None,
        start_time=_iso_to_ns(d["start_time"]) if d.get("start_time") else 0,
        end_time=_iso_to_ns(d["end_time"]) if d.get("end_time") else 0,
        status=_SpanStatus(status_code=status_code, description=description),
        events=events,
        resource=_SpanResource(attributes={}),
    )


# ────────────────────── spans → envelope (pure) ─────────────────────────


def spans_to_envelope(spans: list[Any]) -> TraceEnvelope:
    """Convert a list of OTel-shaped spans (or Phoenix dicts) into a TraceEnvelope.

    Spans may arrive in any order; they're sorted by ``start_time`` before
    processing. The trace's "task" root span (host-marked via
    ``kairos.task`` name or ``kairos.span.kind=task``) bookends the trace
    with synthesized TraceStart / TraceEnd events. Without a task root,
    the envelope is produced from whatever LLM / tool / retrieval spans
    exist.
    """
    if not spans:
        return TraceEnvelope(
            trace_id="",
            source="kairos_phoenix",
            is_valid=False,
            validation_warnings=["no spans provided"],
        )

    # Accept either Phoenix dicts or already-wrapped spans. Both shapes
    # duck-type as ReadableSpan for genai_mapping; cast to Any to keep
    # mypy quiet at the boundary.
    wrapped: list[Any] = [_phoenix_dict_to_span(s) if isinstance(s, dict) else s for s in spans]
    wrapped.sort(key=lambda s: s.start_time)

    task_span: Any | None = next((s for s in wrapped if classify_span(s) == "task"), None)
    events: list[AnyEvent] = []
    step_index = 0

    if task_span is not None:
        events.append(span_to_trace_start(task_span, step_index=step_index))
        step_index += 1

    for span in wrapped:
        if span is task_span:
            continue
        kind = classify_span(span)
        event: AnyEvent | None = None
        if kind == "llm":
            event = span_to_llm_call(span, step_index=step_index)
        elif kind == "tool":
            event = span_to_tool_call(span, step_index=step_index)
        elif kind == "retrieval":
            event = span_to_retrieval(span, step_index=step_index)
        if event is not None:
            events.append(event)
            step_index += 1

    if task_span is not None:
        events.append(span_to_trace_end(task_span, step_index=step_index))

    envelope = LiveNormalizer().normalize(events)
    logger.info(
        "phoenix_reader.spans_to_envelope",
        trace_id=envelope.trace_id,
        span_count=len(wrapped),
        event_count=len(events),
        had_task_root=task_span is not None,
    )
    return envelope


# ───────────────────────────── PhoenixReader ────────────────────────────


try:
    from phoenix.client import Client
except ImportError:  # pragma: no cover — fallback for environments without phoenix-client
    Client = None  # type: ignore[assignment,misc]


class PhoenixReader:
    """Query Phoenix for spans by trace_id and produce a TraceEnvelope."""

    def __init__(
        self,
        *,
        client: Any | None = None,
        endpoint: str | None = None,
        project: str = _DEFAULT_PROJECT,
        span_limit: int = _DEFAULT_LIMIT,
    ) -> None:
        if client is not None:
            self._client = client
        else:
            if Client is None:
                msg = "PhoenixReader requires arize-phoenix-client. Install with: pip install arize-phoenix-client"
                raise RuntimeError(msg)
            self._client = Client(base_url=endpoint) if endpoint else Client()
        self._project = project
        self._span_limit = span_limit

    def fetch_envelope(self, trace_id: str) -> TraceEnvelope:
        """Fetch all spans for ``trace_id`` from Phoenix, return a TraceEnvelope."""
        spans = list(
            self._client.spans.get_spans(
                project_identifier=self._project,
                trace_ids=[trace_id],
                limit=self._span_limit,
            )
        )
        # When span_count == span_limit we may have been truncated — warn but
        # continue so callers get analysis on whatever spans arrived. Raise the
        # default (100_000) or pass PhoenixReader(span_limit=N) if needed.
        if len(spans) >= self._span_limit:
            logger.warning(
                "phoenix_reader.span_limit_reached",
                trace_id=trace_id,
                span_count=len(spans),
                limit=self._span_limit,
                hint="Increase PhoenixReader(span_limit=N) to capture all spans.",
            )
        envelope = spans_to_envelope(spans)
        logger.info(
            "phoenix_reader.fetched",
            trace_id=trace_id,
            project=self._project,
            span_count=len(spans),
            envelope_valid=envelope.is_valid,
        )
        return envelope
