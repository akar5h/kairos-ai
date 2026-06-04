"""Derive BusinessContext from a tool catalog (no hand-YAML per agent)."""

from __future__ import annotations

import pytest

from kairos.taxonomy.tool_catalog import business_context_from_tool_catalog


def test_groups_by_mcp_server_and_core() -> None:
    ctx = business_context_from_tool_catalog(
        agent_name="pc",
        agent_description="desc",
        tools=[
            "Read",
            "Bash",
            "Edit",
            "mcp__notion__create_page",
            "mcp__notion__search",
            "mcp__github__merge_pr",
        ],
    )
    ops = {op.name: op for op in ctx.operations}
    assert set(ops) == {"core", "notion", "github"}
    assert ops["core"].expected_tools == ["Read", "Bash", "Edit"]
    assert ops["notion"].expected_tools == ["mcp__notion__create_page", "mcp__notion__search"]
    assert ops["github"].expected_tools == ["mcp__github__merge_pr"]


def test_side_effect_tools_detected() -> None:
    ctx = business_context_from_tool_catalog(
        agent_name="pc",
        agent_description="desc",
        tools=[
            "Read",
            "Edit",
            "mcp__notion__create_page",
            "mcp__notion__search",
            "mcp__github__merge_pr",
        ],
    )
    ops = {op.name: op for op in ctx.operations}
    # create/edit/merge mutate state; read/search do not.
    assert ops["core"].required_side_effect_tools == ["Edit"]
    assert ops["notion"].required_side_effect_tools == ["mcp__notion__create_page"]
    assert ops["github"].required_side_effect_tools == ["mcp__github__merge_pr"]


def test_accepts_mapping_tools_and_dedups() -> None:
    ctx = business_context_from_tool_catalog(
        agent_name="pc",
        agent_description="desc",
        tools=[{"name": "Read"}, {"name": "Read"}, {"name": "Bash"}],
    )
    assert ctx.agent_name == "pc"
    core = ctx.operations[0]
    assert core.expected_tools == ["Read", "Bash"]


def test_empty_catalog_fails_loud() -> None:
    with pytest.raises(ValueError, match="empty"):
        business_context_from_tool_catalog("pc", "desc", tools=[])
