"""Directly-Follows Graph built from tool bigrams across traces."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from kairos.log import get_logger

if TYPE_CHECKING:
    from kairos.models.trace import TraceEnvelope

logger = get_logger(__name__)


@dataclass
class DFG:
    """Directly-Follows Graph built from tool bigrams across traces."""

    edges: dict[tuple[str, str], int] = field(default_factory=dict)
    nodes: dict[str, int] = field(default_factory=dict)
    total_traces: int = 0

    def edge_weight(self, a: str, b: str) -> float:
        """Normalized weight: count / total_traces. Returns 0 if edge doesn't exist."""
        if self.total_traces == 0:
            return 0.0
        return self.edges.get((a, b), 0) / self.total_traces

    def high_weight_edges(self, threshold: float = 0.3) -> set[tuple[str, str]]:
        """Return edges with normalized weight >= threshold."""
        if self.total_traces == 0:
            return set()
        return {edge for edge, count in self.edges.items() if count / self.total_traces >= threshold}

    def high_weight_bigrams(self, threshold: float = 0.3) -> set[tuple[str, str]]:
        """Alias for high_weight_edges -- used in Jaccard comparison."""
        return self.high_weight_edges(threshold)

    def winning_path(self) -> list[str]:
        """Most common tool sequence via greedy walk on highest-weight edges.

        Start from the most common first tool, always follow the
        highest-weight outgoing edge.  Stop when no outgoing edge or
        revisiting a node.
        """
        if not self.edges:
            return []

        # Find most common starting tool (highest total outgoing weight)
        start_counts: dict[str, int] = {}
        for (a, _b), count in self.edges.items():
            start_counts[a] = start_counts.get(a, 0) + count

        if not start_counts:
            return []

        current = max(start_counts, key=lambda k: start_counts[k])
        path = [current]
        visited = {current}

        while True:
            outgoing = {b: count for (a, b), count in self.edges.items() if a == current and b not in visited}
            if not outgoing:
                break
            next_tool = max(outgoing, key=lambda k: outgoing[k])
            path.append(next_tool)
            visited.add(next_tool)
            current = next_tool

        return path


class DFGBuilder:
    """Builds a DFG from a list of TraceEnvelopes."""

    def build(self, traces: list[TraceEnvelope]) -> DFG:
        """Build DFG from tool_bigrams across all traces."""
        edges: dict[tuple[str, str], int] = {}
        nodes: dict[str, int] = {}

        for trace in traces:
            for bigram in trace.tool_bigrams:
                edges[bigram] = edges.get(bigram, 0) + 1
            for tool in trace.tool_sequence:
                nodes[tool] = nodes.get(tool, 0) + 1

        dfg = DFG(edges=edges, nodes=nodes, total_traces=len(traces))
        logger.info(
            "dfg.built",
            n_edges=len(edges),
            n_nodes=len(nodes),
            total_traces=len(traces),
        )
        return dfg
