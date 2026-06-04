"""Paperclip run → TraceEnvelope.

Paperclip orchestrates a coding agent (Claude Code via the ``claude_local``
adapter by default, or Codex / OpenCode) and records the run. The agent's
*tool calls* — including the Paperclip control-plane API calls it makes — live
in that underlying transcript, so this adapter wraps an inner transcript
normalizer and enriches the result with Paperclip run/issue provenance:

- ``source`` is stamped ``"paperclip"``
- ``trace_id`` becomes the Paperclip ``run_id`` (so traces key on the run, not
  the raw coding-session id) when a run context is supplied
- run/issue/company/agent ids are folded into ``TraceStart.metadata`` and thus
  into the envelope metadata

Business context for Paperclip agents is *derived from the MCP/tool catalog*
(see :func:`kairos.taxonomy.tool_catalog.business_context_from_tool_catalog`),
not hand-written per agent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kairos.normalization.agents.base import AgentTranscriptNormalizer
from kairos.normalization.agents.claude_code import ClaudeCodeNormalizer
from kairos.normalization.events import TraceStart

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from kairos.normalization.events import AnyEvent

# Run-context keys folded into trace metadata.
_META_KEYS = ("run_id", "issue", "company_id", "agent_id", "project_id")


class PaperclipNormalizer(AgentTranscriptNormalizer):
    """Normalize a Paperclip run by wrapping its underlying agent transcript."""

    source = "paperclip"

    def __init__(
        self,
        run_context: Mapping[str, Any] | None = None,
        inner: AgentTranscriptNormalizer | None = None,
    ) -> None:
        """``run_context`` carries Paperclip run/issue ids; ``inner`` is the
        coding-agent transcript normalizer (defaults to Claude Code)."""
        self.run_context = dict(run_context) if run_context else {}
        self.inner = inner if inner is not None else ClaudeCodeNormalizer()

    def to_events(self, records: Sequence[Mapping[str, Any]]) -> list[AnyEvent]:
        events = self.inner.to_events(records)
        if not events:
            return events

        run_id = self.run_context.get("run_id")
        if run_id:
            run_id = str(run_id)
            for event in events:
                event.trace_id = run_id

        extra = {k: self.run_context[k] for k in _META_KEYS if self.run_context.get(k) is not None}
        if extra:
            for event in events:
                if isinstance(event, TraceStart):
                    merged = dict(event.metadata or {})
                    merged["paperclip"] = extra
                    event.metadata = merged
                    break
        return events
