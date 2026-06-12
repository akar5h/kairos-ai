"""Business context: customer-defined operation taxonomy the engine maps traces to."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
                    excluded_tools=op_data.get("excluded_tools", []),
                    correctness_criteria=op_data.get("correctness_criteria", []),
                    membership_recall_threshold=op_data.get("membership_recall_threshold"),
                )
            )

        return cls(
            agent_name=data.get("agent_name", ""),
            agent_description=data.get("agent_description", ""),
            operations=operations,
        )
