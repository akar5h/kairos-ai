"""Tests for eval/taubench_corpus.py.

Covers:
  - Label semantics (reward 1.0 → PASS, 0.0 → FAIL, partial → PARTIAL)
  - Skip counting (empty traj, invalid reward)
  - Trace ID determinism
  - Terminal status inference (STOP sentinel, transfer_to_human_agents)
  - Traj normalization produces a valid TraceEnvelope with correct tool_sequence
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# Add the eval directory to sys.path so the module can be imported normally.
_EVAL_DIR = Path(__file__).parents[2] / "eval"
if str(_EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(_EVAL_DIR))

import taubench_corpus as _mod  # type: ignore[import-untyped]  # noqa: E402

_label = _mod._label
_deterministic_id = _mod._deterministic_id
_terminal_status = _mod._terminal_status
_normalize_traj = _mod._normalize_traj


# ── Label semantics ────────────────────────────────────────────────────────


def test_label_pass() -> None:
    assert _label(1.0) == "PASS"


def test_label_fail() -> None:
    assert _label(0.0) == "FAIL"


def test_label_partial_low() -> None:
    assert _label(0.5) == "PARTIAL"


def test_label_partial_high() -> None:
    assert _label(0.99) == "PARTIAL"


def test_label_partial_epsilon() -> None:
    assert _label(1e-9) == "PARTIAL"


# ── Deterministic ID ──────────────────────────────────────────────────────


def test_deterministic_id_stable() -> None:
    id1 = _deterministic_id("bundle_a", "mode_x", 19, 0)
    id2 = _deterministic_id("bundle_a", "mode_x", 19, 0)
    assert id1 == id2


def test_deterministic_id_different_task() -> None:
    id1 = _deterministic_id("bundle_a", "mode_x", 19, 0)
    id2 = _deterministic_id("bundle_a", "mode_x", 20, 0)
    assert id1 != id2


def test_deterministic_id_different_bundle() -> None:
    id1 = _deterministic_id("bundle_a", "mode_x", 19, 0)
    id2 = _deterministic_id("bundle_b", "mode_x", 19, 0)
    assert id1 != id2


def test_deterministic_id_different_mode() -> None:
    id1 = _deterministic_id("bundle_a", "baseline_no_kairos", 19, 0)
    id2 = _deterministic_id("bundle_a", "memory_cascade", 19, 0)
    assert id1 != id2


def test_deterministic_id_is_hex_string() -> None:
    tid = _deterministic_id("bundle_a", "mode_x", 19, 0)
    assert len(tid) == 32
    assert all(c in "0123456789abcdef" for c in tid)


# ── Terminal status ────────────────────────────────────────────────────────


def test_terminal_status_stop() -> None:
    traj: list[dict[str, Any]] = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi"},
        {"role": "user", "content": "###STOP###"},
    ]
    from kairos.models.enums import TerminalStatus
    assert _terminal_status(traj) == TerminalStatus.COMPLETED


def test_terminal_status_transfer() -> None:
    traj: list[dict[str, Any]] = [
        {"role": "user", "content": "I need help"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "abc",
                    "function": {"name": "transfer_to_human_agents", "arguments": "{}"},
                    "type": "function",
                    "index": 0,
                }
            ],
        },
        {"role": "user", "content": "###STOP###"},
    ]
    from kairos.models.enums import TerminalStatus
    assert _terminal_status(traj) == TerminalStatus.HUMAN_ESCALATION


def test_terminal_status_no_stop_completed() -> None:
    """A trajectory that ends with assistant turn (no STOP) is still COMPLETED."""
    traj: list[dict[str, Any]] = [
        {"role": "user", "content": "Do something"},
        {"role": "assistant", "content": "Done."},
    ]
    from kairos.models.enums import TerminalStatus
    assert _terminal_status(traj) == TerminalStatus.COMPLETED


# ── Traj normalization ────────────────────────────────────────────────────


def _minimal_traj(*, tool_name: str = "get_user_details", tool_output: str = '{"id": "u1"}') -> list[dict[str, Any]]:
    """Return a minimal synthetic traj for normalization tests."""
    return [
        {"role": "system", "content": "You are an airline agent."},
        {"role": "user", "content": "Please help me."},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "function": {"name": tool_name, "arguments": '{"user_id": "u1"}'},
                    "type": "function",
                    "index": 0,
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "name": tool_name,
            "content": tool_output,
        },
        {"role": "user", "content": "Thanks! ###STOP###"},
    ]


def test_normalize_traj_tool_sequence() -> None:
    traj = _minimal_traj(tool_name="update_reservation_flights")
    env = _normalize_traj(traj, "test-id", 19, None, 0)
    assert "update_reservation_flights" in env.tool_sequence


def test_normalize_traj_tool_output_attached() -> None:
    traj = _minimal_traj(tool_name="get_user_details", tool_output='{"ok": true}')
    env = _normalize_traj(traj, "test-id", 19, None, 0)
    # First tool step should have the output.
    tool_steps = [s for s in env.steps if s.tool_name == "get_user_details"]
    assert len(tool_steps) == 1
    assert tool_steps[0].tool_output == '{"ok": true}'


def test_normalize_traj_terminal_completed() -> None:
    traj = _minimal_traj()
    env = _normalize_traj(traj, "test-id", 19, None, 0)
    from kairos.models.enums import TerminalStatus
    assert env.terminal_status == TerminalStatus.COMPLETED


def test_normalize_traj_user_input_extracted() -> None:
    traj = _minimal_traj()
    env = _normalize_traj(traj, "test-id", 19, None, 0)
    # First non-STOP user message should be the user_input.
    assert env.user_input == "Please help me."


def test_normalize_traj_instruction_fallback() -> None:
    """When traj has no user turn, instruction param is used as user_input."""
    traj: list[dict[str, Any]] = [
        {"role": "system", "content": "Sys"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "c1", "function": {"name": "think", "arguments": "{}"}, "type": "function", "index": 0}
            ],
        },
    ]
    env = _normalize_traj(traj, "test-id", 17, "Fallback instruction", 0)
    assert env.user_input == "Fallback instruction"


def test_normalize_traj_integrity_complete() -> None:
    traj = _minimal_traj()
    env = _normalize_traj(traj, "test-id", 19, None, 0)
    assert env.integrity == "complete"


def test_normalize_traj_step_count() -> None:
    traj = _minimal_traj()
    env = _normalize_traj(traj, "test-id", 19, None, 0)
    # One tool step + zero LLM steps (assistant turn has tool_calls, no content).
    assert env.step_count == 1


# ── Skip counting integration (tiny synthetic corpus) ────────────────────


def test_build_corpus_skip_empty_traj(tmp_path: Path) -> None:
    """Rows with empty traj[] are skipped and counted."""
    bundle: dict[str, Any] = {
        "created_at": "2026-05-13T00:00:00Z",
        "repo_root": "/fake",
        "args": {"env": "airline", "model": None},
        "modes": [
            {
                "mode": "baseline",
                "checkpoint_rows": [
                    {"task_id": 1, "reward": 0.0, "traj": [], "trial": 0},    # empty traj → skip
                    {"task_id": 2, "reward": 1.0, "traj": [                  # valid row
                        {"role": "user", "content": "help"},
                        {"role": "assistant", "content": "ok", "tool_calls": [
                            {
                                "id": "c1",
                                "function": {"name": "get_user_details", "arguments": "{}"},
                                "type": "function",
                                "index": 0,
                            }
                        ]},
                        {"role": "tool", "tool_call_id": "c1", "name": "get_user_details", "content": "{}"},
                        {"role": "user", "content": "###STOP###"},
                    ], "trial": 0},
                ],
                "kairos_run_dir": None,
            }
        ],
    }
    bundles_dir = tmp_path / "bundles"
    bundles_dir.mkdir()
    (bundles_dir / "test_bundle.json").write_text(json.dumps(bundle))
    corpus_dir = tmp_path / "corpus"

    coverage = _mod.build_corpus(bundles_dir, corpus_dir, verbose=False)
    stats = coverage["test_bundle.json"]
    assert stats["total_rows"] == 2
    assert stats["paired"] == 1
    assert stats["skipped"] == 1


def test_build_corpus_labels_written(tmp_path: Path) -> None:
    """labels.jsonl is written with correct fields."""
    traj_valid: list[dict[str, Any]] = [
        {"role": "user", "content": "help"},
        {"role": "assistant", "content": "ok", "tool_calls": [
            {
                "id": "c1",
                "function": {"name": "update_reservation_flights", "arguments": "{}"},
                "type": "function",
                "index": 0,
            }
        ]},
        {"role": "tool", "tool_call_id": "c1", "name": "update_reservation_flights", "content": "{}"},
        {"role": "user", "content": "###STOP###"},
    ]
    bundle: dict[str, Any] = {
        "created_at": "2026-05-13T00:00:00Z",
        "repo_root": "/fake",
        "args": {"env": "airline", "model": "kimi-k2"},
        "modes": [
            {
                "mode": "memory_cascade",
                "checkpoint_rows": [{"task_id": 5, "reward": 1.0, "traj": traj_valid, "trial": 0}],
                "kairos_run_dir": None,
            }
        ],
    }
    bundles_dir = tmp_path / "bundles"
    bundles_dir.mkdir()
    (bundles_dir / "my_bundle.json").write_text(json.dumps(bundle))
    corpus_dir = tmp_path / "corpus"

    _mod.build_corpus(bundles_dir, corpus_dir, verbose=False)
    labels_path = corpus_dir / "labels.jsonl"
    assert labels_path.exists()
    records = [json.loads(line) for line in labels_path.read_text().splitlines() if line.strip()]
    assert len(records) == 1
    rec = records[0]
    assert rec["task_id"] == 5
    assert rec["label"] == "PASS"
    assert rec["reward"] == 1.0
    assert rec["mode"] == "memory_cascade"
    assert rec["bundle"] == "my_bundle.json"
