"""Tests for hooks/kairos_hook.py (F1.2).

Tests run the hook as a subprocess (feeding JSON to stdin) to verify:
  (a) valid PostToolUse payload → exit 0, spool line written
  (b) valid PostToolUseFailure payload → exit 0, is_error=True in spool
  (c) malformed / empty stdin → exit 0, no spool file written
  (d) KAIROS_HOOK_DISABLED set → exit 0, no-op

Also verifies:
  - A planted secret (sk-... key) in tool_input is redacted in the spool line
  - Each spool line is valid JSON
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

_HOOK_SCRIPT = Path(__file__).parent.parent / "hooks" / "kairos_hook.py"


def _run_hook(
    payload: Mapping[str, object] | str,
    spool_dir: Path,
    env_override: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the hook script with the given payload on stdin."""
    env = os.environ.copy()
    env["KAIROS_SPOOL_DIR"] = str(spool_dir)
    env.pop("KAIROS_HOOK_DISABLED", None)  # ensure not set by default
    if env_override:
        env.update(env_override)

    stdin_text: str = json.dumps(payload) if not isinstance(payload, str) else payload
    return subprocess.run(
        [sys.executable, str(_HOOK_SCRIPT)],
        input=stdin_text,
        capture_output=True,
        text=True,
        env=env,
    )


class TestKairosHookExists:
    def test_hook_script_exists(self) -> None:
        assert _HOOK_SCRIPT.exists(), f"Hook script not found at {_HOOK_SCRIPT}"


class TestKairosHookExitCodes:
    def test_valid_post_tool_use_exits_0(self, tmp_path: Path) -> None:
        """Valid PostToolUse payload → exit 0."""
        payload = {
            "hook_event_name": "PostToolUse",
            "session_id": f"test-{uuid.uuid4().hex[:8]}",
            "tool_name": "Bash",
            "tool_use_id": uuid.uuid4().hex,
            "tool_input": {"command": "ls -la"},
            "tool_output": "file1\nfile2",
            "is_error": False,
            "permission_mode": "default",
        }
        result = _run_hook(payload, tmp_path)
        assert result.returncode == 0

    def test_valid_post_tool_use_failure_exits_0(self, tmp_path: Path) -> None:
        """Valid PostToolUseFailure payload → exit 0."""
        payload = {
            "hook_event_name": "PostToolUseFailure",
            "session_id": f"test-{uuid.uuid4().hex[:8]}",
            "tool_name": "Bash",
            "tool_use_id": uuid.uuid4().hex,
            "tool_input": {"command": "bad-command"},
            "tool_output": "command not found",
            "is_error": True,
            "permission_mode": "default",
        }
        result = _run_hook(payload, tmp_path)
        assert result.returncode == 0

    def test_malformed_stdin_exits_0(self, tmp_path: Path) -> None:
        """Malformed JSON on stdin → exit 0, no crash."""
        result = _run_hook("NOT JSON AT ALL {{{{", tmp_path)
        assert result.returncode == 0

    def test_empty_stdin_exits_0(self, tmp_path: Path) -> None:
        """Empty stdin → exit 0."""
        result = _run_hook("", tmp_path)
        assert result.returncode == 0

    def test_disabled_env_exits_0_noop(self, tmp_path: Path) -> None:
        """KAIROS_HOOK_DISABLED set → exit 0, no spool file written."""
        session_id = f"test-disabled-{uuid.uuid4().hex[:8]}"
        payload = {
            "hook_event_name": "PostToolUse",
            "session_id": session_id,
            "tool_name": "Bash",
            "tool_use_id": uuid.uuid4().hex,
            "tool_input": {"command": "echo hi"},
            "tool_output": "hi",
            "is_error": False,
            "permission_mode": "default",
        }
        result = _run_hook(payload, tmp_path, env_override={"KAIROS_HOOK_DISABLED": "1"})
        assert result.returncode == 0
        # No spool file should have been written.
        spool_file = tmp_path / f"{session_id}.jsonl"
        assert not spool_file.exists()


class TestKairosHookSpoolOutput:
    def test_spool_line_is_valid_json(self, tmp_path: Path) -> None:
        """Each spool line produced by the hook is valid JSON."""
        session_id = f"test-spool-{uuid.uuid4().hex[:8]}"
        payload = {
            "hook_event_name": "PostToolUse",
            "session_id": session_id,
            "tool_name": "Read",
            "tool_use_id": uuid.uuid4().hex,
            "tool_input": {"file_path": "/tmp/test.txt"},
            "tool_output": "contents",
            "is_error": False,
            "permission_mode": "default",
        }
        _run_hook(payload, tmp_path)
        spool_file = tmp_path / f"{session_id}.jsonl"
        assert spool_file.exists(), "Spool file was not created"

        lines = [ln for ln in spool_file.read_text().splitlines() if ln.strip()]
        assert len(lines) == 1
        record = json.loads(lines[0])  # must not raise
        assert isinstance(record, dict)
        assert record["session_id"] == session_id
        assert record["event_name"] == "PostToolUse"

    def test_is_error_true_in_failure_spool(self, tmp_path: Path) -> None:
        """PostToolUseFailure payload → is_error=True in spool record."""
        session_id = f"test-err-{uuid.uuid4().hex[:8]}"
        payload = {
            "hook_event_name": "PostToolUseFailure",
            "session_id": session_id,
            "tool_name": "Bash",
            "tool_use_id": uuid.uuid4().hex,
            "tool_input": {"command": "bad"},
            "tool_output": "error: bad command",
            "is_error": True,
            "permission_mode": "default",
        }
        _run_hook(payload, tmp_path)
        spool_file = tmp_path / f"{session_id}.jsonl"
        record = json.loads(spool_file.read_text().strip())
        assert record["is_error"] is True

    def test_malformed_stdin_no_spool_file(self, tmp_path: Path) -> None:
        """Malformed stdin → no spool file created (nothing to write)."""
        _run_hook("{{not json}}", tmp_path)
        jsonl_files = list(tmp_path.glob("*.jsonl"))
        assert jsonl_files == []

    def test_secret_redacted_in_tool_input(self, tmp_path: Path) -> None:
        """A secret (sk-... key) planted in tool_input is scrubbed in spool."""
        session_id = f"test-redact-{uuid.uuid4().hex[:8]}"
        secret = "sk-" + "A" * 25  # matches sk-[A-Za-z0-9_-]{20,} pattern
        payload = {
            "hook_event_name": "PostToolUse",
            "session_id": session_id,
            "tool_name": "Bash",
            "tool_use_id": uuid.uuid4().hex,
            "tool_input": {"command": f"curl -H 'Authorization: sk-secret' http://example.com; echo {secret}"},
            "tool_output": "ok",
            "is_error": False,
            "permission_mode": "default",
        }
        _run_hook(payload, tmp_path)
        spool_file = tmp_path / f"{session_id}.jsonl"
        raw_spool = spool_file.read_text()

        # The raw secret must NOT appear in the spool.
        assert secret not in raw_spool, "Secret was not redacted from spool"
        assert "[REDACTED]" in raw_spool, "Expected [REDACTED] marker not found"

    def test_bearer_token_redacted_in_payload(self, tmp_path: Path) -> None:
        """Bearer token in tool_input is redacted in payload_redacted too."""
        session_id = f"test-bearer-{uuid.uuid4().hex[:8]}"
        payload = {
            "hook_event_name": "PostToolUse",
            "session_id": session_id,
            "tool_name": "Bash",
            "tool_use_id": uuid.uuid4().hex,
            "tool_input": {"command": "curl -H 'Authorization: Bearer supersecrettoken123'"},
            "tool_output": "ok",
            "is_error": False,
            "permission_mode": "default",
        }
        _run_hook(payload, tmp_path)
        spool_file = tmp_path / f"{session_id}.jsonl"
        raw_spool = spool_file.read_text()

        assert "supersecrettoken123" not in raw_spool
        assert "[REDACTED]" in raw_spool

    def test_tool_use_id_captured(self, tmp_path: Path) -> None:
        """tool_use_id from payload is stored in the spool record."""
        session_id = f"test-tuid-{uuid.uuid4().hex[:8]}"
        tool_use_id = uuid.uuid4().hex
        payload = {
            "hook_event_name": "PostToolUse",
            "session_id": session_id,
            "tool_name": "Read",
            "tool_use_id": tool_use_id,
            "tool_input": {"file_path": "/tmp/x"},
            "tool_output": "x",
            "is_error": False,
            "permission_mode": "default",
        }
        _run_hook(payload, tmp_path)
        spool_file = tmp_path / f"{session_id}.jsonl"
        record = json.loads(spool_file.read_text().strip())
        assert record["tool_use_id"] == tool_use_id

    def test_session_start_no_tool_use_id(self, tmp_path: Path) -> None:
        """SessionStart payload (no tool_use_id) spools with tool_use_id=None."""
        session_id = f"test-start-{uuid.uuid4().hex[:8]}"
        payload = {
            "hook_event_name": "SessionStart",
            "session_id": session_id,
            "permission_mode": "default",
            "source": "startup",
            "model": "claude-opus-4-5",
        }
        _run_hook(payload, tmp_path)
        spool_file = tmp_path / f"{session_id}.jsonl"
        record = json.loads(spool_file.read_text().strip())
        assert record["tool_use_id"] is None
        assert record["event_name"] == "SessionStart"

    def test_multiple_events_append_to_same_file(self, tmp_path: Path) -> None:
        """Multiple hook calls for the same session append lines to one file."""
        session_id = f"test-multi-{uuid.uuid4().hex[:8]}"
        for i in range(3):
            payload = {
                "hook_event_name": "PostToolUse",
                "session_id": session_id,
                "tool_name": "Bash",
                "tool_use_id": uuid.uuid4().hex,
                "tool_input": {"command": f"echo {i}"},
                "tool_output": str(i),
                "is_error": False,
                "permission_mode": "default",
            }
            _run_hook(payload, tmp_path)

        spool_file = tmp_path / f"{session_id}.jsonl"
        lines = [ln for ln in spool_file.read_text().splitlines() if ln.strip()]
        assert len(lines) == 3
        for line in lines:
            assert isinstance(json.loads(line), dict)
