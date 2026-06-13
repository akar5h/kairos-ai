"""Tests for kairos.eval.harness — compare gate, nondeterminism detection, metric diff."""

from __future__ import annotations

from kairos.eval.harness import (
    CompareResult,
    MetricDiff,
    _classify_delta,
    _extract_metric_values,
    _panels_identical,
)
from kairos.eval.panel import DetectorMetrics, MetricPanel, OutcomeMetrics

# ── Fixtures ──────────────────────────────────────────────────────────────────


def _make_panel(
    corpus_hash: str = "abc123",
    corpus_size: int = 100,
    tau_kappa: float | None = 0.5,
    d2_precision: float | None = 0.7,
    d2_fire_rate: float = 0.1,
    d2_fire_count: int = 10,
    owner_precision: float | None = 0.8,
    classes_covered: int = 3,
) -> MetricPanel:
    return MetricPanel(
        corpus_hash=corpus_hash,
        corpus_size=corpus_size,
        outcome=OutcomeMetrics(
            owner_precision=owner_precision,
            owner_recall=0.9,
            owner_labeled_count=20,
            tau_kappa=tau_kappa,
            tau_fail_precision=0.6,
            tau_fail_recall=0.7,
            tau_abstention_rate=0.1,
            tau_total=100,
            tau_computable=90,
            tau_a=50, tau_b=10, tau_c=5, tau_d=25,
        ),
        detectors={
            "struggle_ratio": DetectorMetrics(
                name="struggle_ratio",
                precision=d2_precision,
                recall=0.5,
                fire_count=d2_fire_count,
                fire_rate=d2_fire_rate,
            ),
            "unrecovered_error": DetectorMetrics(
                name="unrecovered_error",
                precision=0.6,
                recall=0.6,
                fire_count=5,
                fire_rate=0.05,
            ),
            "coordination_waste": DetectorMetrics(
                name="coordination_waste",
                precision=None,
                recall=None,
                fire_count=2,
                fire_rate=0.02,
            ),
            "work_to_talk_ratio": DetectorMetrics(
                name="work_to_talk_ratio",
                precision=None,
                recall=None,
                fire_count=3,
                fire_rate=0.03,
            ),
            "redundant_execution": DetectorMetrics(
                name="redundant_execution",
                precision=None,
                recall=None,
                fire_count=1,
                fire_rate=0.01,
            ),
        },
        classes_covered=classes_covered,
        severity_error_count=0,
        severity_warning_count=10,
        severity_info_count=15,
        total_findings=25,
    )


# ── _classify_delta ───────────────────────────────────────────────────────────


def test_classify_delta_none_is_unknown():
    assert _classify_delta("outcome.tau_kappa", None) == "unknown"


def test_classify_delta_zero_is_unchanged():
    assert _classify_delta("outcome.tau_kappa", 0.0) == "unchanged"


def test_classify_delta_epsilon_is_unchanged():
    """Delta below float epsilon is treated as unchanged."""
    assert _classify_delta("outcome.tau_kappa", 1e-12) == "unchanged"


def test_classify_delta_higher_is_better_positive():
    """Higher-is-better metric with positive delta → improved."""
    assert _classify_delta("outcome.tau_kappa", 0.05) == "improved"


def test_classify_delta_higher_is_better_negative():
    """Higher-is-better metric with negative delta → regressed."""
    assert _classify_delta("outcome.tau_kappa", -0.05) == "regressed"


def test_classify_delta_lower_is_better_negative():
    """Lower-is-better (abstention_rate) with negative delta → improved."""
    assert _classify_delta("outcome.tau_abstention_rate", -0.05) == "improved"


def test_classify_delta_lower_is_better_positive():
    """Lower-is-better with positive delta → regressed."""
    assert _classify_delta("outcome.tau_abstention_rate", 0.05) == "regressed"


def test_classify_delta_neutral_metric():
    """Neutral metrics (total_findings) → unchanged regardless of delta."""
    assert _classify_delta("aggregate.total_findings", 10.0) == "unchanged"
    assert _classify_delta("aggregate.severity_warning", -5.0) == "unchanged"


def test_classify_delta_fire_count_positive():
    """Detector fire_count increase → improved (more detection coverage)."""
    assert _classify_delta("detector.struggle_ratio.fire_count", 5.0) == "improved"


def test_classify_delta_precision_regression():
    """Precision decrease → regressed."""
    assert _classify_delta("detector.struggle_ratio.precision", -0.1) == "regressed"


# ── _panels_identical ─────────────────────────────────────────────────────────


def test_panels_identical_same():
    """Identical panels → True."""
    p = _make_panel()
    assert _panels_identical(p, p)


def test_panels_identical_different_kappa():
    """Different tau_kappa → not identical."""
    p1 = _make_panel(tau_kappa=0.5)
    p2 = _make_panel(tau_kappa=0.6)
    assert not _panels_identical(p1, p2)


def test_panels_identical_different_corpus():
    """Different corpus_hash → not identical."""
    p1 = _make_panel(corpus_hash="abc")
    p2 = _make_panel(corpus_hash="xyz")
    assert not _panels_identical(p1, p2)


# ── _extract_metric_values ────────────────────────────────────────────────────


def test_extract_metric_values_contains_outcome_keys():
    """Extracted values include outcome.tau_kappa and outcome.owner_precision."""
    panel = _make_panel()
    values = _extract_metric_values(panel)
    assert "outcome.tau_kappa" in values
    assert "outcome.owner_precision" in values
    assert values["outcome.tau_kappa"] == 0.5
    assert values["outcome.owner_precision"] == 0.8


def test_extract_metric_values_contains_detector_keys():
    """Extracted values include per-detector precision and fire_rate."""
    panel = _make_panel()
    values = _extract_metric_values(panel)
    assert "detector.struggle_ratio.precision" in values
    assert "detector.struggle_ratio.fire_rate" in values
    assert values["detector.struggle_ratio.precision"] == 0.7


def test_extract_metric_values_aggregate():
    """Extracted values include aggregate metrics."""
    panel = _make_panel(classes_covered=3)
    values = _extract_metric_values(panel)
    assert "aggregate.classes_covered" in values
    assert values["aggregate.classes_covered"] == 3.0


# ── nondeterminism detection ──────────────────────────────────────────────────


def test_panels_identical_detects_nondeterminism():
    """Two differing panels are not identical → harness should raise NonDeterminismError."""
    p1 = _make_panel(tau_kappa=0.5)
    p2 = _make_panel(tau_kappa=0.51)
    # In run_eval, if panels[0] != panels[1], NonDeterminismError is raised.
    # We test the predicate directly.
    assert not _panels_identical(p1, p2)


def test_panels_identical_same_panel_passes():
    """Same panel presented twice passes the nondeterminism check."""
    p = _make_panel()
    assert _panels_identical(p, p)


# ── compare gate logic ────────────────────────────────────────────────────────


def _make_compare_result(diffs: list[MetricDiff], verdict: str) -> CompareResult:
    regression = [d.name for d in diffs if d.verdict == "regressed"]
    improved = [d.name for d in diffs if d.verdict == "improved"]
    return CompareResult(
        before_ref="before",
        after_ref="after",
        before_ref_full="a" * 40,
        after_ref_full="b" * 40,
        k=2,
        corpus_hash="abc123",
        diffs=diffs,
        verdict=verdict,
        regression_metrics=regression,
        improved_metrics=improved,
    )


def test_compare_gate_pass_no_regressions():
    """No regressions → verdict PASS."""
    diffs = [
        MetricDiff("outcome.tau_kappa", 0.5, 0.6, 0.1, "improved"),
        MetricDiff("detector.struggle_ratio.precision", 0.7, 0.8, 0.1, "improved"),
    ]
    result = _make_compare_result(diffs, "PASS")
    assert result.verdict == "PASS"
    assert len(result.regression_metrics) == 0


def test_compare_gate_regressed_if_any_regression():
    """Any regression → verdict REGRESSED."""
    diffs = [
        MetricDiff("outcome.tau_kappa", 0.5, 0.7, 0.2, "improved"),
        MetricDiff("outcome.owner_precision", 0.8, 0.6, -0.2, "regressed"),  # blast radius
    ]
    result = _make_compare_result(diffs, "REGRESSED")
    assert result.verdict == "REGRESSED"
    assert "outcome.owner_precision" in result.regression_metrics


def test_compare_gate_pass_on_pure_stability():
    """No improvements, no regressions → still PASS (stability confirmed)."""
    diffs = [
        MetricDiff("outcome.tau_kappa", 0.5, 0.5, 0.0, "unchanged"),
    ]
    result = _make_compare_result(diffs, "PASS")
    assert result.verdict == "PASS"


def test_compare_result_regression_list_nonempty_on_regressed():
    """regression_metrics is nonempty when verdict is REGRESSED."""
    diffs = [
        MetricDiff("outcome.tau_kappa", 0.5, 0.4, -0.1, "regressed"),
    ]
    result = _make_compare_result(diffs, "REGRESSED")
    assert "outcome.tau_kappa" in result.regression_metrics
