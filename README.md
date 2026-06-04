# Kairos AI

Agent tracing SDK + on-demand failure-clustering engine. **One IR
(`TraceEnvelope`), one path, no fallbacks.** Collection is always-on
(OTel → Phoenix); analysis is pull-based via `KairosEngine.analyze`.

> Status: Phase 1 (SDK + engine) and Phase 2 (agent transcript adapters)
> complete; Phase 3 hardening (end-to-end across all adapters, scalability
> review, security pass) done. See `CLAUDE.md` for layout and hard rules,
> `docs/phase3-review.md` for the public-readiness review.

## Install

```bash
uv pip install -e ".[dev]"        # core + dev tooling
uv pip install -e ".[phoenix]"    # + Phoenix reader (live source)
```

Python 3.13.

## The single path

```
[source] → TraceEnvelope (IR) → KairosEngine.analyze(envelopes, business_context) → AnalysisResult (JSON)
```

- **Live source:** host emits OTel spans → Phoenix; `PhoenixReader` pulls them
  back to IR.
- **Offline source:** Langfuse JSONL export or per-trace JSON files via the
  `ingest/` ingestors.
- **Transcript agents (Phase 2):** Claude Code / Codex / OpenCode / Paperclip
  transcripts → IR via `normalization/agents/` adapters. Each captures every
  tool call (args + result), model turn, error, and timing the transcript
  exposes.

## CLI

```bash
kairos analyze --phoenix <trace_ids>
kairos analyze --normalized-dir <dir> --context <business_context.yaml>
```

## Wiring an ongoing agent session (Phase 2)

Each coding agent's native transcript normalizes to the **same** `TraceEnvelope`
and flows through `KairosEngine.analyze` unchanged. Wiring a live session is one
adapter call, then `kairos analyze`.

The adapters (`kairos.normalization.agents`):

| Agent | Adapter | Reads |
| --- | --- | --- |
| Claude Code | `ClaudeCodeNormalizer` | `~/.claude/projects/**/*.jsonl` |
| Codex CLI | `CodexNormalizer` | `~/.codex/sessions/**/rollout-*.jsonl` |
| OpenCode | `OpenCodeNormalizer` | `~/.local/share/opencode/storage/{message,part}/<ses>/…` |
| Paperclip | `PaperclipNormalizer` | wraps any of the above + run/issue provenance |

Each adapter captures everything the transcript exposes — every model turn,
every tool call with full args + result, errors, and timing.

### Example: analyze a Claude Code session

```python
from kairos.normalization.agents import ClaudeCodeNormalizer
from kairos.store.json_store import JSONStore

# 1. Normalize the live session transcript → IR.
adapter = ClaudeCodeNormalizer()
sessions = ClaudeCodeNormalizer.discover_sessions()      # ~/.claude/projects/**/*.jsonl
envelope = adapter.normalize_jsonl(sessions[-1])

# 2. Persist the IR where `kairos analyze` reads it.
JSONStore("traces/").save(envelope)
```

```bash
# 3. Analyze (offline source) against a business context.
kairos analyze --normalized-dir traces/ --context business_context.yaml
```

### Business context without hand-YAML (Paperclip agents)

For Paperclip agents, derive the workflow taxonomy from the agent's MCP/tool
catalog instead of writing YAML per agent:

```python
from kairos.taxonomy.tool_catalog import business_context_from_tool_catalog
from kairos.engine import KairosEngine

context = business_context_from_tool_catalog(
    agent_name="paperclip-coder",
    agent_description="Paperclip coding agent",
    tools=["Read", "Bash", "Edit", "mcp__paperclip__create_issue"],
)
result = KairosEngine().analyze([envelope], context)   # in-process, no CLI
```

OpenCode uses `OpenCodeNormalizer().normalize_session(session_id)`; Codex uses
`CodexNormalizer().normalize_jsonl(rollout_path)`; Paperclip wraps an inner
adapter: `PaperclipNormalizer(run_context={"run_id": ..., "issue": ...})`.

## Development

```bash
ruff check src/ tests/ --fix && ruff format src/ tests/
mypy src/
pytest -x --tb=short --cov=kairos --cov-report=term-missing
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, the pre-commit gate, and the
non-negotiable hard rules.

## License

MIT — see [LICENSE](LICENSE).
