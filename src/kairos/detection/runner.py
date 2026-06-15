"""Tier 1 detection runner — orchestrates all Tier 1 pattern detectors."""

from __future__ import annotations

from typing import TYPE_CHECKING

from kairos.config import settings
from kairos.detection.coordination import detect_coordination_context
from kairos.detection.loops import loop_assertion, loop_guard
from kairos.detection.redundant import redundant_assertion, redundant_guard

if TYPE_CHECKING:
    from kairos.detection.models import Finding
    from kairos.models.trace import TraceEnvelope


def detect_tier1(
    traces: list[TraceEnvelope],
    cluster_median_steps: float,
    coordination_markers: list[str] | None = None,
    coordination_tools: list[str] | None = None,
) -> list[Finding]:
    """Run all Tier 1 detectors on *traces* and return aggregated findings.

    ``coordination_markers`` and ``coordination_tools`` are forwarded to
    ``detect_coordination_context``; pass them from ``BusinessContext`` when
    available.  Both default to ``[]`` (feature off) when omitted.
    """
    markers: list[str] = coordination_markers or []
    tools: list[str] = coordination_tools or []

    findings: list[Finding] = []

    for trace in traces:
        if redundant_guard(trace):
            findings.extend(
                redundant_assertion(
                    trace,
                    threshold=settings.redundant_jaccard_threshold,
                )
            )
        if loop_guard(trace, cluster_median_steps):
            findings.extend(loop_assertion(trace, min_repeats=settings.loop_min_repeats))

        coord_finding = detect_coordination_context(trace, markers=markers, tools=tools)
        if coord_finding is not None:
            findings.append(coord_finding)

    return findings
