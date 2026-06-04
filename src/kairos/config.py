"""Application configuration via environment variables.

Uses pydantic-settings for type-safe config with .env file support.

Usage:
    from kairos.config import settings
    print(settings.log_level)
"""

from pydantic import SecretStr
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

    # Logging
    log_level: str = "INFO"
    log_format: str = "json"  # "json" or "console"

    # Semantic recovery
    semantic_provider: str = "openrouter"
    semantic_model: str = "microsoft/phi-4-mini-instruct"
    semantic_temperature: float = 0.0
    semantic_timeout_s: float = 60.0
    semantic_openrouter_api_key: SecretStr | None = None

    # Detection thresholds (engine half)
    redundant_jaccard_threshold: float = 0.60
    loop_min_repeats: int = 3


settings = KairosSettings()
