"""AgentTranscriptNormalizer — coding-agent transcript → TraceEnvelope IR.

Phase-2 adapters turn a coding agent's *native* transcript (Claude Code,
Codex, OpenCode, Paperclip) into the same ``TraceEnvelope`` the engine reads.

Every adapter emits the typed live-event vocabulary (``normalization.events``)
and folds it through ``LiveNormalizer``. So all adapters land on one IR and
flow through ``KairosEngine.analyze`` unchanged — one path, no per-source
engine branches.

Data-completeness contract (board gate): an adapter captures *everything the
transcript exposes* — every model turn (``LLMCall``), every tool call with its
full arguments and result (``ToolCall``), retrievals (``Retrieval``), errors,
and timing. What a given transcript does not record (e.g. per-message tokens in
Codex) is left ``None`` rather than invented.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from kairos.log import get_logger
from kairos.normalization.live_normalizer import LiveNormalizer

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from kairos.models.trace import TraceEnvelope
    from kairos.normalization.events import AnyEvent

logger = get_logger(__name__)

# Single shared normalizer (stateless). No import cycle: live_normalizer does
# not import this module.
_LIVE_NORMALIZER = LiveNormalizer()


class AgentTranscriptNormalizer(ABC):
    """Base class: native transcript records → ``TraceEnvelope``.

    Subclasses implement :meth:`to_events`. The concrete :meth:`normalize`
    folds those events through ``LiveNormalizer`` and stamps the adapter's
    ``source`` so downstream provenance is correct.
    """

    #: Stable IR ``source`` tag for this adapter (e.g. ``"claude_code"``).
    source: ClassVar[str]

    @abstractmethod
    def to_events(self, records: Sequence[Mapping[str, Any]]) -> list[AnyEvent]:
        """Map parsed transcript records into the typed live-event vocabulary.

        ``records`` is the adapter's parsed transcript (one mapping per
        transcript entry). Implementations must emit a ``TraceStart`` first and
        a ``TraceEnd`` last so the folded envelope is ``is_valid``.
        """

    def normalize(self, records: Sequence[Mapping[str, Any]]) -> TraceEnvelope:
        """Fold transcript records into a TraceEnvelope tagged with ``source``."""
        events = self.to_events(records)
        envelope = _LIVE_NORMALIZER.normalize(events)
        envelope.source = self.source
        if events:
            envelope.source_trace_id = events[0].trace_id
        logger.info(
            "agent_normalizer.normalized",
            source=self.source,
            trace_id=envelope.trace_id,
            step_count=envelope.step_count,
            is_valid=envelope.is_valid,
        )
        return envelope

    def normalize_jsonl(self, path: str | Path) -> TraceEnvelope:
        """Read a JSONL transcript (one JSON object per line) and normalize it."""
        return self.normalize(read_jsonl(path))


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """Parse a JSONL file into a list of dicts. Blank lines are skipped.

    Malformed lines fail loud (``json.JSONDecodeError``) — no silent skipping.
    """
    records: list[dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        records.append(json.loads(stripped))
    return records


def parse_ts(value: Any) -> datetime | None:
    """Parse a transcript timestamp into an aware datetime.

    Accepts ISO-8601 strings (``...Z`` tolerated), epoch seconds (int/float),
    or an existing ``datetime``. Returns ``None`` only when ``value`` is absent.
    Malformed strings fail loud (``ValueError``) — no silent coercion.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, bool):  # bool is an int subclass; reject explicitly
        msg = f"cannot parse bool as timestamp: {value!r}"
        raise ValueError(msg)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=UTC)
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    msg = f"unsupported timestamp type: {type(value).__name__}"
    raise ValueError(msg)
