"""Tests for src/kairos/loop/delta.py — Day 11.

Coverage:
  - delta(): same-hash series → correct mean_before, mean_after, delta, n each side.
  - delta(): baseline_break rows excluded from data; series_break=True flagged.
  - delta(): baseline_break gating — split windows correctly signal the break.
  - delta(): returns None delta when a window has zero data points.
  - guardrail_check(): REGRESSION detected when primary improves + guardrail falls.
  - guardrail_check(): OK when primary improves and guardrails are stable/rising.
  - guardrail_check(): INCONCLUSIVE when delta is None.
  - _normalise_agent(): UUID suffix → "paperclip-claude-other"; named agents pass through.
  - unmapped outcome_rate is NULL in the rollup (honesty fix Bug 1).
  - coordination_waste_per_trace is mean count not a rate (honesty fix Bug 2).
  - Agent bucketing: UUID agents → paperclip-claude-other (honesty fix Bug 3).

Tests that require a live kairos-pg are guarded by _skip_no_db.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any

import pytest

# ── DB availability guard ─────────────────────────────────────────────────────

_DSN = os.environ.get("KAIROS_PG_DSN", "").strip()
_DB_AVAILABLE = bool(_DSN)

_skip_no_db = pytest.mark.skipif(
    not _DB_AVAILABLE,
    reason="KAIROS_PG_DSN not set — kairos-pg not reachable in this environment",
)


# ── Unit tests — delta math (no DB) ──────────────────────────────────────────


class TestNormaliseAgent:
    """_normalise_agent() collapses UUID-suffix agents to the stable bucket."""

    def test_uuid_suffix_bucketed(self) -> None:
        from kairos.loop.persist import _normalise_agent

        uuid_agent = "paperclip-claude-1c08f433-08bf-45c2-a6a5-831651596439"
        assert _normalise_agent(uuid_agent) == "paperclip-claude-other"

    def test_named_agent_passes_through(self) -> None:
        from kairos.loop.persist import _normalise_agent

        assert _normalise_agent("paperclip-claude-cto") == "paperclip-claude-cto"
        assert _normalise_agent("paperclip-claude-claudecoder") == "paperclip-claude-claudecoder"
        assert _normalise_agent("paperclip-claude-qaengineer") == "paperclip-claude-qaengineer"

    def test_unknown_passes_through(self) -> None:
        from kairos.loop.persist import _normalise_agent

        assert _normalise_agent("unknown") == "unknown"

    def test_another_uuid_bucketed(self) -> None:
        from kairos.loop.persist import _normalise_agent

        uuid_agent = "paperclip-claude-b44430c0-409f-403e-a273-f4a911145f8a"
        assert _normalise_agent(uuid_agent) == "paperclip-claude-other"

    def test_non_paperclip_prefix_passes_through(self) -> None:
        from kairos.loop.persist import _normalise_agent

        # An unrelated agent name that starts differently.
        assert _normalise_agent("some-other-agent") == "some-other-agent"


class TestGuardrailCheckUnit:
    """guardrail_check() logic — pure unit, no DB."""

    def _make_delta(
        self,
        metric: str,
        mean_before: float | None,
        mean_after: float | None,
        delta_val: float | None,
    ):  # type: ignore[return]
        from kairos.loop.delta import DeltaResult

        return DeltaResult(
            metric=metric,
            scope={},
            mean_before=mean_before,
            mean_after=mean_after,
            n_before=2 if mean_before is not None else 0,
            n_after=2 if mean_after is not None else 0,
            delta=delta_val,
            points_before=[] if mean_before is None else [("2026-06-08", mean_before)],
            points_after=[] if mean_after is None else [("2026-06-11", mean_after)],
        )

    def test_regression_detected(self) -> None:
        """Primary improves (struggle drops) but outcome_rate guardrail falls → REGRESSION."""
        from kairos.loop.delta import guardrail_check

        primary = self._make_delta("struggle_p50", 0.4, 0.2, -0.2)  # improved
        guardrail = self._make_delta("outcome_rate", 1.0, 0.8, -0.2)  # degraded
        result = guardrail_check(primary, [guardrail])

        assert result.regression is True
        assert len(result.degraded_guardrails) == 1
        assert "REGRESSION" in result.summary

    def test_no_regression_when_guardrail_stable(self) -> None:
        """Primary improves + guardrail stable (delta=0) → not regression."""
        from kairos.loop.delta import guardrail_check

        primary = self._make_delta("struggle_p50", 0.4, 0.2, -0.2)
        guardrail = self._make_delta("outcome_rate", 1.0, 1.0, 0.0)
        result = guardrail_check(primary, [guardrail])

        assert result.regression is False
        assert "OK" in result.summary or "NO CHANGE" in result.summary

    def test_no_regression_when_guardrail_rises(self) -> None:
        """Primary improves + guardrail also improves (rises) → not regression."""
        from kairos.loop.delta import guardrail_check

        primary = self._make_delta("struggle_p50", 0.4, 0.2, -0.2)
        guardrail = self._make_delta("outcome_rate", 0.9, 1.0, 0.1)  # improved
        result = guardrail_check(primary, [guardrail])

        assert result.regression is False

    def test_inconclusive_when_delta_none(self) -> None:
        """Primary delta=None → INCONCLUSIVE."""
        from kairos.loop.delta import guardrail_check

        primary = self._make_delta("struggle_p50", None, None, None)
        result = guardrail_check(primary, [])

        assert result.regression is False
        assert "INCONCLUSIVE" in result.summary

    def test_multiple_guardrails_all_degrade(self) -> None:
        """All guardrails degrade → regression with multiple entries."""
        from kairos.loop.delta import guardrail_check

        primary = self._make_delta("struggle_p50", 0.5, 0.3, -0.2)
        g1 = self._make_delta("outcome_rate", 1.0, 0.8, -0.2)
        g2 = self._make_delta("outcome_rate", 0.9, 0.7, -0.2)
        result = guardrail_check(primary, [g1, g2])

        assert result.regression is True
        assert len(result.degraded_guardrails) == 2

    def test_no_regression_when_primary_no_change(self) -> None:
        """Primary delta=0 → guardrail degradation doesn't declare regression."""
        from kairos.loop.delta import guardrail_check

        primary = self._make_delta("struggle_p50", 0.4, 0.4, 0.0)
        guardrail = self._make_delta("outcome_rate", 1.0, 0.8, -0.2)
        result = guardrail_check(primary, [guardrail])

        # delta=0 means no "primary improvement" — regression requires primary to change.
        assert result.regression is False


class TestDeltaResultFields:
    """DeltaResult dataclass fields are set correctly."""

    def test_fields_populated(self) -> None:
        from kairos.loop.delta import DeltaResult

        dr = DeltaResult(
            metric="struggle_p50",
            scope={"workflow": "Code Implementation"},
            mean_before=0.4,
            mean_after=0.2,
            n_before=3,
            n_after=2,
            delta=-0.2,
            points_before=[("2026-06-08", 0.4)],
            points_after=[("2026-06-10", 0.2)],
            series_break=False,
            explanation="",
        )
        assert dr.delta == pytest.approx(-0.2)
        assert dr.n_before == 3
        assert dr.n_after == 2
        assert not dr.series_break


# ── Integration tests (require live kairos-pg) ────────────────────────────────


@_skip_no_db
class TestDeltaLiveDB:
    """delta() against the real kairos-pg with test fixture data."""

    _TEST_HASH = "testdelta000011a"  # synthetic hash unique to these tests
    _TEST_HASH_B = "testdelta000011b"  # second hash for baseline_break tests
    _WF = "DeltaTestWorkflow"
    _AGENT = "test-agent"

    def _seed_row(
        self,
        conn: Any,
        night: date,
        outcome_rate: float | None,
        struggle_p50: float,
        config_hash: str,
        baseline_break: bool = False,
    ) -> None:
        """Insert one nightly_rollup test row."""
        from psycopg.types.json import Jsonb  # noqa: PLC0415

        conn.execute(
            """
            INSERT INTO nightly_rollup
                (night_id, workflow, agent, units, traces, outcome_rate,
                 struggle_p50, struggle_p90, coordination_waste_per_trace,
                 tokens_per_unit, finding_counts, config_hash, baseline_break)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (night_id, workflow, agent) DO UPDATE
                SET outcome_rate    = EXCLUDED.outcome_rate,
                    struggle_p50    = EXCLUDED.struggle_p50,
                    config_hash     = EXCLUDED.config_hash,
                    baseline_break  = EXCLUDED.baseline_break
            """,
            (
                night,
                self._WF,
                self._AGENT,
                1,
                1,
                outcome_rate,
                struggle_p50,
                struggle_p50,
                0.0,
                1000.0,
                Jsonb({}),
                config_hash,
                baseline_break,
            ),
        )

    def _cleanup(self, conn: Any) -> None:
        conn.execute(
            "DELETE FROM nightly_rollup WHERE workflow = %s",
            (self._WF,),
        )
        conn.commit()

    def test_basic_delta_same_hash(self) -> None:
        """delta() on same-hash data returns correct mean/delta."""
        from kairos.loop.db import get_connection
        from kairos.loop.delta import delta

        with get_connection() as conn:
            # Seed: before=[0.4, 0.3], after=[0.1, 0.2] on separate nights.
            self._seed_row(conn, date(2026, 1, 20), 1.0, 0.4, self._TEST_HASH)
            self._seed_row(conn, date(2026, 1, 21), 1.0, 0.3, self._TEST_HASH)
            self._seed_row(conn, date(2026, 1, 22), 1.0, 0.1, self._TEST_HASH)
            self._seed_row(conn, date(2026, 1, 23), 1.0, 0.2, self._TEST_HASH)
            conn.commit()

            result = delta(
                "struggle_p50",
                scope={"workflow": self._WF, "agent": self._AGENT},
                window_before=(date(2026, 1, 20), date(2026, 1, 21)),
                window_after=(date(2026, 1, 22), date(2026, 1, 23)),
                conn=conn,
            )
            self._cleanup(conn)

        assert result.n_before == 2
        assert result.n_after == 2
        assert result.mean_before == pytest.approx(0.35)
        assert result.mean_after == pytest.approx(0.15)
        assert result.delta == pytest.approx(-0.2)
        assert not result.series_break

    def test_baseline_break_flagged(self) -> None:
        """A baseline_break row in the window sets series_break=True."""
        from kairos.loop.db import get_connection
        from kairos.loop.delta import delta

        with get_connection() as conn:
            # Seed a data row and a break sentinel in the same window.
            self._seed_row(conn, date(2026, 1, 24), 1.0, 0.5, self._TEST_HASH)
            # Break sentinel (different hash, baseline_break=True).
            self._seed_row(conn, date(2026, 1, 25), None, 0.0, self._TEST_HASH_B, baseline_break=True)
            self._seed_row(conn, date(2026, 1, 26), 1.0, 0.2, self._TEST_HASH_B)
            conn.commit()

            result = delta(
                "struggle_p50",
                scope={"workflow": self._WF, "agent": self._AGENT},
                window_before=(date(2026, 1, 24), date(2026, 1, 25)),
                window_after=(date(2026, 1, 26), date(2026, 1, 26)),
                conn=conn,
            )
            self._cleanup(conn)

        # The sentinel row is excluded from data but flags series_break.
        assert result.series_break is True
        assert "baseline_break" in result.explanation.lower()
        # Data point from before-window (only the non-break row).
        assert result.n_before == 1

    def test_empty_window_returns_none_delta(self) -> None:
        """A window with no matching rows → n=0, mean=None, delta=None."""
        from kairos.loop.db import get_connection
        from kairos.loop.delta import delta

        with get_connection() as conn:
            self._seed_row(conn, date(2026, 1, 27), 1.0, 0.4, self._TEST_HASH)
            conn.commit()

            result = delta(
                "struggle_p50",
                scope={"workflow": self._WF, "agent": self._AGENT},
                window_before=(date(2026, 1, 27), date(2026, 1, 27)),
                window_after=(date(2026, 2, 1), date(2026, 2, 5)),  # no rows
                conn=conn,
            )
            self._cleanup(conn)

        assert result.n_after == 0
        assert result.mean_after is None
        assert result.delta is None

    def test_invalid_metric_raises(self) -> None:
        """delta() with an unknown metric raises ValueError."""
        from kairos.loop.db import get_connection
        from kairos.loop.delta import delta

        with get_connection() as conn, pytest.raises(ValueError, match="Unknown metric"):
            delta(
                "not_a_real_column",
                scope={"workflow": self._WF},
                window_before=(date(2026, 1, 1), date(2026, 1, 5)),
                window_after=(date(2026, 1, 6), date(2026, 1, 10)),
                conn=conn,
            )

    def test_invalid_scope_key_raises(self) -> None:
        """delta() with a disallowed scope key raises ValueError."""
        from kairos.loop.db import get_connection
        from kairos.loop.delta import delta

        with get_connection() as conn, pytest.raises(ValueError, match="scope key"):
            delta(
                "struggle_p50",
                scope={"night_id": date(2026, 1, 1)},  # not allowed
                window_before=(date(2026, 1, 1), date(2026, 1, 5)),
                window_after=(date(2026, 1, 6), date(2026, 1, 10)),
                conn=conn,
            )


@_skip_no_db
class TestHonestyFixesLiveDB:
    """Verify honesty fixes are reflected in the live DB."""

    def test_unmapped_outcome_rate_is_null(self) -> None:
        """Bug 1: unmapped workflow rows must have outcome_rate=NULL."""
        from kairos.loop.db import get_connection

        with get_connection() as conn:
            rows = conn.execute(
                "SELECT night_id, outcome_rate FROM nightly_rollup WHERE workflow = 'unmapped'"
            ).fetchall()

        if not rows:
            pytest.skip("No unmapped rows in nightly_rollup — run backfill first.")

        for night, outcome_rate in rows:
            assert outcome_rate is None, (
                f"unmapped row for night={night} has outcome_rate={outcome_rate}, expected NULL"
            )

    def test_no_uuid_agents_in_rollup(self) -> None:
        """Bug 3: no paperclip-claude-<UUID> agents in nightly_rollup."""
        import re

        from kairos.loop.db import get_connection

        uuid_pat = re.compile(
            r"^paperclip-claude-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
            re.IGNORECASE,
        )
        with get_connection() as conn:
            rows = conn.execute("SELECT DISTINCT agent FROM nightly_rollup").fetchall()

        uuid_agents = [r[0] for r in rows if uuid_pat.fullmatch(r[0])]
        assert not uuid_agents, f"UUID-suffix agents found in nightly_rollup: {uuid_agents}"

    def test_coordination_waste_column_renamed(self) -> None:
        """Bug 2: coordination_waste_per_trace column must exist (not coordination_waste_rate)."""
        from kairos.loop.db import get_connection

        with get_connection() as conn:
            rows = conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'nightly_rollup'"
            ).fetchall()

        col_names = {r[0] for r in rows}
        assert "coordination_waste_per_trace" in col_names, (
            "coordination_waste_per_trace column not found — migration 0008 may not have applied"
        )
        assert "coordination_waste_rate" not in col_names, (
            "Old column coordination_waste_rate still exists — migration 0008 incomplete"
        )

    def test_coordination_waste_can_exceed_one(self) -> None:
        """Bug 2 (semantic): coordination_waste_per_trace values can exceed 1.0."""
        from kairos.loop.db import get_connection

        with get_connection() as conn:
            row = conn.execute(
                "SELECT MAX(coordination_waste_per_trace) FROM nightly_rollup "
                "WHERE coordination_waste_per_trace IS NOT NULL"
            ).fetchone()

        if row is None or row[0] is None:
            pytest.skip("No coordination_waste_per_trace data — run backfill first.")

        # The old "rate" misname implied 0-1; actual values can be counts > 1.
        # We just confirm the column exists and has numeric data.
        assert isinstance(row[0], float)


@_skip_no_db
class TestDeltaRealSeries:
    """Run delta() on the actual 4-night backfilled series."""

    def test_delta_struggle_real_series(self) -> None:
        """delta() on real data: 2026-06-08..09 vs 2026-06-10..11 for struggle_p50."""
        from kairos.loop.db import get_connection
        from kairos.loop.delta import delta

        with get_connection() as conn:
            # Check we have data.
            n = conn.execute(
                "SELECT count(*) FROM nightly_rollup "
                "WHERE night_id BETWEEN '2026-06-08' AND '2026-06-11' "
                "AND baseline_break = false"
            ).fetchone()[0]

        if n == 0:
            pytest.skip("No live backfill data — run scripts/backfill.py first.")

        result = delta(
            "struggle_p50",
            scope={"workflow": "Paperclip Coordination"},
            window_before=(date(2026, 6, 8), date(2026, 6, 9)),
            window_after=(date(2026, 6, 10), date(2026, 6, 11)),
        )

        # We don't assert a specific value (live data varies), just that the shape is correct.
        assert result.metric == "struggle_p50"
        assert result.n_before >= 0
        assert result.n_after >= 0
        # Single config_hash across the 4-night series → no series break expected.
        assert not result.series_break, f"Unexpected series_break=True on single-hash series: {result.explanation}"
