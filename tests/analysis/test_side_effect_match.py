"""side_effect_match "any" vs "all" semantics.

Day 4 spot-check root cause: Code Implementation declares
required_side_effect_tools [Edit, Write] but real coding sessions usually
call one OR the other. Under "all" semantics every Edit-only / Write-only
trace failed missing_side_effect (7/7 owner-disputed N rows). "any" mode
fixes this at three call sites:
  1. membership FULL vs ATTEMPTED (engine/pipeline.classify_membership)
  2. outcome condition 4 (_side_effect_result three-valued any-logic)
  3. outcome condition-2 coverage gate (skipped under "any")
"""

from __future__ import annotations

import pytest

from kairos.analysis.outcome_metric import evaluate_outcome
from kairos.analysis.workflow_membership import MembershipKind
from kairos.engine.pipeline import classify_membership
from kairos.models.enums import (
    FailureReason,
    StepStatus,
    StepStatusSource,
    StepType,
    TerminalStatus,
)
from kairos.models.trace import Step, TraceEnvelope
from kairos.taxonomy.business_context import BusinessContext, BusinessOperation


def _op(match: str) -> BusinessOperation:
    return BusinessOperation(
        name="Code Implementation",
        description="test",
        expected_tools=["Read", "Edit", "Write"],
        priority="high",
        required_side_effect_tools=["Edit", "Write"],
        side_effect_match=match,  # type: ignore[arg-type]
    )


def _step(
    i: int,
    tool: str,
    *,
    status: StepStatus = StepStatus.OK,
    status_source: StepStatusSource = StepStatusSource.ATTR_SUCCESS,
    output: str | None = None,
) -> Step:
    """Live-shaped step: structured status, no readable output (F10)."""
    return Step(
        step_index=i,
        step_type=StepType.TOOL_CALL,
        tool_name=tool,
        tool_output=output,
        status=status,
        status_source=status_source,
    )


def _trace(steps: list[Step], trace_id: str = "t-any") -> TraceEnvelope:
    return TraceEnvelope(
        trace_id=trace_id,
        user_input="test",
        steps=steps,
        terminal_status=TerminalStatus.COMPLETED,
    )


# ── Outcome condition 4 + coverage gate ──────────────────────────────────


class TestOutcomeAnyMode:
    def test_edit_only_passes(self) -> None:
        trace = _trace([_step(0, "Read"), _step(1, "Edit")])
        result = evaluate_outcome(trace, _op("any"))
        assert result.outcome_pass is True
        assert result.computable is True
        assert result.failure_reason is None

    def test_write_only_passes(self) -> None:
        trace = _trace([_step(0, "Read"), _step(1, "Write")])
        result = evaluate_outcome(trace, _op("any"))
        assert result.outcome_pass is True

    def test_neither_fails_missing_side_effect(self) -> None:
        trace = _trace([_step(0, "Read"), _step(1, "Read")])
        result = evaluate_outcome(trace, _op("any"))
        assert result.outcome_pass is False
        assert result.computable is True
        assert result.failure_reason == FailureReason.MISSING_SIDE_EFFECT

    def test_coverage_gate_skipped(self) -> None:
        """Edit-only must NOT fail condition 2's coverage<1.0 gate under any-mode."""
        trace = _trace([_step(0, "Edit")])
        result = evaluate_outcome(trace, _op("any"))
        assert result.reason != "missing_required_tool (coverage<1.0)"
        assert result.outcome_pass is True

    def test_non_computable_when_evidence_unknown(self) -> None:
        """No tool satisfied, but Edit attempts carry no evidence → non-computable."""
        edit_unknown = _step(0, "Edit", status_source=StepStatusSource.NONE, output=None)
        trace = _trace([edit_unknown])
        result = evaluate_outcome(trace, _op("any"))
        assert result.computable is False
        assert result.outcome_pass is False

    def test_output_failed_preferred_over_missing(self) -> None:
        """Edit succeeded but text contradicts; Write absent → SIDE_EFFECT_OUTPUT_FAILED.

        status_source must be NONE: rung 4 (textual) never overrides structured
        rungs 1–3, so the downgrade only applies to unstructured steps.
        """
        edit_contradicted = _step(0, "Edit", status_source=StepStatusSource.NONE, output="Error: ENOENT no such file")
        trace = _trace([edit_contradicted])
        result = evaluate_outcome(trace, _op("any"))
        assert result.outcome_pass is False
        assert result.computable is True
        assert result.failure_reason == FailureReason.SIDE_EFFECT_OUTPUT_FAILED


class TestOutcomeAllModeUnchanged:
    def test_edit_only_still_fails_all_mode(self) -> None:
        """Regression: all-mode keeps demanding both tools (coverage gate fires)."""
        trace = _trace([_step(0, "Read"), _step(1, "Edit")])
        result = evaluate_outcome(trace, _op("all"))
        assert result.outcome_pass is False
        assert result.failure_reason == FailureReason.MISSING_SIDE_EFFECT

    def test_both_tools_pass_all_mode(self) -> None:
        trace = _trace([_step(0, "Edit"), _step(1, "Write")])
        result = evaluate_outcome(trace, _op("all"))
        assert result.outcome_pass is True


# ── Membership FULL vs ATTEMPTED ─────────────────────────────────────────


class TestMembershipAnyMode:
    def test_edit_only_full_under_any(self) -> None:
        trace = _trace([_step(0, "Read"), _step(1, "Edit")])
        membership = classify_membership(trace, _op("any"))
        assert membership.kind == MembershipKind.FULL

    def test_edit_only_attempted_under_all(self) -> None:
        trace = _trace([_step(0, "Read"), _step(1, "Edit")])
        membership = classify_membership(trace, _op("all"))
        assert membership.kind == MembershipKind.ATTEMPTED

    def test_no_required_tool_none_under_any(self) -> None:
        trace = _trace([_step(0, "Read")])
        membership = classify_membership(trace, _op("any"))
        assert membership.kind == MembershipKind.NONE


# ── Schema parsing ───────────────────────────────────────────────────────


class TestSchema:
    def test_default_is_all(self) -> None:
        op = BusinessOperation(name="x", description="")
        assert op.side_effect_match == "all"

    def test_from_dict_parses_any(self) -> None:
        ctx = BusinessContext.from_dict(
            {
                "operations": [
                    {
                        "name": "Code Implementation",
                        "expected_tools": ["Edit", "Write"],
                        "required_side_effect_tools": ["Edit", "Write"],
                        "side_effect_match": "any",
                    }
                ]
            }
        )
        assert ctx.operations[0].side_effect_match == "any"

    def test_from_dict_rejects_invalid(self) -> None:
        with pytest.raises(ValueError, match="side_effect_match"):
            BusinessContext.from_dict(
                {
                    "operations": [
                        {
                            "name": "Bad Op",
                            "side_effect_match": "some",
                        }
                    ]
                }
            )
