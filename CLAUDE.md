# Kairos AI — agent contributor rules

One merged SDK: **live tracing + on-demand failure-clustering engine**. One IR
(`TraceEnvelope`), one path, no fallbacks. No intercept / runtime-correction /
semantic-recovery. No UI. Open-source-ready is the exit bar.

## Hard rules (non-negotiable)

- **No inline imports.** All imports at module top (or under `TYPE_CHECKING`).
  Never `import` inside a function to dodge a cycle — fix the layering.
- **No fallback loops.** No try-source-A-then-source-B chains. Sources are
  explicit at the call site.
- **No bare `except`.** Catch the specific exception. Never `except:` or
  `except Exception:` to swallow.
- **No silent degradation.** Fail loud. The only soft-fail is
  `normalize() -> is_valid=False` at the ingest boundary; the engine then
  *explicitly* filters invalid envelopes — a typed, visible skip, not a hidden
  except.

## Build & environment

- Python 3.13. Package + tooling via `uv`.
- Install dev: `uv pip install -e ".[dev]"`
- Engine extras (LLM/CLI/offline) land with the engine port (M1.3).

## Code quality — run before every commit

```bash
ruff check src/ tests/ --fix
ruff format src/ tests/
mypy src/
pytest -x --tb=short
```

Coverage target: **80%**. `pytest --cov=kairos --cov-report=term-missing`

## Layout

```
src/kairos/
  models/        trace.py, enums.py            # the one IR
  ingest/        base.py, file.py, jsonl.py
  normalization/ events.py, live_normalizer.py, arg_normalizer.py, field_extractors.py
                 agents/  base.py, claude_code.py, codex.py, opencode.py, paperclip.py
  readers/       genai_mapping.py, phoenix.py
  store/         base.py, json_store.py
  engine/        pipeline.py                    # on-demand orchestrator (KairosEngine.analyze)
  analysis/      evidence_coverage, outcome_metric, reference_behavior, workflow_divergence,
                 decision_state, semantic_decision, workflow_membership, llm_client, sampler, correctness_score
  detection/     runner, loops, redundant, similarity, models
  taxonomy/      business_context.py, dfg.py
  config.py, log.py, cli.py
tests/                                          # mirrors src layout
```

## Conventions

- New Pydantic models → `models/`. Config values → `config.py` (one class).
- Tests mirror src: `src/kairos/readers/phoenix.py` → `tests/readers/test_phoenix.py`.
- Tests alongside implementation, not after.

## The single path (no fallback)

```
[source] → IR (TraceEnvelope) → KairosEngine.analyze(envelopes, business_context) → AnalysisResult(JSON)
```

Collection is always-on (OTel → Phoenix). Analysis is pull-based:
`KairosEngine.analyze` / `kairos analyze` CLI. No daemon.

## DROPPED — never reintroduce

`intercept/`, `runtime_correction/`, `semantic_recovery/`,
`models/semantic_recovery.py`, the HDBSCAN discovery clustering
(`clusterer/embedder/subclustering/health/input_cleaner/prefix_blocker/clustering-pipeline`),
`reporting/`, `analysis/demo_report`, `baselines/`. CI asserts these are absent.
