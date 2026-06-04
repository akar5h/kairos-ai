"""Red-phase tests for the semantic-decision analyzer.

Target module (not yet implemented):
    src.kairos.analysis.semantic_decision

Expected surface:
    class FindingType(StrEnum): ...
    class FixArea(StrEnum): ...
    class Confidence(StrEnum): HIGH | MEDIUM | LOW
    class DecisionAdvanced(StrEnum): YES | NO | UNCLEAR

    class SemanticDecisionFinding(BaseModel):
        trace_id, workflow_name, step_index,
        decision_advanced_task: DecisionAdvanced,
        finding_type: FindingType,
        evidence_refs: list[str],
        missing_evidence: list[str],
        likely_fix_area: FixArea,
        confidence: Confidence,
        ticket_title: str,
        verification_target: str,

    def analyze_flagged_traces(
        packets_by_pattern: dict[str, list[DecisionStatePacket]],
        client: LLMClient,
        *,
        trace_metrics: dict[str, tuple[int, int]] | None = None,
        top_n_patterns: int = 3,
        per_pattern_trace_limit: int = 5,
        prompt_template: str | None = None,
    ) -> list[SemanticDecisionFinding]
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from kairos.analysis.decision_state import DecisionStatePacket, MissingReason
from kairos.analysis.semantic_decision import (
    Confidence,
    DecisionAdvanced,
    FindingType,
    FixArea,
    SemanticDecisionFinding,
    analyze_flagged_traces,
)

# ── Helpers ────────────────────────────────────────────────────────────


def _packet(
    trace_id: str,
    *,
    step_index: int = 1,
    workflow_name: str = "Candidate Screening",
) -> DecisionStatePacket:
    """Construct a minimal DecisionStatePacket for selection-order testing."""
    return DecisionStatePacket(
        trace_id=trace_id,
        workflow_name=workflow_name,
        step_index=step_index,
        business_goal=None,
        reliability_metric=None,
        bad_run_means=None,
        user_input="do the thing",
        system_instruction_summary=None,
        available_tools=["get_rubric", "parse_resume"],
        tool_schema_summary=None,
        memory_reads_before_step=[],
        memory_reads_missing_reason=MissingReason.NOT_INSTRUMENTED,
        retrieved_context_before_step=[],
        retrieved_context_missing_reason=MissingReason.NOT_INSTRUMENTED,
        prior_tool_calls=[],
        prior_tool_outputs_missing_reason=MissingReason.PRESENT,
        current_step_tool_name="parse_resume",
        current_step_tool_args={"x": 1},
        current_step_tool_output="ok",
        current_step_error_message=None,
        reference_expected_transition=None,
        actual_transition=None,
        deterministic_flags=[],
    )


def _ok_finding(trace_id: str, step_index: int = 1) -> SemanticDecisionFinding:
    return SemanticDecisionFinding(
        trace_id=trace_id,
        workflow_name="Candidate Screening",
        step_index=step_index,
        decision_advanced_task=DecisionAdvanced.NO,
        finding_type=FindingType.CONTEXT_IGNORED,
        evidence_refs=["step_1.tool_output"],
        missing_evidence=[],
        likely_fix_area=FixArea.PROMPT,
        confidence=Confidence.MEDIUM,
        ticket_title="Agent ignored retrieved rubric at step 1",
        verification_target="Retry with prompt nudge; confirm submit uses retrieved evidence.",
    )


def _make_client(side_effect: object | list[object]) -> MagicMock:
    """Build a MagicMock LLMClient whose generate() is driven by side_effect."""
    client = MagicMock()
    if isinstance(side_effect, list):
        client.generate.side_effect = side_effect
    else:
        client.generate.side_effect = side_effect
    return client


# ── TESTS ─────────────────────────────────────────────────────────────


class TestDeterministicSelection:
    """Top-N pattern selection and per-pattern packet budgeting."""

    def test_top_n_patterns_by_count(self) -> None:
        """5 patterns with counts [10, 5, 3, 2, 1]; top_n=3 → only first 3 analyzed."""
        patterns: dict[str, list[DecisionStatePacket]] = {
            "pat-a": [_packet(f"ta-{i}") for i in range(10)],
            "pat-b": [_packet(f"tb-{i}") for i in range(5)],
            "pat-c": [_packet(f"tc-{i}") for i in range(3)],
            "pat-d": [_packet(f"td-{i}") for i in range(2)],
            "pat-e": [_packet(f"te-{i}") for i in range(1)],
        }
        client = _make_client(side_effect=lambda *a, **kw: _ok_finding("x"))
        findings = analyze_flagged_traces(
            patterns,
            client,
            top_n_patterns=3,
            per_pattern_trace_limit=5,
        )
        # Only top 3 patterns analyzed. pat-a (min 5), pat-b (5), pat-c (3) -> 13 packets
        assert client.generate.call_count == 13
        assert len(findings) == 13

    def test_per_pattern_trace_limit_enforced(self) -> None:
        """One pattern with 10 packets; per_pattern_trace_limit=5 → exactly 5 analyzed."""
        patterns = {"pat-a": [_packet(f"t-{i}") for i in range(10)]}
        client = _make_client(side_effect=lambda *a, **kw: _ok_finding("x"))
        findings = analyze_flagged_traces(
            patterns,
            client,
            top_n_patterns=3,
            per_pattern_trace_limit=5,
        )
        assert client.generate.call_count == 5
        assert len(findings) == 5

    def test_budget_cap_is_product(self) -> None:
        """3 patterns × 5 packets max = 15 LLM calls ceiling."""
        patterns = {
            "p1": [_packet(f"t1-{i}") for i in range(20)],
            "p2": [_packet(f"t2-{i}") for i in range(20)],
            "p3": [_packet(f"t3-{i}") for i in range(20)],
        }
        client = _make_client(side_effect=lambda *a, **kw: _ok_finding("x"))
        findings = analyze_flagged_traces(
            patterns,
            client,
            top_n_patterns=3,
            per_pattern_trace_limit=5,
        )
        assert client.generate.call_count == 15
        assert len(findings) == 15

    def test_sort_by_step_count_desc_then_tokens_desc_then_trace_id_asc(self) -> None:
        """Packets ordered by (step_count desc, total_tokens desc, trace_id asc).

        Metrics:
            trace_a: (10, 500)
            trace_b: (10, 100)
            trace_c: (5, 500)
            trace_d: (10, 500)
        Expected order: trace_a, trace_d, trace_b, trace_c
        """
        packets = [
            _packet("trace_c"),
            _packet("trace_a"),
            _packet("trace_d"),
            _packet("trace_b"),
        ]
        patterns = {"pat-x": packets}
        trace_metrics = {
            "trace_a": (10, 500),
            "trace_b": (10, 100),
            "trace_c": (5, 500),
            "trace_d": (10, 500),
        }

        call_order: list[str] = []

        def capture(prompt: str, schema: type) -> SemanticDecisionFinding:
            # The prompt embeds the packet JSON; pull the trace_id out of the
            # 2nd positional arg by inspecting the packet passed via closure.
            # Simpler: track call ordering via the last-analyzed trace_id from
            # the mock's call_args_list after the fact.
            return _ok_finding("x")

        client = _make_client(side_effect=capture)
        findings = analyze_flagged_traces(
            patterns,
            client,
            trace_metrics=trace_metrics,
            top_n_patterns=1,
            per_pattern_trace_limit=10,
        )
        # Extract trace_ids from the prompts actually sent.
        prompts_sent = [call.args[0] for call in client.generate.call_args_list]
        for packet_trace_id in ["trace_a", "trace_d", "trace_b", "trace_c"]:
            call_order.append(next(p for p in prompts_sent if packet_trace_id in p))
        # Each prompt is unique; verify the order of first-appearance matches expectation.
        first_appearance_order = []
        for prompt in prompts_sent:
            for tid in ["trace_a", "trace_b", "trace_c", "trace_d"]:
                if tid in prompt and tid not in first_appearance_order:
                    first_appearance_order.append(tid)
                    break
        assert first_appearance_order == ["trace_a", "trace_d", "trace_b", "trace_c"]
        assert len(findings) == 4

    def test_missing_trace_metrics_uses_zero_and_still_deterministic(self) -> None:
        """With no trace_metrics, all metrics default to (0, 0); tie broken by trace_id asc."""
        packets = [
            _packet("trace_z"),
            _packet("trace_a"),
            _packet("trace_m"),
        ]
        patterns = {"pat": packets}
        client = _make_client(side_effect=lambda *a, **kw: _ok_finding("x"))

        findings = analyze_flagged_traces(
            patterns,
            client,
            trace_metrics=None,
            top_n_patterns=1,
            per_pattern_trace_limit=10,
        )
        prompts_sent = [call.args[0] for call in client.generate.call_args_list]
        first_appearance_order: list[str] = []
        for prompt in prompts_sent:
            for tid in ["trace_a", "trace_m", "trace_z"]:
                if tid in prompt and tid not in first_appearance_order:
                    first_appearance_order.append(tid)
                    break
        assert first_appearance_order == ["trace_a", "trace_m", "trace_z"]
        assert len(findings) == 3


class TestLLMFailureFallback:
    """When the LLM returns None or raises, a synthetic insufficient-evidence finding is produced."""

    def test_llm_returns_none_produces_insufficient_evidence_finding(self) -> None:
        packets = [_packet("t-1")]
        patterns = {"pat": packets}
        client = _make_client(side_effect=[None])

        findings = analyze_flagged_traces(patterns, client)

        assert len(findings) == 1
        finding = findings[0]
        assert finding.trace_id == "t-1"
        assert finding.finding_type == FindingType.INSUFFICIENT_EVIDENCE
        assert finding.likely_fix_area == FixArea.UNKNOWN
        assert finding.confidence == Confidence.LOW
        assert "llm_analysis_failed" in finding.missing_evidence

    def test_llm_raises_exception_still_returns_insufficient_evidence(self) -> None:
        """Exception bubbling out of client.generate is swallowed into an insufficient-evidence finding."""
        packets = [_packet("t-2")]
        patterns = {"pat": packets}
        client = _make_client(side_effect=RuntimeError("boom"))

        # The function itself MUST NOT raise.
        findings = analyze_flagged_traces(patterns, client)

        assert len(findings) == 1
        assert findings[0].finding_type == FindingType.INSUFFICIENT_EVIDENCE
        assert findings[0].trace_id == "t-2"
        assert "llm_analysis_failed" in findings[0].missing_evidence

    def test_partial_llm_failures_still_produces_full_finding_list(self) -> None:
        """One success, one None, one exception → 3 findings (mix)."""
        packets = [_packet("t-1"), _packet("t-2"), _packet("t-3")]
        patterns = {"pat": packets}

        client = _make_client(
            side_effect=[
                _ok_finding("t-1"),
                None,
                RuntimeError("boom"),
            ],
        )

        findings = analyze_flagged_traces(
            patterns,
            client,
            top_n_patterns=3,
            per_pattern_trace_limit=5,
        )

        assert len(findings) == 3
        finding_types = [f.finding_type for f in findings]
        # Two fallbacks + one real finding.
        assert finding_types.count(FindingType.INSUFFICIENT_EVIDENCE) == 2
        assert finding_types.count(FindingType.CONTEXT_IGNORED) == 1

    def test_successful_call_returns_schema_validated_finding(self) -> None:
        packets = [_packet("t-1")]
        patterns = {"pat": packets}
        good = _ok_finding("t-1")
        client = _make_client(side_effect=[good])

        findings = analyze_flagged_traces(patterns, client)

        assert len(findings) == 1
        assert findings[0].finding_type == FindingType.CONTEXT_IGNORED
        assert findings[0].trace_id == "t-1"
        assert findings[0].ticket_title == good.ticket_title


class TestPromptRendering:
    """Prompt template loading: default path vs explicit override."""

    def test_prompt_template_loaded_from_default_path(self) -> None:
        packets = [_packet("t-1")]
        patterns = {"pat": packets}
        client = _make_client(side_effect=[_ok_finding("t-1")])

        _ = analyze_flagged_traces(patterns, client)

        assert client.generate.call_count == 1
        prompt = client.generate.call_args.args[0]
        # The default template contains a unique stable phrase we can check for.
        assert "DecisionStatePacket" in prompt or "suspicious decision" in prompt
        # The packet JSON should be embedded.
        assert "t-1" in prompt

        # And the default file must exist on disk for the implementer to load.
        default_template_path = (
            Path(__file__).resolve().parents[2] / "src" / "kairos" / "prompts" / "semantic_decision_v1.txt"
        )
        assert default_template_path.exists(), f"Default prompt template not found at {default_template_path}"

    def test_prompt_template_override(self) -> None:
        override = "OVERRIDE TEMPLATE — packet: {packet_json}"
        packets = [_packet("t-1")]
        patterns = {"pat": packets}
        client = _make_client(side_effect=[_ok_finding("t-1")])

        _ = analyze_flagged_traces(patterns, client, prompt_template=override)

        prompt = client.generate.call_args.args[0]
        assert prompt.startswith("OVERRIDE TEMPLATE")
        assert "t-1" in prompt


class TestEmptyInput:
    """Empty pattern dicts short-circuit without LLM calls."""

    def test_no_patterns_returns_empty_list(self) -> None:
        client = _make_client(side_effect=[])
        findings = analyze_flagged_traces({}, client)
        assert findings == []
        assert client.generate.call_count == 0

    def test_patterns_with_empty_lists_returns_empty_list(self) -> None:
        client = _make_client(side_effect=[])
        findings = analyze_flagged_traces(
            {"p1": [], "p2": []},
            client,
        )
        assert findings == []
        assert client.generate.call_count == 0


class TestFindingSchema:
    """SemanticDecisionFinding BaseModel has the expected shape."""

    def test_schema_accepts_all_required_fields(self) -> None:
        f = _ok_finding("t-schema")
        assert f.trace_id == "t-schema"
        assert f.decision_advanced_task == DecisionAdvanced.NO
        assert f.finding_type == FindingType.CONTEXT_IGNORED
        assert f.likely_fix_area == FixArea.PROMPT
        assert f.confidence == Confidence.MEDIUM

    def test_finding_type_enum_has_insufficient_evidence(self) -> None:
        assert FindingType.INSUFFICIENT_EVIDENCE.value == "insufficient_evidence"

    def test_fix_area_enum_has_unknown(self) -> None:
        assert FixArea.UNKNOWN.value == "unknown"

    def test_decision_advanced_enum_has_unclear(self) -> None:
        assert DecisionAdvanced.UNCLEAR.value == "unclear"
