"""Tests for kairos.eval.panel — metric math on fixtures."""

from __future__ import annotations

import json

import pytest

from kairos.eval.corpus import CorpusEntry, EvalCorpus, _compute_corpus_hash
from kairos.eval.panel import (
    DETECTOR_NAMES,
    DetectorMetrics,
    MetricPanel,
    OutcomeMetrics,
    _call_spans_to_envelope,
    _cohen_kappa,
    _compute_detector_metrics,
    _compute_outcome_metrics,
    _safe_div,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_entry(
    trace_id: str,
    outcome_truth: str = "unknown",
    source: str = "spotcheck",
    d1: bool | None = None,
    d2: bool | None = None,
) -> CorpusEntry:
    return CorpusEntry(
        trace_id=trace_id,
        source=source,
        outcome_truth=outcome_truth,
        detector_truth={"D1": d1, "D2": d2},
    )


def _make_corpus(entries: list[CorpusEntry]) -> EvalCorpus:
    ids = sorted(e.trace_id for e in entries)
    return EvalCorpus(
        entries=entries,
        corpus_hash=_compute_corpus_hash(ids),
        trace_ids=ids,
    )


# ── Cohen's κ ─────────────────────────────────────────────────────────────────


def test_kappa_perfect_agreement():
    assert _cohen_kappa(5, 0, 0, 5) == pytest.approx(1.0)


def test_kappa_zero_agreement():
    """All disagreements → κ < 0."""
    kappa = _cohen_kappa(0, 5, 5, 0)
    assert kappa is not None and kappa < 0


def test_kappa_empty_matrix():
    assert _cohen_kappa(0, 0, 0, 0) is None


def test_kappa_degenerate_all_positive():
    """All predicted PASS — pe = 1 if all true PASS too, else κ can be 0."""
    # a=5, b=0, c=0, d=0: all agree PASS; pe = 1 → denom=0 → returns 1.0 if po=1
    # This tests the degenerate guard.
    result = _cohen_kappa(5, 0, 0, 0)
    # po=1.0, pe=1.0 → κ=1.0 (degenerate guard)
    assert result == 1.0


# ── _safe_div ─────────────────────────────────────────────────────────────────


def test_safe_div_normal():
    assert _safe_div(3, 4) == pytest.approx(0.75)


def test_safe_div_zero_denom():
    assert _safe_div(3, 0) is None


def test_safe_div_zero_num():
    assert _safe_div(0, 4) == pytest.approx(0.0)


# ── outcome metrics fixture ───────────────────────────────────────────────────


def test_outcome_precision_recall_fixture():
    """Outcome precision/recall on a small fixture."""
    # 3 entries: 2 labeled (1 pass, 1 fail), 1 unknown
    entries = [
        _make_entry("t1", outcome_truth="pass", source="spotcheck"),  # truth=pass
        _make_entry("t2", outcome_truth="fail", source="spotcheck"),  # truth=fail
        _make_entry("t3", outcome_truth="unknown", source="spotcheck"),
    ]
    outcome_results = {
        "t1": {"outcome_pass": True, "computable": True},  # TP
        "t2": {"outcome_pass": True, "computable": True},  # FP (truth=fail, pred=pass)
        "t3": {"outcome_pass": False, "computable": True},  # not counted (unknown)
    }
    metrics = _compute_outcome_metrics(entries, outcome_results)

    # owner: TP=1 (t1 pass/pass), FP=1 (t2 fail/pass), FN=0, TN=0
    assert metrics.owner_tp == 1
    assert metrics.owner_fp == 1
    assert metrics.owner_fn == 0
    assert metrics.owner_labeled_count == 2
    assert metrics.owner_precision == pytest.approx(0.5)  # 1/(1+1)
    assert metrics.owner_recall == pytest.approx(1.0)  # 1/(1+0)


def test_outcome_abstention_excluded():
    """Non-computable results are excluded from owner precision math."""
    entries = [_make_entry("t1", outcome_truth="pass", source="spotcheck")]
    outcome_results = {"t1": {"outcome_pass": False, "computable": False}}  # abstain

    metrics = _compute_outcome_metrics(entries, outcome_results)
    assert metrics.owner_labeled_count == 0
    assert metrics.owner_precision is None
    assert metrics.owner_recall is None


def test_outcome_no_labels():
    """Zero labeled entries → precision/recall both None."""
    entries = [_make_entry("t1", outcome_truth="unknown")]
    outcome_results = {"t1": {"outcome_pass": True, "computable": True}}
    metrics = _compute_outcome_metrics(entries, outcome_results)
    assert metrics.owner_precision is None
    assert metrics.owner_recall is None


# ── detector metrics fixture ──────────────────────────────────────────────────


def test_detector_precision_recall_fixture():
    """Detector precision/recall on a small labeled fixture."""
    entries = [
        _make_entry("t1", d1=True),  # should fire
        _make_entry("t2", d1=True),  # should fire
        _make_entry("t3", d1=False),  # should NOT fire
        _make_entry("t4", d1=None),  # unknown → excluded
    ]
    # Engine fires on t1, t3 (t2 = FN, t4 = excluded)
    findings = {
        "t1": [{"pattern_name": "unrecovered_error", "severity": "info"}],
        "t2": [],
        "t3": [{"pattern_name": "unrecovered_error", "severity": "info"}],  # FP
        "t4": [],
    }
    dm = _compute_detector_metrics("unrecovered_error", "D1", entries, findings, corpus_size=4)
    # TP=1 (t1), FP=1 (t3), FN=1 (t2), TN=0 (none in should-not-fire fired correctly)
    assert dm.tp == 1
    assert dm.fp == 1
    assert dm.fn == 1
    assert dm.labeled_count == 3
    assert dm.precision == pytest.approx(0.5)  # 1/(1+1)
    assert dm.recall == pytest.approx(0.5)  # 1/(1+1)


def test_detector_fire_rate():
    """fire_rate = fire_count / corpus_size."""
    entries = [_make_entry("t1"), _make_entry("t2")]
    findings = {
        "t1": [{"pattern_name": "struggle_ratio", "severity": "warning"}],
        "t2": [],
    }
    dm = _compute_detector_metrics("struggle_ratio", "D2", entries, findings, corpus_size=2)
    assert dm.fire_count == 1
    assert dm.fire_rate == pytest.approx(0.5)


def test_detector_no_labels_gives_none():
    """When no entries are labeled for this detector, precision and recall are None."""
    entries = [_make_entry("t1", d1=None), _make_entry("t2", d1=None)]
    findings = {"t1": [{"pattern_name": "unrecovered_error", "severity": "info"}], "t2": []}
    dm = _compute_detector_metrics("unrecovered_error", "D1", entries, findings, corpus_size=2)
    assert dm.precision is None
    assert dm.recall is None
    assert dm.fire_count == 1


def test_detector_names_coverage():
    """DETECTOR_NAMES includes all expected detectors."""
    expected = {
        "unrecovered_error",
        "struggle_ratio",
        "coordination_waste",
        "work_to_talk_ratio",
        "redundant_execution",
    }
    assert set(DETECTOR_NAMES) == expected


# ── panel serialization round-trip ───────────────────────────────────────────


def test_panel_to_dict_round_trip():
    """to_dict() produces a dict that contains corpus_hash and all detector names."""
    panel = MetricPanel(
        corpus_hash="abc123",
        corpus_size=10,
        outcome=OutcomeMetrics(
            owner_precision=0.8,
            owner_recall=0.9,
        ),
        detectors={
            "unrecovered_error": DetectorMetrics(
                name="unrecovered_error",
                precision=0.7,
                recall=0.6,
                fire_count=3,
                fire_rate=0.3,
            )
        },
        classes_covered=1,
        severity_error_count=0,
        severity_warning_count=2,
        severity_info_count=5,
        total_findings=7,
    )
    d = panel.to_dict()
    assert d["corpus_hash"] == "abc123"
    assert d["corpus_size"] == 10
    assert "unrecovered_error" in d["detectors"]
    assert d["detectors"]["unrecovered_error"]["precision"] == 0.7

    json_str = panel.to_json()
    restored = json.loads(json_str)
    assert restored["corpus_hash"] == "abc123"


# ── spans_to_envelope signature adaptation ────────────────────────────────────


def test_call_spans_to_envelope_old_signature():
    """A ref whose spans_to_envelope takes only `spans` is called positionally."""
    seen = {}

    def old_reader(spans):
        seen["spans"] = spans
        return "ENV"

    out = _call_spans_to_envelope(old_reader, [{"a": 1}])
    assert out == "ENV"
    assert seen["spans"] == [{"a": 1}]


def test_call_spans_to_envelope_new_signature():
    """A ref with correlation_key_attr is called with that kwarg (=None)."""
    seen = {}

    def new_reader(spans, *, correlation_key_attr=None):
        seen["ck"] = correlation_key_attr
        return "ENV2"

    out = _call_spans_to_envelope(new_reader, [])
    assert out == "ENV2"
    assert seen["ck"] is None


def test_call_spans_to_envelope_no_signature():
    """A callable with no introspectable signature falls back to positional call."""

    # A builtin-like object: use a lambda wrapped so signature works, then a
    # callable whose signature raises — emulate via object with __call__.
    class Reader:
        def __call__(self, spans):
            return ("ENV3", len(spans))

    out = _call_spans_to_envelope(Reader(), [1, 2, 3])
    assert out == ("ENV3", 3)
