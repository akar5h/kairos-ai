"""Dashboard smoke tests — eval/dashboard/app.py (Day 11).

Tests:
  1. Dashboard builds figures from a fixture rollup without error
     (pure-unit, no DB, no Streamlit server).
  2. Dashboard boots headless and serves HTTP 200 (integration, requires
     KAIROS_PG_DSN + Streamlit installed).

The headless boot test launches Streamlit in a subprocess, polls until it
serves HTTP 200, then kills it.  Marked ``integration`` and guarded by
``KAIROS_PG_DSN`` availability.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
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


# ── Fixture rollup data (no DB required) ──────────────────────────────────────


def _make_fixture_rows() -> list[dict[str, Any]]:
    """Return a minimal set of nightly_rollup rows covering all dashboard paths."""
    return [
        {
            "night_id": date(2026, 6, 8),
            "workflow": "Code Implementation",
            "agent": "paperclip-claude-cto",
            "units": 3,
            "traces": 3,
            "outcome_rate": 1.0,
            "struggle_p50": 0.02,
            "struggle_p90": 0.05,
            "coordination_waste_per_trace": 0.33,
            "tokens_per_unit": 12000.0,
            "finding_counts": {"unrecovered_error": 4},
            "config_hash": "abc123def456aaa0",
            "baseline_break": False,
        },
        {
            "night_id": date(2026, 6, 9),
            "workflow": "Code Implementation",
            "agent": "paperclip-claude-cto",
            "units": 2,
            "traces": 2,
            "outcome_rate": 0.5,
            "struggle_p50": 0.04,
            "struggle_p90": 0.08,
            "coordination_waste_per_trace": 1.0,
            "tokens_per_unit": 11000.0,
            "finding_counts": {"unrecovered_error": 2, "struggle_ratio": 1},
            "config_hash": "abc123def456aaa0",
            "baseline_break": False,
        },
        {
            "night_id": date(2026, 6, 8),
            "workflow": "unmapped",
            "agent": "unknown",
            "units": 10,
            "traces": 10,
            "outcome_rate": None,  # Bug 1 fix: NULL for unmapped
            "struggle_p50": 0.0,
            "struggle_p90": 0.0,
            "coordination_waste_per_trace": 0.0,
            "tokens_per_unit": 500.0,
            "finding_counts": {},
            "config_hash": "abc123def456aaa0",
            "baseline_break": False,
        },
        {
            # baseline_break sentinel row.
            "night_id": date(2026, 6, 10),
            "workflow": "_config_change_",
            "agent": "_",
            "units": 0,
            "traces": 0,
            "outcome_rate": None,
            "struggle_p50": 0.0,
            "struggle_p90": 0.0,
            "coordination_waste_per_trace": 0.0,
            "tokens_per_unit": 0.0,
            "finding_counts": {},
            "config_hash": "bbb456aaa789ccc0",
            "baseline_break": True,
        },
    ]


# ── Pure-unit tests (no DB, no Streamlit server) ──────────────────────────────


class TestDashboardHelpers:
    """Test the dashboard's helper functions with fixture data."""

    def test_line_series_excludes_unmapped(self) -> None:
        """_line_series() excludes 'unmapped' rows from the series."""
        # Import the helpers by importing the module (not running main()).
        import importlib.util
        from pathlib import Path

        spec = importlib.util.spec_from_file_location(
            "dashboard_app",
            Path(__file__).parent.parent.parent / "eval" / "dashboard" / "app.py",
        )
        assert spec is not None
        mod = importlib.util.module_from_spec(spec)
        # The module calls st.set_page_config at import-time; mock streamlit.
        sys.modules.setdefault("streamlit", _MockStreamlit())  # type: ignore[arg-type]
        sys.modules.setdefault("pandas", _get_pandas())
        spec.loader.exec_module(mod)  # type: ignore[union-attr]

        rows = _make_fixture_rows()
        series = mod._line_series(rows, "outcome_rate", group_key="workflow")

        # "unmapped" must not appear in the series.
        assert "unmapped" not in series
        assert "_config_change_" not in series

    def test_line_series_excludes_null_values(self) -> None:
        """_line_series() drops rows where the metric is None."""
        import importlib.util
        from pathlib import Path

        spec = importlib.util.spec_from_file_location(
            "dashboard_app",
            Path(__file__).parent.parent.parent / "eval" / "dashboard" / "app.py",
        )
        assert spec is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules.setdefault("streamlit", _MockStreamlit())  # type: ignore[arg-type]
        sys.modules.setdefault("pandas", _get_pandas())
        spec.loader.exec_module(mod)  # type: ignore[union-attr]

        rows = _make_fixture_rows()
        # All fixture rows with non-null outcome_rate are Code Implementation.
        series = mod._line_series(rows, "outcome_rate", group_key="workflow")
        assert "Code Implementation" in series
        assert len(series["Code Implementation"]) == 2

    def test_baseline_break_dates_detected(self) -> None:
        """_baseline_break_dates() returns dates of baseline_break sentinel rows."""
        import importlib.util
        from pathlib import Path

        spec = importlib.util.spec_from_file_location(
            "dashboard_app2",
            Path(__file__).parent.parent.parent / "eval" / "dashboard" / "app.py",
        )
        assert spec is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules.setdefault("streamlit", _MockStreamlit())  # type: ignore[arg-type]
        sys.modules.setdefault("pandas", _get_pandas())
        spec.loader.exec_module(mod)  # type: ignore[union-attr]

        rows = _make_fixture_rows()
        breaks = mod._baseline_break_dates(rows)

        assert date(2026, 6, 10) in breaks


# ── Headless boot test (requires kairos-pg + streamlit) ──────────────────────


@_skip_no_db
@pytest.mark.integration
def test_dashboard_boots_headless() -> None:
    """Dashboard launches headless and serves HTTP 200 on the Streamlit port."""
    import urllib.error
    import urllib.request
    from pathlib import Path

    app_path = Path(__file__).parent.parent.parent / "eval" / "dashboard" / "app.py"
    assert app_path.exists(), f"Dashboard app not found at {app_path}"

    port = 8599  # use a non-default port to avoid collision with dev instance
    env = {**os.environ, "KAIROS_PG_DSN": _DSN}

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(app_path),
            "--server.port",
            str(port),
            "--server.headless",
            "true",
            "--browser.gatherUsageStats",
            "false",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    url = f"http://localhost:{port}/"
    timeout_s = 30
    deadline = time.monotonic() + timeout_s
    http_ok = False

    try:
        while time.monotonic() < deadline:
            try:
                resp = urllib.request.urlopen(url, timeout=2)
                if resp.status == 200:
                    http_ok = True
                    break
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(1)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    assert http_ok, (
        f"Dashboard did not serve HTTP 200 within {timeout_s}s at {url}. "
        "Check KAIROS_PG_DSN and that streamlit is installed."
    )


# ── Streamlit mock (for import-only tests) ────────────────────────────────────


class _MockStreamlit:
    """Minimal mock for streamlit that silently accepts all attribute access."""

    def __getattr__(self, name: str) -> Any:
        def _noop(*args: Any, **kwargs: Any) -> _MockStreamlit:
            return _MockStreamlit()

        return _noop

    def __call__(self, *args: Any, **kwargs: Any) -> _MockStreamlit:
        return _MockStreamlit()

    def __enter__(self) -> _MockStreamlit:
        return self

    def __exit__(self, *args: Any) -> None:
        pass


def _get_pandas() -> Any:
    try:
        import pandas  # noqa: PLC0415

        return pandas
    except ImportError:
        return None
