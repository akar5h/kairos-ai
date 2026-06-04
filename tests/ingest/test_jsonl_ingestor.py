"""Tests for JSONLIngestor — reads Langfuse JSONL trace exports."""

from __future__ import annotations

from pathlib import Path

from kairos.ingest.jsonl import JSONLIngestor

FIXTURES = Path(__file__).parent.parent / "fixtures"
JSONL_FILE = FIXTURES / "test_traces.jsonl"


class TestJSONLIngestor:
    def test_reads_all_traces(self):
        ingestor = JSONLIngestor(JSONL_FILE)
        traces = list(ingestor.ingest())
        assert len(traces) == 5

    def test_filter_by_trace_name(self):
        ingestor = JSONLIngestor(JSONL_FILE)
        traces = list(ingestor.ingest(trace_name="LangGraph"))
        assert len(traces) == 3
        assert all(t["name"] == "LangGraph" for t in traces)

    def test_filter_nonexistent_name_yields_nothing(self):
        ingestor = JSONLIngestor(JSONL_FILE)
        traces = list(ingestor.ingest(trace_name="nonexistent"))
        assert len(traces) == 0

    def test_source_name(self):
        ingestor = JSONLIngestor(JSONL_FILE)
        assert ingestor.source_name() == "langfuse_jsonl"

    def test_traces_have_expected_fields(self):
        ingestor = JSONLIngestor(JSONL_FILE)
        traces = list(ingestor.ingest())
        for trace in traces:
            assert "id" in trace
            assert "name" in trace
