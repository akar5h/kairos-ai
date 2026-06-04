"""File ingestor — reads individual JSON files from a directory."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

from kairos.log import get_logger

from .base import TraceIngestor

logger = get_logger(__name__)


class FileIngestor(TraceIngestor):
    """Load traces from individual JSON files in a directory."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    def source_name(self) -> str:
        return "file"

    def ingest(self, **kwargs: object) -> Iterator[dict[str, Any]]:
        """Yield raw trace dicts from JSON files in directory."""
        count = 0
        for file_path in sorted(self.directory.glob("*.json")):
            with file_path.open() as f:
                count += 1
                yield json.load(f)
        logger.info(
            "file.ingested",
            directory=str(self.directory),
            count=count,
        )
