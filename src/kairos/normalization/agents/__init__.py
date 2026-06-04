"""Agent transcript adapters: native coding-agent logs → TraceEnvelope IR.

Each adapter subclasses :class:`AgentTranscriptNormalizer` and emits the typed
live-event vocabulary, so Claude Code / Codex / OpenCode / Paperclip all flow
through ``KairosEngine.analyze`` unchanged.
"""

from kairos.normalization.agents.base import AgentTranscriptNormalizer
from kairos.normalization.agents.claude_code import ClaudeCodeNormalizer
from kairos.normalization.agents.codex import CodexNormalizer
from kairos.normalization.agents.opencode import OpenCodeNormalizer

__all__ = [
    "AgentTranscriptNormalizer",
    "ClaudeCodeNormalizer",
    "CodexNormalizer",
    "OpenCodeNormalizer",
]
