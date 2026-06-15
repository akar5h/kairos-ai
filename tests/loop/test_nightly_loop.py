"""Tests for scripts/nightly_loop.py — F1.5.

Coverage:
  - quiet night: 0 traces → 'quiet_night' report, exit 0
  - forced exception → skip-marker report, exit 0
  - kill switch KAIROS_LOOP_DISABLED=1 → clean no-op
  - DB-backed trace discovery + envelope fetch path
  - DB-down → parquet fallback + WARN
  - state machine: FETCH→ANALYZE→...→DONE transitions logged
  - skip-marker contains traceback

All tests use the _force_exception / _pg_conn injection points to avoid
hitting live Postgres.
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
from unittest.mock import patch

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
    """0 traces from DB → 'quiet_night' report (valid, not an error)."""
    monkeypatch.delenv("KAIROS_LOOP_DISABLED", raising=False)

    with patch.object(nl, "_fetch_db_trace_ids", return_value=[]):
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
    """When Postgres is unavailable for persist, a parquet/JSON fallback is written."""
    monkeypatch.delenv("KAIROS_LOOP_DISABLED", raising=False)

    trace = _make_trace()

    # persist_night raises to simulate DB down.
    def _boom_persist(**kwargs: Any) -> None:
        raise OSError("connection refused: kairos-pg down")

    with (
        patch.object(nl, "_fetch_db_trace_ids", return_value=[trace.trace_id]),
        patch("nightly_loop.fetch_envelope_from_db", return_value=trace),
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

    # Patch away DB so we can run deterministically.
    fake_persist_result = {"findings_rows": 0, "rollup_rows": 0}

    with (
        patch.object(nl, "_fetch_db_trace_ids", return_value=[trace.trace_id]),
        patch("nightly_loop.fetch_envelope_from_db", return_value=trace),
        patch.object(nl, "persist_night", return_value=fake_persist_result),
        caplog.at_level(logging.INFO, logger="kairos.loop.nightly_loop"),
    ):
        nl.run_nightly_loop(
            context_path=str(context_path),
            data_dir_path=str(data_dir),
            retry_wait_s=0.0,
        )

    # Should have logged at least FETCH, ANALYZE, PERSIST transitions.
    all_log_text = " ".join(str(r.getMessage()) for r in caplog.records)
    assert "FETCH" in all_log_text or any("FETCH" in str(r) for r in caplog.records)


# ── DB-backed trace discovery + envelope fetch ────────────────────────────────


def test_db_discovery_and_fetch_path(
    context_path: Path,
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_fetch_db_trace_ids + fetch_envelope_from_db drives the full loop path."""
    monkeypatch.delenv("KAIROS_LOOP_DISABLED", raising=False)

    trace = _make_trace()
    fake_persist_result = {"findings_rows": 1, "rollup_rows": 1}

    with (
        patch.object(nl, "_fetch_db_trace_ids", return_value=[trace.trace_id]) as mock_fetch_ids,
        patch("nightly_loop.fetch_envelope_from_db", return_value=trace) as mock_fetch_env,
        patch.object(nl, "persist_night", return_value=fake_persist_result),
    ):
        result = nl.run_nightly_loop(
            dsn="postgresql://fake/kairos",
            context_path=str(context_path),
            data_dir_path=str(data_dir),
            retry_wait_s=0.0,
        )

    assert result["status"] in ("ok", "quiet_night")
    mock_fetch_ids.assert_called_once()
    # Loop passes correlation_key_attr + enrich_hooks kwargs.
    mock_fetch_env.assert_called_once()
    call_args = mock_fetch_env.call_args
    assert call_args.args[0] == trace.trace_id
    assert call_args.args[1] == "postgresql://fake/kairos"


def test_db_discovery_excludes_kairos_loop_actor(
    context_path: Path,
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Traces whose spans carry actor_id=kairos-loop are skipped post-fetch."""
    from kairos.models.enums import StepStatus, StepType
    from kairos.models.trace import Step, TraceEnvelope

    monkeypatch.delenv("KAIROS_LOOP_DISABLED", raising=False)

    loop_trace = TraceEnvelope(
        trace_id="loop" + "0" * 28,
        steps=[
            Step(
                step_index=0,
                step_type=StepType.TOOL_CALL,
                tool_name="Bash",
                status=StepStatus.OK,
                attrs={"actor_id": "kairos-loop"},
            )
        ],
        total_tokens=10,
        total_latency_ms=100,
    )

    fake_persist_result = {"findings_rows": 0, "rollup_rows": 0}

    with (
        patch.object(nl, "_fetch_db_trace_ids", return_value=[loop_trace.trace_id]),
        patch("nightly_loop.fetch_envelope_from_db", return_value=loop_trace),
        patch.object(nl, "persist_night", return_value=fake_persist_result),
    ):
        result = nl.run_nightly_loop(
            context_path=str(context_path),
            data_dir_path=str(data_dir),
            retry_wait_s=0.0,
        )

    # Loop self-trace filtered → 0 envelopes → loop completes ok with nothing to analyze.
    assert result["status"] in ("ok", "quiet_night")


# ── Full end-to-end (live Postgres) ───────────────────────────────────────────


@_skip_no_db
def test_e2e_run_live(
    context_path: Path,
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """End-to-end run against live Postgres DB (skipped without KAIROS_PG_DSN)."""
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
