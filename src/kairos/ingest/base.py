"""Abstract base class for trace ingestion."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator


class TraceIngestor(ABC):
    """Abstract base for trace ingestion.

    All ingestors yield raw trace dicts.
    The normalizer converts them to TraceEnvelope.
    """

    @abstractmethod
    def ingest(self, **kwargs: object) -> Iterator[dict[str, Any]]:
        """Yield raw trace dicts from the source."""
        ...

    @abstractmethod
    def source_name(self) -> str:
        """Return the name of this source."""
        ...
