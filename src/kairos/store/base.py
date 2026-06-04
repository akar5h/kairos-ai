"""Abstract base class for trace storage."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kairos.models.trace import TraceEnvelope


class TraceStore(ABC):
    """Abstract base for trace storage."""

    @abstractmethod
    def save(self, envelope: TraceEnvelope) -> str:
        """Save a trace, return the storage path/key."""
        ...

    @abstractmethod
    def load(self, trace_id: str) -> TraceEnvelope | None:
        """Load a trace by ID."""
        ...

    @abstractmethod
    def list_ids(self) -> list[str]:
        """List all stored trace IDs."""
        ...

    @abstractmethod
    def count(self) -> int:
        """Total number of stored traces."""
        ...
