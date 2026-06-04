"""Tests for the week-1 evidence coverage calculator.

Target module (not yet implemented):
    src.kairos.analysis.evidence_coverage

Expected surface:
    @dataclass EvidenceCoverage(
        total_traces,
        valid_traces,
        invalid_traces,
        required_field_counts: dict[str, int],
        context_field_counts: dict[str, int],
    )
    def compute_evidence_coverage(envelopes: list[TraceEnvelope]) -> EvidenceCoverage
    EvidenceCoverage.disabled_claims(minimum_coverage: float = 0.3) -> list[str]
"""

from __future__ import annotations

import json
from pathlib import Path

from kairos.analysis.evidence_coverage import (
    EvidenceCoverage,
    compute_evidence_coverage,
)
from kairos.models.enums import StepStatus, StepType, TerminalStatus
from kairos.models.trace import Step, TraceEnvelope

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _load_raw(name: str) -> dict:
    with (FIXTURES / name).open() as f:
        return json.load(f)


# ── Required and context field keys under test ─────────────────────────

REQUIRED_FIELDS = {
    "user_input",
    "tool_sequence",
    "tool_args",
    "tool_outputs",
    "terminal_status",
}
CONTEXT_FIELDS = {
    "system_prompt",
    "retrieval_chunks",
    "memory_events",
    "graph_node",
    "versions",
}


# ── Synthetic envelope helpers ─────────────────────────────────────────


def _minimal_envelope_without_retrieval(trace_id: str = "t-no-retrieval") -> TraceEnvelope:
    """Envelope with tool calls but no retrieval chunks, no memory events."""
    return TraceEnvelope(
        trace_id=trace_id,
        user_input="Please evaluate candidate Alice Chen.",
        system_prompt=None,
        steps=[
            Step(
                step_index=0,
                step_type=StepType.TOOL_CALL,
                tool_name="get_rubric",
                tool_args={"position": "SE"},
                tool_output="ok",
                status=StepStatus.OK,
            ),
        ],
        terminal_status=TerminalStatus.COMPLETED,
    )


def _full_coverage_envelope(trace_id: str = "t-full") -> TraceEnvelope:
    """Envelope with retrieval, system prompt, memory metadata, node info, versions."""
    return TraceEnvelope(
        trace_id=trace_id,
        user_input="evaluate candidate",
        system_prompt="You are a hiring assistant.",
        steps=[
            Step(
                step_index=0,
                step_type=StepType.RETRIEVAL,
                tool_name="retrieve_docs",
                retrieval_query="rubric for senior engineer",
                retrieval_chunks=["chunk-a", "chunk-b"],
                status=StepStatus.OK,
                node_name="retriever_node",
            ),
            Step(
                step_index=1,
                step_type=StepType.TOOL_CALL,
                tool_name="submit_evaluation",
                tool_args={"score": 9},
                tool_output="ok",
                status=StepStatus.OK,
                node_name="submit_node",
            ),
        ],
        terminal_status=TerminalStatus.COMPLETED,
        metadata={
            "memory_events": [{"type": "read", "key": "session-42"}],
            "prompt_version": "v3",
            "tool_version": "2026.04",
            "agent_version": "1.2.0",
        },
    )


# ── TESTS ─────────────────────────────────────────────────────────────


class TestEvidenceCoverageReturnShape:
    """EvidenceCoverage data class surface."""

    def test_empty_envelopes_list_returns_zeros(self) -> None:
        coverage = compute_evidence_coverage([])
        assert isinstance(coverage, EvidenceCoverage)
        assert coverage.total_traces == 0
        assert coverage.valid_traces == 0
        assert coverage.invalid_traces == 0
        assert coverage.required_field_counts == {} or all(v == 0 for v in coverage.required_field_counts.values())
        assert coverage.context_field_counts == {} or all(v == 0 for v in coverage.context_field_counts.values())


class TestEvidenceCoverageDisabledClaims:
    """Claim gates fire when the context field is below the coverage threshold."""

    def test_disabled_claims_when_retrieval_missing(self) -> None:
        """No retrieval chunks -> context_ignored / retrieval_inconsistency are disabled."""
        envelope = _minimal_envelope_without_retrieval()
        coverage = compute_evidence_coverage([envelope])
        disabled = coverage.disabled_claims()
        assert isinstance(disabled, list)
        # Must mention at least one retrieval-gated claim.
        assert any(claim in disabled for claim in ("context_ignored", "retrieval_inconsistency")), (
            f"expected retrieval-gated claim in {disabled}"
        )

    def test_disabled_claims_when_memory_missing(self) -> None:
        """No memory events -> memory_contamination is disabled."""
        envelope = _minimal_envelope_without_retrieval()
        coverage = compute_evidence_coverage([envelope])
        disabled = coverage.disabled_claims()
        assert "memory_contamination" in disabled

    def test_disabled_claims_empty_when_full_coverage(self) -> None:
        """Envelope with all context fields present should produce no disabled claims."""
        envelope = _full_coverage_envelope()
        coverage = compute_evidence_coverage([envelope])
        disabled = coverage.disabled_claims()
        assert disabled == []
