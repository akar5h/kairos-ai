"""Tests for kairos.eval.eval_set — P3.2 cluster → eval-set generation."""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from kairos.eval.eval_set import (
    EvalSetRecord,
    _discriminator_from_features,
    _dominant_feature_from_cluster_key,
    _make_eval_set_id,
    _mcc,
    batch_compute_mcc,
    compute_eval_set_mcc,
    generate_eval_set,
    load_eval_set,
    store_eval_set,
)

# ── _dominant_feature_from_cluster_key ───────────────────────────────────────


def test_dominant_feature_from_cluster_key_latency():
    assert _dominant_feature_from_cluster_key("Bash::latency_z") == "latency_z"


def test_dominant_feature_from_cluster_key_restart():
    assert _dominant_feature_from_cluster_key("A|B::restart_count") == "restart_count"


def test_dominant_feature_from_cluster_key_no_sep():
    assert _dominant_feature_from_cluster_key("no_sep") == "no_sep"


# ── _discriminator_from_features ─────────────────────────────────────────────


def test_discriminator_latency_z():
    features = [
        {"latency_z": 5.9},
        {"latency_z": 4.8},
        {"latency_z": 5.3},
    ]
    disc_type, config = _discriminator_from_features("latency_z", features)
    assert disc_type == "latency_z_threshold"
    assert config["threshold_z"] == pytest.approx(4.8)


def test_discriminator_restart_count():
    features = [
        {"restart_count": 2},
        {"restart_count": 3},
        {"restart_count": 1},
    ]
    disc_type, config = _discriminator_from_features("restart_count", features)
    assert disc_type == "restart_count_gt"
    assert config["threshold"] == 1


def test_discriminator_rare_ngram():
    features = [
        {"rare_ngrams": ["A>B", "B>C"]},
        {"rare_ngrams": ["A>B", "C>D"]},
    ]
    disc_type, config = _discriminator_from_features("rare_ngram", features)
    assert disc_type == "rare_ngram_present"
    assert config["ngrams"] == ["A>B", "B>C", "C>D"]


def test_discriminator_unknown_feature():
    features = [{"weird": 1.0}]
    disc_type, config = _discriminator_from_features("weird", features)
    assert disc_type == "outcome_only"
    assert config == {}


# ── _make_eval_set_id ─────────────────────────────────────────────────────────


def test_eval_set_id_stable():
    """Same inputs → same eval_set_id."""
    frozen_at = datetime(2026, 6, 17, 0, 0, 0, tzinfo=UTC)
    id1 = _make_eval_set_id("Bash::latency_z", "HEAD", frozen_at)
    id2 = _make_eval_set_id("Bash::latency_z", "HEAD", frozen_at)
    assert id1 == id2
    assert len(id1) == 32
    assert all(c in "0123456789abcdef" for c in id1)


# ── generate_eval_set (no DB) ────────────────────────────────────────────────


def test_generate_eval_set_no_traces(monkeypatch):
    """Empty held_in → raises ValueError."""
    import psycopg

    class _FakeCursor:
        def fetchall(self):
            return []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    class _FakeConn:
        def execute(self, *a, **kw):
            return _FakeCursor()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    monkeypatch.setattr(psycopg, "connect", lambda *a, **kw: _FakeConn())

    with pytest.raises(ValueError, match="No traces found"):
        generate_eval_set("Bash::latency_z", "postgresql://fake/fake")


# ── EvalSetRecord.to_dict round-trip ─────────────────────────────────────────


def test_eval_set_round_trip():
    """to_dict() produces JSON-serializable dict with correct fields."""
    frozen_at = datetime(2026, 6, 17, 12, 0, 0, tzinfo=UTC)
    record = EvalSetRecord(
        eval_set_id="abc123" * 5 + "ab",
        cluster_key="Bash::latency_z",
        detector_version="HEAD",
        frozen_at=frozen_at,
        held_in=[{"trace_id": "t1", "features": {"latency_z": 5.0}}],
        held_out=[{"trace_id": "t2", "outcome_truth": "pass", "source": "labeled"}],
        discriminator_type="latency_z_threshold",
        discriminator_config={"threshold_z": 5.0},
    )
    d = record.to_dict()
    assert d["eval_set_id"] == record.eval_set_id
    assert d["cluster_key"] == "Bash::latency_z"
    assert d["frozen_at"] == frozen_at.isoformat()
    assert d["held_in"] == record.held_in
    assert d["held_out"] == record.held_out
    assert d["discriminator_type"] == "latency_z_threshold"
    assert d["discriminator_config"] == {"threshold_z": 5.0}
    # Must be JSON-serializable
    import json

    json.dumps(d)


# ── DB-gated round-trip ───────────────────────────────────────────────────────


@pytest.mark.skipif(
    not os.environ.get("KAIROS_PG_DSN"),
    reason="KAIROS_PG_DSN not set",
)
def test_store_and_load_eval_set():
    """store_eval_set → load_eval_set round-trip (requires live DB)."""
    dsn = os.environ["KAIROS_PG_DSN"]
    frozen_at = datetime.now(UTC)
    eval_set_id = _make_eval_set_id("Bash|Edit::latency_z", "test_version", frozen_at)
    record = EvalSetRecord(
        eval_set_id=eval_set_id,
        cluster_key="Bash|Edit::latency_z",
        detector_version="test_version",
        frozen_at=frozen_at,
        held_in=[{"trace_id": "trace_abc", "features": {"latency_z": 6.1}}],
        held_out=[{"trace_id": "trace_xyz", "outcome_truth": "pass", "source": "labeled"}],
        discriminator_type="latency_z_threshold",
        discriminator_config={"threshold_z": 6.1},
    )

    stored_id = store_eval_set(record, dsn)
    assert stored_id == eval_set_id

    loaded = load_eval_set(eval_set_id, dsn)
    assert loaded is not None
    assert loaded.eval_set_id == eval_set_id
    assert loaded.cluster_key == "Bash|Edit::latency_z"
    assert loaded.detector_version == "test_version"
    assert loaded.held_in == record.held_in
    assert loaded.held_out == record.held_out
    assert loaded.discriminator_type == "latency_z_threshold"
    assert loaded.discriminator_config == {"threshold_z": 6.1}


# ── _mcc unit tests ───────────────────────────────────────────────────────────


def test_mcc_all_correct():
    """Perfect classifier: TP=3, FP=0, FN=0, TN=3 → mcc=1.0."""
    result = _mcc(3, 0, 0, 3)
    assert result == pytest.approx(1.0)


def test_mcc_all_wrong():
    """All wrong: TP=0, FP=3, FN=3, TN=0 → mcc=-1.0."""
    result = _mcc(0, 3, 3, 0)
    assert result == pytest.approx(-1.0)


def test_mcc_zero_denom():
    """Degenerate case — all predictions are positive, no true negatives: TN=0, FP=0."""
    result = _mcc(0, 0, 5, 0)
    assert result is None


# ── compute_eval_set_mcc ──────────────────────────────────────────────────────


def _make_eval_set_fixture(
    discriminator_type: str,
    discriminator_config: dict,
) -> EvalSetRecord:
    return EvalSetRecord(
        eval_set_id="test-id",
        cluster_key="test::cluster",
        detector_version="HEAD",
        frozen_at=datetime(2026, 6, 17, 0, 0, 0, tzinfo=UTC),
        held_in=[],
        held_out=[],
        discriminator_type=discriminator_type,
        discriminator_config=discriminator_config,
    )


def _make_corpus_entry(trace_id: str, outcome_truth: str):
    """Create a minimal corpus entry duck-typed for compute_eval_set_mcc."""

    class _Entry:
        pass

    e = _Entry()
    e.trace_id = trace_id  # type: ignore[attr-defined]
    e.outcome_truth = outcome_truth  # type: ignore[attr-defined]
    return e


def test_compute_mcc_rare_ngram():
    """rare_ngram_present discriminator: 2 fail traces have ngram, 2 pass traces do not."""
    corpus = [
        _make_corpus_entry("t1", "fail"),  # has ngram → TP
        _make_corpus_entry("t2", "fail"),  # has ngram → TP
        _make_corpus_entry("t3", "pass"),  # no ngram → TN
        _make_corpus_entry("t4", "pass"),  # no ngram → TN
    ]
    tool_sequences = {
        "t1": ["bash", "rare_tool"],
        "t2": ["bash", "rare_tool"],
        "t3": ["bash", "read"],
        "t4": ["write", "read"],
    }
    eval_set = _make_eval_set_fixture("rare_ngram_present", {"ngrams": ["rare_tool"]})
    mcc, count = compute_eval_set_mcc(eval_set, corpus, tool_sequences, {})
    assert count == 4
    assert mcc == pytest.approx(1.0)


def test_compute_mcc_struggle_proxy():
    """struggle_gt discriminator: struggle_ratio fires map onto fail corpus entries."""
    corpus = [
        _make_corpus_entry("t1", "fail"),  # fires → TP
        _make_corpus_entry("t2", "pass"),  # fires → FP
        _make_corpus_entry("t3", "fail"),  # no fire → FN
        _make_corpus_entry("t4", "pass"),  # no fire → TN
    ]
    detector_fires = {
        "t1": ["struggle_ratio"],
        "t2": ["struggle_ratio"],
    }
    eval_set = _make_eval_set_fixture("struggle_gt", {"threshold": 0.5})
    mcc, count = compute_eval_set_mcc(eval_set, corpus, {}, detector_fires)
    assert count == 4
    # TP=1, FP=1, FN=1, TN=1 → MCC = (1*1 - 1*1) / sqrt(2*2*2*2) = 0
    assert mcc == pytest.approx(0.0)


def test_compute_mcc_unsupported_type():
    """outcome_only discriminator → (None, 0)."""
    corpus = [_make_corpus_entry("t1", "fail"), _make_corpus_entry("t2", "pass")]
    eval_set = _make_eval_set_fixture("outcome_only", {})
    mcc, count = compute_eval_set_mcc(eval_set, corpus, {}, {})
    assert mcc is None
    assert count == 0


def test_compute_mcc_no_labels():
    """Corpus with only 'unknown' outcome_truth → (None, 0)."""
    corpus = [_make_corpus_entry("t1", "unknown"), _make_corpus_entry("t2", "partial")]
    eval_set = _make_eval_set_fixture("struggle_gt", {})
    mcc, count = compute_eval_set_mcc(eval_set, corpus, {}, {})
    assert mcc is None
    assert count == 0


# ── DB-gated batch_compute_mcc ────────────────────────────────────────────────


@pytest.mark.skipif(
    not os.environ.get("KAIROS_PG_DSN"),
    reason="KAIROS_PG_DSN not set",
)
def test_batch_compute_mcc_updates_db():
    """batch_compute_mcc runs without error and updates the DB (requires live DB)."""
    dsn = os.environ["KAIROS_PG_DSN"]
    # Use empty corpus/sequences — MCC will be None for all rows, but the DB write still runs.
    results = batch_compute_mcc(
        corpus=[],
        trace_tool_sequences={},
        trace_detector_fires={},
        dsn=dsn,
    )
    # Results should be a dict (possibly empty if no eval_sets exist)
    assert isinstance(results, dict)
    for _eid, mcc_val in results.items():
        assert mcc_val is None or isinstance(mcc_val, float)
