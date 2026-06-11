# Diff Audit — kairos-ai xer108-otel-preload (full branch vs main, 10 commits)

## Summary

Full Kairos rebuild: Phase 2-4 lean-ification, phoenix pagination fix, views improvements (XER-169), OTel security patch. 31 files, 2177 insertions, 898 deletions.

## Files changed

| File | Change type | Reason | Risk |
|---|---|---|---|
| `config/context.yaml` | added | Multi-label context definition covering lead pipeline + coding agent ops | low |
| `config/paperclip_agents_context.yaml` | added | Separate context for coding agents (ClaudeCoder/CTO) | low |
| `deploy/agent-telemetry.md` | deleted | Stale doc, superseded by config files | low |
| `deploy/paperclip-agent-otel.env.example` | deleted | Superseded by scripts/.env flow | low |
| `scripts/package.json` | modified | OTel 0.57.x → 0.218.x (GHSA-q7rr-3cgh-j5r3 patch) | low (security fix) |
| `scripts/package-lock.json` | modified | Regenerated for above dep upgrade | low |
| `scripts/paperclip-otel-preload.mjs` | modified | Gate HTTP/Express/PG instrumentations behind `PAPERCLIP_OTEL_HTTP_SPANS=1` env var | low |
| `src/kairos/analysis/reference_behavior.py` | modified | Replace numpy efficiency model with mode-sequence selection; remove numpy dep | medium |
| `src/kairos/cli.py` | modified | Add `--span-limit` flag; E501/mypy fixes | low |
| `src/kairos/detection/loops.py` | modified | Detection logic improvements | medium |
| `src/kairos/engine/pipeline.py` | modified | Rename `run_week1_pipeline`→`run_pipeline`; remove semantic pass; fix O(N²) loop; add preflight check | medium |
| `src/kairos/readers/phoenix.py` | modified | Raise span limit 1000→100,000; warn instead of raise at limit | medium |
| `src/kairos/taxonomy/utils.py` | added | `required_tool_coverage` utility | low |
| `src/kairos/views/__init__.py` | modified | Export new view types | low |
| `src/kairos/views/analysis_view.py` | modified | Remove semantic/evidence models; add AnalysisSummary, METRIC_DESCRIPTIONS, zero-trace filter, finding_count, max_severity | medium |
| `tests/**` | modified | Tests updated to match new APIs; 486 passing | low |
| `.mlo/**` | modified | Verifier report updates | info |

## Behavior changed

- **Phoenix span limit**: 1000 → 100,000 per trace. Traces no longer hard-fail at 1001 spans; instead a warning is logged. Behavioral change in production phoenix reads — loosens a previous hard guard.
- **Reference trace selection**: numpy efficiency model replaced by mode-sequence selection. Analysis results may differ for sparse cohorts.
- **Workflow view**: zero-trace workflows filtered from output (lead-gen ops with no activity now omitted). Frontend sees fewer rows.
- **Semantic pass removed**: `llm_client` param deprecated and ignored; `AnalysisResult.llm_used` removed.

## Blast radius

- Modules touched: analysis, cli, detection, engine, readers, taxonomy, views
- External services: Phoenix (span pagination behavior change)
- Data models: `AnalysisResult` schema changed (removed `llm_used`, `evidence_coverage`; added `reliability`)
- Auth/payment/user-data: none
- DB schema: none
- Config/env: `PAPERCLIP_OTEL_HTTP_SPANS` env var gate added

## Suspicious areas

- `phoenix.py:245` — span limit warning instead of error: if a trace genuinely truncates at 100k spans, analysis is silently partial. Acceptable given prior hard limit of 1000 was too conservative.
- `reference_behavior.py` mode tie-breaking: `max(seq_counts, key=lambda k: (seq_counts[k], -len(k), tuple(-ord(c) for c in "".join(k))))` — complex tie-breaker. Has dedicated test coverage; logic is deterministic.

## Human inspection required

| File | Reason |
|---|---|
| `src/kairos/readers/phoenix.py` | Span limit relaxation — confirm 100k default is safe for your Phoenix instance memory |
| `src/kairos/engine/pipeline.py` | Semantic pass removal is irreversible — confirm no callers depend on `llm_used` field |
