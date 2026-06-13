"""Tests for kairos.eval.corpus — corpus assembly, hash stability, label mapping."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from kairos.eval.corpus import (
    _ANSWERS_TRUTH,
    _SPOTCHECK_TRUTH,
    EvalCorpus,
    _build_live_entries,
    _compute_corpus_hash,
    _load_answers,
    _load_snapshot_manifest,
    _load_spotcheck,
    _load_taubench,
    build_corpus,
    persist_live_trace_ids,
)

# ── corpus_hash stability ─────────────────────────────────────────────────────


def test_corpus_hash_stable():
    """Same set of trace_ids → same hash regardless of insertion order."""
    ids_a = ["trace_001", "trace_002", "trace_003"]
    ids_b = ["trace_003", "trace_001", "trace_002"]
    assert _compute_corpus_hash(ids_a) == _compute_corpus_hash(ids_b)


def test_corpus_hash_changes_on_new_id():
    """Adding a trace_id changes the hash."""
    ids = ["trace_001", "trace_002"]
    ids_extended = ["trace_001", "trace_002", "trace_003"]
    assert _compute_corpus_hash(ids) != _compute_corpus_hash(ids_extended)


def test_corpus_hash_empty():
    """Empty corpus has a stable hash (SHA256 of empty string)."""
    h = _compute_corpus_hash([])
    assert len(h) == 64  # SHA-256 hex


def test_corpus_hash_matches_sha256():
    """corpus_hash matches manual SHA-256 of sorted IDs joined by newline."""
    ids = ["b", "a", "c"]
    expected = hashlib.sha256("\n".join(sorted(ids)).encode()).hexdigest()
    assert _compute_corpus_hash(ids) == expected


# ── taubench loader ───────────────────────────────────────────────────────────


def test_load_taubench_from_real_corpus():
    """Load tau-bench corpus from eval/corpus/taubench/ — verify counts and field types."""
    taubench_dir = Path(__file__).parent.parent.parent / "eval" / "corpus" / "taubench"
    if not (taubench_dir / "labels.jsonl").exists():
        pytest.skip("tau-bench corpus not available")

    entries = _load_taubench(taubench_dir)
    assert len(entries) > 0, "Expected at least one tau-bench entry"

    # outcome_truth values must be in valid set
    valid_truths = {"pass", "fail", "partial"}
    for e in entries:
        assert e.outcome_truth in valid_truths, f"Invalid outcome_truth: {e.outcome_truth}"
        assert e.source == "taubench"
        assert e.trace_id, "trace_id must not be empty"

    # No duplicate trace_ids
    ids = [e.trace_id for e in entries]
    assert len(ids) == len(set(ids)), "Duplicate trace_ids in tau-bench entries"


def test_load_taubench_no_detector_labels():
    """Tau-bench entries have empty detector_truth (no detector labels in tau-bench)."""
    taubench_dir = Path(__file__).parent.parent.parent / "eval" / "corpus" / "taubench"
    if not (taubench_dir / "labels.jsonl").exists():
        pytest.skip("tau-bench corpus not available")

    entries = _load_taubench(taubench_dir)
    for e in entries:
        assert e.detector_truth == {}, f"Expected no detector labels for {e.trace_id}"


# ── spotcheck loader ──────────────────────────────────────────────────────────


def test_load_spotcheck_count():
    """Spotcheck truth table has exactly 20 entries (one per spotcheck row)."""
    assert len(_SPOTCHECK_TRUTH) == 20


def test_load_spotcheck_outcome_truths():
    """All spotcheck entries have valid outcome_truth values."""
    valid = {"pass", "fail", "unknown"}
    entries = _load_spotcheck(_SPOTCHECK_TRUTH)
    for e in entries:
        assert e.outcome_truth in valid, f"{e.trace_id}: invalid outcome_truth={e.outcome_truth}"


def test_spotcheck_agree_y_maps_to_non_unknown():
    """AGREE=Y traces where engine said pass/fail should map to pass/fail (not unknown)."""
    # Row 11: 8fe79bb7 — AGREE=Y, engine says pass → outcome_truth="pass"
    entries = _load_spotcheck(_SPOTCHECK_TRUTH)
    entry_map = {e.trace_id: e for e in entries}

    pass_entry = entry_map.get("8fe79bb7a022ad93")
    if pass_entry is None:
        # prefix matching
        pass_entry = next((e for e in entries if e.trace_id.startswith("8fe79bb7")), None)
    assert pass_entry is not None
    assert pass_entry.outcome_truth == "pass"


def test_spotcheck_agree_n_maps_to_unknown():
    """AGREE=N entries are excluded from precision math (outcome_truth=unknown)."""
    # Row 5: ea9692b98678 — AGREE=N
    entries = _load_spotcheck(_SPOTCHECK_TRUTH)
    n_entry = next((e for e in entries if e.trace_id.startswith("ea9692b9")), None)
    assert n_entry is not None
    assert n_entry.outcome_truth == "unknown"


def test_spotcheck_unknown_excluded_from_labeled():
    """labeled_for_outcome() excludes unknown entries."""
    entries = _load_spotcheck(_SPOTCHECK_TRUTH)
    corpus = EvalCorpus(
        entries=entries,
        corpus_hash="dummy",
        trace_ids=[e.trace_id for e in entries],
    )
    labeled = corpus.labeled_for_outcome()
    for e in labeled:
        assert e.outcome_truth in {"pass", "fail"}


# ── answers loader ────────────────────────────────────────────────────────────


def test_answers_truth_count():
    """_ANSWERS_TRUTH has entries for the 15 unique trace_ids in answers.jsonl."""
    assert len(_ANSWERS_TRUTH) == 15


def test_load_answers_from_real_file():
    """Load answers.jsonl and verify no fabricated truths."""
    answers_path = Path(__file__).parent.parent.parent / "eval" / "review" / "answers.jsonl"
    if not answers_path.exists():
        pytest.skip("answers.jsonl not available")

    entries = _load_answers(answers_path, _ANSWERS_TRUTH)
    assert len(entries) > 0

    valid_truths = {"pass", "fail", "unknown"}
    for e in entries:
        assert e.outcome_truth in valid_truths, f"Invalid: {e.outcome_truth}"
        # No None values in truth dict (must be True/False/None)
        for det, val in e.detector_truth.items():
            assert val in (True, False, None), f"{e.trace_id}.{det} = {val!r}"


def test_answers_vague_maps_to_unknown():
    """Vague answers (inconclusive) must map to unknown, never pass/fail."""
    # 1c59051c — "inconclusive, no transcript data" → UNKNOWN
    truth = _ANSWERS_TRUTH.get("1c59051c3ba82897")
    assert truth is not None
    assert truth["outcome_truth"] == "unknown"


def test_answers_lgtm_maps_to_pass():
    """LGTM / 'looks good' answers map to pass."""
    # ba036a1d — "LGTM"
    truth = _ANSWERS_TRUTH.get("ba036a1d86e17c79")
    assert truth is not None
    assert truth["outcome_truth"] == "pass"


def test_answers_silent_failure_sets_d1():
    """Answers mentioning silent failure / unrecovered error set D1=True."""
    # d38a760a — "Bash exit code 1, never re-attempted" → D1=True
    truth = _ANSWERS_TRUTH.get("d38a760ac7e43101")
    assert truth is not None
    assert truth["D1"] is True


# ── build_corpus integration ──────────────────────────────────────────────────


def test_build_corpus_no_duplicate_trace_ids():
    """build_corpus() produces no duplicate trace_ids."""
    corpus = build_corpus()
    assert len(corpus.trace_ids) == len(set(corpus.trace_ids))
    assert sorted(corpus.trace_ids) == corpus.trace_ids  # must be sorted


def test_build_corpus_hash_matches_entries():
    """corpus_hash must match _compute_corpus_hash over trace_ids + snapshotted IDs."""
    corpus = build_corpus()
    snapshotted = sorted(e.trace_id for e in corpus.entries if e.raw_spans_file is not None)
    expected_hash = _compute_corpus_hash(corpus.trace_ids, snapshotted)
    assert corpus.corpus_hash == expected_hash


def test_build_corpus_tau_bench_present():
    """tau-bench entries are present when corpus dir exists."""
    taubench_dir = Path(__file__).parent.parent.parent / "eval" / "corpus" / "taubench"
    if not (taubench_dir / "labels.jsonl").exists():
        pytest.skip("tau-bench corpus not available")

    corpus = build_corpus()
    assert corpus.tau_bench_count > 0


def test_build_corpus_spotcheck_present():
    """spotcheck entries are always present (hardcoded truth table)."""
    corpus = build_corpus()
    # All 20 spotcheck entries should appear (minus any that share IDs with tau-bench)
    assert corpus.spotcheck_count > 0


def test_build_corpus_labeled_entries_have_valid_truths():
    """labeled_for_outcome() entries are all pass or fail."""
    corpus = build_corpus()
    labeled = corpus.labeled_for_outcome()
    for e in labeled:
        assert e.outcome_truth in {"pass", "fail"}


def test_build_corpus_stable_across_calls():
    """build_corpus() called twice produces the same corpus_hash."""
    corpus1 = build_corpus()
    corpus2 = build_corpus()
    assert corpus1.corpus_hash == corpus2.corpus_hash


# ── persist_live_trace_ids ────────────────────────────────────────────────────


def test_persist_live_trace_ids(tmp_path):
    """persist_live_trace_ids writes a sorted, one-per-line file."""
    ids = ["z_trace", "a_trace", "m_trace"]
    path = tmp_path / "live_trace_ids.txt"
    persist_live_trace_ids(ids, path)

    written = path.read_text().splitlines()
    assert written == ["a_trace", "m_trace", "z_trace"]


def test_persist_live_trace_ids_idempotent(tmp_path):
    """Calling persist_live_trace_ids twice with the same IDs is idempotent."""
    ids = ["a", "b", "c"]
    path = tmp_path / "live_trace_ids.txt"
    persist_live_trace_ids(ids, path)
    persist_live_trace_ids(ids, path)
    assert path.read_text().count("\n") == len(ids)


# ── raw-span snapshot path ─────────────────────────────────────────────────────


def test_corpus_hash_folds_snapshot_membership():
    """corpus_hash changes when snapshot membership changes (version bump signal)."""
    ids = ["t1", "t2", "t3"]
    h_none = _compute_corpus_hash(ids, [])
    h_some = _compute_corpus_hash(ids, ["t1"])
    h_more = _compute_corpus_hash(ids, ["t1", "t2"])
    assert h_none != h_some
    assert h_some != h_more
    # Same trace_ids + same snapshot set → identical hash regardless of order.
    assert _compute_corpus_hash(ids, ["t2", "t1"]) == _compute_corpus_hash(ids, ["t1", "t2"])


def test_load_snapshot_manifest_authoritative(tmp_path):
    """Manifest file (when present) is authoritative for snapshot membership."""
    snap = tmp_path / "live"
    snap.mkdir()
    manifest = snap / "snapshot_manifest.json"
    manifest.write_text('{"snapshotted_trace_ids": ["aaa", "bbb"]}')
    # A stray raw file not in the manifest must NOT be counted.
    (snap / "ccc.json").write_text("[]")
    got = _load_snapshot_manifest(manifest, snap)
    assert got == {"aaa", "bbb"}


def test_load_snapshot_manifest_falls_back_to_glob(tmp_path):
    """Without a manifest, membership falls back to globbing raw <tid>.json files."""
    snap = tmp_path / "live"
    snap.mkdir()
    (snap / "aaa.json").write_text("[]")
    (snap / "bbb.json").write_text("[]")
    (snap / "spotcheck_resolved.json").write_text("{}")
    got = _load_snapshot_manifest(snap / "snapshot_manifest.json", snap)
    assert got == {"aaa", "bbb"}  # resolved-map file excluded


def test_spotcheck_resolves_to_full_id_when_snapshotted():
    """A spotcheck prefix resolves to its full trace_id when that id is snapshotted."""
    prefix = next(iter(_SPOTCHECK_TRUTH))
    p16 = prefix[:16]
    full = p16 + "deadbeefdeadbeef"  # synthetic full id
    entries = _load_spotcheck(_SPOTCHECK_TRUTH, {p16: full}, {full})
    entry = next(e for e in entries if e.trace_id == full)
    assert entry.raw_spans_file == f"{full}.json"
    assert entry.source == "spotcheck"


def test_spotcheck_falls_back_to_prefix_without_snapshot():
    """Without a snapshot, spotcheck entries keep the 16-char prefix and no snapshot."""
    entries = _load_spotcheck(_SPOTCHECK_TRUTH, {}, set())
    for e in entries:
        assert e.raw_spans_file is None
        assert len(e.trace_id) == 16


def test_answers_sets_raw_spans_file_when_snapshotted(tmp_path):
    """_load_answers sets raw_spans_file for trace_ids in the snapshot set."""
    answers = tmp_path / "answers.jsonl"
    answers.write_text('{"trace_id": "abc123", "verdict_shown": "pass", "answer": "ok"}\n')
    entries = _load_answers(answers, {}, {"abc123"})
    assert len(entries) == 1
    assert entries[0].raw_spans_file == "abc123.json"


def test_live_entries_snapshot_flag():
    """_build_live_entries sets raw_spans_file only for snapshotted ids."""
    entries = _build_live_entries(["x1", "x2"], existing_ids=set(), snapshotted={"x1"})
    by_id = {e.trace_id: e for e in entries}
    assert by_id["x1"].raw_spans_file == "x1.json"
    assert by_id["x2"].raw_spans_file is None
