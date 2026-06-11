# Gate Results — kairos-ai xer108-otel-preload (full branch vs main)

## Summary

PASS

## Results

| Gate | Status | Command | Notes |
|---|---|---|---|
| pytest | ✅ PASS | `uv run pytest -x -q` | 486 passed, 1 skipped (81.27s) — run on full branch tip |
| ruff | ✅ PASS | `uv run ruff check .` | All checks passed |
| mypy | ⚠️ PRE-EXISTING | `uv run mypy .` | 215 errors in 17 files — identical count on `main`; branch introduces zero new errors |
| gitleaks | ✅ PASS | `gitleaks detect --source . --no-git` | No leaks found, 4.19 MB scanned |
| semgrep | ✅ PASS | `semgrep --config=p/default .` | 0 findings, 475 rules, 81 files |
| osv-scanner | ✅ PASS | `osv-scanner --recursive .` | No issues found, 87 packages in scripts/package-lock.json |

## Blocking failures

None.

## Non-blocking warnings

- **mypy 215 errors**: Pre-existing on `main` (confirmed by cross-branch comparison at review time). Primary: `FakeSpan`/`_PhoenixSpan` type incompatibilities in `tests/readers/test_genai_mapping.py` — not in this branch's change set. Branch does not regress typecheck coverage.

## Raw logs

See `.mlo/command-output/` for stored gate outputs.
