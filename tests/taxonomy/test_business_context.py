"""Tests for the business-context taxonomy: config loading + membership thresholds."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from kairos.taxonomy.business_context import (
    BusinessContext,
    BusinessOperation,
    default_membership_threshold,
)

FIXTURE_PATH = Path(__file__).parent.parent / "fixtures" / "test_business_context.yaml"


class TestBusinessContext:
    """Tests for loading and validating BusinessContext config."""

    def test_load_from_yaml(self) -> None:
        ctx = BusinessContext.from_yaml(FIXTURE_PATH)
        assert ctx.agent_name == "HR Recruitment Agent"
        assert len(ctx.operations) == 3
        assert ctx.operations[0].name == "Candidate Evaluation"

    def test_load_from_dict(self) -> None:
        data = {
            "agent_name": "Test Agent",
            "agent_description": "A test agent",
            "operations": [
                {
                    "name": "Op1",
                    "description": "First operation",
                    "expected_tools": ["tool_a"],
                    "priority": "high",
                }
            ],
        }
        ctx = BusinessContext.from_dict(data)
        assert ctx.agent_name == "Test Agent"
        assert len(ctx.operations) == 1
        assert ctx.operations[0].expected_tools == ["tool_a"]

    def test_empty_operations_raises(self) -> None:
        data = {
            "agent_name": "Test",
            "agent_description": "Test",
            "operations": [],
        }
        with pytest.raises(ValueError, match="operations"):
            BusinessContext.from_dict(data)

    def test_operation_without_name_raises(self) -> None:
        data = {
            "agent_name": "Test",
            "agent_description": "Test",
            "operations": [{"description": "missing name"}],
        }
        with pytest.raises((ValueError, KeyError)):
            BusinessContext.from_dict(data)

    def test_operation_without_expected_tools(self) -> None:
        """Operations without expected_tools should still be valid."""
        data = {
            "agent_name": "Test",
            "agent_description": "Test",
            "operations": [{"name": "Op1", "description": "No tools specified"}],
        }
        ctx = BusinessContext.from_dict(data)
        assert ctx.operations[0].expected_tools == []

    def test_priority_defaults_to_medium(self) -> None:
        data = {
            "agent_name": "Test",
            "agent_description": "Test",
            "operations": [{"name": "Op1", "description": "No priority"}],
        }
        ctx = BusinessContext.from_dict(data)
        assert ctx.operations[0].priority == "medium"


class TestBusinessOperationWeek1Fields:
    """Tests for the Week 1 extension fields on BusinessOperation."""

    def test_load_new_fields_from_dict(self) -> None:
        """Dict with all 4 new fields populates them on BusinessOperation."""
        data = {
            "agent_name": "Test Agent",
            "agent_description": "A test agent",
            "operations": [
                {
                    "name": "Candidate Screening",
                    "description": "Screen one candidate",
                    "business_goal": "Reduce recruiter review time without missing evidence.",
                    "reliability_metric": "percent of completed screenings with full evidence.",
                    "bad_run_means": "Missing evidence or unsupported recommendation.",
                    "expected_tools": ["get_rubric", "parse_resume", "submit_evaluation"],
                    "required_side_effect_tools": ["submit_evaluation"],
                    "priority": "high",
                },
            ],
        }
        ctx = BusinessContext.from_dict(data)
        op = ctx.operations[0]
        assert op.business_goal == "Reduce recruiter review time without missing evidence."
        assert op.reliability_metric == "percent of completed screenings with full evidence."
        assert op.bad_run_means == "Missing evidence or unsupported recommendation."
        assert op.required_side_effect_tools == ["submit_evaluation"]

    def test_new_fields_default_to_none_or_empty(self) -> None:
        """Dict without new fields defaults business_goal/reliability_metric/bad_run_means to None."""
        data = {
            "agent_name": "Test Agent",
            "agent_description": "A test agent",
            "operations": [
                {
                    "name": "Legacy Op",
                    "description": "Older op without week1 fields",
                    "expected_tools": ["tool_a"],
                    "priority": "medium",
                },
            ],
        }
        ctx = BusinessContext.from_dict(data)
        op = ctx.operations[0]
        assert op.business_goal is None
        assert op.reliability_metric is None
        assert op.bad_run_means is None
        assert op.required_side_effect_tools == []

    def test_existing_fixture_still_loads(self) -> None:
        """The pre-existing test_business_context.yaml fixture must still load cleanly."""
        ctx = BusinessContext.from_yaml(FIXTURE_PATH)
        assert ctx.agent_name == "HR Recruitment Agent"
        assert len(ctx.operations) == 3
        for op in ctx.operations:
            # Legacy fixture does not set these; they must default to None / [].
            assert op.business_goal is None
            assert op.reliability_metric is None
            assert op.bad_run_means is None
            assert op.required_side_effect_tools == []

    def test_correctness_criteria_parsed_when_present(self) -> None:
        """Week 1.5 Slice A: correctness_criteria is a first-class YAML field."""
        data = {
            "agent_name": "Test Agent",
            "agent_description": "A test agent",
            "operations": [
                {
                    "name": "Candidate Screening",
                    "description": "Screen one candidate",
                    "expected_tools": ["get_rubric", "parse_resume", "submit_evaluation"],
                    "correctness_criteria": [
                        "final recommendation must be supported by evidence from the rubric",
                        "output must cite parsed resume fields",
                    ],
                },
            ],
        }
        ctx = BusinessContext.from_dict(data)
        op = ctx.operations[0]
        assert op.correctness_criteria == [
            "final recommendation must be supported by evidence from the rubric",
            "output must cite parsed resume fields",
        ]

    def test_correctness_criteria_defaults_to_empty_list(self) -> None:
        """Operations without correctness_criteria must default to []."""
        data = {
            "agent_name": "Test Agent",
            "agent_description": "A test agent",
            "operations": [
                {"name": "Op1", "description": "no criteria"},
            ],
        }
        ctx = BusinessContext.from_dict(data)
        assert ctx.operations[0].correctness_criteria == []

    def test_existing_fixture_still_loads_with_empty_criteria(self) -> None:
        """Legacy fixture yaml has no correctness_criteria → all ops default to []."""
        ctx = BusinessContext.from_yaml(FIXTURE_PATH)
        for op in ctx.operations:
            assert op.correctness_criteria == []

    def test_membership_recall_threshold_parsed_when_present(self) -> None:
        """Slice B.0: membership_recall_threshold must load from YAML."""
        data = {
            "agent_name": "Test Agent",
            "agent_description": "A test agent",
            "operations": [
                {
                    "name": "Strict Workflow",
                    "description": "5-tool op with a tight threshold",
                    "expected_tools": ["a", "b", "c", "d", "e"],
                    "membership_recall_threshold": 0.8,
                },
            ],
        }
        ctx = BusinessContext.from_dict(data)
        assert ctx.operations[0].membership_recall_threshold == 0.8

    def test_membership_recall_threshold_defaults_to_none(self) -> None:
        """Slice B.0: membership_recall_threshold omitted → attr is None."""
        data = {
            "agent_name": "Test Agent",
            "agent_description": "A test agent",
            "operations": [
                {
                    "name": "Default Workflow",
                    "description": "no explicit threshold",
                    "expected_tools": ["a", "b"],
                },
            ],
        }
        ctx = BusinessContext.from_dict(data)
        assert ctx.operations[0].membership_recall_threshold is None

    def test_default_membership_threshold_for_single_tool_op(self) -> None:
        op = BusinessOperation(
            name="Outreach",
            description="single-tool workflow",
            expected_tools=["send_email"],
            membership_recall_threshold=None,
        )
        assert default_membership_threshold(op) == 1.0

    def test_default_membership_threshold_for_multi_tool_op(self) -> None:
        op = BusinessOperation(
            name="Screening",
            description="3-tool workflow",
            expected_tools=["a", "b", "c"],
            membership_recall_threshold=None,
        )
        assert default_membership_threshold(op) == 0.5

    def test_default_membership_threshold_honors_explicit_value(self) -> None:
        op = BusinessOperation(
            name="Tuned",
            description="explicit threshold",
            expected_tools=["a", "b", "c"],
            membership_recall_threshold=0.7,
        )
        assert default_membership_threshold(op) == 0.7


# ───────────────────────── Day 1.2: fail-loud from_yaml ─────────────────────────


class TestFromYamlFailLoud:
    """Day 1.2: from_yaml raises with the file path in the message for bad configs."""

    def test_zero_operations_raises_with_path_in_message(self, tmp_path: Path) -> None:
        """A YAML file with an empty operations list must raise with the path named."""
        yaml_content = textwrap.dedent("""\
            agent_name: "Test Agent"
            agent_description: "no ops"
            operations: []
        """)
        ctx_file = tmp_path / "empty_ops.yaml"
        ctx_file.write_text(yaml_content)

        with pytest.raises(ValueError, match=str(ctx_file)):
            BusinessContext.from_yaml(ctx_file)

    def test_missing_operations_key_raises_with_path_in_message(self, tmp_path: Path) -> None:
        """A YAML file without an operations key must raise with the path named."""
        yaml_content = textwrap.dedent("""\
            agent_name: "Test Agent"
            agent_description: "no ops key"
        """)
        ctx_file = tmp_path / "no_ops_key.yaml"
        ctx_file.write_text(yaml_content)

        with pytest.raises(ValueError, match=str(ctx_file)):
            BusinessContext.from_yaml(ctx_file)

    def test_non_mapping_document_raises_with_path_in_message(self, tmp_path: Path) -> None:
        """A YAML file whose root is a list (not a mapping) must raise with the path named."""
        yaml_content = textwrap.dedent("""\
            - item1
            - item2
        """)
        ctx_file = tmp_path / "list_root.yaml"
        ctx_file.write_text(yaml_content)

        with pytest.raises(ValueError, match=str(ctx_file)):
            BusinessContext.from_yaml(ctx_file)

    def test_valid_yaml_still_loads_cleanly(self, tmp_path: Path) -> None:
        """Regression: a well-formed context file must not be broken by the guards."""
        yaml_content = textwrap.dedent("""\
            agent_name: "Good Agent"
            agent_description: "has ops"
            operations:
              - name: "Op One"
                description: "first op"
                expected_tools: [Read, Write]
                required_side_effect_tools: [Write]
        """)
        ctx_file = tmp_path / "good.yaml"
        ctx_file.write_text(yaml_content)

        ctx = BusinessContext.from_yaml(ctx_file)
        assert ctx.agent_name == "Good Agent"
        assert len(ctx.operations) == 1
