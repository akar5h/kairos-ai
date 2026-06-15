"""test_install_sh.py — install.sh / uninstall.sh correctness + idempotency (F1.3).

What we test:
  1. bash -n syntax check on both scripts.
  2. install.sh against a seeded temp HOME:
       - env keys added (new kairos keys present).
       - existing env key preserved (not clobbered).
       - hooks stanza: exactly one kairos entry per event type.
       - existing hook entry NOT removed.
  3. install.sh run a SECOND time (idempotency):
       - still exactly one kairos entry per event type (no duplicates).
       - env keys still present; no duplicates.
  4. uninstall.sh surgical removal:
       - kairos hook entries gone.
       - pre-existing hook entries intact.
       - env keys untouched (uninstall.sh intentionally leaves env).
  5. uninstall.sh idempotent (run twice — no error, no duplicate removals).
  6. uninstall.sh --restore-backup:
       - original file restored exactly (minus the install changes).

All tests use a subprocess call with HOME set to a tmpdir — no real
~/.claude/settings.json is touched.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).parent.parent
_INSTALL_SH = _REPO / "scripts" / "install.sh"
_UNINSTALL_SH = _REPO / "scripts" / "uninstall.sh"
_HOOK_SCRIPT = _REPO / "hooks" / "kairos_hook.py"

_SEEDED_SETTINGS = {
    "env": {
        "EXISTING_KEY": "existing_val",
        "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
    },
    "hooks": {
        "PostToolUse": [
            {
                "matcher": "Write|Edit",
                "hooks": [
                    {
                        "type": "command",
                        "command": "bash ~/.claude/hooks/auto-test.sh",
                    }
                ],
            }
        ]
    },
}

_EXPECTED_ENV_KEYS = {
    "CLAUDE_CODE_ENABLE_TELEMETRY",
    "OTEL_LOGS_EXPORTER",
    "OTEL_METRICS_EXPORTER",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_LOG_TOOL_DETAILS",
    "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA",
    "OTEL_TRACES_EXPORTER",
    "OTEL_LOG_TOOL_CONTENT",
}

_HOOK_EVENTS = ["PostToolUse", "PostToolUseFailure", "SessionStart", "SessionEnd"]


# ── helpers ───────────────────────────────────────────────────────────────────


def _setup_home(tmp_path: Path, seed: dict | None = None) -> Path:
    """Create a minimal ~/.claude/settings.json in tmp_path."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True)
    settings = claude_dir / "settings.json"
    settings.write_text(json.dumps(seed or {}))
    return tmp_path


def _run_script(script: Path, home: Path, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env.pop("KAIROS_HOOK_DISABLED", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        env=env,
    )


def _read_settings(home: Path) -> dict:
    settings_file = home / ".claude" / "settings.json"
    return json.loads(settings_file.read_text())


def _kairos_entries_for_event(settings: dict, event: str) -> list[dict]:
    """Return hook group entries whose hooks[] contain the kairos_hook.py command."""
    hook_str = str(_HOOK_SCRIPT)
    event_groups = settings.get("hooks", {}).get(event, [])
    kairos_groups = []
    for group in event_groups:
        inner = group.get("hooks", [])
        if any(hook_str in h.get("command", "") for h in inner):
            kairos_groups.append(group)
    return kairos_groups


# ── 1. Syntax check ───────────────────────────────────────────────────────────


class TestSyntaxCheck:
    def test_install_sh_syntax(self) -> None:
        result = subprocess.run(["bash", "-n", str(_INSTALL_SH)], capture_output=True, text=True)
        assert result.returncode == 0, f"install.sh syntax error:\n{result.stderr}"

    def test_uninstall_sh_syntax(self) -> None:
        result = subprocess.run(["bash", "-n", str(_UNINSTALL_SH)], capture_output=True, text=True)
        assert result.returncode == 0, f"uninstall.sh syntax error:\n{result.stderr}"


# ── 2. install.sh first run ───────────────────────────────────────────────────


class TestInstallFirstRun:
    @pytest.fixture()
    def installed_home(self, tmp_path: Path) -> Path:
        home = _setup_home(tmp_path, seed=_SEEDED_SETTINGS)
        result = _run_script(_INSTALL_SH, home)
        assert result.returncode == 0, f"install.sh failed:\n{result.stderr}\n{result.stdout}"
        return home

    def test_env_keys_added(self, installed_home: Path) -> None:
        settings = _read_settings(installed_home)
        env = settings.get("env", {})
        for key in _EXPECTED_ENV_KEYS:
            assert key in env, f"Expected env key missing: {key}"

    def test_existing_env_key_preserved(self, installed_home: Path) -> None:
        settings = _read_settings(installed_home)
        assert settings["env"]["EXISTING_KEY"] == "existing_val", "Existing env key was clobbered"
        assert settings["env"]["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] == "1"

    def test_one_kairos_entry_per_event(self, installed_home: Path) -> None:
        settings = _read_settings(installed_home)
        for event in _HOOK_EVENTS:
            entries = _kairos_entries_for_event(settings, event)
            assert len(entries) == 1, (
                f"Expected exactly 1 kairos entry for {event}, got {len(entries)}"
            )

    def test_existing_hook_entry_preserved(self, installed_home: Path) -> None:
        settings = _read_settings(installed_home)
        post_tool_groups = settings.get("hooks", {}).get("PostToolUse", [])
        existing_cmds = [
            h.get("command", "")
            for group in post_tool_groups
            for h in group.get("hooks", [])
        ]
        assert any("auto-test.sh" in cmd for cmd in existing_cmds), (
            "Pre-existing PostToolUse hook was removed by install.sh"
        )

    def test_backup_created(self, installed_home: Path) -> None:
        claude_dir = installed_home / ".claude"
        backups = list(claude_dir.glob("settings.json.kairos-bak.*"))
        assert len(backups) >= 1, "No backup file created by install.sh"

    def test_async_on_post_tool_use(self, installed_home: Path) -> None:
        settings = _read_settings(installed_home)
        for event in ("PostToolUse", "PostToolUseFailure"):
            entries = _kairos_entries_for_event(settings, event)
            assert entries, f"No kairos entry for {event}"
            hook = entries[0]["hooks"][0]
            assert hook.get("async") is True, f"async not set on {event} hook"


# ── 3. install.sh idempotency ─────────────────────────────────────────────────


class TestInstallIdempotency:
    @pytest.fixture()
    def double_installed_home(self, tmp_path: Path) -> Path:
        home = _setup_home(tmp_path, seed=_SEEDED_SETTINGS)
        for _ in range(2):
            result = _run_script(_INSTALL_SH, home)
            assert result.returncode == 0, f"install.sh failed:\n{result.stderr}"
        return home

    def test_exactly_one_kairos_entry_after_double_install(self, double_installed_home: Path) -> None:
        settings = _read_settings(double_installed_home)
        for event in _HOOK_EVENTS:
            entries = _kairos_entries_for_event(settings, event)
            assert len(entries) == 1, (
                f"Duplicate kairos entries for {event} after double install: got {len(entries)}"
            )

    def test_env_keys_present_not_duplicated(self, double_installed_home: Path) -> None:
        settings = _read_settings(double_installed_home)
        env = settings.get("env", {})
        for key in _EXPECTED_ENV_KEYS:
            assert key in env
        # JSON object keys are unique by definition, so no extra check needed.


# ── 4. uninstall.sh surgical removal ─────────────────────────────────────────


class TestUninstallSurgical:
    @pytest.fixture()
    def uninstalled_home(self, tmp_path: Path) -> Path:
        home = _setup_home(tmp_path, seed=_SEEDED_SETTINGS)
        _run_script(_INSTALL_SH, home)
        result = _run_script(_UNINSTALL_SH, home)
        assert result.returncode == 0, f"uninstall.sh failed:\n{result.stderr}"
        return home

    def test_kairos_entries_removed(self, uninstalled_home: Path) -> None:
        settings = _read_settings(uninstalled_home)
        for event in _HOOK_EVENTS:
            entries = _kairos_entries_for_event(settings, event)
            assert len(entries) == 0, (
                f"Kairos entries still present for {event} after uninstall"
            )

    def test_existing_hook_preserved(self, uninstalled_home: Path) -> None:
        settings = _read_settings(uninstalled_home)
        post_tool_groups = settings.get("hooks", {}).get("PostToolUse", [])
        existing_cmds = [
            h.get("command", "")
            for group in post_tool_groups
            for h in group.get("hooks", [])
        ]
        assert any("auto-test.sh" in cmd for cmd in existing_cmds), (
            "Pre-existing PostToolUse hook was removed by uninstall.sh"
        )

    def test_env_keys_untouched(self, uninstalled_home: Path) -> None:
        """uninstall.sh leaves env keys in place (documented behavior)."""
        settings = _read_settings(uninstalled_home)
        env = settings.get("env", {})
        for key in _EXPECTED_ENV_KEYS:
            assert key in env, f"Env key unexpectedly removed by uninstall: {key}"


# ── 5. uninstall.sh idempotency ───────────────────────────────────────────────


class TestUninstallIdempotency:
    def test_double_uninstall_no_error(self, tmp_path: Path) -> None:
        home = _setup_home(tmp_path, seed=_SEEDED_SETTINGS)
        _run_script(_INSTALL_SH, home)
        for _ in range(2):
            result = _run_script(_UNINSTALL_SH, home)
            assert result.returncode == 0, f"uninstall.sh errored on repeat: {result.stderr}"

    def test_double_uninstall_zero_kairos_entries(self, tmp_path: Path) -> None:
        home = _setup_home(tmp_path, seed=_SEEDED_SETTINGS)
        _run_script(_INSTALL_SH, home)
        for _ in range(2):
            _run_script(_UNINSTALL_SH, home)
        settings = _read_settings(home)
        for event in _HOOK_EVENTS:
            assert len(_kairos_entries_for_event(settings, event)) == 0


# ── 6. uninstall.sh --restore-backup ─────────────────────────────────────────


class TestUninstallRestoreBackup:
    def test_restore_backup_reverts_to_original(self, tmp_path: Path) -> None:
        """--restore-backup restores the settings to exactly the pre-install state."""
        home = _setup_home(tmp_path, seed=_SEEDED_SETTINGS)
        original = _read_settings(home)

        _run_script(_INSTALL_SH, home)
        # Verify install happened.
        after_install = _read_settings(home)
        assert len(_kairos_entries_for_event(after_install, "PostToolUse")) == 1

        # --restore-backup must be a positional arg, not an env var.
        env = os.environ.copy()
        env["HOME"] = str(home)
        restore_result = subprocess.run(
            ["bash", str(_UNINSTALL_SH), "--restore-backup"],
            capture_output=True,
            text=True,
            env=env,
        )
        assert restore_result.returncode == 0, f"restore backup failed:\n{restore_result.stderr}"

        restored = _read_settings(home)
        assert restored == original, "Restored settings do not match original"
