"""Tier 1 Pattern 2: Loop detector.

Detects period-1 loops: the same single tool called ≥ min_repeats times
consecutively with identical outputs (no progress). Period-2+ loops are
exceedingly rare in observed data; add period-2 detection only when a real
case is confirmed in production traces.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kairos.detection.models import Finding
from kairos.models.enums import StepStatus, StepType

if TYPE_CHECKING:
    from kairos.models.trace import TraceEnvelope


def loop_guard(trace: TraceEnvelope, cluster_median_steps: float) -> bool:
    """Return True when trace step_count *strictly* exceeds the cluster median."""
    return trace.step_count > cluster_median_steps


def loop_assertion(
    trace: TraceEnvelope,
    min_repeats: int = 3,
) -> list[Finding]:
    """Detect period-1 loops: same tool called ≥ min_repeats times with no progress.

    A run of the same tool is a loop when every tool_output in the run is
    identical (agent is stuck). If every step in the run has status ERROR
    the classification is ``stuck_loop``; otherwise ``loop_detected``.

    Period-2+ detection is not implemented. It covers < 5% of real loops and
    adds 80 lines of edge-case complexity.

    F10 guard: when tool_output is uninstrumented (None/empty) on ALL steps of
    the entire run, loop detection degrades to "same tool ≥N consecutive with
    no progress signal" — NOT a finding. A loop without output evidence is only
    a triage feature (Day 8 consumes it), not a confirmed detection.
    See docs/system-audit-and-self-improvement-roadmap.md for F10 context.
    """
    tool_steps = [s for s in trace.steps if s.step_type == StepType.TOOL_CALL and s.tool_name]
    n = len(tool_steps)

    if n < min_repeats:
        return []

    # F10 guard: if output is uninstrumented on ALL steps in the entire run,
    # do not fire any loop finding — we have no evidence of actual stuck-ness.
    all_outputs_absent = all(not s.tool_output for s in tool_steps)
    if all_outputs_absent:
        return []

    findings: list[Finding] = []
    i = 0

    while i < n:
        tool = tool_steps[i].tool_name
        # Find the run length for this tool
        j = i + 1
        while j < n and tool_steps[j].tool_name == tool:
            j += 1
        run_len = j - i

        if run_len >= min_repeats:
            run_steps = tool_steps[i:j]
            outputs = [s.tool_output for s in run_steps]

            # F10 guard (per-run): identical-but-absent outputs are no evidence
            # of stuck-ness — skip runs whose outputs are all uninstrumented,
            # even when other tools in the trace do carry output.
            if all(not o for o in outputs):
                i = j
                continue

            # Progress check: if any output differs, the agent is advancing.
            if len(set(outputs)) == 1:
                all_error = all(s.status == StepStatus.ERROR for s in run_steps)
                classification = "stuck_loop" if all_error else "loop_detected"

                step_indices = [s.step_index for s in run_steps]
                waste = sum(s.total_tokens or 0 for s in run_steps[1:])

                findings.append(
                    Finding(
                        pattern_name=classification,
                        tier=1,
                        trace_id=trace.trace_id,
                        confidence=1.0,
                        severity="critical" if all_error else "warning",
                        evidence={
                            "pattern": [tool],
                            "period": 1,
                            "repeats": run_len,
                            "step_range": [step_indices[0], step_indices[-1]],
                            "classification": classification,
                        },
                        affected_step_indices=step_indices,
                        estimated_token_waste=waste,
                    )
                )

        i = j

    return findings
