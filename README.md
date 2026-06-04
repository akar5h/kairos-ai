# Kairos AI

Agent tracing SDK + on-demand failure-clustering engine. **One IR
(`TraceEnvelope`), one path, no fallbacks.** Collection is always-on
(OTel → Phoenix); analysis is pull-based via `KairosEngine.analyze`.

> Status: Phase 1 scaffold. Tracing half and engine half are ported in
> subsequent milestones (see `CLAUDE.md` for layout and hard rules).

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

## CLI (after engine port — M1.3)

```bash
kairos analyze --phoenix <trace_ids>
kairos analyze --normalized-dir <dir> --context <business_context.yaml>
```

## Wiring an ongoing agent session (Phase 2)

One adapter call + `kairos analyze`. See per-adapter docs once landed.

## Development

```bash
ruff check src/ tests/ --fix && ruff format src/ tests/
mypy src/
pytest -x --tb=short --cov=kairos --cov-report=term-missing
```

## License

MIT — see [LICENSE](LICENSE).
