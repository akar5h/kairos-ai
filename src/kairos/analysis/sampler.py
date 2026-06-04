"""Stratified sampling for LLM-based dimension judges.

Flagged traces are sampled stratified-by-pattern so each pattern gets
at least some representation in the LLM's view.
Unflagged traces are pure-random with a deterministic seed so results
are reproducible.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from kairos.log import get_logger

if TYPE_CHECKING:
    from kairos.models.trace import TraceEnvelope

logger = get_logger(__name__)


@dataclass
class SampleBudget:
    """Budget caps for a single-dimension LLM sampling pass."""

    flagged_cap: int = 30
    unflagged_cap: int = 15
    unflagged_fraction: float = 0.2


@dataclass
class SampleResult:
    """Populations and their sampled subsets."""

    flagged_sampled: list[TraceEnvelope]
    unflagged_sampled: list[TraceEnvelope]
    flagged_total: int
    unflagged_total: int


def sample_for_judge(
    flagged: list[TraceEnvelope],
    unflagged: list[TraceEnvelope],
    *,
    budget: SampleBudget | None = None,
    seed_key: str = "",
    pattern_for_trace: dict[str, str] | None = None,
) -> SampleResult:
    """Sample flagged + unflagged populations for an LLM judge dimension.

    - Flagged: stratified-by-pattern using ``pattern_for_trace[trace_id]``.
      Equal quota per pattern first (``budget.flagged_cap // num_patterns``).
      Remaining slots assigned proportional-to-population when equal
      quota doesn't divide evenly. Deterministic ordering by trace_id.
    - Unflagged: pure-random sample with seeded RNG
      (``random.Random(hash(seed_key))``).
      Size = ``min(budget.unflagged_cap, ceil(budget.unflagged_fraction * len(unflagged)))``.
    """
    effective_budget = budget or SampleBudget()

    flagged_sampled = _sample_flagged(
        flagged,
        effective_budget.flagged_cap,
        pattern_for_trace or {},
    )
    unflagged_sampled = _sample_unflagged(
        unflagged,
        effective_budget,
        seed_key,
    )

    return SampleResult(
        flagged_sampled=flagged_sampled,
        unflagged_sampled=unflagged_sampled,
        flagged_total=len(flagged),
        unflagged_total=len(unflagged),
    )


def _sample_flagged(
    flagged: list[TraceEnvelope],
    cap: int,
    pattern_for_trace: dict[str, str],
) -> list[TraceEnvelope]:
    if not flagged or cap <= 0:
        return []

    # Group by pattern (trace_id -> pattern). Traces without a known
    # pattern go into a '__unknown__' bucket.
    by_pattern: dict[str, list[TraceEnvelope]] = defaultdict(list)
    for t in flagged:
        by_pattern[pattern_for_trace.get(t.trace_id, "__unknown__")].append(t)

    # Sort each bucket deterministically by trace_id.
    for bucket in by_pattern.values():
        bucket.sort(key=lambda x: x.trace_id)

    num_patterns = len(by_pattern)
    quota = cap // num_patterns if num_patterns else 0

    chosen: list[TraceEnvelope] = []
    remaining_by_pattern: dict[str, list[TraceEnvelope]] = {}
    # Equal-quota pass.
    for name, bucket in sorted(by_pattern.items()):
        take = bucket[:quota]
        chosen.extend(take)
        remaining_by_pattern[name] = bucket[quota:]

    # Fill any leftover budget proportional to remaining populations.
    slots_left = cap - len(chosen)
    if slots_left > 0:
        leftovers: list[TraceEnvelope] = []
        for _name, bucket in sorted(remaining_by_pattern.items()):
            leftovers.extend(bucket)
        leftovers.sort(key=lambda x: x.trace_id)
        chosen.extend(leftovers[:slots_left])

    return chosen[:cap]


def _sample_unflagged(
    unflagged: list[TraceEnvelope],
    budget: SampleBudget,
    seed_key: str,
) -> list[TraceEnvelope]:
    if not unflagged:
        return []
    target = min(
        budget.unflagged_cap,
        math.ceil(budget.unflagged_fraction * len(unflagged)),
    )
    if target <= 0:
        return []
    # Deterministic RNG from seed_key.
    rng = random.Random(hash(seed_key))  # noqa: S311 — sampling only, not crypto
    # Sort first for stable ordering before sampling so the RNG result
    # is deterministic regardless of input ordering.
    sorted_unflagged = sorted(unflagged, key=lambda x: x.trace_id)
    sample_size = min(target, len(sorted_unflagged))
    return rng.sample(sorted_unflagged, sample_size)
