"""Tests for FileIngestor — reads individual JSON files from a directory."""

from __future__ import annotations

import json

from kairos.ingest.file import FileIngestor


class TestFileIngestor:
    def test_reads_all_json_files(self, tmp_path):
        for i in range(3):
            (tmp_path / f"trace_{i}.json").write_text(json.dumps({"id": f"trace-{i}", "name": "test"}))

        ingestor = FileIngestor(tmp_path)
        traces = list(ingestor.ingest())
        assert len(traces) == 3
        ids = {t["id"] for t in traces}
        assert ids == {"trace-0", "trace-1", "trace-2"}

    def test_empty_directory_yields_nothing(self, tmp_path):
        ingestor = FileIngestor(tmp_path)
        traces = list(ingestor.ingest())
        assert len(traces) == 0

    def test_source_name(self, tmp_path):
        ingestor = FileIngestor(tmp_path)
        assert ingestor.source_name() == "file"
