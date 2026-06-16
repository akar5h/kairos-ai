"""Tests for kairos.eval.store — round-trip + DB availability check.

Store tests only run when KAIROS_PG_DSN is set and the DB is reachable.
They are marked with pytest.mark.integration so the CI can skip them
without --integration flag.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from kairos.eval.panel import DetectorMetrics, MetricPanel, OutcomeMetrics
from kairos.eval.store import (
    _make_run_id,
    is_db_available,
    load_run,
    store_run,
)

# ── _make_run_id ──────────────────────────────────────────────────────────────


def test_make_run_id_stable():
    """Same inputs → same run_id."""
    ts = datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC)
    r1 = _make_run_id("abc" * 13 + "x", "corpus_hash_xyz", None, ts)
    r2 = _make_run_id("abc" * 13 + "x", "corpus_hash_xyz", None, ts)
    assert r1 == r2


def test_make_run_id_different_refs():
    """Different refs → different run_ids."""
    ts = datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC)
    r1 = _make_run_id("ref_a" * 8, "corpus", None, ts)
    r2 = _make_run_id("ref_b" * 8, "corpus", None, ts)
    assert r1 != r2


def test_make_run_id_different_corpus_hash():
    """Different corpus_hash → different run_ids."""
    ts = datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC)
    ref = "a" * 40
    r1 = _make_run_id(ref, "hash_1", None, ts)
    r2 = _make_run_id(ref, "hash_2", None, ts)
    assert r1 != r2


def test_make_run_id_length():
    """run_id is 32 hex chars."""
    ts = datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC)
    run_id = _make_run_id("a" * 40, "corpus", None, ts)
    assert len(run_id) == 32
    assert all(c in "0123456789abcdef" for c in run_id)


# ── is_db_available ───────────────────────────────────────────────────────────


def test_is_db_available_false_when_no_dsn(monkeypatch):
    """Returns False when KAIROS_PG_DSN is not set."""
    monkeypatch.delenv("KAIROS_PG_DSN", raising=False)
    assert is_db_available() is False


# ── Round-trip tests (integration, require live DB) ───────────────────────────


def _make_test_panel() -> MetricPanel:
    return MetricPanel(
        corpus_hash="test_corpus_hash_abc123",
        corpus_size=50,
        outcome=OutcomeMetrics(
            owner_precision=0.8,
            owner_recall=0.75,
            owner_labeled_count=10,
            tau_kappa=0.5,
            tau_fail_precision=0.6,
            tau_fail_recall=0.7,
            tau_abstention_rate=0.1,
            tau_total=50,
            tau_computable=45,
            tau_a=25,
            tau_b=5,
            tau_c=3,
            tau_d=12,
        ),
        detectors={
            "struggle_ratio": DetectorMetrics(
                name="struggle_ratio",
                precision=0.7,
                recall=0.5,
                fire_count=5,
                fire_rate=0.1,
            )
        },
        classes_covered=1,
        severity_error_count=0,
        severity_warning_count=5,
        severity_info_count=10,
        total_findings=15,
    )


@pytest.mark.skipif(not os.environ.get("KAIROS_PG_DSN"), reason="KAIROS_PG_DSN not set — skipping DB round-trip test")
def test_store_and_load_round_trip():
    """Store a run and load it back by run_id."""
    panel = _make_test_panel()
    ts = datetime(2026, 6, 13, 12, 34, 56, tzinfo=UTC)

    run_id = store_run(
        ref="test_ref",
        ref_full="a" * 40,
        corpus_hash="test_corpus_hash_abc123",
        k=2,
        panel=panel,
        verdict="PASS",
        config_hash=None,
        ts=ts,
    )

    assert run_id, "store_run must return a non-empty run_id"
    assert len(run_id) == 32

    # Load it back
    record = load_run(run_id)
    assert record is not None
    assert record.run_id == run_id
    assert record.ref == "test_ref"
    assert record.verdict == "PASS"
    assert record.k == 2
    assert record.corpus_hash == "test_corpus_hash_abc123"
    assert "corpus_size" in record.panel
    assert record.panel["corpus_size"] == 50


@pytest.mark.skipif(not os.environ.get("KAIROS_PG_DSN"), reason="KAIROS_PG_DSN not set — skipping DB round-trip test")
def test_store_idempotent():
    """Storing the same run twice (same inputs) is idempotent (ON CONFLICT DO NOTHING)."""
    panel = _make_test_panel()
    ts = datetime(2026, 6, 13, 11, 0, 0, tzinfo=UTC)

    run_id_1 = store_run(
        ref="idempotent_test",
        ref_full="b" * 40,
        corpus_hash="test_corpus_hash_abc123",
        k=2,
        panel=panel,
        verdict="PASS",
        ts=ts,
    )
    run_id_2 = store_run(
        ref="idempotent_test",
        ref_full="b" * 40,
        corpus_hash="test_corpus_hash_abc123",
        k=2,
        panel=panel,
        verdict="PASS",
        ts=ts,
    )
    # Same inputs → same run_id, no error
    assert run_id_1 == run_id_2


@pytest.mark.skipif(not os.environ.get("KAIROS_PG_DSN"), reason="KAIROS_PG_DSN not set — skipping DB round-trip test")
def test_load_nonexistent_run_returns_none():
    """load_run with an unknown run_id returns None."""
    result = load_run("0" * 32)
    assert result is None
