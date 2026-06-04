"""Derive a BusinessContext from an agent's MCP / tool catalog.

Paperclip agents do not ship a hand-written ``business_context.yaml`` per
agent. Instead the workflow taxonomy is *derived* from the tools the agent can
actually call: each MCP server (``mcp__<server>__<action>``) becomes one
business operation grouping its actions, and the built-in tools (Read, Bash,
Edit, …) become a ``core`` operation. Side-effecting actions (create/update/
delete/write/send/run/…) populate ``required_side_effect_tools`` so the engine
can tell FULL workflow runs from read-only ATTEMPTED ones.

This keeps the taxonomy honest: it can only reference tools that exist, and it
updates automatically when the catalog changes.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from kairos.taxonomy.business_context import BusinessContext, BusinessOperation

if TYPE_CHECKING:
    from collections.abc import Iterable

# An MCP tool name: mcp__<server>__<action>.
_MCP_RE = re.compile(r"^mcp__(?P<server>[^_].*?)__(?P<action>.+)$")

# Action substrings that imply a side effect (mutates external state).
_SIDE_EFFECT_HINTS = (
    "create",
    "update",
    "delete",
    "remove",
    "write",
    "edit",
    "send",
    "post",
    "patch",
    "move",
    "add",
    "run",
    "exec",
    "set",
    "put",
    "upload",
    "merge",
    "comment",
    "deploy",
)

_CORE_GROUP = "core"


def _tool_name(tool: Any) -> str | None:
    if isinstance(tool, str):
        return tool
    if isinstance(tool, Mapping):
        name = tool.get("name")
        return str(name) if name else None
    return None


def _group_and_action(name: str) -> tuple[str, str]:
    match = _MCP_RE.match(name)
    if match:
        return match.group("server"), match.group("action")
    return _CORE_GROUP, name


def _is_side_effect(action: str) -> bool:
    lowered = action.lower()
    return any(hint in lowered for hint in _SIDE_EFFECT_HINTS)


def business_context_from_tool_catalog(
    agent_name: str,
    agent_description: str,
    tools: Iterable[Any],
) -> BusinessContext:
    """Build a BusinessContext by grouping a tool catalog into operations.

    ``tools`` may be tool-name strings or mappings with a ``name`` key.
    Duplicate names are collapsed; ordering within a group follows first sight.
    """
    grouped: dict[str, list[str]] = {}
    side_effects: dict[str, list[str]] = {}
    seen: set[str] = set()

    for tool in tools:
        name = _tool_name(tool)
        if name is None or name in seen:
            continue
        seen.add(name)
        group, action = _group_and_action(name)
        grouped.setdefault(group, []).append(name)
        if _is_side_effect(action):
            side_effects.setdefault(group, []).append(name)

    if not grouped:
        msg = "tool catalog is empty: cannot derive a business context"
        raise ValueError(msg)

    operations = [
        BusinessOperation(
            name=group,
            description=f"{group} operations (derived from tool catalog)",
            expected_tools=names,
            required_side_effect_tools=side_effects.get(group, []),
        )
        for group, names in grouped.items()
    ]

    return BusinessContext(
        agent_name=agent_name,
        agent_description=agent_description,
        operations=operations,
    )
