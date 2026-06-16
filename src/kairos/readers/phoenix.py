"""Phoenix span primitives — OTel span adapter + envelope builder.

F1.5: The ``PhoenixReader`` HTTP/GraphQL fetch class has been removed.
Kairos now ingests spans itself (OTLP → spans table) and reads them via
``kairos.readers.db.fetch_envelope_from_db``.

This module retains the span adapter primitives (``_PhoenixSpan`` and family)
and the pure conversion functions (``spans_to_envelope``, ``_phoenix_dict_to_span``)
that the DB reader and the OTLP ingest path both depend on.  DO NOT delete
these — ``kairos.readers.db`` imports them directly.

Public API::

    from kairos.readers.phoenix import spans_to_envelope

    # spans may be Phoenix dicts or _PhoenixSpan instances
    envelope = spans_to_envelope(spans)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from opentelemetry.trace import StatusCode  # noqa: TC002 — runtime use in adapter

from kairos.log import get_logger
from kairos.models.enums import StepStatus, StepStatusSource, TerminalStatus
from kairos.models.trace import TraceEnvelope
from kairos.normalization.agents.base import apply_step_outcomes
from kairos.normalization.agents.claude_code import ClaudeCodeNormalizer
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
from kairos.readers.transcript_join import tool_args_from_transcript, tool_errors_from_transcript

logger = get_logger(__name__)

# Shared stateless adapter for rung 3 on claude_code-shaped live traces.
# No import cycle: normalization.agents.* never imports kairos.readers.
_CLAUDE_CODE_NORMALIZER = ClaudeCodeNormalizer()


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


def _is_session_end_blocked_on_user(wrapped: list[Any]) -> bool:
    """Return True when the session ended awaiting human input.

    Conservative rule (spec §5): HUMAN_ESCALATION is mapped ONLY when the
    literal final span of the trace (by end_time, excluding the task root) is
    ``claude_code.tool.blocked_on_user`` AND no ``llm_request`` span has a
    start_time after that blocked_on_user span ended.

    Rationale: blocked_on_user appears on EVERY permission-phase interaction,
    not just session ends.  Using end_time order + no-subsequent-llm guard
    prevents false positives on mid-trace permission waits.

    This is the conservative option from the spec:
      "implement the conservative version (only map when the literal final span
       of the trace is blocked_on_user AND no llm_request follows it)"
    """
    # Exclude task spans — they wrap the whole trace and always end last.
    non_task = [s for s in wrapped if classify_span(s) != "task"]
    if not non_task:
        return False

    # Sort by end_time descending to find the last non-task span.
    by_end = sorted(non_task, key=lambda s: s.end_time, reverse=True)
    last_span = by_end[0]

    # The last span must be a blocked_on_user span.
    span_name = getattr(last_span, "name", "")
    if span_name != "claude_code.tool.blocked_on_user":
        return False

    # Guard: no llm_request span with start_time after the blocked_on_user end_time.
    blocked_end = last_span.end_time
    for span in non_task:
        if getattr(span, "name", "") == "claude_code.llm_request" and span.start_time > blocked_end:
            return False

    return True


def _propagate_execution_success(wrapped: list[Any]) -> None:
    """Copy the ``success`` attr from ``claude_code.tool.execution`` children onto
    their parent ``claude_code.tool`` spans (in place).

    The parent tool span is what becomes the tool Step, but the emitter marks it
    OK unconditionally; the execution child carries the real structured verdict.
    A parent that already has a ``success`` attribute is never overwritten.
    """
    span_by_id: dict[int, Any] = {s.context.span_id: s for s in wrapped}
    for span in wrapped:
        if getattr(span, "name", "") != "claude_code.tool.execution":
            continue
        child_attrs = getattr(span, "attributes", None)
        if not isinstance(child_attrs, dict):
            continue
        success = child_attrs.get("success")
        if success is None or span.parent is None:
            continue
        parent = span_by_id.get(span.parent.span_id)
        if parent is None or getattr(parent, "name", "") != "claude_code.tool":
            continue
        parent_attrs = getattr(parent, "attributes", None)
        if isinstance(parent_attrs, dict) and "success" not in parent_attrs:
            parent_attrs["success"] = success


def _correct_tool_errors_from_transcript(
    wrapped: list[Any],
    envelope: TraceEnvelope,
) -> int:
    """Flip tool step status to ERROR for steps the transcript marks is_error=true.

    Called AFTER the envelope is built and AFTER rung-3 adapter outcomes are
    applied — only for claude_code-shaped traces (caller guards on is_claude_code).

    The OTel emitter stamps ``success=true`` on ``claude_code.tool.execution``
    child spans even when the harness returned ``is_error: true`` (e.g.
    Edit/Write returning ``<tool_use_error>File has been modified since read…``).
    This is Bug 1 (silent phantom side-effects).  The transcript's tool_result
    ``is_error`` field is ground truth.

    Provenance decision: corrected steps keep ``status_source == ATTR_SUCCESS``
    (the signal that originally set them), not a new source value.  Rationale:
    lowest blast-radius — no new enum value needed, downstream consumers that
    key on ATTR_SUCCESS remain correct (the signal fired; the transcript overrode
    its value).  The step's ``status`` is authoritative; ``status_source`` records
    where the original structured signal came from.

    TESTBED-SCOPED ENRICHMENT: the durable fix is emitter-side — the OTel emitter
    should set success=false on tool.execution spans when is_error=true.  This
    function exists only until that emitter fix ships.

    Returns the number of steps corrected.
    """
    if not envelope.steps:
        return 0

    error_indices = tool_errors_from_transcript(wrapped, list(envelope.steps))
    if not error_indices:
        return 0

    corrected = 0
    for step in envelope.steps:
        if step.step_index not in error_indices:
            continue
        # Only flip steps that were previously marked OK by a structured signal
        # (ATTR_SUCCESS from the execution-child propagation, or NONE).
        # Steps already ERROR are not touched — don't downgrade good signals.
        if step.status is StepStatus.ERROR:
            continue
        step.status = StepStatus.ERROR
        # Preserve the original status_source — see provenance decision above.
        # If the step had no structured signal (NONE), stamp it ATTR_SUCCESS to
        # indicate the correction came from the structured transcript is_error field.
        if step.status_source is StepStatusSource.NONE:
            step.status_source = StepStatusSource.ATTR_SUCCESS
        corrected += 1

    if corrected:
        envelope.error_count = sum(1 for s in envelope.steps if s.status == StepStatus.ERROR)

    return corrected


def _enrich_tool_args_from_transcript(
    wrapped: list[Any],
    envelope: TraceEnvelope,
) -> int:
    """Populate tool step ``tool_args`` from the session transcript's tool_use.input.

    Called AFTER ``_correct_tool_errors_from_transcript`` — only for
    claude_code-shaped traces (caller guards on is_claude_code).

    The F10 emitter limitation means spans carry no tool args/outputs on live
    Phoenix data.  The transcript's ``tool_use.input`` is ground truth for args.
    This function uses the SAME ordinal-per-tool-name, time-windowed alignment
    as the is_error correction (via ``tool_args_from_transcript`` → shared
    ``_fetch_windowed_calls`` pipeline).

    Only populates steps where ``tool_args`` is currently empty (None or {}) —
    never overwrites a step that already has args from the span (future-proof
    for when the emitter is fixed to populate args on spans).

    Args are pre-redacted by ``transcript_join._redact_args`` before this call.

    Graceful degradation: missing transcript / no match → args stay empty, NO crash.

    Returns the number of steps enriched.
    """
    if not envelope.steps:
        return 0

    args_map = tool_args_from_transcript(wrapped, list(envelope.steps))
    if not args_map:
        return 0

    from kairos.models.enums import StepType  # local import
    from kairos.normalization.arg_normalizer import normalize_args

    enriched = 0
    for step in envelope.steps:
        if step.step_type != StepType.TOOL_CALL:
            continue
        if step.step_index not in args_map:
            continue
        # Only populate if the span didn't already provide args.
        if step.tool_args:
            continue
        args = args_map[step.step_index]
        step.tool_args = args
        step.tool_args_normalized = normalize_args(args)
        enriched += 1

    return enriched


def spans_to_envelope(
    spans: list[Any],
    *,
    correlation_key_attr: str | None = None,
) -> TraceEnvelope:
    """Convert a list of OTel-shaped spans (or Phoenix dicts) into a TraceEnvelope.

    Spans may arrive in any order; they're sorted by ``start_time`` before
    processing. The trace's "task" root span (host-marked via
    ``kairos.task`` name or ``kairos.span.kind=task``) bookends the trace
    with synthesized TraceStart / TraceEnd events. Without a task root,
    the envelope is produced from whatever LLM / tool / retrieval spans
    exist.

    Terminal status override: when the session ended awaiting human input
    (conservative check: final non-task span is ``blocked_on_user`` AND no
    subsequent ``llm_request``), the TraceEnd gets ``TerminalStatus.HUMAN_ESCALATION``
    regardless of the task root's OTel status.  HUMAN_ESCALATION is pass-eligible.

    Rung 3: when the trace is claude_code-shaped (any ``claude_code.*`` span),
    the ClaudeCode adapter extractor runs over tool steps still at
    ``status_source == NONE`` after rungs 1–2 (kairos.outcome / success attr /
    OTel status) were silent.  Steps decided by rungs 1–2 are never touched.

    Parameters
    ----------
    spans:
        Raw spans — either Phoenix dicts or OTel ReadableSpan-shaped objects.
    correlation_key_attr:
        When set, scan all spans for this attribute name and store the first
        value found on ``envelope.correlation_key_value``.  On live data the
        attribute is present on every span in the trace (interaction, llm_request,
        tool, tool.execution), so the first span that carries it wins.
        ``None`` → ``envelope.correlation_key_value`` stays ``None``.
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

    # Day 9: extract correlation key value from spans (first span that carries it).
    # The attribute is present on every span in a live Paperclip trace, so the
    # first match is sufficient.  Scan pre-sort wrapping so dict spans are already
    # normalised via _phoenix_dict_to_span.
    correlation_key_value: str | None = None
    if correlation_key_attr:
        for span in wrapped:
            attrs = getattr(span, "attributes", None)
            if isinstance(attrs, dict):
                raw_val = attrs.get(correlation_key_attr)
                if raw_val is not None:
                    correlation_key_value = str(raw_val)
                    break

    # Day 4 fix (rung 2a propagation): the emitter sets status_code=OK
    # unconditionally on ``claude_code.tool`` spans (live: 4904 OK / 0 ERROR);
    # the real verdict lives on the ``tool.execution`` sub-phase child as a
    # ``success`` attribute (live: True/False matches the child's OTel status).
    # Copy it onto the parent BEFORE event conversion so
    # ``_step_status_with_source`` resolves rung 2a (ATTR_SUCCESS). Without
    # this, live tool steps land at status_source=NONE with no readable output
    # and outcome pass is structurally impossible.
    _propagate_execution_success(wrapped)

    task_span: Any | None = next((s for s in wrapped if classify_span(s) == "task"), None)

    # Detect session-end blocked state BEFORE building events, using all spans.
    session_blocked = _is_session_end_blocked_on_user(wrapped)

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
        trace_end = span_to_trace_end(task_span, step_index=step_index)
        if session_blocked:
            # Override terminal status: session ended awaiting human input.
            trace_end = trace_end.model_copy(update={"terminal_status": TerminalStatus.HUMAN_ESCALATION})
        events.append(trace_end)

    envelope = LiveNormalizer().normalize(events)

    # Rung 3: claude_code-shaped traces get the adapter extractor applied to
    # tool steps that rungs 1–2 left undecided (status_source == NONE).
    is_claude_code = any(getattr(s, "name", "").startswith("claude_code.") for s in wrapped)
    if is_claude_code:
        apply_step_outcomes(envelope, _CLAUDE_CODE_NORMALIZER)

    # Bug 1 correction (transcript_join): for claude_code traces, enrich tool
    # step status from the session transcript's tool_result.is_error field.
    # The OTel emitter stamps success=true on tool.execution children even when
    # the harness rejected the call (is_error=true) — e.g. Edit returning
    # "<tool_use_error>File has been modified since read</tool_use_error>".
    # transcript_join aligns steps by ordinal-per-tool-name and corrects them.
    # Graceful degradation: missing transcript → no correction, no crash.
    corrected_count = 0
    enriched_args_count = 0
    if is_claude_code:
        corrected_count = _correct_tool_errors_from_transcript(wrapped, envelope)
        if corrected_count:
            logger.info(
                "transcript_join.corrected_tool_errors",
                n=corrected_count,
                trace_id=envelope.trace_id,
            )
        else:
            logger.debug(
                "transcript_join.no_corrections",
                trace_id=envelope.trace_id,
            )

        # Day 8 fix (F10 args enrichment): populate tool_args on steps from the
        # transcript's tool_use.input — the emitter doesn't carry args on live spans.
        # Uses the same ordinal-per-tool-name alignment as the is_error correction.
        # Args are pre-redacted (secrets stripped). Only enriches steps with empty args.
        # Graceful degradation: missing transcript → no enrichment, no crash.
        enriched_args_count = _enrich_tool_args_from_transcript(wrapped, envelope)
        if enriched_args_count:
            logger.info(
                "transcript_join.enriched_tool_args",
                n=enriched_args_count,
                trace_id=envelope.trace_id,
            )
        else:
            logger.debug(
                "transcript_join.no_args_enrichment",
                trace_id=envelope.trace_id,
            )

    # Day 9: stamp correlation key value on the envelope.
    if correlation_key_value is not None:
        envelope = envelope.model_copy(update={"correlation_key_value": correlation_key_value})

    # Day 4: orphan/integrity check.
    # A span is an orphan when it has a parent_id that is not present in this trace's
    # span set AND it is not a root span (root = parent is None).
    span_ids: set[int] = {s.context.span_id for s in wrapped}
    orphans = [s for s in wrapped if s.parent is not None and s.parent.span_id not in span_ids]
    if orphans:
        envelope = envelope.model_copy(update={"integrity": "partial"})
        logger.warning(
            "phoenix_reader.orphan_spans_detected",
            trace_id=envelope.trace_id,
            orphan_count=len(orphans),
            orphan_span_ids=[hex(s.context.span_id) for s in orphans[:5]],
        )

    logger.info(
        "phoenix_reader.spans_to_envelope",
        trace_id=envelope.trace_id,
        span_count=len(wrapped),
        event_count=len(events),
        had_task_root=task_span is not None,
        human_escalation=session_blocked,
        adapter_outcomes_applied=is_claude_code,
        transcript_corrected=corrected_count,
        transcript_args_enriched=enriched_args_count,
    )
    return envelope


# PhoenixReader (HTTP/GraphQL fetch class) removed in F1.5.
# Use kairos.readers.db.fetch_envelope_from_db instead.
