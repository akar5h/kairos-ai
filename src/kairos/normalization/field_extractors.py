"""Field extractors for trace normalization.

Extracts user_input, system_prompt, infers output_type and terminal_status
from raw trace data and normalized steps.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kairos.models.enums import OutputType, StepStatus, TerminalStatus

if TYPE_CHECKING:
    from kairos.models.trace import Step


def extract_user_input(
    trace_input: str | dict[str, Any] | None,
    first_generation_input: str | dict[str, Any] | None,
) -> tuple[str | None, str | None]:
    """Extract (user_input, system_prompt) from trace and generation inputs.

    Priority:
    1. If trace_input is a string -> that's the user input
    2. If trace_input has messages -> parse roles
    3. If first_generation_input has messages -> parse roles
    4. Fallback: (None, None)
    """
    sources = [trace_input, first_generation_input]

    for source in sources:
        if source is None:
            continue
        if isinstance(source, str):
            return (source, None)
        if isinstance(source, dict):
            messages = source.get("messages", [])
            if not messages:
                continue
            system_prompt = None
            user_input = None
            for msg in messages:
                role = msg.get("role", "")
                content = msg.get("content", "")
                if role == "system" and system_prompt is None:
                    system_prompt = content
                if role == "user" and user_input is None:
                    user_input = content
            if user_input is not None:
                return (user_input, system_prompt)

    return (None, None)


def infer_output_type(
    steps: list[Step],
    trace_output: str | dict[str, Any] | None,
) -> OutputType:
    """Infer what the agent produced from the final steps and trace output."""
    if not steps:
        return OutputType.UNKNOWN

    last_step = steps[-1]

    # Check if last tool call produced a file
    if last_step.tool_name and last_step.tool_output:
        output_lower = str(last_step.tool_output).lower()
        file_indicators = [
            ".pdf",
            ".docx",
            ".pptx",
            ".xlsx",
            ".csv",
            ".png",
            ".jpg",
            "file_path",
            "file_url",
            "download_url",
            "s3://",
            "gs://",
        ]
        if any(ind in output_lower for ind in file_indicators):
            return OutputType.FILE

        api_indicators = [
            "created",
            "updated",
            "deleted",
            "posted",
            "sent",
            "status_code",
            "response_code",
        ]
        if any(ind in output_lower for ind in api_indicators):
            return OutputType.API_CALL

    # Default: if there's any tool output it's mixed, otherwise text
    has_tool_output = any(s.tool_output for s in steps)
    if has_tool_output:
        return OutputType.MIXED

    return OutputType.TEXT


def infer_terminal_status(
    steps: list[Step],
    trace_metadata: dict[str, Any] | None,
) -> TerminalStatus:
    """Infer how the trace ended."""
    if not steps:
        return TerminalStatus.UNKNOWN

    last_step = steps[-1]

    # Check for errors in last step
    if last_step.status == StepStatus.ERROR:
        return TerminalStatus.ERROR

    # Check for timeout patterns
    if last_step.error_message and any(
        kw in last_step.error_message.lower() for kw in ["timeout", "timed out", "deadline exceeded"]
    ):
        return TerminalStatus.TIMEOUT

    # Check for human escalation patterns
    if last_step.tool_name and any(
        kw in last_step.tool_name.lower() for kw in ["human", "escalat", "handoff", "transfer"]
    ):
        return TerminalStatus.HUMAN_ESCALATION

    # If no errors anywhere -> completed
    error_count = sum(1 for s in steps if s.status == StepStatus.ERROR)
    if error_count == 0:
        return TerminalStatus.COMPLETED

    # Has errors but last step is OK -> completed with issues
    return TerminalStatus.COMPLETED
