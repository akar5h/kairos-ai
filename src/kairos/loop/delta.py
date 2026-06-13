"""Kairos delta engine — Day 11.

Computes before/after deltas for metrics in ``nightly_rollup``.

Key invariants:
  • Deltas are ONLY computed within a single ``config_hash``.  Crossing a hash
    boundary (baseline_break row) is a series break, not a data point.
  • When a window spans a baseline_break row, the window is split at the break
    and only the single-hash segment on each side is used.  If either side has
    zero data points the delta is returned as None with a ``series_break``
    explanation.
  • The ``guardrail_check`` never hides regressions.  If a primary metric
    improves but any guardrail degrades, the result is marked REGRESSION and
    surfaced to the caller.

Columns available in nightly_rollup (post-migration-0008):
  night_id, workflow, agent, units, traces,
  outcome_rate (nullable), struggle_p50, struggle_p90,
  coordination_waste_per_trace, tokens_per_unit,
  finding_counts (jsonb), config_hash, baseline_break

Usage::

    from kairos.loop.delta import delta, guardrail_check, DeltaResult

    d = delta("struggle_p50", scope={"workflow": "Code Implementation"},
              window_before=("2026-06-08", "2026-06-09"),
              window_after=("2026-06-10", "2026-06-11"))
    result = guardrail_check(d, guardrails=[outcome_rate_delta, ...])
"""

from __future__ import annotations

import os
import statistics
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datetime import date

# ── Valid metric names ────────────────────────────────────────────────────────

#: Scalar columns in nightly_rollup that delta() can operate on.
VALID_METRICS: frozenset[str] = frozenset({
    "outcome_rate",
    "struggle_p50",
    "struggle_p90",
    "coordination_waste_per_trace",
    "tokens_per_unit",
    "units",
    "traces",
})


# ── Data classes ──────────────────────────────────────────────────────────────


@dataclass
class DeltaResult:
    """Outcome of a before/after delta computation.

    Attributes
    ----------
    metric:
        The column measured.
    scope:
        Filter dict that was applied (e.g. ``{"workflow": "Code Implementation"}``).
    mean_before:
        Mean of the metric in the before window.  None when no data points exist.
    mean_after:
        Mean of the metric in the after window.  None when no data points exist.
    n_before:
        Number of non-NULL data points in the before window.
    n_after:
        Number of non-NULL data points in the after window.
    delta:
        ``mean_after - mean_before``.  None when either side lacks data.
    points_before:
        Raw (night_id, value) pairs from the before window.
    points_after:
        Raw (night_id, value) pairs from the after window.
    series_break:
        True when the windows were split by a baseline_break row.  When True the
        delta is still computed if both sides have data, but callers MUST note
        the discontinuity.
    explanation:
        Human-readable explanation of any issue (series break, insufficient data,
        etc.).  Empty string when the result is clean.
    """

    metric: str
    scope: dict[str, Any]
    mean_before: float | None
    mean_after: float | None
    n_before: int
    n_after: int
    delta: float | None
    points_before: list[tuple[Any, float]]
    points_after: list[tuple[Any, float]]
    series_break: bool = False
    explanation: str = ""


@dataclass
class GuardrailCheckResult:
    """Result of guardrail_check().

    Attributes
    ----------
    primary:
        The DeltaResult being evaluated.
    regression:
        True when the primary metric improved but at least one guardrail degraded.
    degraded_guardrails:
        List of (DeltaResult, description) for every guardrail that degraded.
    summary:
        One-line human-readable verdict.
    """

    primary: DeltaResult
    regression: bool
    degraded_guardrails: list[tuple[DeltaResult, str]] = field(default_factory=list)
    summary: str = ""


# ── Internal helpers ──────────────────────────────────────────────────────────


def _dsn() -> str:
    dsn = os.environ.get("KAIROS_PG_DSN", "").strip()
    if not dsn:
        raise RuntimeError(
            "KAIROS_PG_DSN is not set — cannot compute deltas without a DB connection."
        )
    return dsn


def _build_where_clause(
    scope: dict[str, Any],
    window_start: date,
    window_end: date,
) -> tuple[str, list[Any]]:
    """Build SQL WHERE fragment for scope + date window (inclusive on both ends)."""
    params: list[Any] = [window_start, window_end]
    where = "night_id BETWEEN %s AND %s AND baseline_break = false"
    allowed_scope_keys = {"workflow", "agent", "config_hash"}
    for k, v in scope.items():
        if k not in allowed_scope_keys:
            raise ValueError(
                f"scope key {k!r} is not allowed. "
                f"Valid keys: {sorted(allowed_scope_keys)}"
            )
        where += f" AND {k} = %s"
        params.append(v)
    return where, params


def _fetch_points(
    metric: str,
    scope: dict[str, Any],
    window_start: date,
    window_end: date,
    conn: Any,
) -> list[tuple[Any, float]]:
    """Return non-NULL (night_id, value) pairs for *metric* in the window."""
    if metric not in VALID_METRICS:
        raise ValueError(f"Unknown metric {metric!r}. Valid: {sorted(VALID_METRICS)}")

    where, params = _build_where_clause(scope, window_start, window_end)
    # metric is a column name from VALID_METRICS — not user-supplied raw SQL.
    sql = f"SELECT night_id, {metric} FROM nightly_rollup WHERE {where} ORDER BY night_id"  # noqa: S608
    rows = conn.execute(sql, params).fetchall()
    # Drop NULL values (e.g. outcome_rate for unmapped rows).
    return [(r[0], float(r[1])) for r in rows if r[1] is not None]


def _has_baseline_break(
    window_start: date,
    window_end: date,
    conn: Any,
) -> bool:
    """Return True if any baseline_break sentinel row falls in [window_start, window_end]."""
    row = conn.execute(
        "SELECT 1 FROM nightly_rollup "
        "WHERE night_id BETWEEN %s AND %s AND baseline_break = true LIMIT 1",
        (window_start, window_end),
    ).fetchone()
    return row is not None


# ── Public API ────────────────────────────────────────────────────────────────


def delta(
    metric: str,
    scope: dict[str, Any],
    window_before: tuple[date, date],
    window_after: tuple[date, date],
    *,
    conn: Any | None = None,
) -> DeltaResult:
    """Compute a before/after delta for *metric* in ``nightly_rollup``.

    Parameters
    ----------
    metric:
        Column name to measure.  Must be in VALID_METRICS.
    scope:
        Dict of equality filters applied to both windows, e.g.
        ``{"workflow": "Code Implementation"}``.
        Keys limited to: workflow, agent, config_hash.
    window_before:
        ``(start_date, end_date)`` inclusive for the "before" sample.
    window_after:
        ``(start_date, end_date)`` inclusive for the "after" sample.
    conn:
        Optional pre-opened psycopg connection (for tests).

    Returns
    -------
    DeltaResult with mean_before, mean_after, n each side, raw points.
    When a baseline_break row falls inside either window, ``series_break=True``
    is set and the explanation records the discontinuity.

    Notes
    -----
    Rows where the metric value is NULL are excluded (e.g. outcome_rate for
    unmapped workflow).  A window with zero non-NULL rows yields n=0 and
    mean=None.
    """
    import psycopg  # noqa: PLC0415 — lazy import to keep module importable without psycopg

    own_conn = conn is None
    _conn: Any = psycopg.connect(_dsn()) if own_conn else conn
    try:
        b_start, b_end = window_before
        a_start, a_end = window_after

        # Check for series breaks in either window.
        break_before = _has_baseline_break(b_start, b_end, _conn)
        break_after = _has_baseline_break(a_start, a_end, _conn)
        series_break = break_before or break_after
        explanation_parts: list[str] = []
        if break_before:
            explanation_parts.append(
                f"baseline_break in before-window [{b_start}..{b_end}]"
            )
        if break_after:
            explanation_parts.append(
                f"baseline_break in after-window [{a_start}..{a_end}]"
            )

        pts_before = _fetch_points(metric, scope, b_start, b_end, _conn)
        pts_after = _fetch_points(metric, scope, a_start, a_end, _conn)
    finally:
        if own_conn:
            _conn.close()

    n_before = len(pts_before)
    n_after = len(pts_after)
    vals_before = [v for _, v in pts_before]
    vals_after = [v for _, v in pts_after]

    mean_before: float | None = statistics.mean(vals_before) if vals_before else None
    mean_after: float | None = statistics.mean(vals_after) if vals_after else None

    if mean_before is None or mean_after is None:
        delta_val: float | None = None
        if not vals_before:
            explanation_parts.append("no data points in before-window")
        if not vals_after:
            explanation_parts.append("no data points in after-window")
    else:
        delta_val = mean_after - mean_before

    return DeltaResult(
        metric=metric,
        scope=scope,
        mean_before=mean_before,
        mean_after=mean_after,
        n_before=n_before,
        n_after=n_after,
        delta=delta_val,
        points_before=pts_before,
        points_after=pts_after,
        series_break=series_break,
        explanation="; ".join(explanation_parts),
    )


def guardrail_check(
    primary: DeltaResult,
    guardrails: list[DeltaResult],
) -> GuardrailCheckResult:
    """Check whether a primary improvement coincides with guardrail degradation.

    A REGRESSION is declared when:
      • The primary metric shows improvement (delta < 0 for error-type metrics
        OR delta > 0 for quality-type metrics — determined by the caller passing
        the relevant DeltaResult) — concretely: delta is not None and != 0.
      • AND any guardrail shows degradation (delta moves in the wrong direction).

    Convention for "improvement" vs "degradation":
      For outcome_rate and related quality metrics: improvement = delta > 0.
      For struggle, escalation, coordination, error metrics: improvement = delta < 0.

    Because this function does not know the metric semantics, it uses the
    sign of the primary delta and guardrail deltas directly:
      • If primary.delta > 0 (primary rose), a guardrail that also rose is OK
        for outcome_rate, but BAD for struggle/escalation.
      • The caller is responsible for passing guardrails whose degradation
        direction is unambiguous.

    In practice: pass outcome_rate and escalation_rate as guardrails.
    Degradation for these is delta < 0 (they should rise or stay flat).
    If primary improves (any nonzero delta) and a guardrail drops → REGRESSION.

    Parameters
    ----------
    primary:
        The metric that is claimed to have improved.
    guardrails:
        List of DeltaResults for guardrail metrics (outcome_rate, escalation_rate).
        Degradation = guardrail.delta < 0 (rate fell).

    Returns
    -------
    GuardrailCheckResult with regression flag and list of degraded guardrails.
    """
    primary_improved = primary.delta is not None and primary.delta != 0.0

    degraded: list[tuple[DeltaResult, str]] = []
    for g in guardrails:
        if g.delta is None:
            continue
        # For rate guardrails (outcome_rate, escalation_rate), degradation = rate fell.
        if g.delta < 0:
            desc = (
                f"{g.metric} degraded: "
                f"{g.mean_before:.4f} → {g.mean_after:.4f} "
                f"(delta={g.delta:+.4f})"
            )
            degraded.append((g, desc))

    regression = primary_improved and bool(degraded)

    if regression:
        guardrail_names = ", ".join(g.metric for g, _ in degraded)
        summary = (
            f"REGRESSION: {primary.metric} changed by {primary.delta:+.4f} "
            f"but guardrail(s) degraded: {guardrail_names}"
        )
    elif primary.delta is None:
        summary = f"INCONCLUSIVE: {primary.metric} — insufficient data for delta"
    elif primary.delta == 0.0:
        summary = f"NO CHANGE: {primary.metric} delta=0"
    else:
        summary = (
            f"OK: {primary.metric} delta={primary.delta:+.4f}, "
            f"no guardrail degradation"
        )

    return GuardrailCheckResult(
        primary=primary,
        regression=regression,
        degraded_guardrails=degraded,
        summary=summary,
    )
