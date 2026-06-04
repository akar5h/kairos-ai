"""Application configuration via environment variables.

Uses pydantic-settings for type-safe config with .env file support.

Usage:
    from kairos.config import settings
    setup_logging(level=settings.log_level, json_output=settings.log_format == "json")

Only values something actually reads live here (one config class, per
CLAUDE.md). The OpenRouter API key is resolved in ``analysis.llm_client`` and
held there as a ``SecretStr``, at the single point it is used.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class KairosSettings(BaseSettings):
    """Kairos SDK configuration. All values read from env vars prefixed KAIROS_."""

    model_config = SettingsConfigDict(
        env_prefix="KAIROS_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Logging — consumed by the CLI via kairos.log.setup_logging.
    log_level: str = "INFO"
    log_format: str = "json"  # "json" or "console"

    # Detection thresholds — consumed by detection.runner.
    redundant_jaccard_threshold: float = 0.60
    loop_min_repeats: int = 3


settings = KairosSettings()
