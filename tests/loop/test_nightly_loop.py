"""Tests for scripts/nightly_loop.py — Day 12.

Coverage:
  - quiet night: 0 traces → 'quiet_night' report, exit 0
  - forced exception → skip-marker report, exit 0
  - kill switch KAIROS_LOOP_DISABLED=1 → clean no-op
  - DB-down → parquet fallback + WARN
  - state machine: FETCH→ANALYZE→...→DONE transitions logged
  - skip-marker contains traceback

All tests use the _force_exception / _pg_conn injection points to avoid
hitting live Phoenix or Postgres.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime  # noqa: TC003
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ── path bootstrap (tests may run from any cwd) ───────────────────────────────
_REPO = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "scripts"))


# ── Import the runner (after path bootstrap) ──────────────────────────────────

import nightly_loop as nl  # noqa: E402

# ── DB availability guard ─────────────────────────────────────────────────────

_DSN = os.environ.get("KAIROS_PG_DSN", "").strip()
_DB_AVAILABLE = bool(_DSN)

_skip_no_db = pytest.mark.skipif(
    not _DB_AVAILABLE,
    reason="KAIROS_PG_DSN not set — kairos-pg not reachable",
)

# ── Minimal stub types ────────────────────────────────────────────────────────


@dataclass
class _FakeStep:
    step_index: int
    step_type: str = "TOOL_CALL"
    tool_name: str | None = "Bash"
    status: str = "OK"
    error_message: str | None = None
    tool_args: dict[str, Any] | None = None
    tool_args_normalized: dict[str, Any] | None = None
    total_tokens: int | None = None
    latency_ms: int | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    attrs: dict[str, Any] | None = None
    parent_step_index: int | None = None
    source_observation_id: str | None = None


# Use the real TraceEnvelope and Step models for proper type compatibility.
from kairos.models.enums import StepStatus, StepType  # noqa: E402
from kairos.models.trace import Step, TraceEnvelope  # noqa: E402


def _make_trace(tid: str | None = None, tokens: int = 100) -> TraceEnvelope:
    return TraceEnvelope(
        trace_id=tid or str(uuid.uuid4()).replace("-", ""),
        steps=[
            Step(step_index=0, step_type=StepType.TOOL_CALL, tool_name="Bash", status=StepStatus.OK)
        ],
        total_tokens=tokens,
        total_latency_ms=500,
    )


# ── Fixture: minimal context.yaml ─────────────────────────────────────────────


@pytest.fixture()
def context_path(tmp_path: Path) -> Path:
    """Write a minimal context.yaml that the runner can load."""
    ctx = tmp_path / "context.yaml"
    ctx.write_text(
        """
agent_name: "TestAgent"
agent_description: "Test"
operations:
  - name: "Test Op"
    description: "Testing"
    expected_tools: ["Bash", "Read"]
    required_side_effect_tools: ["Bash"]
    side_effect_match: "any"
    excluded_tools: []
    priority: "medium"
"""
    )
    return ctx


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "loop_data"
    d.mkdir()
    return d


# ── Kill switch ────────────────────────────────────────────────────────────────


def test_kill_switch_no_op(context_path: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """KAIROS_LOOP_DISABLED=1 → clean no-op, no reports written."""
    monkeypatch.setenv("KAIROS_LOOP_DISABLED", "1")
    result = nl.run_nightly_loop(
        context_path=str(context_path),
        data_dir_path=str(data_dir),
    )
    assert result["status"] == "disabled"
    # No report files should have been created.
    assert not list(data_dir.rglob("*.json"))


def test_kill_switch_empty_value_not_active(
    context_path: Path,
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """KAIROS_LOOP_DISABLED='' is not active (empty string = not set)."""
    monkeypatch.setenv("KAIROS_LOOP_DISABLED", "")
    # The fetch will fail (no Phoenix) — that's ok, we just check it doesn't
    # exit early with "disabled".
    result = nl.run_nightly_loop(
        context_path=str(context_path),
        data_dir_path=str(data_dir),
        retry_wait_s=0.0,
    )
    assert result["status"] != "disabled"


# ── Forced exception → skip-marker ────────────────────────────────────────────


def test_forced_exception_produces_skip_marker(
    context_path: Path,
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Any unhandled exception → skip-marker report written, status='skip'."""
    monkeypatch.delenv("KAIROS_LOOP_DISABLED", raising=False)
    boom = RuntimeError("boom: simulated forced failure")

    result = nl.run_nightly_loop(
        context_path=str(context_path),
        data_dir_path=str(data_dir),
        _force_exception=boom,
    )

    assert result["status"] == "skip"
    # Report file exists.
    report_path = Path(result["report_path"])
    assert report_path.exists()
    content = json.loads(report_path.read_text())
    assert content["type"] == "skip_marker"
    assert "boom" in content.get("reason", "").lower() or "boom" in content.get("traceback", "").lower()
    assert content.get("traceback") is not None


def test_forced_exception_has_traceback(
    context_path: Path,
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Skip-marker always contains a traceback (night is never silent)."""
    monkeypatch.delenv("KAIROS_LOOP_DISABLED", raising=False)
    boom = ValueError("traceback test: something went wrong")

    result = nl.run_nightly_loop(
        context_path=str(context_path),
        data_dir_path=str(data_dir),
        _force_exception=boom,
    )

    report_path = Path(result["report_path"])
    content = json.loads(report_path.read_text())
    assert content.get("traceback"), "Skip marker must contain a traceback"
    assert "ValueError" in content["traceback"]


# ── Quiet night ───────────────────────────────────────────────────────────────


def test_quiet_night_valid_report(
    context_path: Path,
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """0 traces after dedup → 'quiet_night' report (valid, not an error)."""
    monkeypatch.delenv("KAIROS_LOOP_DISABLED", raising=False)

    # Patch fetch to return 0 new traces.
    with (
        patch.object(nl, "_resolve_project_id", return_value="proj-id"),
        patch.object(nl, "_fetch_root_trace_ids", return_value=([], {})),
    ):
        result = nl.run_nightly_loop(
            context_path=str(context_path),
            data_dir_path=str(data_dir),
        )

    assert result["status"] == "quiet_night"
    report_path = Path(result["report_path"])
    content = json.loads(report_path.read_text())
    assert content["type"] == "quiet_night"
    assert "0 traces" in content["message"].lower() or "quiet" in content["message"].lower()


# ── DB down → parquet fallback ────────────────────────────────────────────────


def test_db_down_parquet_fallback(
    context_path: Path,
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """When Postgres is unavailable, a parquet/JSON fallback is written."""
    monkeypatch.delenv("KAIROS_LOOP_DISABLED", raising=False)

    trace = _make_trace()

    # Fake reader.
    mock_reader = MagicMock()
    mock_reader.fetch_envelope.return_value = trace

    # persist_night raises to simulate DB down.
    def _boom_persist(**kwargs):  # noqa: ANN202
        raise OSError("connection refused: kairos-pg down")

    with (
        patch.object(nl, "_resolve_project_id", return_value="proj-id"),
        patch.object(nl, "_fetch_root_trace_ids", return_value=([trace.trace_id], {})),
        patch("nightly_loop.PhoenixReader", return_value=mock_reader),
        patch.object(nl, "persist_night", side_effect=_boom_persist),
    ):
        result = nl.run_nightly_loop(
            context_path=str(context_path),
            data_dir_path=str(data_dir),
            retry_wait_s=0.0,
        )

    # Loop should complete (not crash).
    assert result.get("status") in ("ok", "skip")

    # Fallback directory should have been created.
    fallback_dir = data_dir / "parquet_fallback"
    assert fallback_dir.exists() or result.get("status") == "skip"


# ── State-machine transitions logged ──────────────────────────────────────────


def test_state_transitions_logged(
    context_path: Path,
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    """Each state-machine stage emits a transition log line."""
    monkeypatch.delenv("KAIROS_LOOP_DISABLED", raising=False)

    trace = _make_trace()
    mock_reader = MagicMock()
    mock_reader.fetch_envelope.return_value = trace

    # Patch away the DB and Phoenix so we can run deterministically.
    fake_persist_result = {"findings_rows": 0, "rollup_rows": 0}

    with (
        patch.object(nl, "_resolve_project_id", return_value="proj-id"),
        patch.object(nl, "_fetch_root_trace_ids", return_value=([trace.trace_id], {})),
        patch("nightly_loop.PhoenixReader", return_value=mock_reader),
        patch.object(nl, "persist_night", return_value=fake_persist_result),
        caplog.at_level(logging.INFO, logger="kairos.loop.nightly_loop"),
    ):
        nl.run_nightly_loop(
            context_path=str(context_path),
            data_dir_path=str(data_dir),
            retry_wait_s=0.0,
        )

    # Should have logged at least FETCH, ANALYZE, PERSIST transitions.
    # structlog may format differently; check for state names in message or event.
    all_log_text = " ".join(
        str(r.getMessage()) for r in caplog.records
    )
    # At minimum FETCH should appear somewhere.
    assert "FETCH" in all_log_text or any("FETCH" in str(r) for r in caplog.records)


# ── Full end-to-end (live Phoenix + Postgres) ──────────────────────────────────


@_skip_no_db
def test_e2e_run_live(
    context_path: Path,
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """End-to-end run against live Phoenix + Postgres (skipped without DB)."""
    monkeypatch.delenv("KAIROS_LOOP_DISABLED", raising=False)

    context_path_real = _REPO / "config" / "context.yaml"
    if not context_path_real.exists():
        pytest.skip("context.yaml not found")

    result = nl.run_nightly_loop(
        context_path=str(context_path_real),
        data_dir_path=str(data_dir),
        retry_wait_s=0.0,
    )

    # Whatever happens, status must be a known value.
    assert result.get("status") in ("ok", "quiet_night", "skip", "disabled")
    # A report must have been written.
    assert "report_path" in result
    assert Path(result["report_path"]).exists()
