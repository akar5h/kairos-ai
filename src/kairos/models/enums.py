"""Enumerations for the Kairos IR models."""

from enum import StrEnum


class TerminalStatus(StrEnum):
    COMPLETED = "completed"
    ERROR = "error"
    TIMEOUT = "timeout"
    HUMAN_ESCALATION = "human_escalation"
    UNKNOWN = "unknown"


class OutputType(StrEnum):
    TEXT = "text"
    FILE = "file"
    API_CALL = "api_call"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class StepStatus(StrEnum):
    OK = "ok"
    ERROR = "error"


class StepType(StrEnum):
    LLM = "llm"
    TOOL_CALL = "tool_call"
    RETRIEVAL = "retrieval"
    AGENT = "agent"
    OTHER = "other"


class StepStatusSource(StrEnum):
    """Which rung of the evidence ladder set the step status."""

    ATTR_SUCCESS = "attr_success"
    """claude_code ``success`` attribute (rung 2 primary signal)."""
    OTEL_STATUS = "otel_status"
    """OTel span StatusCode ERROR/OK (rung 2 secondary signal)."""
    KAIROS_OUTCOME = "kairos_outcome"
    """Explicit ``kairos.outcome`` attribute override (rung 1)."""
    ADAPTER = "adapter"
    """Per-agent adapter extractor hook (rung 3)."""
    TEXTUAL = "textual"
    """Rung 4 word-boundary marker scan (last resort)."""
    NONE = "none"
    """No structured signal; status defaulted to OK."""


class FailureReason(StrEnum):
    """Why an outcome evaluation returned outcome_pass=False."""

    TERMINAL_ERROR = "terminal_error"
    """Trace terminal status was ERROR or TIMEOUT."""
    TERMINAL_UNKNOWN = "terminal_unknown"
    """Trace terminal status could not be determined."""
    CRITICAL_TOOL_ERROR = "critical_tool_error"
    """An expected or side-effect tool errored with no recovery."""
    MISSING_SIDE_EFFECT = "missing_side_effect"
    """Required side-effect tool was never called or every call failed."""
    SIDE_EFFECT_OUTPUT_FAILED = "side_effect_output_failed"
    """Side-effect tool was called but output indicates failure (rung 4)."""
    PARTIAL_TRACE = "partial_trace"
    """Trace has orphan spans — integrity is partial, outcome is non-computable."""
