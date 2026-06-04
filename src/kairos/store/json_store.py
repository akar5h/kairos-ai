"""JSON file-based trace store."""

from __future__ import annotations

from pathlib import Path

from kairos.log import get_logger
from kairos.models.trace import TraceEnvelope

from .base import TraceStore

logger = get_logger(__name__)


class JSONStore(TraceStore):
    """Store TraceEnvelopes as individual JSON files on disk."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path_for(self, trace_id: str) -> Path:
        return self.directory / f"{trace_id}.json"

    def save(self, envelope: TraceEnvelope) -> str:
        """Save a TraceEnvelope as a JSON file. Returns the file path."""
        path = self._path_for(envelope.trace_id)
        path.write_text(envelope.model_dump_json(indent=2))
        logger.info("trace.saved", trace_id=envelope.trace_id, path=str(path))
        return str(path)

    def load(self, trace_id: str) -> TraceEnvelope | None:
        """Load a TraceEnvelope by trace ID. Returns None if not found."""
        path = self._path_for(trace_id)
        if not path.exists():
            return None
        data = path.read_text()
        envelope = TraceEnvelope.model_validate_json(data)
        logger.info("trace.loaded", trace_id=trace_id, path=str(path))
        return envelope

    def list_ids(self) -> list[str]:
        """List all stored trace IDs."""
        return [p.stem for p in self.directory.glob("*.json")]

    def count(self) -> int:
        """Total number of stored traces."""
        return len(list(self.directory.glob("*.json")))
