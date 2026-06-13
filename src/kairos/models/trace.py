"""Pydantic IR models: Step, TraceEnvelope, NormalizationReport."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from kairos.models.enums import (  # noqa: TCH001
    OutputType,
    StepStatus,
    StepStatusSource,
    StepType,
    TerminalStatus,
)


class Step(BaseModel):
    """Single step in an agent execution trace."""

    step_index: int
    step_type: StepType
    agent_name: str | None = None
    node_name: str | None = None

    # Tool call fields
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
    tool_args_normalized: dict[str, Any] | None = None
    tool_output: str | None = None

    # LLM fields
    llm_input: str | None = None
    llm_output: str | None = None
    llm_model: str | None = None

    # Retrieval fields
    retrieval_query: str | None = None
    retrieval_chunks: list[str] | None = None

    # Metrics
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int = 0
    total_tokens: int | None = None
    """Token spend for this step.

    Semantics (LLM steps only): output + max(input − cache_read, 0), floor 0.
    This is the chargeable spend that detectors report as potential waste —
    cache hits are NOT counted because they were paid for at creation time.
    Absent (None) means the step was not instrumented; absent is never
    substituted with 0.
    """
    tokens_instrumented: bool = False
    """True when extract_usage() returned a non-None Usage for this step.

    Use this instead of ``total_tokens > 0`` to test whether token data is
    present; a genuinely zero-token step (e.g. fully-cached call) is
    instrumented even though total_tokens == 0.
    """
    latency_ms: int | None = None

    # Status
    status: StepStatus = StepStatus.OK
    status_source: StepStatusSource = StepStatusSource.NONE
    """Which rung of the evidence ladder set the step status.

    "attr_success"  — claude_code.tool.execution ``success`` attribute
    "otel_status"   — OTel span StatusCode (ERROR/OK)
    "adapter"       — per-agent adapter extractor (step_outcome hook)
    "textual"       — rung 4 word-boundary marker scan (last resort)
    "none"          — no signal; status defaulted to OK
    """
    error_message: str | None = None

    # Raw span attributes — preserved for the adapter extractor hook (step_outcome).
    # Only populated on the live OTel path (genai_mapping.py); None on transcript
    # adapters where attributes come through structured fields instead.
    attrs: dict[str, Any] | None = None

    # Hierarchy
    parent_step_index: int | None = None

    # Timestamps
    started_at: datetime | None = None
    ended_at: datetime | None = None

    # Provenance
    source_observation_id: str | None = None


class TraceEnvelope(BaseModel):
    """Normalized representation of one agent execution trace."""

    # Identity
    trace_id: str
    source: str = "langfuse"
    source_trace_id: str | None = None

    # Intent
    user_input: str | None = None
    system_prompt: str | None = None
    agent_type: str | None = None

    # Execution
    steps: list[Step] = Field(default_factory=list)

    # Aggregated metrics
    total_tokens: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_latency_ms: int = 0
    step_count: int = 0

    # Terminal state
    terminal_status: TerminalStatus = TerminalStatus.UNKNOWN
    output_type: OutputType = OutputType.UNKNOWN

    # Derived fields (computed in model_post_init)
    tool_sequence: list[str] = Field(default_factory=list)
    tool_bigrams: list[tuple[str, str]] = Field(default_factory=list)
    unique_tool_count: int = 0
    error_count: int = 0
    has_retrieval: bool = False
    retrieval_step_count: int = 0

    # Metadata
    session_id: str | None = None
    user_id: str | None = None
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] | None = None

    # Timestamps
    started_at: datetime | None = None
    ended_at: datetime | None = None
    normalized_at: datetime = Field(
        default_factory=lambda: datetime.now(tz=UTC),
    )

    # Provenance
    source_metadata: dict[str, Any] | None = None

    # Day 9: correlation key value (populated by the reader when a
    # correlation_key attribute name is configured in BusinessContext).
    correlation_key_value: str | None = None
    """Value of the configured correlation-key span attribute for this trace.

    Populated by the Phoenix reader when ``correlation_key_attr`` is passed to
    ``spans_to_envelope``.  ``None`` means either (a) no attribute was
    configured, or (b) the attribute was absent from all spans in this trace.
    """

    # Integrity (Day 4: orphan check)
    integrity: str = "complete"
    """Span-set integrity. "complete" when all parent_ids resolve within the trace;
    "partial" when any non-root span references a parent_id not present in the span set.
    Outcome evaluation refuses to score a "partial" trace (non-computable, reason=PARTIAL_TRACE).
    """

    # Validation
    is_valid: bool = True
    validation_warnings: list[str] = Field(default_factory=list)

    def model_post_init(self, __context: object) -> None:
        """Compute derived fields from steps."""
        self.step_count = len(self.steps)

        self.tool_sequence = [
            s.tool_name for s in self.steps if s.step_type == StepType.TOOL_CALL and s.tool_name is not None
        ]

        self.tool_bigrams = [
            (self.tool_sequence[i], self.tool_sequence[i + 1]) for i in range(len(self.tool_sequence) - 1)
        ]

        self.unique_tool_count = len(set(self.tool_sequence))

        self.error_count = sum(1 for s in self.steps if s.status == StepStatus.ERROR)

        retrieval_steps = [s for s in self.steps if s.step_type == StepType.RETRIEVAL]
        self.has_retrieval = len(retrieval_steps) > 0
        self.retrieval_step_count = len(retrieval_steps)

        if self.user_input is None:
            self.validation_warnings.append("Missing user_input: trace cannot be clustered by intent")


class NormalizationReport(BaseModel):
    """Summary of a normalization batch run."""

    total_traces_ingested: int = 0
    total_traces_normalized: int = 0
    total_traces_failed: int = 0
    traces_missing_user_input: int = 0
    traces_missing_system_prompt: int = 0
    traces_missing_tool_calls: int = 0
    traces_with_errors: int = 0
    avg_steps_per_trace: float = 0.0
    avg_tokens_per_trace: float = 0.0
    errors: list[dict[str, Any]] = Field(default_factory=list)
