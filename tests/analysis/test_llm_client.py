"""Red-phase tests for the LLM client (OpenRouter-compatible via openai SDK).

Target module (not yet implemented):
    src.kairos.analysis.llm_client

Expected surface:
    class LLMClient:
        def __init__(
            self, *,
            api_key: str | None = None,
            model: str | None = None,
            base_url: str | None = None,
            timeout_s: float = 45.0,
            max_retries: int = 3,
            max_output_tokens: int = 1200,
            temperature: float = 0.0,
        ) -> None: ...

        def generate(self, prompt: str, schema: type[BaseModel]) -> BaseModel | None: ...

    Module-level constants:
        DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
        DEFAULT_MODEL = "anthropic/claude-sonnet-4.5"
        DEFAULT_API_KEY_ENV = "OPENROUTER_API_KEY"
        DEFAULT_MODEL_ENV = "KAIROS_LLM_MODEL"
        DEFAULT_BASE_URL_ENV = "OPENROUTER_BASE_URL"

Mocking strategy:
    ``openai`` is not a top-level dependency of kairos (lazy-imported in the
    LLMClient constructor). Tests inject a stand-in ``openai`` module into
    ``sys.modules`` so the client can be constructed without the real SDK.
    The stand-in uses ``MagicMock`` objects for OpenAI() plus synthetic
    exception classes that mirror the real ones the client catches.
"""

from __future__ import annotations

import json
import os
import sys
import types
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel

if TYPE_CHECKING:
    from collections.abc import Iterator

# ── Fake openai module injected into sys.modules ───────────────────────


class _FakeAPIError(Exception):
    """Base mirror of openai.APIError."""


class _FakeAPIConnectionError(_FakeAPIError):
    """Mirror of openai.APIConnectionError (transient)."""


class _FakeAPITimeoutError(_FakeAPIError):
    """Mirror of openai.APITimeoutError (transient)."""


class _FakeRateLimitError(_FakeAPIError):
    """Mirror of openai.RateLimitError (transient, should retry)."""


class _FakeInternalServerError(_FakeAPIError):
    """Mirror of openai.InternalServerError (transient)."""


class _FakeAuthenticationError(_FakeAPIError):
    """Mirror of openai.AuthenticationError (permanent, do not retry)."""


class _FakePermissionDeniedError(_FakeAPIError):
    """Mirror of openai.PermissionDeniedError (permanent, do not retry)."""


def _install_fake_openai() -> MagicMock:
    """Install a fake ``openai`` module into ``sys.modules`` and return the
    ``OpenAI`` constructor mock so tests can wire response behavior.
    """
    fake_openai = types.ModuleType("openai")
    openai_constructor = MagicMock(name="openai.OpenAI")
    fake_openai.OpenAI = openai_constructor  # type: ignore[attr-defined]
    fake_openai.APIError = _FakeAPIError  # type: ignore[attr-defined]
    fake_openai.APIConnectionError = _FakeAPIConnectionError  # type: ignore[attr-defined]
    fake_openai.APITimeoutError = _FakeAPITimeoutError  # type: ignore[attr-defined]
    fake_openai.RateLimitError = _FakeRateLimitError  # type: ignore[attr-defined]
    fake_openai.InternalServerError = _FakeInternalServerError  # type: ignore[attr-defined]
    fake_openai.AuthenticationError = _FakeAuthenticationError  # type: ignore[attr-defined]
    fake_openai.PermissionDeniedError = _FakePermissionDeniedError  # type: ignore[attr-defined]
    sys.modules["openai"] = fake_openai
    return openai_constructor


def _uninstall_fake_openai() -> None:
    sys.modules.pop("openai", None)
    # Drop any cached import of the target module so a fresh import picks up
    # either the fake openai or a missing-openai scenario.
    sys.modules.pop("kairos.analysis.llm_client", None)


@pytest.fixture
def fake_openai() -> Iterator[MagicMock]:
    """Install a fake ``openai`` module for the duration of a test."""
    constructor = _install_fake_openai()
    # Drop a stale llm_client import so the lazy import picks up the fake.
    sys.modules.pop("kairos.analysis.llm_client", None)
    try:
        yield constructor
    finally:
        _uninstall_fake_openai()


@pytest.fixture
def openrouter_api_key(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)
    monkeypatch.delenv("KAIROS_LLM_MODEL", raising=False)
    return "test-key"


# ── Helpers ────────────────────────────────────────────────────────────


class _HelloSchema(BaseModel):
    greeting: str


def _make_completion(content: str) -> MagicMock:
    completion = MagicMock()
    choice = MagicMock()
    choice.message.content = content
    completion.choices = [choice]
    return completion


def _wire_chat_completions(
    openai_constructor: MagicMock,
    *,
    side_effect: Any,
) -> MagicMock:
    """Make openai.OpenAI() return a client whose chat.completions.create
    is driven by ``side_effect`` (a list, single return, or exception type).
    """
    client = MagicMock()
    client.chat.completions.create.side_effect = side_effect
    openai_constructor.return_value = client
    return client


# ── TESTS ─────────────────────────────────────────────────────────────


class TestLLMClientInit:
    """Construction semantics: dependency check, env parsing, kwarg overrides."""

    def test_missing_api_key_raises_value_error(
        self,
        fake_openai: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        from kairos.analysis.llm_client import LLMClient

        with pytest.raises(ValueError) as excinfo:
            LLMClient()
        assert "OPENROUTER_API_KEY" in str(excinfo.value)

    def test_default_base_url_is_openrouter(
        self,
        fake_openai: MagicMock,
        openrouter_api_key: str,
    ) -> None:
        from kairos.analysis.llm_client import DEFAULT_BASE_URL, LLMClient

        _ = LLMClient()
        # openai.OpenAI(...) should have been constructed with the OpenRouter base_url
        assert fake_openai.call_count == 1
        kwargs = fake_openai.call_args.kwargs
        assert kwargs["base_url"] == DEFAULT_BASE_URL
        assert DEFAULT_BASE_URL == "https://openrouter.ai/api/v1"

    def test_env_overrides_apply(
        self,
        fake_openai: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "env-key")
        monkeypatch.setenv("OPENROUTER_BASE_URL", "https://example.com/v1")
        monkeypatch.setenv("KAIROS_LLM_MODEL", "env-model")

        from kairos.analysis.llm_client import LLMClient

        client = LLMClient()
        kwargs = fake_openai.call_args.kwargs
        assert kwargs["base_url"] == "https://example.com/v1"
        # The model is sticky on the client instance and should be the env value.
        # Implementation stores `model` on the instance; verify via attribute.
        assert getattr(client, "model", None) == "env-model"

    def test_explicit_kwargs_override_env(
        self,
        fake_openai: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "env-key")
        monkeypatch.setenv("KAIROS_LLM_MODEL", "env-model")

        from kairos.analysis.llm_client import LLMClient

        client = LLMClient(model="explicit-model", api_key="explicit-key")
        assert getattr(client, "model", None) == "explicit-model"
        # api_key was passed into the underlying OpenAI() constructor.
        assert fake_openai.call_args.kwargs["api_key"] == "explicit-key"

    def test_api_key_held_as_secret_and_not_leaked(
        self,
        fake_openai: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from pydantic import SecretStr

        monkeypatch.setenv("OPENROUTER_API_KEY", "super-secret-key")
        from kairos.analysis.llm_client import LLMClient

        client = LLMClient()
        # The credential is stored as a SecretStr, so repr/str never expose it.
        assert isinstance(client._api_key, SecretStr)
        assert "super-secret-key" not in repr(client._api_key)
        assert "super-secret-key" not in str(client._api_key)
        # The real value still reaches the OpenAI client unchanged.
        assert client._api_key.get_secret_value() == "super-secret-key"
        assert fake_openai.call_args.kwargs["api_key"] == "super-secret-key"

    def test_default_model_is_claude_sonnet_4_5(
        self,
        fake_openai: MagicMock,
        openrouter_api_key: str,
    ) -> None:
        from kairos.analysis.llm_client import DEFAULT_MODEL, LLMClient

        assert DEFAULT_MODEL == "anthropic/claude-sonnet-4.5"
        client = LLMClient()
        assert getattr(client, "model", None) == DEFAULT_MODEL


class TestLLMClientGenerate:
    """LLMClient.generate(prompt, schema) behavior under various server responses."""

    def test_generate_returns_parsed_schema_on_success(
        self,
        fake_openai: MagicMock,
        openrouter_api_key: str,
    ) -> None:
        completion = _make_completion(json.dumps({"greeting": "hello"}))
        _wire_chat_completions(fake_openai, side_effect=[completion])

        from kairos.analysis.llm_client import LLMClient

        client = LLMClient()
        result = client.generate("hi", _HelloSchema)

        assert isinstance(result, _HelloSchema)
        assert result.greeting == "hello"

    def test_generate_returns_none_when_schema_validation_fails_all_retries(
        self,
        fake_openai: MagicMock,
        openrouter_api_key: str,
    ) -> None:
        """Malformed JSON returned on every retry → generate returns None."""
        bad = _make_completion("not-json-at-all")
        # max_retries=3 → up to 3 attempts
        _wire_chat_completions(fake_openai, side_effect=[bad, bad, bad])

        from kairos.analysis.llm_client import LLMClient

        client = LLMClient(max_retries=3)
        with patch("time.sleep"):
            result = client.generate("hi", _HelloSchema)
        assert result is None

    def test_generate_retries_on_transient_error_with_backoff(
        self,
        fake_openai: MagicMock,
        openrouter_api_key: str,
    ) -> None:
        """Two APIConnectionError then success → 3 attempts → returns parsed schema."""
        good = _make_completion(json.dumps({"greeting": "ok"}))
        _wire_chat_completions(
            fake_openai,
            side_effect=[
                _FakeAPIConnectionError("boom"),
                _FakeAPIConnectionError("boom"),
                good,
            ],
        )

        from kairos.analysis.llm_client import LLMClient

        client = LLMClient(max_retries=3)
        with patch("time.sleep") as sleep_patch:
            result = client.generate("hi", _HelloSchema)
        assert isinstance(result, _HelloSchema)
        assert result.greeting == "ok"
        # Client should have made 3 API calls.
        api = fake_openai.return_value.chat.completions.create
        assert api.call_count == 3
        # And at least 2 sleeps between the 3 attempts.
        assert sleep_patch.call_count >= 2

    def test_generate_returns_none_on_auth_error_without_retry(
        self,
        fake_openai: MagicMock,
        openrouter_api_key: str,
    ) -> None:
        _wire_chat_completions(
            fake_openai,
            side_effect=[_FakeAuthenticationError("bad key")],
        )

        from kairos.analysis.llm_client import LLMClient

        client = LLMClient(max_retries=3)
        with patch("time.sleep") as sleep_patch:
            result = client.generate("hi", _HelloSchema)
        assert result is None
        api = fake_openai.return_value.chat.completions.create
        assert api.call_count == 1
        # No backoff sleep expected because we do not retry on auth errors.
        assert sleep_patch.call_count == 0

    def test_generate_retries_on_rate_limit(
        self,
        fake_openai: MagicMock,
        openrouter_api_key: str,
    ) -> None:
        """RateLimitError is transient → retry up to max_retries; success on 3rd try."""
        good = _make_completion(json.dumps({"greeting": "yo"}))
        _wire_chat_completions(
            fake_openai,
            side_effect=[
                _FakeRateLimitError("slow down"),
                _FakeRateLimitError("slow down"),
                good,
            ],
        )

        from kairos.analysis.llm_client import LLMClient

        client = LLMClient(max_retries=3)
        with patch("time.sleep"):
            result = client.generate("hi", _HelloSchema)
        assert isinstance(result, _HelloSchema)
        assert result.greeting == "yo"
        api = fake_openai.return_value.chat.completions.create
        assert api.call_count == 3

    def test_generate_exponential_backoff_delays(
        self,
        fake_openai: MagicMock,
        openrouter_api_key: str,
    ) -> None:
        """Between attempts the sleep delay doubles: 1s, 2s, 4s (plus jitter)."""
        _wire_chat_completions(
            fake_openai,
            side_effect=[
                _FakeAPIConnectionError("boom"),
                _FakeAPIConnectionError("boom"),
                _FakeAPIConnectionError("boom"),
            ],
        )

        from kairos.analysis.llm_client import LLMClient

        client = LLMClient(max_retries=3)
        with patch("time.sleep") as sleep_patch:
            result = client.generate("hi", _HelloSchema)
        assert result is None
        # 3 attempts => 2 sleeps (between attempts) OR 3 sleeps if the client
        # sleeps before each attempt except the first. Accept either; just
        # enforce the doubling pattern on observed sleep magnitudes.
        observed = [c.args[0] if c.args else c.kwargs.get("secs") for c in sleep_patch.call_args_list]
        assert len(observed) >= 2
        # Jitter adds ≤ 1s. Base delays: 1, 2, 4.
        # Verify doubling: each observed >= previous * 1.5 (absorbs jitter).
        # Also verify the first sleep is in [1.0, 2.0] band.
        assert 1.0 <= observed[0] <= 2.0
        if len(observed) >= 2:
            assert 2.0 <= observed[1] <= 3.1
        if len(observed) >= 3:
            assert 4.0 <= observed[2] <= 5.1

    def test_generate_passes_response_format_json_object(
        self,
        fake_openai: MagicMock,
        openrouter_api_key: str,
    ) -> None:
        completion = _make_completion(json.dumps({"greeting": "hi"}))
        _wire_chat_completions(fake_openai, side_effect=[completion])

        from kairos.analysis.llm_client import LLMClient

        client = LLMClient()
        _ = client.generate("prompt", _HelloSchema)

        api = fake_openai.return_value.chat.completions.create
        assert api.call_count == 1
        kwargs = api.call_args.kwargs
        assert kwargs.get("response_format") == {"type": "json_object"}
        # Also: messages carries the prompt in the user role.
        messages = kwargs.get("messages")
        assert isinstance(messages, list)
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "prompt"


class TestLLMClientRealCall:
    """End-to-end call to a real LLM provider. Gated on real_llm + API key."""

    @pytest.mark.real_llm
    def test_real_openrouter_call_returns_valid_schema(self) -> None:
        if not os.environ.get("OPENROUTER_API_KEY"):
            pytest.skip("OPENROUTER_API_KEY not set")
        # Make sure no stale fake openai is still installed.
        _uninstall_fake_openai()
        from kairos.analysis.llm_client import LLMClient

        client = LLMClient()
        result = client.generate(
            prompt='Return valid JSON that matches the schema {"greeting": string}. '
            'Respond with exactly {"greeting": "hello"} and nothing else.',
            schema=_HelloSchema,
        )
        assert result is not None
        assert isinstance(result, _HelloSchema)
        assert isinstance(result.greeting, str)
