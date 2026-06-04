# Contributing to Kairos AI

Thanks for your interest. Kairos is **one merged SDK**: live tracing + an
on-demand failure-clustering engine. One IR (`TraceEnvelope`), one path, no
fallbacks. Keep changes aligned with that shape.

## Development setup

Python 3.13, tooling via [`uv`](https://docs.astral.sh/uv/).

```bash
uv venv --python 3.13
uv pip install -e ".[dev,phoenix]"
```

Copy `.env.example` to `.env` for local config (it is gitignored). No secret is
required for `kairos analyze`; `OPENROUTER_API_KEY` is only needed for the
optional LLM semantic-decision pass.

## Before every commit

Run the full local gate (matches CI):

```bash
uv run ruff check src/ tests/ --fix
uv run ruff format src/ tests/
uv run mypy src/
uv run pytest -x --tb=short --cov=kairos --cov-report=term-missing
```

Coverage target: **80%**.

## Hard rules (non-negotiable)

These are enforced in review and partly by CI:

- **No inline imports.** All imports at module top (or under `TYPE_CHECKING`).
  Fix layering instead of importing inside a function to dodge a cycle.
- **No fallback loops.** No try-source-A-then-source-B chains. Sources are
  explicit at the call site.
- **No bare `except`** and no `except Exception:` to swallow. Catch the
  specific exception.
- **No silent degradation.** Fail loud. The only soft-fail is
  `normalize() -> is_valid=False` at the ingest boundary; the engine then
  explicitly filters invalid envelopes.
- **No secrets in the tree.** Use env vars; add only names to `.env.example`.
  `.env`, keys, and data dirs are gitignored.
- **Tests alongside implementation.** A feature is done when its tests pass.
  Tests mirror `src/` layout.

Some subsystems were deliberately dropped and must never reappear (intercept,
runtime correction, semantic recovery, HDBSCAN discovery clustering, reporting,
baselines). CI asserts their absence — see `tests/test_no_dropped_modules.py`.

## Pull requests

1. Branch from `main`.
2. Keep commits small and focused; use [Conventional Commits](https://www.conventionalcommits.org/)
   (`feat:`, `fix:`, `docs:`, `chore:`, `ci:`, `test:`).
3. Ensure the full local gate above is green.
4. Describe what changed and why; link the issue if there is one.

## Reporting bugs / requesting features

Open a GitHub issue with a minimal reproduction (a small transcript or
`TraceEnvelope` JSON plus the command you ran) and the observed vs expected
behavior.
