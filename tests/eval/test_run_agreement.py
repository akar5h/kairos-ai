"""Tests for eval/run_agreement.py.

Covers:
  - Cohen's kappa formula on known matrices
  - AgreementStats derivation from rows
  - Decision tree branching (κ ≥ 0.7, κ < 0.7, abstention > 30%)
  - Disagreement classification
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Add the eval directory to sys.path so the module can be imported normally.
_EVAL_DIR = Path(__file__).parents[2] / "eval"
if str(_EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(_EVAL_DIR))

# Import the module normally now that its directory is on sys.path.
import run_agreement as _mod  # type: ignore[import-untyped]  # noqa: E402

_cohen_kappa = _mod._cohen_kappa
_compute_stats = _mod._compute_stats
_decision_tree = _mod._decision_tree
AgreementRow = _mod.AgreementRow
AgreementStats = _mod.AgreementStats


# ── Cohen's kappa formula ──────────────────────────────────────────────────


def test_kappa_perfect_agreement() -> None:
    """When a == d and b == c == 0, kappa should be 1.0."""
    kappa = _cohen_kappa(a=10, b=0, c=0, d=10)
    assert kappa is not None
    assert abs(kappa - 1.0) < 1e-9


def test_kappa_no_agreement() -> None:
    """Symmetric random agreement: a == d == 0, kappa should be -1.0."""
    kappa = _cohen_kappa(a=0, b=10, c=10, d=0)
    assert kappa is not None
    assert abs(kappa - (-1.0)) < 1e-9


def test_kappa_chance_level() -> None:
    """When verdicts are independent, κ ≈ 0.  Use a known small example.
    a=15, b=10, c=5, d=20 → po=0.7, pe=((25*20)+(25*30))/50^2 = (500+750)/2500=0.5, κ=0.4
    """
    a, b, c, d = 15, 10, 5, 20
    n = a + b + c + d  # 50
    po = (a + d) / n  # 0.7
    pe = ((a + b) * (a + c) + (c + d) * (b + d)) / (n * n)
    expected = (po - pe) / (1 - pe)
    kappa = _cohen_kappa(a=a, b=b, c=c, d=d)
    assert kappa is not None
    assert abs(kappa - expected) < 1e-9


def test_kappa_empty_matrix_returns_none() -> None:
    kappa = _cohen_kappa(a=0, b=0, c=0, d=0)
    assert kappa is None


def test_kappa_known_value() -> None:
    """Handcrafted: a=40, b=10, c=5, d=45 should give κ ≈ 0.7."""
    a, b, c, d = 40, 10, 5, 45
    n = a + b + c + d
    po = (a + d) / n
    pe = ((a + b) * (a + c) + (c + d) * (b + d)) / (n * n)
    expected = (po - pe) / (1 - pe)
    kappa = _cohen_kappa(a=a, b=b, c=c, d=d)
    assert kappa is not None
    assert abs(kappa - expected) < 1e-9


# ── AgreementStats from rows ──────────────────────────────────────────────


def _make_row(
    *,
    task_id: int = 1,
    reward: float = 1.0,
    bench_label: str = "PASS",
    kairos_verdict: str = "outcome_pass",
    failure_reason: str | None = None,
    trial: int = 0,
    env: str = "airline",
) -> AgreementRow:
    return AgreementRow(
        trace_id=f"trace-{task_id}-{trial}",
        task_id=task_id,
        trial=trial,
        env=env,
        model="kimi",
        reward=reward,
        bench_label=bench_label,
        kairos_verdict=kairos_verdict,
        failure_reason=failure_reason,
        bundle="bundle.json",
        mode="baseline",
    )


def test_stats_perfect_kappa() -> None:
    rows = [
        _make_row(task_id=1, bench_label="PASS", kairos_verdict="outcome_pass"),
        _make_row(task_id=2, bench_label="PASS", kairos_verdict="outcome_pass"),
        _make_row(task_id=3, bench_label="FAIL", reward=0.0, kairos_verdict="outcome_fail"),
        _make_row(task_id=4, bench_label="FAIL", reward=0.0, kairos_verdict="outcome_fail"),
    ]
    stats = _compute_stats(rows)
    assert stats.kappa == pytest.approx(1.0, abs=1e-9)
    assert stats.accuracy == pytest.approx(1.0, abs=1e-9)
    assert stats.abstention_rate == 0.0


def test_stats_abstention_counted() -> None:
    rows = [
        _make_row(task_id=1, bench_label="PASS", kairos_verdict="non_computable"),
        _make_row(task_id=2, bench_label="FAIL", reward=0.0, kairos_verdict="outcome_fail"),
    ]
    stats = _compute_stats(rows)
    assert stats.non_computable == 1
    # abstention_rate = non_computable / total = 1/2
    assert stats.abstention_rate == pytest.approx(0.5, abs=1e-9)


def test_stats_partial_excluded_from_binary() -> None:
    rows = [
        _make_row(task_id=1, bench_label="PARTIAL", reward=0.5, kairos_verdict="outcome_pass"),
        _make_row(task_id=2, bench_label="PASS", kairos_verdict="outcome_pass"),
        _make_row(task_id=3, bench_label="FAIL", reward=0.0, kairos_verdict="outcome_fail"),
    ]
    stats = _compute_stats(rows)
    # Only 2 binary-eligible rows
    assert stats.binary_eligible == 2
    # Computable binary should be 2
    assert stats.computable == 2
    assert stats.a == 1
    assert stats.d == 1


def test_stats_confusion_matrix_cells() -> None:
    rows = [
        _make_row(task_id=1, bench_label="PASS", kairos_verdict="outcome_pass"),     # a
        _make_row(task_id=2, bench_label="FAIL", reward=0.0, kairos_verdict="outcome_pass"),   # b
        _make_row(task_id=3, bench_label="PASS", kairos_verdict="outcome_fail"),   # c
        _make_row(task_id=4, bench_label="FAIL", reward=0.0, kairos_verdict="outcome_fail"),   # d
    ]
    stats = _compute_stats(rows)
    assert stats.a == 1
    assert stats.b == 1
    assert stats.c == 1
    assert stats.d == 1


def test_stats_unique_task_dedup() -> None:
    """Duplicate task_id across bundles: unique-task deduplicated correctly."""
    rows = [
        # Same task_id=5 across two bundles (two distinct executions).
        _make_row(task_id=5, bench_label="PASS", kairos_verdict="outcome_pass", trial=0),
        _make_row(task_id=5, bench_label="FAIL", reward=0.0, kairos_verdict="outcome_fail", trial=1),
        _make_row(task_id=6, bench_label="FAIL", reward=0.0, kairos_verdict="outcome_fail"),
    ]
    stats = _compute_stats(rows)
    # Total rows = 3; unique tasks = 2 (task_id 5 and 6).
    assert stats.unique_task_n == 2


# ── Decision tree ─────────────────────────────────────────────────────────


def _make_stats(
    *,
    total: int = 100,
    kappa: float | None = 0.75,
    abstention_rate: float = 0.05,
) -> AgreementStats:
    return AgreementStats(
        total=total,
        binary_eligible=total,
        computable=int(total * (1 - abstention_rate)),
        non_computable=int(total * abstention_rate),
        abstention_rate=abstention_rate,
        a=40,
        b=5,
        c=5,
        d=40,
        accuracy=0.8,
        kappa=kappa,
        unique_task_kappa=kappa,
        unique_task_n=30,
    )


def test_decision_proceed_day7() -> None:
    stats = _make_stats(kappa=0.75, abstention_rate=0.05)
    decision = _decision_tree(stats)
    assert "proceed to Day 7" in decision


def test_decision_kappa_below_threshold() -> None:
    stats = _make_stats(kappa=0.50, abstention_rate=0.05)
    decision = _decision_tree(stats)
    assert "kappa<0.7" in decision


def test_decision_abstention_too_high() -> None:
    stats = _make_stats(kappa=0.80, abstention_rate=0.35)
    decision = _decision_tree(stats)
    assert "abstention>30%" in decision


def test_decision_small_n() -> None:
    stats = _make_stats(total=50, kappa=0.8, abstention_rate=0.0)
    decision = _decision_tree(stats)
    assert "pairs<75" in decision


def test_decision_kappa_none() -> None:
    stats = _make_stats(kappa=None, abstention_rate=0.0)
    decision = _decision_tree(stats)
    assert "kappa=None" in decision or "inspect" in decision
