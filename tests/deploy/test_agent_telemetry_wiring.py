"""XER-69 Phase B — live-emit wiring contract for Paperclip Claude Code agents.

The emit side is pure OTel env wiring (Claude Code native tracer); Kairos is not
imported on the agent hot path. These tests lock that contract:

1. the telemetry env example carries the keys that enable traces-only OTLP emit;
2. the documented provenance attributes mirror ``PaperclipNormalizer`` run_context
   (so the live path and the offline transcript path agree on provenance);
3. grep gate — the emit-side artifacts import zero ``kairos``.
"""

from __future__ import annotations

import re
from pathlib import Path

from kairos.normalization.agents.paperclip import _META_KEYS

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENV_EXAMPLE = _REPO_ROOT / "deploy" / "paperclip-agent-otel.env.example"
_WIRING_DOC = _REPO_ROOT / "deploy" / "agent-telemetry.md"

# Emit-side artifacts: anything an agent reads/runs to emit. None may pull Kairos.
_EMIT_ARTIFACTS = (_ENV_EXAMPLE, _WIRING_DOC)


def _env_keys(text: str) -> set[str]:
    return {m.group(1) for m in re.finditer(r"^([A-Z][A-Z0-9_]*)=", text, re.MULTILINE)}


def test_env_example_enables_traces_only_otlp_emit() -> None:
    keys = _env_keys(_ENV_EXAMPLE.read_text())
    # Native tracer on + OTLP traces to the deploy backend.
    assert {
        "CLAUDE_CODE_ENABLE_TELEMETRY",
        "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA",
        "OTEL_TRACES_EXPORTER",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_PROTOCOL",
        "OTEL_RESOURCE_ATTRIBUTES",
    } <= keys
    text = _ENV_EXAMPLE.read_text()
    assert "CLAUDE_CODE_ENABLE_TELEMETRY=1" in text
    # Emission gate is `enable && enhanced-beta` — the enable flag alone emits
    # zero spans (XER-73). Both must be wired or the patch is a no-op.
    assert "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1" in text
    assert "OTEL_TRACES_EXPORTER=otlp" in text
    # Traces only: metrics/logs exporters explicitly off (deploy has a traces
    # pipeline only).
    assert "OTEL_METRICS_EXPORTER=none" in text
    assert "OTEL_LOGS_EXPORTER=none" in text


def test_provenance_attrs_mirror_paperclip_run_context() -> None:
    """Every PaperclipNormalizer run_context key has a documented
    ``paperclip.<key>`` live resource attribute — no drift between the live emit
    path and the offline transcript adapter."""
    doc = _WIRING_DOC.read_text()
    documented = set(re.findall(r"paperclip\.([a-z_]+)", doc))
    expected = set(_META_KEYS)  # run_id, issue, company_id, agent_id, project_id
    missing = expected - documented
    assert not missing, f"run_context keys not documented as paperclip.* attrs: {missing}"


def test_static_provenance_present_in_env_example() -> None:
    """company_id + agent_id are static (config-time) and must be wired in the
    env example; service.name carries agent identity."""
    text = _ENV_EXAMPLE.read_text()
    assert "service.name=paperclip-claude-" in text
    assert "paperclip.company_id=" in text
    assert "paperclip.agent_id=" in text


def test_emit_path_imports_zero_kairos() -> None:
    """Grep gate: the agent emit path pulls in no Kairos. Emit is vendor-neutral
    OTel; Kairos enters only at read time."""
    # Match a real `kairos` module import (kairos followed by space/dot/end),
    # not the hyphenated repo name "kairos-ai" in prose.
    pattern = re.compile(r"(?:import|from)\s+kairos(?=[\s.]|$)", re.MULTILINE)
    for artifact in _EMIT_ARTIFACTS:
        body = artifact.read_text()
        assert not pattern.search(body), f"{artifact.name} references a kairos import on the emit path"
