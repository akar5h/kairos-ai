"""eval/dashboard/app.py — Kairos nightly-rollup dashboard (Day 11).

Minimal one-page Streamlit dashboard.  Reads ``nightly_rollup`` from
``kairos-pg`` via ``KAIROS_PG_DSN``.  Read-only — no write actions, no auth.

Launch::

    uv run streamlit run eval/dashboard/app.py

Honest empty-states: if a metric is NULL or sparse for a given series, a note
is displayed rather than plotting 0 for "no data".  The unmapped workflow is
excluded from outcome_rate plots (its outcome_rate is NULL — no contract).
baseline_break rows are rendered as visible vertical rules on all line charts.
"""

from __future__ import annotations

import os
from collections import defaultdict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datetime import date

import streamlit as st

# ── Page config ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Kairos Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── DB loading ─────────────────────────────────────────────────────────────────


@st.cache_data(ttl=300)  # refresh every 5 minutes
def load_rollup() -> list[dict[str, Any]]:
    """Load all nightly_rollup rows from kairos-pg.

    Returns a list of dicts with keys matching the column names.
    Returns an empty list (not an error) when KAIROS_PG_DSN is unset so the
    dashboard can show an honest empty-state instead of crashing.
    """
    dsn = os.environ.get("KAIROS_PG_DSN", "").strip()
    if not dsn:
        return []

    try:
        import psycopg  # noqa: PLC0415
        from psycopg.rows import dict_row  # noqa: PLC0415

        with psycopg.connect(dsn, row_factory=dict_row) as conn:
            rows: list[dict[str, Any]] = conn.execute(
                """
                SELECT night_id, workflow, agent, units, traces,
                       outcome_rate, struggle_p50, struggle_p90,
                       coordination_waste_per_trace, tokens_per_unit,
                       finding_counts, config_hash, baseline_break
                FROM nightly_rollup
                ORDER BY night_id, workflow, agent
                """
            ).fetchall()
        return list(rows)
    except Exception as exc:  # noqa: BLE001
        st.error(f"DB error loading nightly_rollup: {exc}")
        return []


# ── Helper: build a simple line-chart dict (date → value) ────────────────────


def _line_series(
    rows: list[dict[str, Any]],
    metric: str,
    group_key: str | None = None,
    exclude_workflows: frozenset[str] = frozenset({"unmapped", "_config_change_"}),
) -> dict[str, list[tuple[date, float]]]:
    """Group rows into {label: [(night_id, value), ...]} for line plotting.

    Rows where the metric value is None are excluded (honest empty-state).
    baseline_break rows are excluded from the series data (they appear as rules).
    """
    series: dict[str, list[tuple[date, float]]] = defaultdict(list)
    for row in rows:
        if row.get("baseline_break"):
            continue
        if row["workflow"] in exclude_workflows:
            continue
        val = row.get(metric)
        if val is None:
            continue
        label = str(row[group_key]) if group_key else "all"
        series[label].append((row["night_id"], float(val)))
    # Sort each series by date.
    for k in series:
        series[k].sort(key=lambda x: x[0])
    return dict(series)


def _baseline_break_dates(rows: list[dict[str, Any]]) -> list[date]:
    """Return dates where a baseline_break sentinel row exists."""
    return sorted({row["night_id"] for row in rows if row.get("baseline_break")})


# ── Chart renderers ───────────────────────────────────────────────────────────


def _render_line_chart(
    series: dict[str, list[tuple[date, float]]],
    title: str,
    y_label: str,
    break_dates: list[date],
    note_null: str = "",
) -> None:
    """Render a line chart using Streamlit's native line_chart.

    Shows an honest empty-state note when the series is empty or all-null.
    Annotates baseline_break dates as a text note (Streamlit native charts don't
    support vertical rules; the break dates are explicitly listed).
    """
    if not series:
        st.markdown(f"**{title}**")
        st.info(note_null if note_null else "No data available for this metric.")
        return

    # Build a flat dict: {date_str: {label: value}} for Streamlit's line_chart.
    all_dates: list[date] = sorted(
        {d for pts in series.values() for d, _ in pts}
    )
    chart_data: dict[str, dict[str, float | None]] = {
        str(d): {label: None for label in series} for d in all_dates
    }
    for label, pts in series.items():
        for d, v in pts:
            chart_data[str(d)][label] = v

    import pandas as pd  # noqa: PLC0415

    df = pd.DataFrame.from_dict(chart_data, orient="index").sort_index()
    df.index.name = "night"

    st.markdown(f"**{title}**")
    if break_dates:
        break_str = ", ".join(str(d) for d in break_dates)
        st.caption(
            f"Config-change discontinuity on: {break_str} "
            "(series break — delta engine ignores cross-hash comparisons)"
        )
    st.line_chart(df, use_container_width=True)
    if y_label:
        st.caption(y_label)


def _render_bar_chart(
    data: dict[str, float],
    title: str,
    x_label: str = "",
    y_label: str = "",
) -> None:
    import pandas as pd  # noqa: PLC0415

    if not data:
        st.markdown(f"**{title}**")
        st.info("No data available.")
        return

    st.markdown(f"**{title}**")
    df = pd.DataFrame({"label": list(data.keys()), "value": list(data.values())})
    df = df.sort_values("value", ascending=False)
    st.bar_chart(df.set_index("label"), use_container_width=True)
    if y_label:
        st.caption(y_label)


# ── Main dashboard ─────────────────────────────────────────────────────────────


def main() -> None:
    # ── Sidebar ───────────────────────────────────────────────────────────────
    with st.sidebar:
        st.markdown("## Kairos Dashboard")
        st.markdown("*Day 11 — Delta engine + nightly rollup*")
        st.divider()

        rows_all = load_rollup()

        if not rows_all:
            st.error(
                "No data loaded.  "
                "Set KAIROS_PG_DSN and ensure kairos-pg is running."
            )
            # Don't st.stop() — render the rest of the page with empty states.
            rows_all = []

        # Config-hash timeline.
        st.markdown("### Config-hash timeline")
        hash_by_night: dict[str, set[str]] = defaultdict(set)
        for row in rows_all:
            if not row.get("baseline_break") and row["workflow"] != "_config_change_":
                hash_by_night[str(row["night_id"])].add(row["config_hash"])
        if hash_by_night:
            for night in sorted(hash_by_night):
                hashes = ", ".join(sorted(hash_by_night[night]))
                st.markdown(f"`{night}` → `{hashes[:12]}…`")
        else:
            st.info("No config hashes found.")

        st.divider()
        st.markdown("### Granularity note")
        st.markdown(
            "All metrics are **unit-of-work** granularity (grouped by "
            "`paperclip.issue` correlation key) except where noted.  "
            "Each row in `nightly_rollup` is one (night, workflow, agent) cell."
        )
        st.divider()
        st.markdown("### Honest empty-state policy")
        st.markdown(
            "- `outcome_rate` is **NULL** for the *unmapped* workflow "
            "(no pass/fail contract — excluded from all outcome plots).\n"
            "- `coordination_waste_per_trace` is the **mean count** of "
            "coordination_waste findings per trace — not a 0–1 rate; values >1 "
            "are expected.\n"
            "- UUID agent IDs are bucketed to `paperclip-claude-other` (instance "
            "IDs, not class names — cannot be mapped deterministically)."
        )

    # Filter non-data rows for most charts.
    rows_data = [
        r for r in rows_all
        if not r.get("baseline_break") and r["workflow"] not in ("unmapped", "_config_change_")
    ]
    break_dates = _baseline_break_dates(rows_all)

    st.title("Kairos Nightly Rollup Dashboard")

    # ── Section 1: Outcome rate per workflow ─────────────────────────────────
    st.header("1  Outcome Rate by Workflow")
    st.caption(
        "outcome_rate = fraction of computable units that passed.  "
        "The *unmapped* workflow is excluded (NULL outcome_rate — no contract)."
    )
    outcome_series = _line_series(rows_data, "outcome_rate", group_key="workflow")
    _render_line_chart(
        outcome_series,
        title="Outcome rate per workflow over time",
        y_label="outcome_rate (0–1; NULL rows excluded)",
        break_dates=break_dates,
        note_null="No outcome_rate data found (all rows may be unmapped or NULL).",
    )

    # ── Section 2: Struggle + coordination by agent ───────────────────────────
    st.header("2  Struggle & Coordination by Agent")
    col1, col2 = st.columns(2)

    with col1:
        struggle_p50_series = _line_series(rows_data, "struggle_p50", group_key="agent")
        _render_line_chart(
            struggle_p50_series,
            title="struggle_p50 by agent",
            y_label="struggle p50 (errors / max(1, side-effect successes))",
            break_dates=break_dates,
        )

    with col2:
        struggle_p90_series = _line_series(rows_data, "struggle_p90", group_key="agent")
        _render_line_chart(
            struggle_p90_series,
            title="struggle_p90 by agent",
            y_label="struggle p90",
            break_dates=break_dates,
        )

    coord_series = _line_series(rows_data, "coordination_waste_per_trace", group_key="agent")
    _render_line_chart(
        coord_series,
        title="coordination_waste_per_trace by agent",
        y_label=(
            "mean coordination_waste findings per trace in cell.  "
            "Values >1 mean multiple coordination events per trace on average."
        ),
        break_dates=break_dates,
    )

    # ── Section 3: Kairos detection curve (thesis view) ──────────────────────
    st.header("3  Kairos Detection Curve (Self-Improvement Signal)")
    st.caption(
        "These charts show how many failure classes Kairos is covering and "
        "how finding volume per detector evolves.  Rising coverage + stable "
        "finding counts per detector signals the flywheel is turning."
    )

    # Compute: per night, how many distinct detector types had ≥1 finding.
    detector_classes_by_night: dict[str, int] = {}
    findings_by_detector_night: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for row in rows_data:
        night = str(row["night_id"])
        fc: dict[str, int] = row.get("finding_counts") or {}
        for det, cnt in fc.items():
            findings_by_detector_night[night][det] += cnt

    for night, det_map in sorted(findings_by_detector_night.items()):
        detector_classes_by_night[night] = len(det_map)

    # Number of failure classes covered by night.
    col3, col4 = st.columns(2)
    with col3:
        if detector_classes_by_night:
            import pandas as pd  # noqa: PLC0415

            df_cov = pd.DataFrame(
                {"night": list(detector_classes_by_night.keys()),
                 "classes_covered": list(detector_classes_by_night.values())}
            ).set_index("night").sort_index()
            st.markdown("**Failure classes covered per night**")
            st.line_chart(df_cov, use_container_width=True)
            st.caption(
                "Count of distinct detector types that fired at least once that night.  "
                "Increases when a new detector is deployed and captures real signals."
            )
        else:
            st.info("No finding_counts data found.")

    # Finding volume by detector (totals across all nights).
    with col4:
        total_by_detector: dict[str, int] = defaultdict(int)
        for det_map in findings_by_detector_night.values():
            for det, cnt in det_map.items():
                total_by_detector[det] += cnt

        _render_bar_chart(
            dict(total_by_detector),
            title="Total findings by detector (all nights)",
            y_label="total count",
        )

    # Findings-by-detector over time (stacked view per night).
    st.markdown("**Findings per detector by night**")
    if findings_by_detector_night:
        import pandas as pd  # noqa: PLC0415

        all_nights = sorted(findings_by_detector_night.keys())
        all_detectors = sorted(
            {d for dm in findings_by_detector_night.values() for d in dm}
        )
        det_night_data = {
            det: [findings_by_detector_night[n].get(det, 0) for n in all_nights]
            for det in all_detectors
        }
        df_det = pd.DataFrame(det_night_data, index=all_nights)
        df_det.index.name = "night"
        st.line_chart(df_det, use_container_width=True)
        st.caption(
            "Count of findings per detector per night.  "
            "A new detector appearing in later nights = flywheel discovery firing."
        )
    else:
        st.info("No finding_counts data found.")

    # ── Section 4: Tokens trend ───────────────────────────────────────────────
    st.header("4  Tokens per Unit")
    tokens_series = _line_series(rows_data, "tokens_per_unit", group_key="workflow")
    _render_line_chart(
        tokens_series,
        title="tokens_per_unit by workflow over time",
        y_label="mean tokens consumed per unit-of-work",
        break_dates=break_dates,
    )

    # ── Section 5: Rollup table ───────────────────────────────────────────────
    st.header("5  Raw Nightly Rollup (latest night)")
    if rows_all:
        latest_night = max(
            r["night_id"] for r in rows_all if not r.get("baseline_break")
        )
        latest_rows = [
            r for r in rows_all
            if r["night_id"] == latest_night and not r.get("baseline_break")
        ]
        if latest_rows:
            import pandas as pd  # noqa: PLC0415

            display_rows = []
            for r in latest_rows:
                fc: dict[str, int] = r.get("finding_counts") or {}
                display_rows.append({
                    "workflow": r["workflow"],
                    "agent": r["agent"],
                    "units": r["units"],
                    "traces": r["traces"],
                    "outcome_rate": r["outcome_rate"],
                    "struggle_p50": r.get("struggle_p50"),
                    "coordination_waste_per_trace": r.get("coordination_waste_per_trace"),
                    "tokens_per_unit": r.get("tokens_per_unit"),
                    "finding_counts": str(fc) if fc else "{}",
                })
            df_table = pd.DataFrame(display_rows)
            st.dataframe(df_table, use_container_width=True)
            st.caption(
                f"Night: {latest_night}.  "
                "outcome_rate=None for unmapped (no contract).  "
                "coordination_waste_per_trace is avg finding count per trace."
            )
        else:
            st.info("No data rows for the latest night.")
    else:
        st.info("No rollup data loaded.")


if __name__ == "__main__":
    main()
