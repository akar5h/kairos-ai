"""test_redaction_pattern_sync.py — drift guard + unit tests for new secret patterns.

Three concerns tested here:
  1. DRIFT GUARD — hooks/kairos_hook.py and src/kairos/readers/transcript_join.py
     must have an identical _SECRET_PATTERNS list (pattern strings + flags).
     If they diverge, this test fails loudly so we catch it at CI time.

  2. NEW PATTERN COVERAGE — each of the four patterns added in F1.3 must
     correctly redact a planted secret string and must NOT mangle benign text.

  3. REGRESSION GUARD — original six patterns still redact their target shapes.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

# ── helpers to load both modules ──────────────────────────────────────────────

_REPO = Path(__file__).parent.parent
_HOOK_PATH = _REPO / "hooks" / "kairos_hook.py"
_SRC_MODULE = "kairos.readers.transcript_join"


def _load_hook_patterns() -> list[Any]:
    """Load _SECRET_PATTERNS from hooks/kairos_hook.py without importing kairos pkg."""
    spec = importlib.util.spec_from_file_location("_kairos_hook_test_shim", _HOOK_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    return mod._SECRET_PATTERNS  # type: ignore[attr-defined]


def _load_src_patterns() -> list[Any]:
    from kairos.readers.transcript_join import _SECRET_PATTERNS  # noqa: PLC0415

    return _SECRET_PATTERNS


def _redact(text: str, patterns: list[Any]) -> str:
    result = text
    for pat in patterns:
        result = pat.sub("[REDACTED]", result)
    return result


# ── 1. DRIFT GUARD ────────────────────────────────────────────────────────────


class TestPatternSync:
    """The two _SECRET_PATTERNS lists must be identical."""

    def test_pattern_count_matches(self) -> None:
        hook = _load_hook_patterns()
        src = _load_src_patterns()
        assert len(hook) == len(src), (
            f"Pattern count mismatch: hook has {len(hook)}, src has {len(src)}. "
            "Sync hooks/kairos_hook.py and src/kairos/readers/transcript_join.py."
        )

    def test_pattern_strings_identical(self) -> None:
        hook = _load_hook_patterns()
        src = _load_src_patterns()
        hook_strs = [p.pattern for p in hook]
        src_strs = [p.pattern for p in src]
        assert hook_strs == src_strs, (
            "Pattern strings differ between hook and transcript_join.\n"
            f"hook:  {hook_strs}\n"
            f"src:   {src_strs}"
        )

    def test_pattern_flags_identical(self) -> None:
        hook = _load_hook_patterns()
        src = _load_src_patterns()
        hook_flags = [p.flags for p in hook]
        src_flags = [p.flags for p in src]
        assert hook_flags == src_flags, (
            "Pattern flags differ between hook and transcript_join.\n"
            f"hook:  {hook_flags}\n"
            f"src:   {src_flags}"
        )


# ── 2. NEW PATTERN UNIT TESTS ─────────────────────────────────────────────────


@pytest.fixture(scope="module")
def hook_patterns() -> list[Any]:
    return _load_hook_patterns()


@pytest.fixture(scope="module")
def src_patterns() -> list[Any]:
    return _load_src_patterns()


class TestDBUrlPattern:
    """postgres://user:pass@host/db and variants must be redacted."""

    SECRETS = [
        "postgresql://admin:hunter2@db.prod.example.com/mydb",
        "postgres://alice:s3cr3t@localhost:5432/orders",
        "mysql://root:Pa$$w0rd@mysql.host/shop",
        "mongodb://user:pass@mongo.host:27017/app",
        "mongodb+srv://user:pass@cluster.mongodb.net/prod",
        "redis://default:redispass@cache.example.com:6379",
    ]

    BENIGN = [
        # plain URL without creds
        "https://example.com/path",
        # postgres URL without password
        "postgres://localhost/mydb",
    ]

    @pytest.mark.parametrize("secret", SECRETS)
    def test_redacts_db_url(self, secret: str, src_patterns: list[Any]) -> None:
        result = _redact(secret, src_patterns)
        assert "[REDACTED]" in result, f"DB URL not redacted: {secret!r}"
        assert secret not in result, f"Secret survived redaction: {secret!r}"

    @pytest.mark.parametrize("text", BENIGN)
    def test_does_not_mangle_benign(self, text: str, src_patterns: list[Any]) -> None:
        result = _redact(text, src_patterns)
        # should not introduce [REDACTED] for benign URLs
        assert "[REDACTED]" not in result, (
            f"Benign text was mangled: {text!r} → {result!r}"
        )


class TestAssignmentPatterns:
    """KEY=value and KEY: value shapes must be redacted; normal prose must not."""

    SECRETS = [
        "API_KEY=abc123xyz",
        "api_key=abc123xyz",
        "SECRET=mysecretvalue",
        "TOKEN=tok_live_abc",
        "PASSWORD=hunter2",
        "PASSWD=hunter2",
        "PWD=hunter2",
        "ACCESS_KEY=AKIAIOSFODNN7EXAMPLE",
        "api-key=some-value",
        "access-key=val123",
        # colon delimiter
        "token: Bearer_abc123",
        "password: correct-horse-battery",
    ]

    BENIGN = [
        # plain words without a secret key prefix
        "the quick brown fox",
        "filename=report.csv",
        "count=42",
        "user=alice",
    ]

    @pytest.mark.parametrize("secret", SECRETS)
    def test_redacts_assignment_secret(self, secret: str, src_patterns: list[Any]) -> None:
        result = _redact(secret, src_patterns)
        assert "[REDACTED]" in result, f"Assignment secret not redacted: {secret!r}"

    @pytest.mark.parametrize("text", BENIGN)
    def test_does_not_mangle_benign(self, text: str, src_patterns: list[Any]) -> None:
        result = _redact(text, src_patterns)
        assert "[REDACTED]" not in result, (
            f"Benign text was mangled: {text!r} → {result!r}"
        )


class TestGitHubFinePAT:
    """github_pat_... tokens must be redacted."""

    SECRETS = [
        "github_pat_" + "A" * 22,
        "github_pat_" + "aB1_xyz" * 5,
        "export GH_TOKEN=github_pat_" + "Z" * 30,
    ]

    BENIGN = [
        "github_pat_short",           # too short — under 22 chars after prefix
        "see github.com/pat for docs",
    ]

    @pytest.mark.parametrize("secret", SECRETS)
    def test_redacts_github_pat(self, secret: str, src_patterns: list[Any]) -> None:
        result = _redact(secret, src_patterns)
        assert "[REDACTED]" in result, f"github_pat_ not redacted: {secret!r}"

    @pytest.mark.parametrize("text", BENIGN)
    def test_does_not_mangle_benign(self, text: str, src_patterns: list[Any]) -> None:
        result = _redact(text, src_patterns)
        assert "[REDACTED]" not in result, (
            f"Benign text was mangled: {text!r} → {result!r}"
        )


class TestSlackTokenPattern:
    """xox{b,a,p,r,s}- Slack tokens must be redacted."""

    SECRETS = [
        "xox" "b-12345678901-abcdefghij",
        "xox" "a-abcdefghijklmnopqrstuvwxyz",
        "xox" "p-" + "abc123-" * 3,
        "xox" "r-longtoken1234567890",
        "xox" "s-some-slack-token-here",
    ]

    BENIGN = [
        "xox is a scrabble word",
        "xoxo-hugs-and-kisses",   # 'o' not in [baprs]
        "the slack app config",
    ]

    @pytest.mark.parametrize("secret", SECRETS)
    def test_redacts_slack_token(self, secret: str, src_patterns: list[Any]) -> None:
        result = _redact(secret, src_patterns)
        assert "[REDACTED]" in result, f"Slack token not redacted: {secret!r}"

    @pytest.mark.parametrize("text", BENIGN)
    def test_does_not_mangle_benign(self, text: str, src_patterns: list[Any]) -> None:
        result = _redact(text, src_patterns)
        assert "[REDACTED]" not in result, (
            f"Benign text was mangled: {text!r} → {result!r}"
        )


# ── 3. REGRESSION — original six patterns still work ──────────────────────────


class TestOriginalPatternsRegression:
    """Ensure the pre-existing patterns still fire after the additive change."""

    CASES = [
        # (description, secret_string)
        ("sk- key", "sk-" + "A" * 25),
        ("GitHub PAT ghp_", "ghp_" + "B" * 36),
        ("AWS access key AKIA", "AKIA" + "C" * 16),
        ("Bearer token", "Bearer supersecrettoken123"),
        ("JWT", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"),
    ]

    @pytest.mark.parametrize("desc,secret", CASES)
    def test_original_pattern_still_redacts(
        self, desc: str, secret: str, src_patterns: list[Any]
    ) -> None:
        result = _redact(secret, src_patterns)
        assert "[REDACTED]" in result, f"Original pattern '{desc}' stopped working: {secret!r}"
        assert secret not in result


# ── 4. HOOK SUBPROCESS — planted secrets are redacted in spool ────────────────


class TestHookSubprocessNewPatterns:
    """Run the hook process with new-pattern secrets; verify spool is clean."""

    def _run_hook(
        self,
        command: str,
        tmp_path: Path,
    ) -> str:
        import json
        import os
        import subprocess
        import uuid

        session_id = f"test-rp-{uuid.uuid4().hex[:8]}"
        payload = {
            "hook_event_name": "PostToolUse",
            "session_id": session_id,
            "tool_name": "Bash",
            "tool_use_id": uuid.uuid4().hex,
            "tool_input": {"command": command},
            "tool_output": "ok",
            "is_error": False,
            "permission_mode": "default",
        }
        env = os.environ.copy()
        env["KAIROS_SPOOL_DIR"] = str(tmp_path)
        env.pop("KAIROS_HOOK_DISABLED", None)

        subprocess.run(
            [sys.executable, str(_HOOK_PATH)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
        )
        spool = tmp_path / f"{session_id}.jsonl"
        return spool.read_text() if spool.exists() else ""

    def test_pg_url_redacted_in_hook_spool(self, tmp_path: Path) -> None:
        secret = "postgresql://admin:hunter2@prod.db.example.com/orders"
        raw = self._run_hook(f"psql {secret}", tmp_path)
        assert "hunter2" not in raw, "PG URL password survived hook spool"
        assert "[REDACTED]" in raw

    def test_assignment_secret_redacted_in_hook_spool(self, tmp_path: Path) -> None:
        raw = self._run_hook("export API_KEY=topsecret123", tmp_path)
        assert "topsecret123" not in raw, "API_KEY value survived hook spool"
        assert "[REDACTED]" in raw

    def test_github_pat_redacted_in_hook_spool(self, tmp_path: Path) -> None:
        pat = "github_pat_" + "X" * 30
        raw = self._run_hook(f"git clone https://{pat}@github.com/org/repo", tmp_path)
        assert pat not in raw, "github_pat_ survived hook spool"
        assert "[REDACTED]" in raw

    def test_slack_token_redacted_in_hook_spool(self, tmp_path: Path) -> None:
        tok = "xox" "b-12345678901-abcdefghijklmno"
        raw = self._run_hook(f"curl -H 'Authorization: {tok}' https://slack.com/api/chat.postMessage", tmp_path)
        assert tok not in raw, "Slack token survived hook spool"
        assert "[REDACTED]" in raw
