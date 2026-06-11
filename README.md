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
# Live (primary): pull a Xero trace from Phoenix on demand, analyze it.
kairos analyze --phoenix <trace_ids> --context <business_context.yaml>

# Offline backfill: a raw agent transcript (historical / non-instrumentable run).
kairos analyze --transcript <session.jsonl> --agent claude_code --context <business_context.yaml>

# Offline: a directory of already-normalized IR JSON files.
kairos analyze --normalized-dir <dir> --context <business_context.yaml>
```

Sources are mutually exclusive — pick exactly one, no fallback chain. Analysis
is pull-based and **never spends LLM credits per run**: the CLI skips the
semantic decision pass (`llm_used: false`); pass an `LLMClient` to
`KairosEngine.analyze` in-process if you want it.

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

### Offline backfill: analyze one past Claude Code session

The transcript adapters are the **offline backfill** path — for historical runs
and engines we can't instrument live. One command normalizes the raw transcript
and analyzes it (no intermediate store, no LLM spend):

```bash
kairos analyze \
  --transcript ~/.claude/projects/<proj>/<session>.jsonl \
  --agent claude_code \
  --context business_context.yaml
```

`--agent` selects the adapter: `claude_code`, `codex`, or `paperclip` (each
reads a single JSONL transcript). Discover Claude Code sessions with
`ClaudeCodeNormalizer.discover_sessions()`.

OpenCode stores a session across many files rather than one JSONL, so it is
driven via its Python API instead of `--transcript`:

```python
from kairos.normalization.agents import OpenCodeNormalizer
from kairos.engine import KairosEngine
from kairos.taxonomy.business_context import BusinessContext

envelope = OpenCodeNormalizer().normalize_session(OpenCodeNormalizer.discover_sessions()[-1])
result = KairosEngine().analyze([envelope], BusinessContext.from_yaml("business_context.yaml"))
```

To stage many transcripts for one analysis run, normalize each to a
`JSONStore` directory and point `--normalized-dir` at it.

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

## Trace topology

```
Paperclip node process
  │ OTLP :4317 / :4318
  ▼
otel-collector  (Docker container: deploy-otel-collector-1)
  │ forwards to :4319
  ▼
Phoenix         (Docker container: deploy-phoenix-1, UI :6006)
  project "default"
  │ GraphQL / span fetch
  ▼
kairos view CLI  →  AnalysisView JSON
```

- **OTel collector** listens on `:4317` (gRPC) and `:4318` (HTTP/protobuf), forwards to Phoenix on `:4319`.
- **Phoenix UI / GraphQL** at <http://localhost:6006>, project `"default"`.
- **Archived SQLite** at `~/.phoenix/phoenix-taubench-archive-2026-05.db` — May 2026 tau-bench traces kept as eval-corpus raw material (Day 6 of the sprint). This is **not** the live store; the live store lives inside the `deploy-phoenix-1` container.

## License

MIT — see [LICENSE](LICENSE).
