"""Tier 1 Pattern 1: Redundant execution detector.

Detects consecutive tool calls with the same tool name and near-identical
normalised arguments (Jaccard similarity above threshold).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kairos.detection.models import Finding
from kairos.detection.similarity import jaccard_dict_similarity
from kairos.models.enums import StepStatus, StepType

if TYPE_CHECKING:
    from kairos.models.trace import TraceEnvelope


def redundant_guard(trace: TraceEnvelope) -> bool:
    """Check if trace has 2+ consecutive calls to the same tool.

    Uses only metadata (tool_sequence) — O(n) scan, sub-millisecond.
    """
    seq = trace.tool_sequence
    return any(seq[i] == seq[i + 1] for i in range(len(seq) - 1))


def redundant_assertion(
    trace: TraceEnvelope,
    threshold: float = 0.85,
) -> list[Finding]:
    """Identify redundant consecutive tool calls via normalised-arg Jaccard."""
    tool_steps = [s for s in trace.steps if s.step_type == StepType.TOOL_CALL and s.tool_name]
    findings: list[Finding] = []

    i = 0
    while i < len(tool_steps) - 1:
        curr = tool_steps[i]
        nxt = tool_steps[i + 1]

        if curr.tool_name == nxt.tool_name:
            # Skip retries: if previous call errored, the next same-tool call is a retry
            if curr.status == StepStatus.ERROR:
                i += 1
                continue
            args_a = curr.tool_args_normalized or curr.tool_args
            args_b = nxt.tool_args_normalized or nxt.tool_args
            # F10 guard: if BOTH steps have no args (uninstrumented tool spans —
            # live claude_code.tool spans carry no tool_args/tool_output), skip
            # the pair entirely. jaccard_dict_similarity returns 1.0 for None/None
            # and ∅/∅, which produced a historical 642-finding flood at confidence
            # 1.0 on live data. Fix at the detector layer, not by changing Jaccard
            # semantics globally (tau-bench corpus depends on them).
            if not args_a and not args_b:
                i += 1
                continue
            sim = jaccard_dict_similarity(args_a, args_b)

            if sim >= threshold:
                token_waste = nxt.total_tokens or 0
                findings.append(
                    Finding(
                        pattern_name="redundant_execution",
                        tier=1,
                        trace_id=trace.trace_id,
                        confidence=sim,
                        severity="warning",
                        evidence={
                            "tool": curr.tool_name,
                            "jaccard_similarity": round(sim, 4),
                            "step_a": curr.step_index,
                            "step_b": nxt.step_index,
                        },
                        affected_step_indices=[curr.step_index, nxt.step_index],
                        estimated_token_waste=token_waste,
                    )
                )
                # Don't skip — check next pair too (A,A,A → 2 findings)
        i += 1

    return findings
