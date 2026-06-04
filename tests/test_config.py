"""Config tests — only live settings exist; dead semantic_* fields are gone."""

from __future__ import annotations

from typing import TYPE_CHECKING

from kairos.config import KairosSettings

if TYPE_CHECKING:
    import pytest


def test_live_fields_present_with_defaults() -> None:
    s = KairosSettings()
    assert s.log_level == "INFO"
    assert s.log_format == "json"
    assert s.redundant_jaccard_threshold == 0.60
    assert s.loop_min_repeats == 3


def test_dead_semantic_fields_removed() -> None:
    # The whole semantic_* config block was unused; nothing should read these.
    fields = set(KairosSettings.model_fields)
    assert not {f for f in fields if f.startswith("semantic_")}, f"dead semantic_* config reintroduced: {fields}"
    # The OpenRouter key lives as a SecretStr in analysis.llm_client, not here.
    assert "semantic_openrouter_api_key" not in fields


def test_env_prefix_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KAIROS_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("KAIROS_LOOP_MIN_REPEATS", "5")
    s = KairosSettings()
    assert s.log_level == "DEBUG"
    assert s.loop_min_repeats == 5
