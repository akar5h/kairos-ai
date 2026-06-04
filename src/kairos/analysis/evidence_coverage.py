"""Evidence coverage calculator for week-1 analysis.

Given a population of TraceEnvelopes, count how many have each required and
context field populated, and derive which failure-pattern claims must be
disabled because their supporting evidence is too sparse.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from kairos.log import get_logger
from kairos.models.enums import TerminalStatus

if TYPE_CHECKING:
    from kairos.models.trace import TraceEnvelope

logger = get_logger(__name__)


REQUIRED_FIELD_KEYS: tuple[str, ...] = (
    "user_input",
    "tool_sequence",
    "tool_args",
    "tool_outputs",
    "terminal_status",
)

CONTEXT_FIELD_KEYS: tuple[str, ...] = (
    "system_prompt",
    "retrieval_chunks",
    "memory_events",
    "graph_node",
    "versions",
)

_VERSION_METADATA_KEYS: tuple[str, ...] = (
    "prompt_version",
    "tool_version",
    "agent_version",
)


@dataclass
class EvidenceCoverage:
    """Per-population counts of which evidence fields were populated."""

    total_traces: int
    valid_traces: int
    invalid_traces: int
    required_field_counts: dict[str, int] = field(default_factory=dict)
    context_field_counts: dict[str, int] = field(default_factory=dict)

    def disabled_claims(self, minimum_coverage: float = 0.3) -> list[str]:
        """Return the list of claims whose supporting evidence is too sparse.

        Coverage ratio = count / total_traces. A claim is disabled when its
        underlying context field's ratio falls below ``minimum_coverage``.
        When no traces exist, all claims are disabled.
        """
        disabled: list[str] = []

        if self.total_traces == 0:
            # No traces -> no evidence for anything.
            disabled.extend(
                [
                    "context_ignored",
                    "retrieval_inconsistency",
                    "memory_contamination",
                    "version_regression",
                ]
            )
            return disabled

        retrieval_ratio = self.context_field_counts.get("retrieval_chunks", 0) / self.total_traces
        memory_ratio = self.context_field_counts.get("memory_events", 0) / self.total_traces
        versions_ratio = self.context_field_counts.get("versions", 0) / self.total_traces

        if retrieval_ratio < minimum_coverage:
            disabled.append("context_ignored")
            disabled.append("retrieval_inconsistency")
        if memory_ratio < minimum_coverage:
            disabled.append("memory_contamination")
        if versions_ratio < minimum_coverage:
            disabled.append("version_regression")

        return disabled


def _has_user_input(envelope: TraceEnvelope) -> bool:
    return bool(envelope.user_input and envelope.user_input.strip())


def _has_tool_sequence(envelope: TraceEnvelope) -> bool:
    return len(envelope.tool_sequence) > 0


def _has_tool_args(envelope: TraceEnvelope) -> bool:
    return any(step.tool_args for step in envelope.steps)


def _has_tool_outputs(envelope: TraceEnvelope) -> bool:
    return any(step.tool_output is not None and step.tool_output != "" for step in envelope.steps)


def _has_terminal_status(envelope: TraceEnvelope) -> bool:
    return envelope.terminal_status != TerminalStatus.UNKNOWN


def _has_system_prompt(envelope: TraceEnvelope) -> bool:
    return bool(envelope.system_prompt and envelope.system_prompt.strip())


def _has_retrieval_chunks(envelope: TraceEnvelope) -> bool:
    if envelope.has_retrieval:
        return True
    return any(step.retrieval_chunks for step in envelope.steps)


def _has_memory_events(envelope: TraceEnvelope) -> bool:
    if envelope.metadata is None:
        return False
    memory_events = envelope.metadata.get("memory_events")
    if memory_events is None:
        return False
    if isinstance(memory_events, (list, tuple, dict, set)):
        return len(memory_events) > 0
    return bool(memory_events)


def _has_graph_node(envelope: TraceEnvelope) -> bool:
    return any(step.node_name is not None and step.node_name != "" for step in envelope.steps)


def _has_versions(envelope: TraceEnvelope) -> bool:
    if envelope.metadata is None:
        return False
    return any(key in envelope.metadata for key in _VERSION_METADATA_KEYS)


def compute_evidence_coverage(envelopes: list[TraceEnvelope]) -> EvidenceCoverage:
    """Compute per-field coverage counts across a list of envelopes."""
    required_counts: dict[str, int] = {key: 0 for key in REQUIRED_FIELD_KEYS}
    context_counts: dict[str, int] = {key: 0 for key in CONTEXT_FIELD_KEYS}

    valid_traces = 0

    for envelope in envelopes:
        if envelope.is_valid:
            valid_traces += 1

        if _has_user_input(envelope):
            required_counts["user_input"] += 1
        if _has_tool_sequence(envelope):
            required_counts["tool_sequence"] += 1
        if _has_tool_args(envelope):
            required_counts["tool_args"] += 1
        if _has_tool_outputs(envelope):
            required_counts["tool_outputs"] += 1
        if _has_terminal_status(envelope):
            required_counts["terminal_status"] += 1

        if _has_system_prompt(envelope):
            context_counts["system_prompt"] += 1
        if _has_retrieval_chunks(envelope):
            context_counts["retrieval_chunks"] += 1
        if _has_memory_events(envelope):
            context_counts["memory_events"] += 1
        if _has_graph_node(envelope):
            context_counts["graph_node"] += 1
        if _has_versions(envelope):
            context_counts["versions"] += 1

    total = len(envelopes)
    invalid = total - valid_traces

    coverage = EvidenceCoverage(
        total_traces=total,
        valid_traces=valid_traces,
        invalid_traces=invalid,
        required_field_counts=required_counts,
        context_field_counts=context_counts,
    )

    logger.info(
        "evidence_coverage.computed",
        total_traces=total,
        valid_traces=valid_traces,
        invalid_traces=invalid,
        required_field_counts=required_counts,
        context_field_counts=context_counts,
    )

    return coverage
