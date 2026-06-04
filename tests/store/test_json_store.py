"""Tests for JSONStore — store layer for TraceEnvelopes."""

from __future__ import annotations

from kairos.models.trace import TraceEnvelope
from kairos.store.json_store import JSONStore


def _make_envelope(trace_id: str = "test-trace-001") -> TraceEnvelope:
    """Build a minimal valid TraceEnvelope for testing."""
    return TraceEnvelope(
        trace_id=trace_id,
        source="langfuse",
        source_trace_id=f"lf-{trace_id}",
        user_input="Hello, how are you?",
        tags=["test"],
    )


class TestJSONStore:
    def test_save_and_load_roundtrip(self, tmp_path):
        store = JSONStore(tmp_path / "traces")
        envelope = _make_envelope()

        path = store.save(envelope)
        assert path.endswith(".json")

        loaded = store.load("test-trace-001")
        assert loaded is not None
        assert loaded.trace_id == envelope.trace_id
        assert loaded.source == envelope.source
        assert loaded.source_trace_id == envelope.source_trace_id
        assert loaded.user_input == envelope.user_input
        assert loaded.tags == envelope.tags

    def test_list_ids(self, tmp_path):
        store = JSONStore(tmp_path / "traces")
        store.save(_make_envelope("trace-a"))
        store.save(_make_envelope("trace-b"))
        store.save(_make_envelope("trace-c"))

        ids = store.list_ids()
        assert sorted(ids) == ["trace-a", "trace-b", "trace-c"]

    def test_count(self, tmp_path):
        store = JSONStore(tmp_path / "traces")
        assert store.count() == 0

        store.save(_make_envelope("t1"))
        assert store.count() == 1

        store.save(_make_envelope("t2"))
        assert store.count() == 2

    def test_load_nonexistent_returns_none(self, tmp_path):
        store = JSONStore(tmp_path / "traces")
        result = store.load("does-not-exist")
        assert result is None

    def test_creates_directory_if_not_exists(self, tmp_path):
        target = tmp_path / "nested" / "deep" / "traces"
        assert not target.exists()

        store = JSONStore(target)
        assert target.exists()

        # Verify it works
        store.save(_make_envelope())
        assert store.count() == 1

    def test_save_overwrites_existing(self, tmp_path):
        store = JSONStore(tmp_path / "traces")

        env1 = _make_envelope("same-id")
        env1_input = "first version"
        env1.user_input = env1_input
        store.save(env1)

        env2 = _make_envelope("same-id")
        env2.user_input = "second version"
        store.save(env2)

        assert store.count() == 1
        loaded = store.load("same-id")
        assert loaded is not None
        assert loaded.user_input == "second version"
