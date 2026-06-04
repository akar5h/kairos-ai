"""Detection data models — Finding is the output of all pattern detectors."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Finding:
    """A single failure pattern finding on one trace."""

    pattern_name: str  # "redundant_execution" | "loop_detected"
    tier: int
    trace_id: str
    confidence: float  # 0.0 - 1.0
    severity: str  # "critical" | "warning"
    evidence: dict[str, Any] = field(default_factory=dict)
    affected_step_indices: list[int] = field(default_factory=list)
    estimated_token_waste: int = 0
