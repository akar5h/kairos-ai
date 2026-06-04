"""Agent Correctness Score — 5-dimension layered scoring.

Each dimension returns a ``DimensionScore`` with:
  - deterministic_rate : pass rate from rule-based layer (None when not computable)
  - estimated_rate     : LLM-extrapolated pass rate (Slice A: stubbed to None)
  - ci_low / ci_high   : Wilson score interval around estimated_rate
  - sample_size        : number of traces an LLM judge actually evaluated
  - mode               : "deterministic" | "sampled" | "stubbed"
  - note               : human-readable explanation

Two headline numbers:
  - intersection_rate : % of traces correct on ALL 5 dimensions (None when any is stubbed)
  - composite_rate    : weighted average of computed dimension rates (None when nothing computed)

Slice A: Task Execution + Path Integrity are deterministic. The other 3
are stubbed (return mode="stubbed") until Slice B wires real LLM judges.
``provisional=True`` whenever any dimension is stubbed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from kairos.log import get_logger

if TYPE_CHECKING:
    from kairos.analysis.llm_client import LLMClient
    from kairos.engine.pipeline import WorkflowSummary
    from kairos.models.trace import TraceEnvelope
    from kairos.taxonomy.business_context import BusinessOperation

logger = get_logger(__name__)


DIMENSION_TASK_EXECUTION = "task_execution"
DIMENSION_OUTPUT_CORRECTNESS = "output_correctness"
DIMENSION_PATH_INTEGRITY = "path_integrity"
DIMENSION_DECISION_QUALITY = "decision_quality"
DIMENSION_CONTEXT_HANDLING = "context_handling"

ALL_DIMENSIONS = (
    DIMENSION_TASK_EXECUTION,
    DIMENSION_OUTPUT_CORRECTNESS,
    DIMENSION_PATH_INTEGRITY,
    DIMENSION_DECISION_QUALITY,
    DIMENSION_CONTEXT_HANDLING,
)

DEFAULT_LLM_BUDGET = 200


class DimensionVerdict(StrEnum):
    """Per-dimension verdicts emitted by LLM judges (Slice B)."""

    PASS = "pass"  # noqa: S105 — verdict label, not a secret
    FAIL = "fail"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


@dataclass
class DimensionScore:
    """One dimension of the Agent Correctness Score."""

    name: str
    deterministic_rate: float | None
    estimated_rate: float | None
    ci_low: float | None
    ci_high: float | None
    sample_size: int
    mode: str
    note: str | None = None


@dataclass
class AgentCorrectnessScore:
    """Top-level 5-dimension correctness score for one workflow."""

    workflow_name: str
    mapped_trace_count: int
    dimensions: dict[str, DimensionScore]
    intersection_rate: float | None
    composite_rate: float | None
    provisional: bool
    budget_used: int
    budget_cap_hit: bool


def wilson_ci(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion."""
    if total <= 0:
        return (0.0, 1.0)
    p = successes / total
    denom = 1 + (z * z) / total
    center = (p + (z * z) / (2 * total)) / denom
    margin_numerator = p * (1 - p) / total + (z * z) / (4 * total * total)
    margin = z * math.sqrt(max(0.0, margin_numerator)) / denom
    low = max(0.0, center - margin)
    high = min(1.0, center + margin)
    return (low, high)


def compute_correctness_score(
    workflow: WorkflowSummary,
    mapped_traces: list[TraceEnvelope],
    *,
    operation: BusinessOperation,
    llm_client: LLMClient | None = None,  # noqa: ARG001 (reserved for Slice B)
    budget: int = DEFAULT_LLM_BUDGET,  # noqa: ARG001
    seed_key: str = "",  # noqa: ARG001
) -> AgentCorrectnessScore:
    """Compute the Agent Correctness Score for one workflow.

    Slice A: Task Execution + Path Integrity are fully deterministic.
    Output Correctness / Decision Quality / Context Handling return
    mode='stubbed' placeholders because the LLM judges land in Slice B.
    """
    total = len(mapped_traces) or workflow.mapped_trace_count

    task_exec = _score_task_execution(workflow, total)
    path_int = _score_path_integrity(workflow, mapped_traces, total)
    output_corr = _stub_dimension(
        DIMENSION_OUTPUT_CORRECTNESS,
        total,
        criteria_missing=len(operation.correctness_criteria) == 0,
        business_goal_missing=operation.business_goal is None,
    )
    decision_q = _stub_dimension(
        DIMENSION_DECISION_QUALITY,
        total,
        criteria_missing=False,
        business_goal_missing=operation.business_goal is None,
    )
    ctx_handling = _stub_dimension(
        DIMENSION_CONTEXT_HANDLING,
        total,
        criteria_missing=False,
        business_goal_missing=operation.business_goal is None,
    )

    dimensions: dict[str, DimensionScore] = {
        DIMENSION_TASK_EXECUTION: task_exec,
        DIMENSION_OUTPUT_CORRECTNESS: output_corr,
        DIMENSION_PATH_INTEGRITY: path_int,
        DIMENSION_DECISION_QUALITY: decision_q,
        DIMENSION_CONTEXT_HANDLING: ctx_handling,
    }

    intersection = _compute_intersection(dimensions)
    composite = _compute_composite(dimensions)
    provisional = any(d.mode == "stubbed" for d in dimensions.values())

    return AgentCorrectnessScore(
        workflow_name=workflow.operation_name,
        mapped_trace_count=total,
        dimensions=dimensions,
        intersection_rate=intersection,
        composite_rate=composite,
        provisional=provisional,
        budget_used=0,
        budget_cap_hit=False,
    )


def _score_task_execution(workflow: WorkflowSummary, total: int) -> DimensionScore:
    outcome = workflow.outcome
    computable = outcome.computable_count
    passed = outcome.passed_count
    if computable == 0 or total == 0:
        return DimensionScore(
            name=DIMENSION_TASK_EXECUTION,
            deterministic_rate=None,
            estimated_rate=None,
            ci_low=None,
            ci_high=None,
            sample_size=0,
            mode="insufficient_evidence",
            note="no computable traces",
        )
    rate = passed / computable
    ci = wilson_ci(passed, computable)
    return DimensionScore(
        name=DIMENSION_TASK_EXECUTION,
        deterministic_rate=rate,
        estimated_rate=rate,
        ci_low=ci[0],
        ci_high=ci[1],
        sample_size=0,
        mode="deterministic",
        note=None,
    )


def _score_path_integrity(
    workflow: WorkflowSummary,
    mapped_traces: list[TraceEnvelope],
    total: int,
) -> DimensionScore:
    if total == 0:
        return DimensionScore(
            name=DIMENSION_PATH_INTEGRITY,
            deterministic_rate=None,
            estimated_rate=None,
            ci_low=None,
            ci_high=None,
            sample_size=0,
            mode="insufficient_evidence",
            note="no mapped traces",
        )
    flagged_ids: set[str] = {f.trace_id for f in workflow.deterministic_findings}
    for d in workflow.divergences:
        if d.first_divergence_step is not None:
            flagged_ids.add(d.trace_id)
    trace_ids = [t.trace_id for t in mapped_traces] or [str(i) for i in range(total)]
    passed = sum(1 for tid in trace_ids if tid not in flagged_ids)
    rate = passed / total
    ci = wilson_ci(passed, total)
    return DimensionScore(
        name=DIMENSION_PATH_INTEGRITY,
        deterministic_rate=rate,
        estimated_rate=rate,
        ci_low=ci[0],
        ci_high=ci[1],
        sample_size=0,
        mode="deterministic",
        note=None,
    )


def _stub_dimension(
    name: str,
    total: int,
    *,
    criteria_missing: bool,
    business_goal_missing: bool,
) -> DimensionScore:
    if total == 0:
        note = "no mapped traces"
        mode = "insufficient_evidence"
    elif name == DIMENSION_OUTPUT_CORRECTNESS and criteria_missing:
        note = "correctness_criteria not defined — define in YAML to unlock this dimension"
        mode = "stubbed"
    elif business_goal_missing:
        note = "business_goal not defined — judgments require business taxonomy"
        mode = "stubbed"
    else:
        note = "LLM judge not yet wired (Slice B)"
        mode = "stubbed"
    return DimensionScore(
        name=name,
        deterministic_rate=None,
        estimated_rate=None,
        ci_low=None,
        ci_high=None,
        sample_size=0,
        mode=mode,
        note=note,
    )


def _compute_intersection(dimensions: dict[str, DimensionScore]) -> float | None:
    # Intersection requires every dimension to have a real rate.
    rates: list[float] = []
    for d in dimensions.values():
        if d.estimated_rate is None:
            return None
        rates.append(d.estimated_rate)
    # Conservative upper bound on "correct on all dimensions" — product
    # of per-dimension rates when we have no per-trace correlation signal.
    product = 1.0
    for r in rates:
        product *= r
    return product


def _compute_composite(dimensions: dict[str, DimensionScore]) -> float | None:
    rates = [d.estimated_rate for d in dimensions.values() if d.estimated_rate is not None]
    if not rates:
        return None
    return sum(rates) / len(rates)
