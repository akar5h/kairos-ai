"""Red-phase tests for the workflow divergence detector.

Target module (not yet implemented):
    src.kairos.analysis.workflow_divergence

Expected surface:
    @dataclass DivergenceFinding(
        trace_id,
        first_divergence_step: int | None,
        expected_transition: tuple[str, str] | None,
        actual_transition: tuple[str, str] | None,
        extra_rate: float,
        coverage: float,
        variant_candidate: bool,
    )
    def detect_workflow_divergence(
        traces: list[TraceEnvelope],
        reference: ReferenceCohort,
    ) -> list[DivergenceFinding]
"""

from __future__ import annotations

from kairos.analysis.reference_behavior import (
    ReferenceCohort,
    ReferenceConfidence,
)
from kairos.analysis.workflow_divergence import (
    DivergenceFinding,
    detect_workflow_divergence,
)
from kairos.models.enums import StepStatus, StepType, TerminalStatus
from kairos.models.trace import Step, TraceEnvelope
from kairos.taxonomy.dfg import DFG

# ── Synthesis helpers ──────────────────────────────────────────────────


def _step(i: int, tool: str, *, status: StepStatus = StepStatus.OK) -> Step:
    return Step(
        step_index=i,
        step_type=StepType.TOOL_CALL,
        tool_name=tool,
        tool_args={"stub": True},
        tool_args_normalized={"stub": True},
        tool_output="ok",
        status=status,
    )


def _trace(trace_id: str, tools: list[str]) -> TraceEnvelope:
    steps = [_step(i, tool) for i, tool in enumerate(tools)]
    return TraceEnvelope(
        trace_id=trace_id,
        user_input="ut",
        steps=steps,
        terminal_status=TerminalStatus.COMPLETED,
    )


def _ref(
    edges: dict[tuple[str, str], int],
    *,
    path: list[str] | None = None,
    confidence: ReferenceConfidence = ReferenceConfidence.MEDIUM,
) -> ReferenceCohort:
    """Construct a synthetic ReferenceCohort with a known DFG and edges."""
    # Derive nodes
    nodes: dict[str, int] = {}
    for (a, b), w in edges.items():
        nodes[a] = nodes.get(a, 0) + w
        nodes[b] = nodes.get(b, 0) + w
    dfg = DFG(edges=edges, nodes=nodes, total_traces=max(1, max(edges.values()) if edges else 1))
    reference_edges = set(edges.keys())
    reference_path = path if path is not None else dfg.winning_path()
    return ReferenceCohort(
        eligible_traces=[],
        reference_traces=[],
        confidence=confidence,
        reference_dfg=dfg,
        reference_edges=reference_edges,
        reference_path=reference_path,
        step_budget_p75=None,
        token_budget_p75=None,
    )


def _ref_none() -> ReferenceCohort:
    return ReferenceCohort(
        eligible_traces=[],
        reference_traces=[],
        confidence=ReferenceConfidence.NONE,
        reference_dfg=None,
        reference_edges=set(),
        reference_path=[],
        step_budget_p75=None,
        token_budget_p75=None,
    )


# ── TESTS ─────────────────────────────────────────────────────────────


class TestDivergenceBasics:
    """Core divergence detection logic."""

    def test_trace_fully_on_reference_path_has_no_divergence(self) -> None:
        ref = _ref(
            {("A", "B"): 5, ("B", "C"): 5, ("C", "D"): 5},
            path=["A", "B", "C", "D"],
        )
        trace = _trace("t1", ["A", "B", "C", "D"])
        findings = detect_workflow_divergence([trace], ref)
        assert len(findings) == 1
        finding = findings[0]
        assert isinstance(finding, DivergenceFinding)
        assert finding.first_divergence_step is None
        assert finding.variant_candidate is False

    def test_first_off_reference_bigram_is_divergence(self) -> None:
        """Trace [A, B, C, X, D] with reference edges {(A,B), (B,C), (C,D)}."""
        ref = _ref(
            {("A", "B"): 5, ("B", "C"): 5, ("C", "D"): 5},
            path=["A", "B", "C", "D"],
        )
        trace = _trace("t1", ["A", "B", "C", "X", "D"])
        findings = detect_workflow_divergence([trace], ref)
        assert len(findings) == 1
        finding = findings[0]
        # first bigram off reference = (C, X) → divergence at step of X (index 3).
        assert finding.first_divergence_step == 3

    def test_actual_transition_is_the_off_reference_bigram(self) -> None:
        ref = _ref(
            {("A", "B"): 5, ("B", "C"): 5, ("C", "D"): 5},
            path=["A", "B", "C", "D"],
        )
        trace = _trace("t1", ["A", "B", "C", "X", "D"])
        findings = detect_workflow_divergence([trace], ref)
        assert findings[0].actual_transition == ("C", "X")

    def test_expected_transition_from_reference_dfg(self) -> None:
        """Reference DFG has C→D with highest weight; expected_transition == (C, D)."""
        ref = _ref(
            {("A", "B"): 5, ("B", "C"): 5, ("C", "D"): 5},
            path=["A", "B", "C", "D"],
        )
        trace = _trace("t1", ["A", "B", "C", "X"])
        findings = detect_workflow_divergence([trace], ref)
        assert findings[0].expected_transition == ("C", "D")

    def test_expected_transition_none_when_no_outgoing_edge(self) -> None:
        """Divergence at a terminal node in reference DFG → expected_transition is None."""
        # Reference DFG: A→B. Trace [A, B, X] diverges at (B, X), but B has no
        # outgoing edge in the reference DFG → expected_transition = None.
        ref = _ref({("A", "B"): 5}, path=["A", "B"])
        trace = _trace("t1", ["A", "B", "X"])
        findings = detect_workflow_divergence([trace], ref)
        assert len(findings) == 1
        finding = findings[0]
        assert finding.actual_transition == ("B", "X")
        assert finding.expected_transition is None


class TestRejoinCaveat:
    """Rejoin within 2 tool transitions AND low extra_rate → variant_candidate."""

    def test_rejoin_within_one_transition_and_low_extra_rate_is_variant(self) -> None:
        """[A, B, X, C, D]: detour to X then returns to C within 1 step; low extra_rate."""
        # Reference has 4 canonical edges so trace adds ~2 extra → extra_rate ≤ 0.20.
        # Build a wide reference DFG so extra_rate stays low.
        # Trace has 4 bigrams: (A,B), (B,X), (X,C), (C,D)
        # extra bigrams: {(B,X), (X,C)} = 2 out of 4 → extra_rate = 0.5 → TOO HIGH.
        # To satisfy low extra_rate ≤ 0.20 we need more on-reference bigrams.
        # Use trace [A, B, X, C, D, E, F] and reference covering most of it.
        ref = _ref(
            {
                ("A", "B"): 5,
                ("B", "C"): 5,
                ("C", "D"): 5,
                ("D", "E"): 5,
                ("E", "F"): 5,
                ("F", "G"): 5,
                ("G", "H"): 5,
                ("H", "I"): 5,
                ("I", "J"): 5,
                ("J", "K"): 5,
            },
            path=["A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K"],
        )
        # Trace follows reference for most of its length, detours once briefly.
        trace = _trace(
            "t1",
            [
                "A",
                "B",
                "X",  # detour
                "C",  # rejoin within 1 step
                "D",
                "E",
                "F",
                "G",
                "H",
                "I",
                "J",
                "K",
            ],
        )
        # bigrams: 11 total; (B,X) and (X,C) off-ref → 2/11 ≈ 0.18 → ≤ 0.20 ✔
        findings = detect_workflow_divergence([trace], ref)
        assert len(findings) == 1
        finding = findings[0]
        assert finding.variant_candidate is True
        assert finding.first_divergence_step is None

    def test_rejoin_within_two_transitions_is_variant(self) -> None:
        """[A, B, X, Y, C, D]: returns within 2 extra steps."""
        ref = _ref(
            {
                ("A", "B"): 5,
                ("B", "C"): 5,
                ("C", "D"): 5,
                ("D", "E"): 5,
                ("E", "F"): 5,
                ("F", "G"): 5,
                ("G", "H"): 5,
                ("H", "I"): 5,
                ("I", "J"): 5,
                ("J", "K"): 5,
                ("K", "L"): 5,
                ("L", "M"): 5,
                ("M", "N"): 5,
                ("N", "O"): 5,
            },
            path=["A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L", "M", "N", "O"],
        )
        # Long trace with detour [X, Y] between B and C.
        trace = _trace(
            "t1",
            [
                "A",
                "B",
                "X",
                "Y",
                "C",  # rejoin within 2
                "D",
                "E",
                "F",
                "G",
                "H",
                "I",
                "J",
                "K",
                "L",
                "M",
                "N",
                "O",
            ],
        )
        # 16 bigrams, (B,X), (X,Y), (Y,C) = 3 off-ref → 3/16 = 0.1875 ≤ 0.20 ✔
        findings = detect_workflow_divergence([trace], ref)
        assert len(findings) == 1
        finding = findings[0]
        assert finding.variant_candidate is True
        assert finding.first_divergence_step is None

    def test_no_rejoin_within_two_is_divergence(self) -> None:
        """[A, B, X, Y, Z, D]: stays off reference 3+ steps → divergence."""
        ref = _ref(
            {("A", "B"): 5, ("B", "C"): 5, ("C", "D"): 5},
            path=["A", "B", "C", "D"],
        )
        trace = _trace("t1", ["A", "B", "X", "Y", "Z", "D"])
        findings = detect_workflow_divergence([trace], ref)
        assert len(findings) == 1
        finding = findings[0]
        assert finding.variant_candidate is False
        assert finding.first_divergence_step is not None

    def test_high_extra_rate_despite_rejoin_is_divergence(self) -> None:
        """Quick rejoin but extra_rate > 0.20 → still divergence."""
        # Short trace makes extra_rate high even with a 1-step detour.
        ref = _ref({("A", "B"): 5, ("B", "C"): 5}, path=["A", "B", "C"])
        # Trace [A, B, X, C]: bigrams (A,B), (B,X), (X,C). 2/3 extras → 0.67.
        trace = _trace("t1", ["A", "B", "X", "C"])
        findings = detect_workflow_divergence([trace], ref)
        assert len(findings) == 1
        finding = findings[0]
        assert finding.variant_candidate is False
        assert finding.first_divergence_step is not None

    def test_rejoin_preserves_expected_and_actual_for_explanation(self) -> None:
        """variant_candidate=True still populates expected + actual transitions."""
        ref = _ref(
            {
                ("A", "B"): 5,
                ("B", "C"): 5,
                ("C", "D"): 5,
                ("D", "E"): 5,
                ("E", "F"): 5,
                ("F", "G"): 5,
                ("G", "H"): 5,
                ("H", "I"): 5,
                ("I", "J"): 5,
                ("J", "K"): 5,
            },
            path=["A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K"],
        )
        trace = _trace(
            "t1",
            [
                "A",
                "B",
                "X",
                "C",
                "D",
                "E",
                "F",
                "G",
                "H",
                "I",
                "J",
                "K",
            ],
        )
        findings = detect_workflow_divergence([trace], ref)
        finding = findings[0]
        assert finding.variant_candidate is True
        assert finding.actual_transition == ("B", "X")
        assert finding.expected_transition == ("B", "C")


class TestCoverageAndExtraRate:
    """Coverage and extra_rate are set on every finding."""

    def test_coverage_ratio_computation(self) -> None:
        """Trace edges {(A,B), (B,C)} vs reference {(A,B), (B,C), (C,D)} → coverage = 2/3."""
        ref = _ref(
            {("A", "B"): 5, ("B", "C"): 5, ("C", "D"): 5},
            path=["A", "B", "C", "D"],
        )
        trace = _trace("t1", ["A", "B", "C"])
        findings = detect_workflow_divergence([trace], ref)
        finding = findings[0]
        assert abs(finding.coverage - (2 / 3)) < 1e-6

    def test_extra_rate_ratio_computation(self) -> None:
        """Trace edges {(A,B), (X,Y)} vs reference {(A,B)} → extra_rate = 1/2."""
        ref = _ref({("A", "B"): 5}, path=["A", "B"])
        trace = _trace("t1", ["A", "B", "X", "Y"])
        findings = detect_workflow_divergence([trace], ref)
        finding = findings[0]
        # trace_edges = {(A,B), (B,X), (X,Y)} → 3 edges
        # extras = {(B,X), (X,Y)} → 2 extras
        # extra_rate = 2/3
        assert abs(finding.extra_rate - (2 / 3)) < 1e-6

    def test_empty_trace_edges_yields_zero_rates_safely(self) -> None:
        """Single-step trace has no bigrams — finding is safe with 0-rates."""
        ref = _ref({("A", "B"): 5}, path=["A", "B"])
        trace = _trace("t1", ["A"])
        findings = detect_workflow_divergence([trace], ref)
        assert len(findings) == 1
        finding = findings[0]
        assert finding.extra_rate == 0.0
        # coverage with 0 trace edges is 0 (no reference edges covered).
        assert finding.coverage == 0.0
        assert finding.first_divergence_step is None

    def test_empty_reference_edges_yields_zero_coverage_safely(self) -> None:
        """Empty reference edges → function returns empty list (no reference to compare)."""
        ref = _ref({}, path=[], confidence=ReferenceConfidence.LOW)
        # With no reference edges, function should return [].
        trace = _trace("t1", ["A", "B"])
        findings = detect_workflow_divergence([trace], ref)
        assert findings == []


class TestNoReferenceGuard:
    """When ReferenceCohort has no usable reference, detector is a no-op."""

    def test_confidence_none_returns_empty_list(self) -> None:
        ref = _ref_none()
        traces = [_trace("t1", ["A", "B", "C"])]
        findings = detect_workflow_divergence(traces, ref)
        assert findings == []

    def test_empty_reference_edges_returns_empty_list(self) -> None:
        ref = _ref({}, path=[], confidence=ReferenceConfidence.LOW)
        traces = [_trace("t1", ["A", "B"])]
        findings = detect_workflow_divergence(traces, ref)
        assert findings == []


class TestDeterministicOrder:
    """Findings are returned in input trace order."""

    def test_findings_follow_input_trace_order(self) -> None:
        ref = _ref(
            {("A", "B"): 5, ("B", "C"): 5, ("C", "D"): 5},
            path=["A", "B", "C", "D"],
        )
        traces = [
            _trace("first", ["A", "B", "C", "D"]),
            _trace("second", ["A", "B", "X"]),
            _trace("third", ["A", "B", "C", "D"]),
        ]
        findings = detect_workflow_divergence(traces, ref)
        assert len(findings) == 3
        assert [f.trace_id for f in findings] == ["first", "second", "third"]
