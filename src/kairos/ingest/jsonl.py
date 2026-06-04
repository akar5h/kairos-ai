"""JSONL file ingestor — reads Langfuse JSONL trace exports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

from kairos.log import get_logger

from .base import TraceIngestor

logger = get_logger(__name__)


class JSONLIngestor(TraceIngestor):
    """Read traces from a JSONL file (one JSON object per line)."""

    def __init__(self, file_path: str | Path) -> None:
        self.file_path = Path(file_path)

    def source_name(self) -> str:
        return "langfuse_jsonl"

    def ingest(self, **kwargs: object) -> Iterator[dict[str, Any]]:
        """Yield raw trace dicts from the JSONL file.

        Keyword args:
            trace_name: If provided, only yield traces matching this name.
        """
        trace_name = kwargs.get("trace_name")
        if trace_name is not None and not isinstance(trace_name, str):
            raise TypeError(f"trace_name must be str | None, got {type(trace_name).__name__}")
        count = 0
        yielded = 0
        with self.file_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                count += 1
                record = json.loads(line, strict=False)
                if trace_name is not None and record.get("name") != trace_name:
                    continue
                yielded += 1
                yield record
        logger.info(
            "jsonl.ingested",
            file=str(self.file_path),
            total_lines=count,
            yielded=yielded,
            trace_name=trace_name,
        )
