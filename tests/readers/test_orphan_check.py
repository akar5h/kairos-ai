"""Day 4: orphan/integrity check tests.

Phase test matrix item:
  W3 | orphan parent span | partial → non-computable, reason=partial_trace

Covers:
  - spans_to_envelope with an orphan span → envelope.integrity == "partial"
  - complete trace fixture → envelope.integrity == "complete"
  - evaluate_outcome on partial envelope → computable=False, reason=PARTIAL_TRACE
  - transcript-sourced envelopes (LiveNormalizer direct path) default to "complete"
"""

from __future__ import annotations

from typing import Any

from kairos.analysis.outcome_metric import evaluate_outcome
from kairos.models.enums import FailureReason, StepStatus, StepType, TerminalStatus
from kairos.models.trace import Step, TraceEnvelope
from kairos.readers.phoenix import spans_to_envelope
from kairos.taxonomy.business_context import BusinessOperation

# ── Phoenix span dict helpers ─────────────────────────────────────────────

TRACE_ID = "abcdef1234567890abcdef1234567890"
ROOT_SPAN_ID = "1111111111111111"
CHILD_SPAN_ID = "2222222222222222"
ORPHAN_PARENT_ID = "ffffffffffffffff"  # not present in the span set


def _span_dict(
    *,
    name: str,
    span_id: str = ROOT_SPAN_ID,
    parent_id: str | None = None,
    trace_id: str = TRACE_ID,
    status_code: str = "OK",
    start_time: str = "2026-05-07T12:00:00.000000+00:00",
    end_time: str = "2026-05-07T12:00:01.000000+00:00",
    attributes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "context": {"trace_id": trace_id, "span_id": span_id},
        "parent_id": parent_id,
        "span_kind": "INTERNAL",
        "start_time": start_time,
        "end_time": end_time,
        "status_code": status_code,
        "status_message": "",
        "attributes": attributes or {},
        "events": [],
    }


def _root_task_span(*, span_id: str = ROOT_SPAN_ID, status_code: str = "OK") -> dict[str, Any]:
    """A task root span — marked with kairos.span.kind = task."""
    return _span_dict(
        name="kairos.task",
        span_id=span_id,
        parent_id=None,
        status_code=status_code,
        attributes={"kairos.span.kind": "task"},
    )


def _tool_span(
    *,
    span_id: str = CHILD_SPAN_ID,
    parent_id: str | None = ROOT_SPAN_ID,
    tool_name: str = "Write",
    status_code: str = "OK",
    start_time: str = "2026-05-07T12:00:00.100000+00:00",
    end_time: str = "2026-05-07T12:00:00.500000+00:00",
) -> dict[str, Any]:
    return _span_dict(
        name="claude_code.tool.execution",
        span_id=span_id,
        parent_id=parent_id,
        status_code=status_code,
        start_time=start_time,
        end_time=end_time,
        attributes={"claude_code.tool.name": tool_name, "success": True},
    )


# ── Integrity tests ───────────────────────────────────────────────────────


class TestOrphanDetection:
    """spans_to_envelope marks the envelope as partial when orphan spans exist."""

    def test_complete_trace_has_complete_integrity(self) -> None:
        """All parent_ids resolve → integrity == 'complete'."""
        spans = [
            _root_task_span(),
            _tool_span(parent_id=ROOT_SPAN_ID),
        ]
        envelope = spans_to_envelope(spans)
        assert envelope.integrity == "complete"

    def test_orphan_span_marks_partial(self) -> None:
        """A span whose parent_id is not in the span set → integrity == 'partial'."""
        orphan = _tool_span(
            span_id=CHILD_SPAN_ID,
            parent_id=ORPHAN_PARENT_ID,  # dangling reference
        )
        spans = [_root_task_span(), orphan]
        envelope = spans_to_envelope(spans)
        assert envelope.integrity == "partial"

    def test_root_only_trace_is_complete(self) -> None:
        """A single root span with no parent → complete (no orphans possible)."""
        spans = [_root_task_span()]
        envelope = spans_to_envelope(spans)
        assert envelope.integrity == "complete"

    def test_multiple_orphans_still_partial(self) -> None:
        """More than one orphan span still results in a single 'partial' marker."""
        orphan_a = _tool_span(span_id="aaaaaaaaaaaaaaaa", parent_id=ORPHAN_PARENT_ID)
        orphan_b = _tool_span(span_id="bbbbbbbbbbbbbbbb", parent_id="cccccccccccccccc")
        spans = [_root_task_span(), orphan_a, orphan_b]
        envelope = spans_to_envelope(spans)
        assert envelope.integrity == "partial"

    def test_no_spans_is_invalid_not_partial(self) -> None:
        """Empty span list → is_valid=False; integrity defaults to 'complete' (no orphans computed)."""
        envelope = spans_to_envelope([])
        assert envelope.is_valid is False
        assert envelope.integrity == "complete"


# ── Outcome evaluation with integrity ────────────────────────────────────


def _op() -> BusinessOperation:
    return BusinessOperation(
        name="Code Implementation",
        description="test op",
        expected_tools=["Write", "Bash"],
        priority="high",
        required_side_effect_tools=["Write"],
    )


class TestPartialTraceOutcome:
    """integrity == 'partial' → computable=False, failure_reason=PARTIAL_TRACE."""

    def test_partial_envelope_is_non_computable(self) -> None:
        """NEVER score a partial trace as failed — it's non-computable."""
        op = _op()
        # Build an envelope directly with integrity='partial' to isolate the check.
        envelope = TraceEnvelope(
            trace_id="t-partial",
            terminal_status=TerminalStatus.COMPLETED,
            integrity="partial",
            steps=[
                Step(
                    step_index=0,
                    step_type=StepType.TOOL_CALL,
                    tool_name="Write",
                    status=StepStatus.OK,
                    tool_output="written",
                )
            ],
        )
        result = evaluate_outcome(envelope, op)
        assert result.computable is False
        assert result.outcome_pass is False
        assert result.failure_reason == FailureReason.PARTIAL_TRACE
        assert result.reason == "partial_trace"

    def test_partial_does_not_count_as_failed(self) -> None:
        """Partial trace must NOT produce outcome_pass=False with a failure verdict.

        The integrity check returns computable=False, not a fail verdict — the trace
        should land in non_computable, not in the fail count.
        """
        from kairos.analysis.outcome_metric import compute_outcome_rate

        op = _op()
        partial = TraceEnvelope(
            trace_id="t-partial",
            terminal_status=TerminalStatus.COMPLETED,
            integrity="partial",
        )
        summary = compute_outcome_rate([partial], op)
        assert summary.computable_count == 0
        assert summary.passed_count == 0
        assert summary.outcome_rate is None

    def test_complete_envelope_still_evaluated_normally(self) -> None:
        """integrity == 'complete' is transparent — evaluation proceeds normally."""
        op = _op()
        envelope = TraceEnvelope(
            trace_id="t-complete",
            terminal_status=TerminalStatus.COMPLETED,
            integrity="complete",
            steps=[
                Step(
                    step_index=0,
                    step_type=StepType.TOOL_CALL,
                    tool_name="Write",
                    status=StepStatus.OK,
                    tool_output="written",
                )
            ],
        )
        result = evaluate_outcome(envelope, op)
        assert result.computable is True
        assert result.outcome_pass is True

    def test_partial_trace_via_spans_to_envelope_is_non_computable(self) -> None:
        """End-to-end: orphan span → partial → non-computable in evaluate_outcome."""
        orphan = _tool_span(span_id=CHILD_SPAN_ID, parent_id=ORPHAN_PARENT_ID)
        spans = [_root_task_span(status_code="OK"), orphan]
        envelope = spans_to_envelope(spans)
        assert envelope.integrity == "partial"

        op = _op()
        result = evaluate_outcome(envelope, op)
        assert result.computable is False
        assert result.failure_reason == FailureReason.PARTIAL_TRACE


# ── LiveNormalizer / transcript path defaults to complete ─────────────────


class TestTranscriptEnvelopeDefault:
    """Envelopes built by LiveNormalizer (transcript path) default to 'complete'."""

    def test_live_normalizer_envelope_defaults_complete(self) -> None:
        """TraceEnvelope integrity field defaults to 'complete' (no orphan check on transcript path)."""
        from kairos.models.trace import TraceEnvelope

        envelope = TraceEnvelope(trace_id="t-live", terminal_status=TerminalStatus.COMPLETED)
        assert envelope.integrity == "complete"
