"""Tests for DFG builder."""

from kairos.models import Step, StepType, TraceEnvelope
from kairos.taxonomy.dfg import DFG, DFGBuilder


def make_trace(trace_id: str, tool_names: list[str]) -> TraceEnvelope:
    steps = [Step(step_index=i, step_type=StepType.TOOL_CALL, tool_name=name) for i, name in enumerate(tool_names)]
    return TraceEnvelope(trace_id=trace_id, steps=steps)


class TestDFGBuilder:
    def test_build_dfg_basic(self):
        """3 traces with known sequences -> correct edge and node counts."""
        traces = [
            make_trace("t1", ["A", "B", "C"]),  # bigrams: (A,B), (B,C)
            make_trace("t2", ["A", "B", "C"]),  # bigrams: (A,B), (B,C)
            make_trace("t3", ["A", "C"]),  # bigrams: (A,C)
        ]
        dfg = DFGBuilder().build(traces)

        assert dfg.edges[("A", "B")] == 2
        assert dfg.edges[("B", "C")] == 2
        assert dfg.edges[("A", "C")] == 1
        assert len(dfg.edges) == 3
        assert dfg.nodes["A"] == 3
        assert dfg.nodes["B"] == 2
        assert dfg.nodes["C"] == 3
        assert dfg.total_traces == 3

    def test_edge_weight(self):
        """edge count / total_traces = correct normalized weight."""
        dfg = DFG(
            edges={("A", "B"): 6, ("B", "C"): 3},
            nodes={"A": 6, "B": 9, "C": 3},
            total_traces=10,
        )
        assert dfg.edge_weight("A", "B") == 0.6
        assert dfg.edge_weight("B", "C") == 0.3
        assert dfg.edge_weight("X", "Y") == 0.0

    def test_edge_weight_zero_traces(self):
        dfg = DFG(edges={("A", "B"): 1}, total_traces=0)
        assert dfg.edge_weight("A", "B") == 0.0

    def test_high_weight_edges(self):
        """Only edges above threshold returned."""
        dfg = DFG(
            edges={("A", "B"): 5, ("B", "C"): 2, ("C", "D"): 1},
            nodes={"A": 5, "B": 7, "C": 3, "D": 1},
            total_traces=10,
        )
        result = dfg.high_weight_edges(threshold=0.3)
        assert result == {("A", "B")}

        result_lower = dfg.high_weight_edges(threshold=0.2)
        assert ("A", "B") in result_lower
        assert ("B", "C") in result_lower
        assert ("C", "D") not in result_lower

    def test_winning_path(self):
        """Follows highest-weight edges greedily."""
        dfg = DFG(
            edges={
                ("A", "B"): 10,
                ("B", "C"): 8,
                ("A", "C"): 2,
                ("C", "D"): 5,
            },
            nodes={"A": 12, "B": 10, "C": 10, "D": 5},
            total_traces=10,
        )
        path = dfg.winning_path()
        # A has highest outgoing (10+2=12), then A->B (10), B->C (8), C->D (5)
        assert path == ["A", "B", "C", "D"]

    def test_empty_traces(self):
        """Empty list -> empty DFG."""
        dfg = DFGBuilder().build([])
        assert dfg.edges == {}
        assert dfg.nodes == {}
        assert dfg.total_traces == 0
        assert dfg.winning_path() == []

    def test_single_trace(self):
        """Single trace -> DFG with weight 1.0 on all edges."""
        traces = [make_trace("t1", ["A", "B", "C"])]
        dfg = DFGBuilder().build(traces)

        assert dfg.total_traces == 1
        assert dfg.edge_weight("A", "B") == 1.0
        assert dfg.edge_weight("B", "C") == 1.0
        assert len(dfg.edges) == 2
