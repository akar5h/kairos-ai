"""Tests for jaccard_dict_similarity utility."""

from __future__ import annotations

from kairos.detection.similarity import jaccard_dict_similarity


class TestJaccardDictSimilarity:
    """Test suite for jaccard_dict_similarity."""

    def test_identical_dicts_score_1(self) -> None:
        d = {"query": "openai pricing", "limit": 10}
        assert jaccard_dict_similarity(d, d) == 1.0

    def test_disjoint_dicts_score_0(self) -> None:
        a = {"x": 1}
        b = {"y": 2}
        assert jaccard_dict_similarity(a, b) == 0.0

    def test_partial_overlap(self) -> None:
        a = {"query": "pricing", "limit": 10}
        b = {"query": "pricing", "limit": 20}
        # Shared: ("query", "pricing"). Unique: ("limit","10") and ("limit","20")
        # intersection=1, union=3 → 1/3
        result = jaccard_dict_similarity(a, b)
        assert abs(result - 1 / 3) < 1e-9

    def test_nested_dicts(self) -> None:
        a = {"a": {"b": 1}}
        b = {"a": {"b": 1, "c": 2}}
        # a flattens to {("a.b","1")}, b to {("a.b","1"),("a.c","2")}
        # intersection=1, union=2 → 0.5
        assert jaccard_dict_similarity(a, b) == 0.5

    def test_both_none_returns_1(self) -> None:
        assert jaccard_dict_similarity(None, None) == 1.0

    def test_one_none_returns_0(self) -> None:
        assert jaccard_dict_similarity({"a": 1}, None) == 0.0
        assert jaccard_dict_similarity(None, {"a": 1}) == 0.0

    def test_empty_dicts_returns_1(self) -> None:
        assert jaccard_dict_similarity({}, {}) == 1.0

    def test_list_values(self) -> None:
        a = {"ids": [1, 2, 3]}
        b = {"ids": [1, 2, 4]}
        # a: {("ids.0","1"),("ids.1","2"),("ids.2","3")}
        # b: {("ids.0","1"),("ids.1","2"),("ids.2","4")}
        # intersection=2, union=4 → 0.5
        assert jaccard_dict_similarity(a, b) == 0.5
