# Security & Edge-Case Review — kairos-ai xer108-otel-preload (full branch)

## Verdict: PASS

## Security checks

| Area | Finding |
|------|---------|
| Secrets / credentials | None. gitleaks clean. No env vars, tokens, or auth paths in diff. |
| Input validation | `--span-limit` CLI param is typed `int \| None`; passed directly to PhoenixReader. No user-controlled string interpolation. No injection surface. |
| Injection (SQL/shell/template) | None. |
| Serialization | `AnalysisView` Pydantic model changed (removed `llm_used`, `evidence_coverage`; added `reliability`, `summary`, `metric_descriptions`). Breaking change to any consumer serializing/deserializing the old schema — but this is an internal SDK, no external contract to break. |
| Data exposure | No new data paths. Phoenix reads are read-only. |
| npm security | GHSA-q7rr-3cgh-j5r3 (CVSS 7.5 HIGH) **patched** by OTel 0.57.x → 0.218.x upgrade. Net positive. |
| CORS / rate limiting | None changed. |
| Auth / payment | None touched. |

## Edge cases

| Scenario | Handled? |
|----------|----------|
| `span_limit=0` passed via CLI | Passes to PhoenixReader; Phoenix will return 0 spans → empty envelope. Caller gets empty analysis. Acceptable (user explicitly requested). |
| `enable_divergence=False` (default) | Divergences list is `[]`. WorkflowView renders correctly with empty divergence. Tested. |
| Zero eligible traces for p75 | `step_counts` is empty → `min(int(0.75 * 0), -1)` = -1. This is an off-by-one: `step_counts[-1]` would raise IndexError on empty list. **However**: p75 is only computed when `reference_traces` is non-empty (it's derived from `reference_traces`), so this path can't be reached with an empty list. Low risk. |
| Mode tie-breaking with empty eligible | `_select_reference_traces([])` returns `[]` (early return guard). Safe. |
| `seq_counts` with single item | `max()` on a 1-item Counter works. Returns that item. |
| Phoenix span_limit hit at 100k | Warning logged, analysis continues on truncated trace. Downstream analysis may be partial but won't crash. |
| Callers passing `llm_client` to `run_pipeline` | `llm_client: object \| None` accepted and ignored. No exception. Backward compat preserved. |

## Human review triggers (from policy)

| Trigger | Status |
|---------|--------|
| Auth logic changed | NO |
| Payment logic changed | NO |
| User data access changed | NO |
| Database schema changed | NO |
| Environment/config changed | YES — `PAPERCLIP_OTEL_HTTP_SPANS` env gate. Low risk: controls instrumentation only. |
| Logging around sensitive data | NO |
| CORS / rate limiting | NO |
| Background job / retry | NO |
| External API contract | MINOR — `AnalysisView` schema changed. Internal-only, no external consumers confirmed. |

## Recommendation

PASS with two low-severity notes:
1. Verify no external consumers rely on the removed `llm_used` / `evidence_coverage` fields in `AnalysisView`.
2. Verify Phoenix instance handles 100k span requests without OOM on large traces.
