"""Workflow membership model: a trace can belong to multiple workflows.

Three-tier classification:
  FULL       — all required_side_effect_tools succeeded AND recall >= threshold
  ATTEMPTED  — recall >= threshold but side-effects not all successful
  NONE       — recall below threshold OR trace has no tools OR op has no expected tools
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class MembershipKind(StrEnum):
    FULL = "full"
    ATTEMPTED = "attempted"
    NONE = "none"


@dataclass(frozen=True)
class WorkflowMembership:
    operation_name: str
    kind: MembershipKind
    recall: float
