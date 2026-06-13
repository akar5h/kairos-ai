"""Correlation-key rollup: group traces into logical units of work.

A *unit of work* is one or more traces that all share the same value of a
configured span attribute (``correlation_key`` in ``BusinessContext``).
When ``correlation_key`` is ``None``, each trace is its own unit — behaviour
is byte-identical to before this module existed.

Rollup rules (``last-wins`` — the only mode, YAGNI guards the rest):
  unit_outcome  = outcome of the LAST chronologically computable trace.
                  Intermediate failures on an ultimately-green unit are
                  progress, not failure.
  unit_findings = UNION of findings across all traces in the group.
  unit_cost     = SUM of total_tokens + SUM of struggle (error_count proxy).
  unit_span     = earliest started_at .. latest ended_at across the group.

Traces with no correlation-key value (or when no key is configured) are
treated as "unattributed": each becomes its own unit, scored per-trace.

Public API
----------
rollup_units(traces, outcome_results, correlation_key)
    -> list[UnitOutcomeSummary]

Both the per-trace ``OutcomeResult`` list (from ``compute_outcome_rate``) and
the ``UnitOutcomeSummary`` list are kept alive side-by-side; neither replaces
the other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from kairos.log import get_logger

if TYPE_CHECKING:
    from datetime import datetime

    from kairos.analysis.outcome_metric import OutcomeResult
    from kairos.detection.models import Finding
    from kairos.models.trace import TraceEnvelope

logger = get_logger(__name__)

_UNATTRIBUTED: str = "unattributed"


@dataclass
class UnitOutcomeSummary:
    """Outcome rollup for one logical unit of work.

    A unit is identified by a ``unit_id`` which is either:
    - the correlation key *value* (e.g. the issue UUID), or
    - the ``trace_id`` of the single trace when no key is configured / the
      trace carries no key value (``correlation_key`` was ``None`` or the
      attribute was absent from all spans).

    Fields
    ------
    unit_id:
        Unique identifier for the unit (key value or trace_id).
    correlation_key_value:
        Raw value of the configured span attribute.  ``None`` when the unit
        is unattributed (no key value found).
    trace_ids:
        All trace IDs in this unit, chronological order.
    unit_outcome_pass:
        Outcome of the *last computable* trace (last-wins).  ``None`` when no
        trace in the unit is computable.
    unit_computable:
        True when at least one trace in the unit is computable.
    unit_findings:
        UNION of ``Finding`` objects across all traces in the unit.
    unit_total_tokens:
        SUM of ``TraceEnvelope.total_tokens`` across the unit.
    unit_struggle:
        SUM of ``TraceEnvelope.error_count`` across the unit (deterministic
        struggle proxy — same signal as the session-quality detector).
    unit_started_at:
        Earliest ``started_at`` timestamp across all traces (or ``None``).
    unit_ended_at:
        Latest ``ended_at`` timestamp across all traces (or ``None``).
    """

    unit_id: str
    correlation_key_value: str | None
    trace_ids: list[str]
    unit_outcome_pass: bool | None
    unit_computable: bool
    unit_findings: list[Finding] = field(default_factory=list)
    unit_total_tokens: int = 0
    unit_struggle: int = 0
    unit_started_at: datetime | None = None
    unit_ended_at: datetime | None = None


def rollup_units(
    traces: list[TraceEnvelope],
    outcome_results: list[OutcomeResult],
    findings_per_trace: dict[str, list[Finding]],
    *,
    correlation_key: str | None,
) -> list[UnitOutcomeSummary]:
    """Group traces into units and compute per-unit rollup.

    Parameters
    ----------
    traces:
        All envelopes being analysed.  Must include every trace whose
        ``OutcomeResult`` appears in ``outcome_results``.
    outcome_results:
        Per-trace outcome evaluation (from ``compute_outcome_rate`` /
        ``evaluate_outcome``).  One entry per trace in ``traces``.
    findings_per_trace:
        Map of ``trace_id -> [Finding, ...]`` from tier-1 detection.
    correlation_key:
        The span attribute name whose *value* groups traces into units.
        ``None`` → every trace is its own unit (backward-compatible).

    Returns
    -------
    list[UnitOutcomeSummary]
        One entry per unit, ordered by ``unit_started_at`` ascending (units
        with no timestamps sort last).

    Notes
    -----
    When ``correlation_key`` is ``None``, this function returns one
    ``UnitOutcomeSummary`` per trace whose ``unit_outcome_pass`` and
    ``unit_computable`` exactly mirror the per-trace ``OutcomeResult`` —
    byte-identical to the pre-Day-9 per-trace behaviour.
    """
    # Index outcome results by trace_id for O(1) lookup.
    result_by_id: dict[str, OutcomeResult] = {r.trace_id: r for r in outcome_results}

    # ── Group traces ────────────────────────────────────────────────────
    # key_value -> [envelope, ...] (insertion order = chronological if traces
    # are sorted chronologically; we sort explicitly below).
    groups: dict[str, list[TraceEnvelope]] = {}

    for env in traces:
        if correlation_key is None:
            # No grouping: each trace is its own unit, keyed by trace_id.
            key_val = env.trace_id
        else:
            key_val = env.correlation_key_value if env.correlation_key_value else _UNATTRIBUTED
            if key_val == _UNATTRIBUTED:
                # Unattributed traces each become their own unit (keyed by
                # trace_id so they don't merge with each other).
                key_val = f"{_UNATTRIBUTED}:{env.trace_id}"

        groups.setdefault(key_val, []).append(env)

    # ── Build UnitOutcomeSummary per group ───────────────────────────────
    summaries: list[UnitOutcomeSummary] = []

    for group_key, group_traces in groups.items():
        # Sort chronologically by started_at (None traces last).
        group_traces_sorted = sorted(
            group_traces,
            key=lambda e: (e.started_at is None, e.started_at),
        )

        trace_ids = [e.trace_id for e in group_traces_sorted]

        # Determine the display correlation_key_value.
        if correlation_key is None:
            # Each trace is its own unit — use trace_id as the unit_id.
            ckv: str | None = None
            unit_id = group_key  # == trace_id
        elif group_key.startswith(f"{_UNATTRIBUTED}:"):
            ckv = None
            unit_id = group_key  # "unattributed:<trace_id>"
        else:
            ckv = group_key
            unit_id = group_key

        # last-wins: find the last chronologically computable trace.
        last_computable_result: OutcomeResult | None = None
        for env in reversed(group_traces_sorted):
            r = result_by_id.get(env.trace_id)
            if r is not None and r.computable:
                last_computable_result = r
                break

        unit_computable = last_computable_result is not None
        unit_outcome_pass: bool | None = (
            last_computable_result.outcome_pass if last_computable_result is not None else None
        )

        # Union findings across the group.
        unit_findings: list[Finding] = []
        for tid in trace_ids:
            unit_findings.extend(findings_per_trace.get(tid, []))

        # Sum cost metrics.
        unit_total_tokens = sum(e.total_tokens for e in group_traces_sorted)
        unit_struggle = sum(e.error_count for e in group_traces_sorted)

        # Time span.
        started_ats = [e.started_at for e in group_traces_sorted if e.started_at is not None]
        ended_ats = [e.ended_at for e in group_traces_sorted if e.ended_at is not None]
        unit_started_at = min(started_ats) if started_ats else None
        unit_ended_at = max(ended_ats) if ended_ats else None

        summaries.append(
            UnitOutcomeSummary(
                unit_id=unit_id,
                correlation_key_value=ckv,
                trace_ids=trace_ids,
                unit_outcome_pass=unit_outcome_pass,
                unit_computable=unit_computable,
                unit_findings=unit_findings,
                unit_total_tokens=unit_total_tokens,
                unit_struggle=unit_struggle,
                unit_started_at=unit_started_at,
                unit_ended_at=unit_ended_at,
            )
        )

    # Sort by unit_started_at (None last), then unit_id for determinism.
    summaries.sort(key=lambda s: (s.unit_started_at is None, s.unit_started_at, s.unit_id))

    logger.info(
        "unit_outcome.rollup_complete",
        correlation_key=correlation_key,
        trace_count=len(traces),
        unit_count=len(summaries),
    )

    return summaries
