"""Red-phase tests for the stratified+random sampler used by the LLM judges.

Target module (not yet implemented):
    src.kairos.analysis.sampler

Expected surface:
    @dataclass SampleBudget
    @dataclass SampleResult
    def sample_for_judge(
        flagged: list[TraceEnvelope],
        unflagged: list[TraceEnvelope],
        *,
        budget: SampleBudget | None = None,
        seed_key: str = "",
        pattern_for_trace: dict[str, str] | None = None,
    ) -> SampleResult

Rules:
    - flagged traces are stratified by pattern_for_trace[trace.trace_id].
      Equal quota per pattern; proportional fallback when uneven.
      Capped at budget.flagged_cap.
    - unflagged traces are a pure random sample using
      random.Random(hash(seed_key)) as the RNG.
      Size = min(unflagged_cap, ceil(unflagged_fraction * len(unflagged))).
    - deterministic given same inputs + same seed_key.
    - empty inputs return empty lists.
"""

from __future__ import annotations

import math
from typing import Any

from kairos.analysis.sampler import (
    SampleBudget,
    SampleResult,
    sample_for_judge,
)
from kairos.models.enums import StepStatus, StepType, TerminalStatus
from kairos.models.trace import Step, TraceEnvelope

# ── Synthesis helpers ──────────────────────────────────────────────────


def _step(
    i: int,
    tool: str,
    *,
    status: StepStatus = StepStatus.OK,
    tool_args: dict[str, Any] | None = None,
    tool_output: str | None = "ok",
) -> Step:
    return Step(
        step_index=i,
        step_type=StepType.TOOL_CALL,
        tool_name=tool,
        tool_args=tool_args if tool_args is not None else {"i": i, "tool": tool},
        tool_args_normalized=tool_args if tool_args is not None else {"i": i, "tool": tool},
        tool_output=tool_output,
        status=status,
    )


def _trace(trace_id: str, tools: list[str] | None = None) -> TraceEnvelope:
    tools = tools if tools is not None else ["tool_a"]
    steps = [_step(i, t, tool_args={"trace": trace_id, "i": i}) for i, t in enumerate(tools)]
    return TraceEnvelope(
        trace_id=trace_id,
        user_input="do the thing",
        steps=steps,
        terminal_status=TerminalStatus.COMPLETED,
    )


# ── TESTS ─────────────────────────────────────────────────────────────


class TestStratifiedSampling:
    """Flagged traces get stratified by pattern."""

    def test_flagged_stratified_by_pattern(self) -> None:
        # 6 flagged across 3 patterns (2 each). budget.flagged_cap=3 → 1 per pattern.
        flagged = [_trace(f"f-{i}") for i in range(6)]
        patterns = {
            "f-0": "pattern_A",
            "f-1": "pattern_A",
            "f-2": "pattern_B",
            "f-3": "pattern_B",
            "f-4": "pattern_C",
            "f-5": "pattern_C",
        }
        budget = SampleBudget(flagged_cap=3, unflagged_cap=0, unflagged_fraction=0.0)
        result = sample_for_judge(
            flagged,
            [],
            budget=budget,
            seed_key="test",
            pattern_for_trace=patterns,
        )

        assert len(result.flagged_sampled) == 3
        sampled_patterns = sorted(patterns[t.trace_id] for t in result.flagged_sampled)
        assert sampled_patterns == ["pattern_A", "pattern_B", "pattern_C"]

    def test_flagged_uneven_populations_equal_quota(self) -> None:
        # 10 flagged: 6 pattern_A, 3 pattern_B, 1 pattern_C; budget=3 → 1-1-1.
        flagged = [_trace(f"f-{i}") for i in range(10)]
        patterns: dict[str, str] = {}
        for i in range(6):
            patterns[f"f-{i}"] = "pattern_A"
        for i in range(6, 9):
            patterns[f"f-{i}"] = "pattern_B"
        patterns["f-9"] = "pattern_C"

        budget = SampleBudget(flagged_cap=3, unflagged_cap=0, unflagged_fraction=0.0)
        result = sample_for_judge(
            flagged,
            [],
            budget=budget,
            seed_key="uneven",
            pattern_for_trace=patterns,
        )

        assert len(result.flagged_sampled) == 3
        counts: dict[str, int] = {}
        for t in result.flagged_sampled:
            counts[patterns[t.trace_id]] = counts.get(patterns[t.trace_id], 0) + 1
        # Equal quota: 1 per pattern.
        assert counts == {"pattern_A": 1, "pattern_B": 1, "pattern_C": 1}

    def test_flagged_uneven_populations_proportional_fallback(self) -> None:
        # 10 flagged: 6 pattern_A, 3 pattern_B, 1 pattern_C; budget=6 → 3-2-1 proportional.
        flagged = [_trace(f"f-{i}") for i in range(10)]
        patterns: dict[str, str] = {}
        for i in range(6):
            patterns[f"f-{i}"] = "pattern_A"
        for i in range(6, 9):
            patterns[f"f-{i}"] = "pattern_B"
        patterns["f-9"] = "pattern_C"

        budget = SampleBudget(flagged_cap=6, unflagged_cap=0, unflagged_fraction=0.0)
        result = sample_for_judge(
            flagged,
            [],
            budget=budget,
            seed_key="prop",
            pattern_for_trace=patterns,
        )

        assert len(result.flagged_sampled) == 6
        counts: dict[str, int] = {}
        for t in result.flagged_sampled:
            counts[patterns[t.trace_id]] = counts.get(patterns[t.trace_id], 0) + 1
        assert counts == {"pattern_A": 3, "pattern_B": 2, "pattern_C": 1}

    def test_flagged_budget_respected(self) -> None:
        flagged = [_trace(f"f-{i}") for i in range(20)]
        patterns = {t.trace_id: "pattern_A" for t in flagged}
        budget = SampleBudget(flagged_cap=5, unflagged_cap=0, unflagged_fraction=0.0)
        result = sample_for_judge(
            flagged,
            [],
            budget=budget,
            seed_key="budget",
            pattern_for_trace=patterns,
        )
        assert len(result.flagged_sampled) == 5


class TestRandomUnflagged:
    """Unflagged traces get sampled as a pure random subsample."""

    def test_unflagged_respects_cap_and_fraction(self) -> None:
        # 100 unflagged, cap=15, fraction=0.2 → min(15, ceil(20)) = 15.
        unflagged = [_trace(f"u-{i}") for i in range(100)]
        budget = SampleBudget(flagged_cap=0, unflagged_cap=15, unflagged_fraction=0.2)
        result = sample_for_judge([], unflagged, budget=budget, seed_key="k")
        assert len(result.unflagged_sampled) == 15

    def test_unflagged_respects_fraction_when_smaller(self) -> None:
        # 10 unflagged, fraction=0.2 → ceil(2) = 2 samples.
        unflagged = [_trace(f"u-{i}") for i in range(10)]
        budget = SampleBudget(flagged_cap=0, unflagged_cap=15, unflagged_fraction=0.2)
        result = sample_for_judge([], unflagged, budget=budget, seed_key="k")
        assert len(result.unflagged_sampled) == math.ceil(0.2 * 10)
        assert len(result.unflagged_sampled) == 2

    def test_unflagged_deterministic_with_same_seed(self) -> None:
        unflagged = [_trace(f"u-{i}") for i in range(50)]
        budget = SampleBudget(flagged_cap=0, unflagged_cap=10, unflagged_fraction=0.5)
        result_a = sample_for_judge([], unflagged, budget=budget, seed_key="same")
        result_b = sample_for_judge([], unflagged, budget=budget, seed_key="same")

        ids_a = [t.trace_id for t in result_a.unflagged_sampled]
        ids_b = [t.trace_id for t in result_b.unflagged_sampled]
        assert ids_a == ids_b

    def test_unflagged_different_seeds_produce_different_samples(self) -> None:
        # Large enough population that the collision probability is negligible.
        unflagged = [_trace(f"u-{i}") for i in range(50)]
        budget = SampleBudget(flagged_cap=0, unflagged_cap=10, unflagged_fraction=0.5)
        result_a = sample_for_judge([], unflagged, budget=budget, seed_key="seed-a")
        result_b = sample_for_judge([], unflagged, budget=budget, seed_key="seed-b")

        ids_a = [t.trace_id for t in result_a.unflagged_sampled]
        ids_b = [t.trace_id for t in result_b.unflagged_sampled]
        # Different seeds should produce at least some difference.
        assert ids_a != ids_b


class TestEdgeCases:
    """Empty / boundary inputs."""

    def test_empty_flagged_returns_empty_sampled(self) -> None:
        unflagged = [_trace(f"u-{i}") for i in range(5)]
        result = sample_for_judge([], unflagged, seed_key="empty-flagged")
        assert result.flagged_sampled == []
        assert result.flagged_total == 0

    def test_empty_unflagged_returns_empty_sampled(self) -> None:
        flagged = [_trace(f"f-{i}") for i in range(2)]
        patterns = {t.trace_id: "pattern_A" for t in flagged}
        result = sample_for_judge(
            flagged,
            [],
            seed_key="empty-unflagged",
            pattern_for_trace=patterns,
        )
        assert result.unflagged_sampled == []
        assert result.unflagged_total == 0

    def test_sample_result_includes_population_totals(self) -> None:
        flagged = [_trace(f"f-{i}") for i in range(10)]
        unflagged = [_trace(f"u-{i}") for i in range(50)]
        patterns = {t.trace_id: "pattern_A" for t in flagged}
        result = sample_for_judge(
            flagged,
            unflagged,
            seed_key="totals",
            pattern_for_trace=patterns,
        )

        assert isinstance(result, SampleResult)
        assert result.flagged_total == 10
        assert result.unflagged_total == 50
