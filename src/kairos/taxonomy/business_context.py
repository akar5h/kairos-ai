"""Business context: customer-defined operation taxonomy the engine maps traces to."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml


@dataclass
class BusinessOperation:
    """A customer-defined business operation.

    ``required_side_effect_tools`` serves a dual role: it gates workflow
    membership AND it distinguishes FULL from ATTEMPTED. An op without
    any required_side_effect_tools has no signature and will never match
    a trace (utility pattern, not a workflow).
    """

    name: str
    description: str
    expected_tools: list[str] = field(default_factory=list)
    priority: str = "medium"  # high / medium / low
    business_goal: str | None = None
    reliability_metric: str | None = None
    bad_run_means: str | None = None
    required_side_effect_tools: list[str] = field(default_factory=list)
    side_effect_match: Literal["all", "any"] = "all"
    excluded_tools: list[str] = field(default_factory=list)
    correctness_criteria: list[str] = field(default_factory=list)
    membership_recall_threshold: float | None = None


def default_membership_threshold(op: BusinessOperation) -> float:
    """Resolve the membership recall threshold for an operation.

    - If explicitly set in YAML, use it.
    - Else if single-tool workflow (exactly 1 expected_tool), default 1.0
      (the single tool must be called).
    - Else default 0.5.
    """
    if op.membership_recall_threshold is not None:
        return op.membership_recall_threshold
    if len(op.expected_tools) == 1:
        return 1.0
    return 0.5


@dataclass
class BusinessContext:
    """Customer-provided business context for an agent."""

    agent_name: str
    agent_description: str
    operations: list[BusinessOperation]
    correlation_key: str | None = None
    """Optional span attribute name that groups multiple traces into one logical
    unit of work.  Example: ``"paperclip.issue"`` groups all traces that worked
    on the same issue; ``"thread_id"`` groups chat turns.

    When ``None`` (the default) each trace is its own unit — behaviour is
    byte-identical to before this field existed.
    """
    coordination_markers: list[str] = field(default_factory=list)
    """Phrase list (case-insensitive) that, when found in a trace's user_input,
    flag the trace as a coordination-context session rather than genuine agent
    work.  Empty list (the default) means the feature is off — no traces are
    flagged.

    Example: ``["wake payload", "resume delta", "heartbeat"]`` identifies
    Paperclip heartbeat sessions.  The engine applies these generically; the
    specific strings are a configuration concern, not engine logic.
    """
    coordination_tools: list[str] = field(default_factory=list)
    """Tool signatures that, when matched by a trace step, flag the trace as a
    coordination-context session.  Each entry is either:
      - ``"ToolName"`` — matches any step whose tool_name equals ToolName.
      - ``"ToolName:substring"`` — matches when tool_name equals ToolName AND
        the step's args string contains ``substring`` (case-insensitive).

    Empty list (the default) means the feature is off.

    Example: ``["Skill:paperclip", "Bash:PAPERCLIP_API"]`` identifies Paperclip
    coordination tool calls generically.
    """

    @classmethod
    def from_yaml(cls, path: str | Path) -> BusinessContext:
        """Load business context from a YAML file.

        Raises ``ValueError`` (with the file path in the message) when:
          - the parsed document is not a mapping (e.g. YAML list at root)
          - the document has zero operations

        These checks happen before ``from_dict`` so the error message always
        names the file, making misconfigured-run errors actionable from the
        first line of the traceback.
        """
        path = Path(path)
        with path.open() as f:
            data = yaml.safe_load(f)

        if not isinstance(data, dict):
            msg = (
                f"Context file {path} did not parse to a YAML mapping "
                f"(got {type(data).__name__}). "
                "Expected a mapping with an 'operations' key."
            )
            raise ValueError(msg)

        raw_ops = data.get("operations")
        if not raw_ops:
            msg = f"Context file {path} has no operations. Add at least one entry under the 'operations' key."
            raise ValueError(msg)

        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BusinessContext:
        """Load business context from a dictionary."""
        raw_ops = data.get("operations", [])
        if not raw_ops:
            msg = "Business context must have at least one operation in 'operations'"
            raise ValueError(msg)

        operations = []
        for op_data in raw_ops:
            if "name" not in op_data:
                msg = "Each operation must have a 'name' field"
                raise ValueError(msg)
            match_mode = op_data.get("side_effect_match", "all")
            if match_mode not in ("all", "any"):
                msg = (
                    f"Operation '{op_data['name']}': side_effect_match must be "
                    f"'all' or 'any', got {match_mode!r}."
                )
                raise ValueError(msg)
            excluded = op_data.get("excluded_tools", [])
            conflict = set(excluded) & set(op_data.get("expected_tools", []))
            if conflict:
                msg = (
                    f"Operation '{op_data['name']}': excluded_tools {sorted(conflict)} "
                    "overlap with expected_tools — remove them from expected_tools or excluded_tools."
                )
                raise ValueError(msg)
            operations.append(
                BusinessOperation(
                    name=op_data["name"],
                    description=op_data.get("description", ""),
                    expected_tools=op_data.get("expected_tools", []),
                    priority=op_data.get("priority", "medium"),
                    business_goal=op_data.get("business_goal"),
                    reliability_metric=op_data.get("reliability_metric"),
                    bad_run_means=op_data.get("bad_run_means"),
                    required_side_effect_tools=op_data.get("required_side_effect_tools", []),
                    side_effect_match=match_mode,
                    excluded_tools=op_data.get("excluded_tools", []),
                    correctness_criteria=op_data.get("correctness_criteria", []),
                    membership_recall_threshold=op_data.get("membership_recall_threshold"),
                )
            )

        correlation_key = data.get("correlation_key") or None

        raw_markers = data.get("coordination_markers", [])
        if not isinstance(raw_markers, list) or not all(isinstance(m, str) for m in raw_markers):
            msg = "'coordination_markers' must be a list of strings"
            raise ValueError(msg)

        raw_tools = data.get("coordination_tools", [])
        if not isinstance(raw_tools, list) or not all(isinstance(t, str) for t in raw_tools):
            msg = "'coordination_tools' must be a list of strings"
            raise ValueError(msg)

        return cls(
            agent_name=data.get("agent_name", ""),
            agent_description=data.get("agent_description", ""),
            operations=operations,
            correlation_key=correlation_key,
            coordination_markers=list(raw_markers),
            coordination_tools=list(raw_tools),
        )
