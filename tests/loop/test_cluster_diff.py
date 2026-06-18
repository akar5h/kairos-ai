"""Tests for P4.3 cluster_diff — pure unit tests, no DB required."""

from __future__ import annotations

from kairos.loop.cluster_diff import ClusterDiff, diff_clusters


def test_diff_clusters_empty_sets() -> None:
    result = diff_clusters(set(), set())
    assert result.new_keys == []
    assert result.removed_keys == []
    assert result.unchanged_count == 0


def test_diff_clusters_all_new() -> None:
    after = {"a::token_z", "b::restart_count"}
    result = diff_clusters(set(), after)
    assert sorted(result.new_keys) == sorted(after)
    assert result.removed_keys == []
    assert result.unchanged_count == 0


def test_diff_clusters_all_removed() -> None:
    before = {"a::token_z", "b::restart_count"}
    result = diff_clusters(before, set())
    assert result.new_keys == []
    assert sorted(result.removed_keys) == sorted(before)
    assert result.unchanged_count == 0


def test_diff_clusters_mixed() -> None:
    before = {"a::token_z", "b::restart_count", "c::struggle"}
    after = {"b::restart_count", "c::struggle", "d::outcome_only"}
    result = diff_clusters(before, after)
    assert result.new_keys == ["d::outcome_only"]
    assert result.removed_keys == ["a::token_z"]
    assert result.unchanged_count == 2


def test_diff_clusters_new_keys_sorted() -> None:
    before: set[str] = set()
    after = {"z::token_z", "a::restart_count", "m::struggle"}
    result = diff_clusters(before, after)
    assert result.new_keys == sorted(after)


def test_diff_clusters_identical_sets() -> None:
    keys = {"a::token_z", "b::restart_count", "c::struggle"}
    result = diff_clusters(keys, keys)
    assert result.new_keys == []
    assert result.removed_keys == []
    assert result.unchanged_count == len(keys)


def test_diff_clusters_returns_cluster_diff_type() -> None:
    result = diff_clusters({"a"}, {"a", "b"})
    assert isinstance(result, ClusterDiff)
