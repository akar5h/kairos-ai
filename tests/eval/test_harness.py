"""Tests for kairos.eval.harness — compare gate, nondeterminism detection, metric diff."""

from __future__ import annotations

from kairos.eval.harness import (
    CompareResult,
    MetricDiff,
    _classify_delta,
    _extract_metric_values,
    _metric_tier,
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


# ── _metric_tier (three-tier classification) ──────────────────────────────────


def test_tier_gate_metrics():
    """Grounded-quality metrics are GATE tier."""
    for m in (
        "outcome.owner_precision",
        "outcome.owner_recall",
        "outcome.tau_kappa",
        "outcome.tau_fail_precision",
        "outcome.tau_fail_recall",
    ):
        assert _metric_tier(m) == "gate", m


def test_tier_review_metrics():
    """Detector precision/recall vs labels are REVIEW tier."""
    assert _metric_tier("detector.struggle_ratio.precision") == "review"
    assert _metric_tier("detector.coordination_waste.recall") == "review"


def test_tier_info_metrics():
    """Volume / severity / abstention / aggregate metrics are INFO tier."""
    for m in (
        "detector.struggle_ratio.fire_count",
        "detector.struggle_ratio.fire_rate",
        "aggregate.total_findings",
        "aggregate.severity_error",
        "aggregate.severity_warning",
        "aggregate.classes_covered",
        "outcome.tau_abstention_rate",
    ):
        assert _metric_tier(m) == "info", m


# ── _classify_delta (tier-aware) ──────────────────────────────────────────────


def test_classify_delta_none_is_unknown():
    assert _classify_delta("outcome.tau_kappa", None) == "unknown"


def test_classify_delta_gate_zero_is_unchanged():
    assert _classify_delta("outcome.tau_kappa", 0.0) == "unchanged"


def test_classify_delta_gate_within_epsilon_unchanged():
    """A GATE delta within epsilon (0.01) is noise → unchanged."""
    assert _classify_delta("outcome.owner_precision", 0.005) == "unchanged"
    assert _classify_delta("outcome.owner_precision", -0.01) == "unchanged"


def test_classify_delta_gate_drop_beyond_epsilon_regressed():
    """A GATE drop beyond epsilon → regressed (fails the gate)."""
    assert _classify_delta("outcome.owner_precision", -0.05) == "regressed"
    assert _classify_delta("outcome.tau_fail_precision", -0.02) == "regressed"


def test_classify_delta_gate_rise_beyond_epsilon_improved():
    assert _classify_delta("outcome.tau_kappa", 0.05) == "improved"


def test_classify_delta_review_precision_drop():
    """REVIEW (detector precision) drop is directional but not gate-failing."""
    assert _classify_delta("detector.struggle_ratio.precision", -0.1) == "regressed"
    assert _classify_delta("detector.struggle_ratio.precision", 0.1) == "improved"


def test_classify_delta_review_recall_drop():
    assert _classify_delta("detector.coordination_waste.recall", -0.5) == "regressed"


def test_classify_delta_info_never_regressed():
    """INFO (volume) deltas are never 'regressed' — diagnostic only."""
    assert _classify_delta("detector.struggle_ratio.fire_count", -54.0) == "unchanged"
    assert _classify_delta("detector.struggle_ratio.fire_rate", -0.1) == "unchanged"
    assert _classify_delta("aggregate.total_findings", 10.0) == "unchanged"
    assert _classify_delta("aggregate.severity_warning", -5.0) == "unchanged"
    assert _classify_delta("outcome.tau_abstention_rate", 0.05) == "unchanged"


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


# ── compare gate logic (three-tier) ───────────────────────────────────────────


def _diff(name: str, before: float, after: float) -> MetricDiff:
    """Build a MetricDiff the way compare() does — tier + verdict from the helpers."""
    delta = after - before
    return MetricDiff(
        name=name,
        before=before,
        after=after,
        delta=delta,
        verdict=_classify_delta(name, delta),
        tier=_metric_tier(name),
    )


def _aggregate(diffs: list[MetricDiff]) -> CompareResult:
    """Mirror compare()'s tier-based verdict aggregation for unit testing."""
    regression = [d.name for d in diffs if d.tier == "gate" and d.verdict == "regressed"]
    improved = [
        d.name for d in diffs
        if d.tier in {"gate", "review"} and d.verdict == "improved"
    ]
    review = [
        d.name for d in diffs
        if d.tier == "review" and d.verdict in {"regressed", "improved"}
    ]
    info = [
        d.name for d in diffs
        if d.tier == "info" and d.delta is not None and abs(d.delta) > 1e-9
    ]
    verdict = "REGRESSED" if regression else "PASS"
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
        review_metrics=review,
        info_metrics=info,
    )


def test_gate_volume_only_change_is_pass():
    """A volume-only drop (the F10 false-positive suppression) → PASS, INFO-only."""
    diffs = [
        _diff("detector.struggle_ratio.fire_count", 78.0, 24.0),
        _diff("detector.coordination_waste.fire_count", 234.0, 162.0),
        _diff("detector.struggle_ratio.fire_rate", 0.1538, 0.0473),
        _diff("outcome.owner_precision", 0.464, 0.464),  # GATE flat
    ]
    result = _aggregate(diffs)
    assert result.verdict == "PASS"
    assert result.regression_metrics == []
    assert "detector.struggle_ratio.fire_count" in result.info_metrics
    assert "detector.coordination_waste.fire_count" in result.info_metrics


def test_gate_metric_drop_is_regressed():
    """A GATE-metric drop beyond epsilon → REGRESSED."""
    diffs = [
        _diff("outcome.owner_precision", 0.80, 0.60),   # GATE drop → fail
        _diff("detector.struggle_ratio.fire_count", 10.0, 50.0),  # INFO rise
    ]
    result = _aggregate(diffs)
    assert result.verdict == "REGRESSED"
    assert "outcome.owner_precision" in result.regression_metrics


def test_gate_detector_precision_drop_is_pass_with_review():
    """A detector precision/recall drop → PASS but surfaced in REVIEW (human decides)."""
    diffs = [
        _diff("detector.coordination_waste.recall", 1.0, 0.5),  # REVIEW drop
        _diff("outcome.tau_kappa", 0.169, 0.169),               # GATE flat
    ]
    result = _aggregate(diffs)
    assert result.verdict == "PASS"
    assert result.regression_metrics == []
    assert "detector.coordination_waste.recall" in result.review_metrics


def test_gate_within_epsilon_is_pass():
    """A GATE move within epsilon is noise → PASS, no regression."""
    diffs = [_diff("outcome.owner_precision", 0.464, 0.4645)]
    result = _aggregate(diffs)
    assert result.verdict == "PASS"
    assert result.regression_metrics == []


def test_gate_improvement_credited():
    """A GATE rise beyond epsilon lands in improvements and keeps PASS."""
    diffs = [_diff("detector.unrecovered_error.precision", 0.538, 0.583)]
    result = _aggregate(diffs)
    assert result.verdict == "PASS"
    # detector precision is REVIEW tier → improvement surfaces in improved_metrics
    assert "detector.unrecovered_error.precision" in result.improved_metrics
