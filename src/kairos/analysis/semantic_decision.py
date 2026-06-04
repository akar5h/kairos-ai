"""Bounded semantic decision analysis via LLM.

Selects representative flagged packets, sends each through the configured
LLMClient with a schema-constrained prompt, and synthesizes an
insufficient_evidence finding on any failure. Hard rule: this function
never raises because of an LLM failure.
"""

from __future__ import annotations

import dataclasses
import json
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from kairos.log import get_logger

if TYPE_CHECKING:
    from kairos.analysis.decision_state import DecisionStatePacket
    from kairos.analysis.llm_client import LLMClient

logger = get_logger(__name__)

# src/kairos/analysis/semantic_decision.py → parents:
#   [0] analysis/, [1] kairos/, [2] src/, [3] repo root
_DEFAULT_PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "semantic_decision_v1.txt"


class FindingType(StrEnum):
    CONTEXT_IGNORED = "context_ignored"
    RETRIEVAL_MISSING = "retrieval_missing"
    MEMORY_CONFLICT = "memory_conflict"
    WRONG_TOOL_SELECTED = "wrong_tool_selected"
    REDUNDANT_ACTION = "redundant_action"
    PREMATURE_COMPLETION = "premature_completion"
    TOOL_SCHEMA_MISMATCH = "tool_schema_mismatch"
    GRAPH_ORCHESTRATION_ISSUE = "graph_orchestration_issue"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class FixArea(StrEnum):
    PROMPT = "prompt"
    RETRIEVAL = "retrieval"
    TOOL_SCHEMA = "tool_schema"
    TOOL_IMPLEMENTATION = "tool_implementation"
    GRAPH_ORCHESTRATION = "graph_orchestration"
    MEMORY_POLICY = "memory_policy"
    MODEL_BEHAVIOR = "model_behavior"
    UNKNOWN = "unknown"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class DecisionAdvanced(StrEnum):
    YES = "yes"
    NO = "no"
    UNCLEAR = "unclear"


class SemanticDecisionFinding(BaseModel):
    trace_id: str
    workflow_name: str
    step_index: int
    decision_advanced_task: DecisionAdvanced
    finding_type: FindingType
    evidence_refs: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    likely_fix_area: FixArea
    confidence: Confidence
    ticket_title: str
    verification_target: str


def analyze_flagged_traces(
    packets_by_pattern: dict[str, list[DecisionStatePacket]],
    client: LLMClient,
    *,
    trace_metrics: dict[str, tuple[int, int]] | None = None,
    top_n_patterns: int = 3,
    per_pattern_trace_limit: int = 5,
    prompt_template: str | None = None,
) -> list[SemanticDecisionFinding]:
    """Analyze the top-N flagged patterns, bounded by per-pattern trace limit.

    Never raises on LLM failure: a synthetic insufficient_evidence finding is
    produced for any packet whose analysis fails.
    """
    if not packets_by_pattern:
        return []

    if prompt_template is None:
        prompt_template = _DEFAULT_PROMPT_PATH.read_text()

    metrics = trace_metrics or {}

    def _metric(trace_id: str) -> tuple[int, int]:
        return metrics.get(trace_id, (0, 0))

    ranked_patterns = sorted(
        packets_by_pattern.items(),
        key=lambda kv: (-len(kv[1]), kv[0]),
    )[:top_n_patterns]

    findings: list[SemanticDecisionFinding] = []
    for _pattern_name, packets in ranked_patterns:
        if not packets:
            continue
        ordered = sorted(
            packets,
            key=lambda p: (
                -_metric(p.trace_id)[0],
                -_metric(p.trace_id)[1],
                p.trace_id,
            ),
        )[:per_pattern_trace_limit]

        for packet in ordered:
            finding = _analyze_one(packet, client, prompt_template)
            findings.append(finding)

    return findings


def _analyze_one(
    packet: DecisionStatePacket,
    client: LLMClient,
    prompt_template: str,
) -> SemanticDecisionFinding:
    try:
        packet_json = json.dumps(dataclasses.asdict(packet), default=str, indent=2)
        prompt = prompt_template.replace("{packet_json}", packet_json)
        result = client.generate(prompt, SemanticDecisionFinding)
    except (RuntimeError, ValueError, TypeError) as exc:
        logger.warning(
            "semantic_decision.client_raised",
            trace_id=packet.trace_id,
            step_index=packet.step_index,
            error=str(exc)[:200],
        )
        result = None

    if result is None:
        return _insufficient_evidence_for(packet)
    if not isinstance(result, SemanticDecisionFinding):
        logger.warning(
            "semantic_decision.unexpected_schema",
            trace_id=packet.trace_id,
            step_index=packet.step_index,
            schema=type(result).__name__,
        )
        return _insufficient_evidence_for(packet)
    return result


def _insufficient_evidence_for(packet: DecisionStatePacket) -> SemanticDecisionFinding:
    return SemanticDecisionFinding(
        trace_id=packet.trace_id,
        workflow_name=packet.workflow_name,
        step_index=packet.step_index,
        decision_advanced_task=DecisionAdvanced.UNCLEAR,
        finding_type=FindingType.INSUFFICIENT_EVIDENCE,
        evidence_refs=[],
        missing_evidence=["llm_analysis_failed"],
        likely_fix_area=FixArea.UNKNOWN,
        confidence=Confidence.LOW,
        ticket_title="LLM analysis failed; manual review required",
        verification_target="Re-run when LLM is available; confirm decision-state packet is complete.",
    )
