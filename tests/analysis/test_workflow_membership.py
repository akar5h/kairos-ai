"""Red-phase tests for the multi-label workflow membership model.

Target module (not yet implemented):
    src.kairos.analysis.workflow_membership

Expected surface:
    class MembershipKind(StrEnum): FULL | ATTEMPTED | NONE
    @dataclass(frozen=True) WorkflowMembership:
        operation_name: str
        kind: MembershipKind
        recall: float
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, is_dataclass

import pytest

from kairos.analysis.workflow_membership import (
    MembershipKind,
    WorkflowMembership,
)


class TestMembershipKind:
    """MembershipKind is a StrEnum with three values."""

    def test_enum_has_three_values(self) -> None:
        # The enum must define exactly FULL, ATTEMPTED, NONE members.
        names = {member.name for member in MembershipKind}
        assert names == {"FULL", "ATTEMPTED", "NONE"}

    def test_string_values(self) -> None:
        assert MembershipKind.FULL.value == "full"
        assert MembershipKind.ATTEMPTED.value == "attempted"
        assert MembershipKind.NONE.value == "none"


class TestWorkflowMembership:
    """WorkflowMembership is a frozen dataclass with operation_name/kind/recall."""

    def test_dataclass_is_frozen_and_hashable(self) -> None:
        m = WorkflowMembership(
            operation_name="Candidate Screening",
            kind=MembershipKind.FULL,
            recall=1.0,
        )
        # Frozen dataclasses raise FrozenInstanceError on attribute set.
        with pytest.raises(FrozenInstanceError):
            m.operation_name = "other"  # type: ignore[misc]

        # Hashable → usable as dict key.
        d = {m: "ok"}
        assert d[m] == "ok"

    def test_fields(self) -> None:
        assert is_dataclass(WorkflowMembership)
        field_map = {f.name: f.type for f in fields(WorkflowMembership)}
        assert "operation_name" in field_map
        assert "kind" in field_map
        assert "recall" in field_map

        m = WorkflowMembership(
            operation_name="Outreach",
            kind=MembershipKind.ATTEMPTED,
            recall=0.75,
        )
        assert m.operation_name == "Outreach"
        assert m.kind == MembershipKind.ATTEMPTED
        assert m.recall == 0.75
