"""LLM client for bounded decision-state analysis.

Wraps the OpenAI SDK but points at OpenRouter so customers can pick any
Anthropic / OpenAI / open-source model via a single API key.

Defaults:
  - base_url: https://openrouter.ai/api/v1
  - model:    anthropic/claude-sonnet-4.5
  - api key:  $OPENROUTER_API_KEY

Retries transient errors (connection, rate limit, 5xx) with exponential
backoff + jitter. Does not retry auth errors. Returns None on exhaustion
so the caller can synthesize an insufficient_evidence finding.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time

import openai
from pydantic import BaseModel, SecretStr, ValidationError

from kairos.log import get_logger

# Match leading/trailing markdown fences like ```json ... ``` or ``` ... ```.
# Some OpenRouter-hosted models wrap JSON output in fences even with
# response_format={"type":"json_object"}.
_MARKDOWN_FENCE_RE = re.compile(
    r"^\s*```(?:json|JSON)?\s*\n?(?P<body>.*?)\n?```\s*$",
    re.DOTALL,
)


def _strip_markdown_fences(content: str) -> str:
    """Unwrap a JSON payload from ```json ... ``` if the model fenced it."""
    stripped = content.strip()
    match = _MARKDOWN_FENCE_RE.match(stripped)
    if match is None:
        return stripped
    return match.group("body").strip()


logger = get_logger(__name__)

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "anthropic/claude-sonnet-4.5"
DEFAULT_API_KEY_ENV = "OPENROUTER_API_KEY"
DEFAULT_MODEL_ENV = "KAIROS_LLM_MODEL"
DEFAULT_BASE_URL_ENV = "OPENROUTER_BASE_URL"
DEFAULT_TIMEOUT_S = 45.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_MAX_OUTPUT_TOKENS = 1200
DEFAULT_TEMPERATURE = 0.0


class LLMClient:
    """Thin wrapper over the OpenAI SDK with retries and schema validation."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
    ) -> None:
        resolved_key = api_key or os.environ.get(DEFAULT_API_KEY_ENV)
        if not resolved_key:
            msg = f"{DEFAULT_API_KEY_ENV} env var not set, or pass api_key=... to LLMClient()."
            raise ValueError(msg)
        # Hold the credential as a SecretStr so it never leaks via repr/logs;
        # unwrap only when handing it to the OpenAI client below.
        self._api_key = SecretStr(resolved_key)

        self.model: str = model or os.environ.get(DEFAULT_MODEL_ENV) or DEFAULT_MODEL
        resolved_url = base_url or os.environ.get(DEFAULT_BASE_URL_ENV) or DEFAULT_BASE_URL
        self._timeout_s = timeout_s
        self._max_retries = max_retries
        self._max_output_tokens = max_output_tokens
        self._temperature = temperature
        self._openai_module = openai
        self._client = openai.OpenAI(
            api_key=self._api_key.get_secret_value(),
            base_url=resolved_url,
            timeout=timeout_s,
        )

    def generate(self, prompt: str, schema: type[BaseModel]) -> BaseModel | None:
        """Send prompt, parse JSON response, validate against schema.

        Returns the parsed schema instance on success; None when all retries
        are exhausted or responses keep failing schema validation.
        """
        openai = self._openai_module
        last_error: str | None = None

        for attempt in range(1, self._max_retries + 1):
            # Sleep BEFORE attempts 2+ so transient failures back off before retry.
            if attempt > 1:
                base_delay = 2 ** (attempt - 2)
                # Cryptographically-random jitter in [0, 1) avoids S311.
                jitter = secrets.randbelow(1000) / 1000.0
                time.sleep(base_delay + jitter)

            try:
                response = self._client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=self._max_output_tokens,
                    temperature=self._temperature,
                    response_format={"type": "json_object"},
                )
            except openai.AuthenticationError as auth_err:
                logger.error("llm_client.auth_failed", error=str(auth_err))
                return None
            except openai.PermissionDeniedError as perm_err:
                logger.error("llm_client.permission_denied", error=str(perm_err))
                return None
            except (
                openai.APIConnectionError,
                openai.APITimeoutError,
                openai.RateLimitError,
                openai.InternalServerError,
            ) as transient_err:
                last_error = f"transient: {transient_err}"
                logger.warning(
                    "llm_client.transient_error",
                    attempt=attempt,
                    error=str(transient_err)[:200],
                )
                continue

            content = response.choices[0].message.content or ""
            cleaned = _strip_markdown_fences(content)
            try:
                return schema.model_validate_json(cleaned)
            except (ValidationError, json.JSONDecodeError) as parse_err:
                last_error = f"schema_validation: {parse_err}"
                logger.warning(
                    "llm_client.parse_failed",
                    attempt=attempt,
                    error=str(parse_err)[:200],
                )
                continue

        logger.error(
            "llm_client.retries_exhausted",
            max_retries=self._max_retries,
            last_error=last_error,
        )
        return None
