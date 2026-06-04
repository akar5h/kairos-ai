"""Tests for argument normalizer."""

import copy

from kairos.normalization.arg_normalizer import normalize_args


class TestNormalizeArgs:
    def test_returns_none_for_none(self):
        assert normalize_args(None) is None

    def test_sorts_keys_alphabetically(self):
        result = normalize_args({"z": 1, "a": 2, "m": 3})
        assert list(result.keys()) == ["a", "m", "z"]

    def test_lowercases_string_values(self):
        result = normalize_args({"name": "Hello WORLD"})
        assert result["name"] == "hello world"

    def test_strips_ephemeral_fields(self):
        args = {
            "query": "test",
            "timestamp": "2024-01-01",
            "session_id": "abc",
            "request_id": "xyz",
            "trace_id": "t1",
            "span_id": "s1",
            "nonce": "n1",
            "idempotency_key": "ik1",
            "x-request-id": "xr1",
            "x-trace-id": "xt1",
            "ts": "123",
            "created_at": "now",
            "updated_at": "now",
            "req_id": "r1",
            "session_token": "tok",
        }
        result = normalize_args(args)
        assert "query" in result
        for field in [
            "timestamp",
            "session_id",
            "request_id",
            "trace_id",
            "span_id",
            "nonce",
            "idempotency_key",
            "x-request-id",
            "x-trace-id",
            "ts",
            "created_at",
            "updated_at",
            "req_id",
            "session_token",
        ]:
            assert field not in result

    def test_strips_uuid_patterns(self):
        result = normalize_args({"msg": "User 550e8400-e29b-41d4-a716-446655440000 logged in"})
        assert "550e8400" not in result["msg"]
        assert "logged in" in result["msg"]

    def test_strips_unix_timestamp_patterns(self):
        result = normalize_args({"msg": "Created at 1704067200000 ok"})
        assert "1704067200000" not in result["msg"]
        assert "ok" in result["msg"]

    def test_handles_nested_dicts(self):
        args = {
            "outer": {
                "timestamp": "strip_me",
                "query": "HELLO",
            }
        }
        result = normalize_args(args)
        assert "timestamp" not in result["outer"]
        assert result["outer"]["query"] == "hello"

    def test_handles_lists(self):
        args = {"items": ["Hello", "WORLD"]}
        result = normalize_args(args)
        assert result["items"] == ["hello", "world"]

    def test_does_not_mutate_original(self):
        original = {"query": "Test", "timestamp": "2024-01-01", "nested": {"a": "B"}}
        original_copy = copy.deepcopy(original)
        normalize_args(original)
        assert original == original_copy

    def test_preserves_numeric_values(self):
        result = normalize_args({"count": 42, "ratio": 3.14, "flag": True})
        assert result["count"] == 42
        assert result["ratio"] == 3.14
        assert result["flag"] is True
