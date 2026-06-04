"""Tier 1 Pattern 2: Loop detector.

Uses a sliding-window algorithm to find repeating tool-call subsequences.
A progress check distinguishes true loops (stuck, same output) from
legitimate iteration (advancing cursor / changing output).
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
    """Detect repeating tool-call subsequences with no progress.

    Algorithm
    ---------
    1. Extract tool-call steps and their names.
    2. For each candidate period *p* (2 .. len // min_repeats), scan for
       *min_repeats* consecutive occurrences of the same tool-name pattern.
    3. Progress check: if ANY tool output differs across repeats at the same
       position the agent is making progress — skip.
    4. All-error check: if every step in the repeated range has status ERROR
       the loop is classified as ``stuck_loop``.
    5. Collect findings, skip past detected loops, and return sorted by
       longest period first.
    """
    tool_steps = [s for s in trace.steps if s.step_type == StepType.TOOL_CALL and s.tool_name]
    tool_names: list[str] = [s.tool_name for s in tool_steps]  # type: ignore[misc]
    n = len(tool_names)

    if n < 2:
        return []

    findings: list[Finding] = []
    consumed: set[int] = set()  # indices already part of a detected loop

    max_period = n // min_repeats

    for period in range(max_period, 1, -1):  # longest period first
        start = 0
        while start <= n - period * min_repeats:
            if start in consumed:
                start += 1
                continue

            pattern = tool_names[start : start + period]

            # Count consecutive repeats
            repeats = 1
            pos = start + period
            while pos + period <= n:
                window = tool_names[pos : pos + period]
                if window != pattern:
                    break
                repeats += 1
                pos += period

            if repeats < min_repeats:
                start += 1
                continue

            # --- Progress check ---
            # For each position *j* within the pattern, collect the
            # tool_output from every repeat.  If ALL outputs are identical
            # at every position the agent is stuck; if any output differs
            # the agent is making progress → not a loop.
            end_idx = start + period * repeats
            has_progress = False
            for j in range(period):
                outputs: list[str | None] = []
                for r in range(repeats):
                    idx = start + r * period + j
                    outputs.append(tool_steps[idx].tool_output)
                if len(set(outputs)) > 1:
                    has_progress = True
                    break

            if has_progress:
                start += 1
                continue

            # --- Classification ---
            all_error = all(tool_steps[idx].status == StepStatus.ERROR for idx in range(start, end_idx))
            classification = "stuck_loop" if all_error else "loop_detected"

            step_indices = [tool_steps[idx].step_index for idx in range(start, end_idx)]

            # Estimate wasted tokens (all repeats beyond the first are waste)
            waste = sum(tool_steps[idx].total_tokens or 0 for idx in range(start + period, end_idx))

            findings.append(
                Finding(
                    pattern_name=classification,
                    tier=1,
                    trace_id=trace.trace_id,
                    confidence=1.0,
                    severity="critical" if all_error else "warning",
                    evidence={
                        "pattern": pattern,
                        "period": period,
                        "repeats": repeats,
                        "step_range": [step_indices[0], step_indices[-1]],
                        "classification": classification,
                    },
                    affected_step_indices=step_indices,
                    estimated_token_waste=waste,
                )
            )

            # Mark these indices as consumed so shorter periods don't
            # re-detect the same span.
            consumed.update(range(start, end_idx))
            start = end_idx

    # Already iterated longest-first, so findings are naturally sorted.
    return findings
