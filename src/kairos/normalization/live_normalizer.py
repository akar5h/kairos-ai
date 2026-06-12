"""LiveNormalizer — folds a list of live events into a TraceEnvelope.

Counterpart to LangfuseNormalizer for the live-emit path. Reads events
produced by Tracer (in-memory list, JSONL file, etc.) and produces the
same TraceEnvelope IR the analysis pipeline already speaks.

Mappings:
    TraceStart  → TraceEnvelope identity + intent fields
    TraceEnd    → terminal_status / output_type / ended_at, metadata merged
    LLMCall     → Step(step_type=LLM)
    ToolCall    → Step(step_type=TOOL_CALL), args canonicalized via arg_normalizer,
                  parent_span_id resolved to parent_step_index
    Retrieval   → Step(step_type=RETRIEVAL)
    MemoryEvent → metadata.memory_events[]   (no Step produced)

Soft-fail rules (matches LangfuseNormalizer): invalid input returns a
TraceEnvelope with is_valid=False + validation_warnings, never raises.
"""

from __future__ import annotations

import json
from pathlib import Path

from kairos.log import get_logger
from kairos.models.enums import OutputType, StepType, TerminalStatus
from kairos.models.trace import Step, TraceEnvelope
from kairos.normalization.arg_normalizer import normalize_args
from kairos.normalization.events import (
    AnyEvent,
    LLMCall,
    MemoryEvent,
    Retrieval,
    ToolCall,
    TraceEnd,
    TraceStart,
    parse_event,
)

logger = get_logger(__name__)


def _latency_ms(started: object, ended: object) -> int | None:
    if started is None or ended is None:
        return None
    try:
        delta = ended - started  # type: ignore[operator]
    except TypeError:
        return None
    return int(delta.total_seconds() * 1000)


def _serialize_output(output: object) -> str | None:
    if output is None:
        return None
    if isinstance(output, str):
        return output
    return json.dumps(output)


def _messages_to_text(messages: list[object]) -> str | None:
    if not messages:
        return None
    parts: list[str] = []
    for msg in messages:
        role = getattr(msg, "role", None)
        content = getattr(msg, "content", None)
        if content is None:
            continue
        parts.append(f"{role}: {content}" if role else str(content))
    return "\n".join(parts) if parts else None


class LiveNormalizer:
    """Fold a list of live events into a TraceEnvelope."""

    def normalize(self, events: list[AnyEvent]) -> TraceEnvelope:
        if not events:
            return TraceEnvelope(
                trace_id="",
                source="kairos_live",
                is_valid=False,
                validation_warnings=["no events provided"],
            )

        trace_id = events[0].trace_id
        warnings: list[str] = []

        start = next((e for e in events if isinstance(e, TraceStart)), None)
        end = next((e for e in events if isinstance(e, TraceEnd)), None)

        if start is None:
            warnings.append("missing trace_start event — trace shape may be incomplete")

        steps: list[Step] = []
        memory_events: list[dict[str, object]] = []
        # Map source span_id → step_index assigned in this envelope.
        span_to_step: dict[str, int] = {}

        for event in events:
            if isinstance(event, (TraceStart, TraceEnd)):
                continue
            if isinstance(event, MemoryEvent):
                memory_events.append(
                    {
                        "kind": event.kind,
                        "key": event.key,
                        "value": event.value,
                        "scope": event.scope,
                        "span_id": event.span_id,
                        "step_index": event.step_index,
                    }
                )
                continue

            step = self._event_to_step(event, len(steps), span_to_step)
            if step is not None:
                span_to_step[event.span_id] = step.step_index
                steps.append(step)

        # Compose envelope-level fields from start/end.
        user_input = start.user_input if start is not None else None
        system_prompt = start.system_prompt if start is not None else None
        agent_type = start.agent_name if start is not None else None
        started_at = start.emitted_at if start is not None else None

        if end is not None:
            terminal_status = end.terminal_status
            output_type = end.output_type
            ended_at = end.emitted_at
        else:
            terminal_status = TerminalStatus.UNKNOWN
            output_type = OutputType.UNKNOWN
            ended_at = None

        metadata: dict[str, object] = {}
        if start is not None and start.metadata:
            metadata.update(start.metadata)
        if end is not None and end.metadata:
            metadata.update(end.metadata)
        if memory_events:
            metadata["memory_events"] = memory_events

        # Token totals from LLM steps.
        total_input = sum(s.input_tokens or 0 for s in steps if s.step_type is StepType.LLM)
        total_output = sum(s.output_tokens or 0 for s in steps if s.step_type is StepType.LLM)
        total_tokens = sum(s.total_tokens or 0 for s in steps if s.step_type is StepType.LLM)
        total_latency = sum(s.latency_ms or 0 for s in steps)

        envelope = TraceEnvelope(
            trace_id=trace_id,
            source="kairos_live",
            source_trace_id=trace_id,
            user_input=user_input,
            system_prompt=system_prompt,
            agent_type=agent_type,
            steps=steps,
            total_tokens=total_tokens,
            total_input_tokens=total_input,
            total_output_tokens=total_output,
            total_latency_ms=total_latency,
            terminal_status=terminal_status,
            output_type=output_type,
            metadata=metadata or None,
            started_at=started_at,
            ended_at=ended_at,
            is_valid=start is not None,
            validation_warnings=warnings,
        )
        logger.info(
            "live_normalizer.normalized",
            trace_id=trace_id,
            step_count=len(steps),
            terminal_status=terminal_status.value,
            had_start=start is not None,
            had_end=end is not None,
        )
        return envelope

    @classmethod
    def from_jsonl(cls, path: str | Path) -> TraceEnvelope:
        """Load events from a JSONL file (one event per line) and normalize."""
        file_path = Path(path)
        events: list[AnyEvent] = []
        for line in file_path.read_text().splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            events.append(parse_event(json.loads(stripped)))
        return cls().normalize(events)

    # ── internals ────────────────────────────────────────────────────────

    def _event_to_step(
        self,
        event: AnyEvent,
        step_index: int,
        span_to_step: dict[str, int],
    ) -> Step | None:
        if isinstance(event, LLMCall):
            return Step(
                step_index=step_index,
                step_type=StepType.LLM,
                llm_input=_messages_to_text(list(event.messages_in)),
                llm_output=event.content_out,
                llm_model=event.model,
                input_tokens=event.input_tokens,
                output_tokens=event.output_tokens,
                cache_read_tokens=event.cache_read_tokens,
                total_tokens=event.total_tokens,
                tokens_instrumented=event.tokens_instrumented,
                latency_ms=_latency_ms(event.started_at, event.ended_at),
                status=event.status,
                status_source=event.status_source,
                error_message=event.error_message,
                started_at=event.started_at,
                ended_at=event.ended_at,
                source_observation_id=event.span_id,
                parent_step_index=(
                    span_to_step.get(event.parent_span_id) if event.parent_span_id is not None else None
                ),
            )

        if isinstance(event, ToolCall):
            return Step(
                step_index=step_index,
                step_type=StepType.TOOL_CALL,
                tool_name=event.name,
                tool_args=event.args,
                tool_args_normalized=normalize_args(event.args),
                tool_output=_serialize_output(event.output),
                latency_ms=_latency_ms(event.started_at, event.ended_at),
                status=event.status,
                status_source=event.status_source,
                error_message=event.error_message,
                started_at=event.started_at,
                ended_at=event.ended_at,
                source_observation_id=event.span_id,
                parent_step_index=(
                    span_to_step.get(event.parent_span_id) if event.parent_span_id is not None else None
                ),
                attrs=event.attrs,
            )

        if isinstance(event, Retrieval):
            return Step(
                step_index=step_index,
                step_type=StepType.RETRIEVAL,
                retrieval_query=event.query,
                retrieval_chunks=list(event.chunks),
                latency_ms=_latency_ms(event.started_at, event.ended_at),
                started_at=event.started_at,
                ended_at=event.ended_at,
                source_observation_id=event.span_id,
                parent_step_index=(
                    span_to_step.get(event.parent_span_id) if event.parent_span_id is not None else None
                ),
            )

        return None
