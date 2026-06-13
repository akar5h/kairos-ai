"""Tier 1.5 session-quality detectors — deterministic, no LLM.

Four detectors that catch session-quality problems contract-completion is
blind to: unrecovered errors, struggle, coordination waste, and low
work-to-talk ratio.  Plus a LEARN stage that computes per-workflow
tool-presence rates and returns expectation-miss candidates for Day-12
discovery.

All detectors produce ``Finding`` objects using the same model as Tier 1.
None of them modify ``outcome_metric.py`` or any outcome verdict — scope
guard is total.

Threshold defaults are annotated with the distribution they came from:
  D2/STRUGGLE_T=2.0 — live corpus median struggle ~0.3, p90 ~1.8; default
    set just above p90 so the detector fires on the top ~10% of sessions.
    (n=153 computable traces from spotcheck-day4.md window.)
  D4/WTT_T=0.05 — side_effect_successes / (llm_tokens/1000): typical
    passing Code Implementation traces show ~0.2–0.5; fire below 0.05
    catches sessions with near-zero productive output per kilo-token.

See docs/sprint-exec-3-loop.md §"Day 8" for the full spec.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from kairos.detection.models import Finding
from kairos.detection.similarity import jaccard_dict_similarity
from kairos.models.enums import StepStatus, StepType

if TYPE_CHECKING:
    from kairos.models.trace import Step, TraceEnvelope
    from kairos.taxonomy.business_context import BusinessOperation

# ── Default thresholds (overridable via context.yaml per-op keys) ─────────────

# D1 — unrecovered_error: how many steps forward to look for a recovery call.
# Default 10: enough to cover a "retry after thinking" pattern while bounding
# the look-ahead.  Session-restart boundary always stops the window.
RECOVERY_WINDOW: int = 10

# D2 — redundant step arg-similarity threshold.
# Consecutive same-tool calls count as redundant ONLY when their args are
# real (non-empty on both sides) AND jaccard similarity >= REDUNDANT_ARG_T.
# This prevents 56 sequential distinct Bash commands from all counting as redundant.
REDUNDANT_ARG_T: float = 0.9

# D2 — struggle_ratio threshold.
# Distribution note: live corpus (n≈153 from spotcheck window), median ≈0.3,
# p90 ≈1.8.  Default 2.0 fires at top ~8% of sessions — confirmed-struggle
# territory by the owner's Day-4 labels (traces with explicit struggle comments
# had ratios of 3–15+).
STRUGGLE_T: float = 2.0

# D3 — coordination_waste
# REPEAT_T: identical-arg calls of one tool ≥ this count.
# Default 3: matches the loop detector's min_repeats convention.
REPEAT_T: int = 3

# Fraction of Bash calls that match coordination-curl shape.
# Default 0.7: signals that >70% of Bash activity is API polling rather than work.
CURL_T: float = 0.7

# D4 — work_to_talk_ratio threshold.
# Distribution note: Code Implementation passing traces ≈0.2–0.5
# side_effect_successes per kilo-token; 0.05 fires only on near-zero
# productivity sessions (no real side-effects per 1k tokens spent).
WTT_T: float = 0.05

# Op names exempt from D4 (research / coordination — low side-effects expected).
# These match the operation names in config/context.yaml exactly.
D4_EXEMPT_OPS: frozenset[str] = frozenset(
    {"Codebase Research", "Paperclip Coordination"}
)

# LEARN stage — tool presence rate threshold for expectation-miss candidates.
# 0.9 means: tool is present in ≥90% of clean traces → expected in every trace.
EXPECT_T: float = 0.9

# Minimum number of clean traces needed before emitting any candidates.
# Too few → presence rates are noise.
EXPECT_MIN_N: int = 5

# ── Coordination-curl pattern ──────────────────────────────────────────────────

# Matches common Bash coordination patterns: curl calls to PAPERCLIP_API_URL,
# localhost API, or github API.  Used by D3 to measure Bash-coordination fraction.
_CURL_PATTERN: re.Pattern[str] = re.compile(
    r"curl\s",
    re.IGNORECASE,
)


def _is_coordination_bash(step: Step) -> bool:
    """Return True when a Bash step's args look like an API polling call."""
    if step.tool_name != "Bash":
        return False
    args = step.tool_args_normalized or step.tool_args
    if not args:
        return False
    # Look for curl in the command string value(s).
    return any(isinstance(v, str) and _CURL_PATTERN.search(v) for v in args.values())


# ── D1 helpers ────────────────────────────────────────────────────────────────


def _find_session_restart_indices(steps: list[Step]) -> frozenset[int]:
    """Return step indices that mark session-restart boundaries.

    A session restart is signalled by a Bash step whose args contain
    common haywire-restart patterns:
      - reading/sourcing the system prompt or stale context files
      - explicit "new session" or "resume" markers
    This is intentionally conservative: if we cannot confidently identify a
    boundary we do not mark it (better a missed boundary than a false recovery).
    """
    boundaries: set[int] = set()
    restart_re = re.compile(
        r"(\.claude|system_prompt|stale.session|resume|checkpoint.*restart)",
        re.IGNORECASE,
    )
    for step in steps:
        if step.step_type != StepType.TOOL_CALL or step.tool_name != "Bash":
            continue
        args = step.tool_args_normalized or step.tool_args
        if not args:
            continue
        for v in args.values():
            if isinstance(v, str) and restart_re.search(v):
                boundaries.add(step.step_index)
                break
    return frozenset(boundaries)


def _args_jaccard(a: Step, b: Step) -> float:
    """Jaccard similarity between two steps' normalized args."""
    args_a = a.tool_args_normalized or a.tool_args
    args_b = b.tool_args_normalized or b.tool_args
    # F10-style guard: if both sides have no args, skip (empty == empty = 1.0
    # is not meaningful for recovery detection).
    if not args_a and not args_b:
        return 0.0
    return jaccard_dict_similarity(args_a, args_b)


# ── D1 — unrecovered_error ───────────────────────────────────────────────────


def detect_unrecovered_error(
    trace: TraceEnvelope,
    operation: BusinessOperation | None = None,
    recovery_window: int = RECOVERY_WINDOW,
) -> list[Finding]:
    """D1: fire when an ERROR step has no recovery within window.

    Recovery modes (applied in order of preference):
      1. Args-based (preferred): later call to same tool within ``recovery_window``
         steps with jaccard(args_norm) >= 0.9.  Used when both the error step
         and the candidate have non-empty args (F10 guard on both sides).
      2. Status-based fallback (safe degradation for no-transcript traces):
         when EITHER the error step OR the candidate has absent args, a later
         OK call to the same tool within the window counts as recovery.
         This degrades safely instead of flagging every error when the
         transcript is unavailable — the conservative direction.

    Session-restart boundaries do NOT count as recovery in either mode
    (haywire restarts look like recovery to over-loose rules).

    Severity:
      "error"   if the erroring tool is in required_side_effect_tools
      "warning" otherwise
    """
    tool_steps = [s for s in trace.steps if s.step_type == StepType.TOOL_CALL and s.tool_name]
    required_side_effects: frozenset[str] = frozenset(
        operation.required_side_effect_tools if operation else []
    )
    restart_indices = _find_session_restart_indices(trace.steps)

    findings: list[Finding] = []

    for i, step in enumerate(tool_steps):
        if step.status != StepStatus.ERROR:
            continue

        tool = step.tool_name
        assert tool is not None  # narrowed above

        # Look forward within window for a recovery call.
        recovered = False
        for j in range(i + 1, min(len(tool_steps), i + 1 + recovery_window)):
            candidate = tool_steps[j]

            # Session-restart boundary: if ANY step in [i+1..j] is a restart,
            # the recovery window is broken — do not count this or any later step.
            if candidate.step_index in restart_indices:
                break

            if candidate.tool_name != tool:
                continue

            # Determine recovery mode based on args availability.
            args_error = step.tool_args_normalized or step.tool_args
            args_cand = candidate.tool_args_normalized or candidate.tool_args
            if args_error and args_cand:
                # Args-based (preferred): require high jaccard similarity.
                sim = _args_jaccard(step, candidate)
                if sim >= 0.9:
                    recovered = True
                    break
            else:
                # Status-based fallback: no args on one or both sides.
                # A later OK call of the same tool conservatively counts as recovery.
                # This avoids flagging every error when the transcript is missing.
                if candidate.status == StepStatus.OK:
                    recovered = True
                    break

        if not recovered:
            severity = "error" if tool in required_side_effects else "warning"
            findings.append(
                Finding(
                    pattern_name="unrecovered_error",
                    tier=1,
                    trace_id=trace.trace_id,
                    confidence=1.0,
                    severity=severity,
                    evidence={
                        "tool": tool,
                        "step_index": step.step_index,
                        "error_message": step.error_message,
                        "recovery_window": recovery_window,
                        "in_required_side_effects": tool in required_side_effects,
                    },
                    affected_step_indices=[step.step_index],
                    estimated_token_waste=step.total_tokens or 0,
                )
            )

    return findings


# ── D2 — struggle_ratio ───────────────────────────────────────────────────────


def _count_redundant_steps(steps: list[Step], arg_t: float = REDUNDANT_ARG_T) -> int:
    """Count consecutive same-tool pairs with real, highly-similar args (redundancy proxy).

    A pair is redundant when:
      1. Same tool name on consecutive tool steps.
      2. First step is NOT an error (error → same tool = intended post-error retry).
      3. BOTH steps have non-empty args (F10 guard: empty args on either side →
         not redundant — we cannot distinguish distinct commands from identical ones
         without real args.  56 sequential distinct Bash commands must NOT fire).
      4. Jaccard similarity of args >= arg_t (default 0.9).

    This prevents the F10 over-fire: on live Phoenix data where span-level args
    are absent, all consecutive same-tool pairs previously counted as redundant.
    Now they are skipped unless the transcript has enriched args.
    """
    count = 0
    tool_steps = [s for s in steps if s.step_type == StepType.TOOL_CALL and s.tool_name]
    for i in range(len(tool_steps) - 1):
        a, b = tool_steps[i], tool_steps[i + 1]
        if a.tool_name != b.tool_name:
            continue
        # Skip post-error retries (intended).
        if a.status == StepStatus.ERROR:
            continue
        # F10 guard: both sides must have real args — empty args on either side
        # means we cannot determine similarity; do NOT count as redundant.
        args_a = a.tool_args_normalized or a.tool_args
        args_b = b.tool_args_normalized or b.tool_args
        if not args_a or not args_b:
            continue
        # Args similarity threshold: only count truly identical/near-identical repeats.
        if jaccard_dict_similarity(args_a, args_b) >= arg_t:
            count += 1
    return count


def detect_struggle_ratio(
    trace: TraceEnvelope,
    operation: BusinessOperation | None = None,
    struggle_t: float = STRUGGLE_T,
) -> list[Finding]:
    """D2: fire when (errors + redundant + rejected) / side_effect_successes >= STRUGGLE_T.

    Distribution note: default 2.0 (live corpus p90 ≈1.8; fires top ~8%).

    ``rejected_tool_calls`` are steps with status=ERROR and a tool_use_error
    in error_message (the harness-rejection pattern seen in owner labels).

    Severity: warning.  Evidence = the churn breakdown.
    """
    tool_steps = [s for s in trace.steps if s.step_type == StepType.TOOL_CALL and s.tool_name]

    error_steps = sum(1 for s in tool_steps if s.status == StepStatus.ERROR)

    # Rejected tool calls: error_message contains tool_use_error marker
    rejected_tool_calls = sum(
        1
        for s in tool_steps
        if s.status == StepStatus.ERROR
        and s.error_message is not None
        and "tool_use_error" in s.error_message.lower()
    )

    redundant_steps = _count_redundant_steps(trace.steps)

    # Side-effect successes: OK calls to any tool (proxy for productive output).
    # If operation is provided, use required_side_effect_tools for a tighter count.
    if operation and operation.required_side_effect_tools:
        side_effect_tools = frozenset(operation.required_side_effect_tools)
        side_effect_successes = sum(
            1
            for s in tool_steps
            if s.status == StepStatus.OK and s.tool_name in side_effect_tools
        )
    else:
        side_effect_successes = sum(1 for s in tool_steps if s.status == StepStatus.OK)

    struggle = (error_steps + redundant_steps + rejected_tool_calls) / max(1, side_effect_successes)

    if struggle < struggle_t:
        return []

    return [
        Finding(
            pattern_name="struggle_ratio",
            tier=1,
            trace_id=trace.trace_id,
            confidence=min(1.0, struggle / (struggle_t * 2)),  # scale to 1.0 at 2×threshold
            severity="warning",
            evidence={
                "struggle_ratio": round(struggle, 3),
                "threshold": struggle_t,
                "error_steps": error_steps,
                "redundant_steps": redundant_steps,
                "rejected_tool_calls": rejected_tool_calls,
                "side_effect_successes": side_effect_successes,
            },
            affected_step_indices=[
                s.step_index for s in tool_steps if s.status == StepStatus.ERROR
            ],
            estimated_token_waste=sum(s.total_tokens or 0 for s in tool_steps if s.status == StepStatus.ERROR),
        )
    ]


# ── D3 — coordination_waste ───────────────────────────────────────────────────


def _normalize_args_key(step: Step) -> tuple[str, str] | None:
    """Return a canonical (tool_name, args_repr) key for identical-arg detection.

    Returns None when args are empty — steps with no args are EXCLUDED from
    the identical-arg count (F10 guard).  Without real args, all empty-arg
    calls of the same tool would collapse into a single ('Bash', '[]') key
    and falsely fire the D3 repeat detector on every Bash-heavy trace.
    """
    tool = step.tool_name or ""
    args = step.tool_args_normalized or step.tool_args
    # F10 guard: empty args → not enough signal to compare; exclude from D3.
    if not args:
        return None
    # Use sorted key-value pairs for a stable repr.
    args_repr = repr(sorted(args.items()))
    return (tool, args_repr)


def detect_coordination_waste(
    trace: TraceEnvelope,
    repeat_t: int = REPEAT_T,
    curl_t: float = CURL_T,
) -> list[Finding]:
    """D3: surfacing only — identical-arg call repeats or high Bash-curl fraction.

    Fires when:
      - Any single tool is called ≥ REPEAT_T times with identical args (inbox
        poll / token re-derivation), OR
      - The fraction of Bash calls matching coordination-curl patterns ≥ CURL_T.

    Severity:
      info   — curl fraction or repeat count just at threshold
      warning — repeat count >= 2× threshold or curl fraction >= 0.9

    SURFACING ONLY: this finding reports waste; the user decides what to fix.
    """
    tool_steps = [s for s in trace.steps if s.step_type == StepType.TOOL_CALL and s.tool_name]
    if not tool_steps:
        return []

    # Count identical-arg call groups.
    # Steps with no args return key=None from _normalize_args_key (F10 guard)
    # and are excluded from the repeat count.
    arg_counts: dict[tuple[str, str], list[int]] = {}
    for step in tool_steps:
        key = _normalize_args_key(step)
        if key is None:
            continue  # empty args — cannot compare; skip
        if key not in arg_counts:
            arg_counts[key] = []
        arg_counts[key].append(step.step_index)

    repeat_violations: list[dict[str, Any]] = []
    for (tool_name, args_repr), indices in arg_counts.items():
        if len(indices) >= repeat_t:
            repeat_violations.append({
                "tool": tool_name,
                "count": len(indices),
                "step_indices": indices,
                "args_repr": args_repr[:120],  # truncate for evidence field
            })

    # Compute coordination-curl fraction for Bash steps.
    bash_steps = [s for s in tool_steps if s.tool_name == "Bash"]
    bash_total = len(bash_steps)
    coordination_bash_count = sum(1 for s in bash_steps if _is_coordination_bash(s))
    curl_fraction = coordination_bash_count / bash_total if bash_total > 0 else 0.0

    fired_curl = curl_fraction >= curl_t and bash_total > 0
    fired_repeat = bool(repeat_violations)

    if not fired_curl and not fired_repeat:
        return []

    # Severity: warning if count >= 2×threshold or curl >= 0.9, else info.
    max_repeat = max((v["count"] for v in repeat_violations), default=0)
    severity = "warning" if (max_repeat >= repeat_t * 2 or curl_fraction >= 0.9) else "info"

    affected: list[int] = []
    for v in repeat_violations:
        affected.extend(v["step_indices"])

    return [
        Finding(
            pattern_name="coordination_waste",
            tier=1,
            trace_id=trace.trace_id,
            confidence=0.8,
            severity=severity,
            evidence={
                "repeat_violations": repeat_violations,
                "curl_fraction": round(curl_fraction, 3),
                "coordination_bash_count": coordination_bash_count,
                "bash_total": bash_total,
                "repeat_t": repeat_t,
                "curl_t": curl_t,
            },
            affected_step_indices=affected,
            estimated_token_waste=0,  # token waste not directly attributable here
        )
    ]


# ── D4 — work_to_talk_ratio ───────────────────────────────────────────────────


def detect_work_to_talk_ratio(
    trace: TraceEnvelope,
    operation: BusinessOperation | None = None,
    wtt_t: float = WTT_T,
) -> list[Finding]:
    """D4: fire when side_effect_successes / (llm_tokens/1000) < WTT_T.

    Op-exempt for research/coordination operations (D4_EXEMPT_OPS) — those
    workflows are expected to have low side-effect counts relative to tokens.

    Distribution note: default 0.05 — Code Implementation passing traces
    show ~0.2–0.5; firing below 0.05 catches near-zero-productivity sessions.

    Severity: info.
    """
    # Op-exempt check.
    if operation is not None and operation.name in D4_EXEMPT_OPS:
        return []

    tool_steps = [s for s in trace.steps if s.step_type == StepType.TOOL_CALL and s.tool_name]

    # Side-effect successes.
    if operation and operation.required_side_effect_tools:
        side_effect_tools = frozenset(operation.required_side_effect_tools)
        side_effect_successes = sum(
            1
            for s in tool_steps
            if s.status == StepStatus.OK and s.tool_name in side_effect_tools
        )
    else:
        side_effect_successes = sum(1 for s in tool_steps if s.status == StepStatus.OK)

    # LLM tokens: sum of total_tokens on LLM steps only.
    llm_steps = [s for s in trace.steps if s.step_type == StepType.LLM]
    llm_tokens = sum(s.total_tokens or 0 for s in llm_steps)

    # Fallback: if no LLM steps with tokens, use trace-level total_tokens.
    if llm_tokens == 0:
        llm_tokens = trace.total_tokens

    # If no LLM token data at all (uninstrumented), skip D4 — we cannot
    # compute a meaningful ratio from absence of data.
    if llm_tokens == 0:
        return []

    ratio = side_effect_successes / max(1, llm_tokens / 1000)

    if ratio >= wtt_t:
        return []

    return [
        Finding(
            pattern_name="work_to_talk_ratio",
            tier=1,
            trace_id=trace.trace_id,
            confidence=0.7,
            severity="info",
            evidence={
                "wtt_ratio": round(ratio, 4),
                "threshold": wtt_t,
                "side_effect_successes": side_effect_successes,
                "llm_tokens": llm_tokens,
                "op_name": operation.name if operation else None,
            },
            affected_step_indices=[],
            estimated_token_waste=llm_tokens,
        )
    ]


# ── LEARN stage ────────────────────────────────────────────────────────────────


@dataclass
class ExpectationMissCandidate:
    """A trace that is missing a tool with high presence rate in its workflow.

    NOT a fired finding — returned to the discovery queue (Day 12).
    """

    trace_id: str
    workflow_name: str
    missing_tool: str
    presence_rate: float
    """Presence rate of the missing tool across clean traces in this workflow."""
    clean_trace_count: int
    """Number of clean traces used to compute the rate."""


@dataclass
class LearnResult:
    """Output of the LEARN stage for one workflow."""

    workflow_name: str
    clean_trace_count: int
    tool_presence_rates: dict[str, float]
    """tool_name -> fraction of clean traces that contain at least one OK call."""
    candidates: list[ExpectationMissCandidate]
    """Expectation-miss candidates for discovery queue (Day 12)."""
    abstained: bool = False
    """True when clean_trace_count < EXPECT_MIN_N — no candidates emitted."""
    abstain_reason: str | None = None


def _is_clean_trace(
    trace: TraceEnvelope,
    operation: BusinessOperation,
    struggle_t: float = STRUGGLE_T,
) -> bool:
    """Return True when a trace qualifies as a clean (outcome-pass, low-struggle) trace.

    Clean = no ERROR steps beyond the struggle threshold AND at least one
    successful side-effect call.  This is a lightweight proxy; full outcome
    evaluation is the authoritative check but would require circular imports
    here.  The proxy is conservative: a trace with any significant struggle
    is excluded from the learning corpus.
    """
    tool_steps = [s for s in trace.steps if s.step_type == StepType.TOOL_CALL and s.tool_name]
    if not tool_steps:
        return False

    error_steps = sum(1 for s in tool_steps if s.status == StepStatus.ERROR)
    side_effect_successes = sum(
        1
        for s in tool_steps
        if s.status == StepStatus.OK
        and s.tool_name in frozenset(operation.required_side_effect_tools)
    )
    if side_effect_successes == 0:
        return False

    # Use same struggle formula as D2 for consistency.
    redundant = _count_redundant_steps(trace.steps)
    struggle = (error_steps + redundant) / max(1, side_effect_successes)
    return struggle < struggle_t


def learn_tool_expectations(
    traces: list[TraceEnvelope],
    operation: BusinessOperation,
    expect_t: float = EXPECT_T,
    expect_min_n: int = EXPECT_MIN_N,
    struggle_t: float = STRUGGLE_T,
) -> LearnResult:
    """Compute per-tool presence rates from clean traces and return miss candidates.

    ``traces`` should be the traces mapped to ``operation`` (FULL or ATTEMPTED
    membership).  Clean traces are filtered internally.

    Returns a LearnResult.  If fewer than expect_min_n clean traces exist,
    abstains (no candidates, abstain_reason set).

    Candidates are NOT findings — they are surfaced to the discovery queue.
    """
    clean_traces = [t for t in traces if _is_clean_trace(t, operation, struggle_t)]
    clean_n = len(clean_traces)

    if clean_n < expect_min_n:
        return LearnResult(
            workflow_name=operation.name,
            clean_trace_count=clean_n,
            tool_presence_rates={},
            candidates=[],
            abstained=True,
            abstain_reason=(
                f"Only {clean_n} clean trace(s) for workflow '{operation.name}'; "
                f"need ≥{expect_min_n} to estimate presence rates."
            ),
        )

    # Compute presence rate per tool over the expected_tools set.
    all_tools = set(operation.expected_tools) | set(operation.required_side_effect_tools)
    tool_presence: dict[str, float] = {}
    for tool in all_tools:
        present_count = sum(
            1
            for t in clean_traces
            if any(
                s.step_type == StepType.TOOL_CALL
                and s.tool_name == tool
                and s.status == StepStatus.OK
                for s in t.steps
            )
        )
        tool_presence[tool] = present_count / clean_n

    # Identify tools that are near-universal (rate >= expect_t).
    expected_tools = {tool for tool, rate in tool_presence.items() if rate >= expect_t}

    # Find traces that are missing an expected tool.
    candidates: list[ExpectationMissCandidate] = []
    for trace in traces:
        # Check all traces (not just clean ones) for missing expectations.
        trace_tools = frozenset(
            s.tool_name
            for s in trace.steps
            if s.step_type == StepType.TOOL_CALL
            and s.tool_name is not None
            and s.status == StepStatus.OK
        )
        for tool in expected_tools:
            if tool not in trace_tools:
                candidates.append(
                    ExpectationMissCandidate(
                        trace_id=trace.trace_id,
                        workflow_name=operation.name,
                        missing_tool=tool,
                        presence_rate=tool_presence[tool],
                        clean_trace_count=clean_n,
                    )
                )

    return LearnResult(
        workflow_name=operation.name,
        clean_trace_count=clean_n,
        tool_presence_rates=tool_presence,
        candidates=candidates,
        abstained=False,
    )


# ── Orchestrator ───────────────────────────────────────────────────────────────


def detect_session_quality(
    traces: list[TraceEnvelope],
    operation: BusinessOperation | None = None,
    recovery_window: int = RECOVERY_WINDOW,
    struggle_t: float = STRUGGLE_T,
    repeat_t: int = REPEAT_T,
    curl_t: float = CURL_T,
    wtt_t: float = WTT_T,
) -> list[Finding]:
    """Run all four session-quality detectors and return aggregated findings.

    Called once per workflow cohort from the pipeline (or per-trace if no
    operation context).  All detectors are deterministic; none calls an LLM.
    """
    findings: list[Finding] = []
    for trace in traces:
        findings.extend(
            detect_unrecovered_error(trace, operation, recovery_window=recovery_window)
        )
        findings.extend(
            detect_struggle_ratio(trace, operation, struggle_t=struggle_t)
        )
        findings.extend(
            detect_coordination_waste(trace, repeat_t=repeat_t, curl_t=curl_t)
        )
        findings.extend(
            detect_work_to_talk_ratio(trace, operation, wtt_t=wtt_t)
        )
    return findings
