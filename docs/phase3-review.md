# Phase 3 — end-to-end hardening + scalability review

Status of the public-readiness gate for `kairos-ai`. Evidence captured during
the Phase 3 pass on top of `main` (Phase 1 SDK/engine + Phase 2 adapters).

## 1. End-to-end run — all adapters → engine

Every transcript adapter folds to the **same** `TraceEnvelope` and flows through
`KairosEngine.analyze` unchanged. Verified by driving all four adapters into one
`JSONStore`, then running the real `kairos analyze` CLI over the mixed store:

| Adapter      | source tag    | steps | valid | tools captured            |
|--------------|---------------|-------|-------|---------------------------|
| Claude Code  | `claude_code` | 5     | yes   | `Bash`, `Read`            |
| Codex        | `codex`       | 5     | yes   | `exec_command`, `apply_patch` |
| OpenCode     | `opencode`    | 4     | yes   | `read`, `edit`            |
| Paperclip    | `paperclip`   | 5     | yes   | `Bash`, `Read` (+ run provenance) |

`kairos analyze --normalized-dir <store> --context <ctx>` exits 0 and emits a
well-formed `AnalysisResult`. Each adapter additionally has a per-adapter
engine round-trip test under `tests/normalization/agents/`.

**Note on context matching.** Findings are produced only when the business
context's workflow taxonomy matches the traces' tools. For coding-agent traces,
derive the context from the agent's tool catalog
(`taxonomy.tool_catalog.business_context_from_tool_catalog`) rather than the
CRM-shaped fixture context. With a mismatched context the engine correctly
reports `unmapped` instead of inventing workflows — expected behavior, not a
bug.

## 2. Scalability review (evidence-driven; no premature infra)

### Store: keep `JSONStore` — do **not** add `MongoStore` yet
- Analysis is **pull-based and bounded**: a run loads a finite set of traces
  (one Phoenix trace, or a normalized dir) into memory once, analyzes, exits.
  There is no daemon, no concurrent-writer contention, no online query load.
- `JSONStore` (one file per trace) satisfies the whole `TraceStore` ABC
  (`save`/`load`/`list_ids`/`count`). The ABC already exists, so a `MongoStore`
  is a drop-in later **if** evidence demands it (10k+ traces per run,
  concurrent ingest, server-side filtering). None of that is in evidence today.
- **Decision: keep `JSONStore`.** Revisit only when a real workload shows
  filesystem listing/loading as the bottleneck.

### Phoenix span-limit handling — fixed this phase
- `PhoenixReader.fetch_envelope` previously fetched a fixed 1000-span page and
  built the envelope from whatever returned; a >1000-span trace was silently
  truncated.
- Now: `span_limit` is configurable (default 1000) and `fetch_envelope` **fails
  loud** when the returned count reaches the limit (can't distinguish "exactly
  N" from "clipped at N"). Matches the repo's no-silent-degradation rule.

### Batch sizes — bounded, no change needed
- Engine holds the run's envelopes in memory: O(traces) — fine for pull-based,
  bounded runs.
- Semantic (LLM) pass is explicitly capped: `DEFAULT_SEMANTIC_TOP_PATTERNS=3` ×
  `DEFAULT_SEMANTIC_PER_PATTERN=5` ⇒ ≤15 LLM calls per analysis, and is skipped
  entirely unless a caller passes an `LLMClient`. No unbounded fan-out.

## 3. Anti-slop findings folded in this phase
- Removed the dead `semantic_*` config block (provider/model/temperature/
  timeout + a `SecretStr` key) — nothing read it.
- `setup_logging` + `log_level`/`log_format` existed but were never called;
  the CLI now wires them, so they are live and CLI-reachable.
- Library entrypoints (`tool_catalog`, the four agent adapters) are reachable as
  the documented public API (README), not dead — kept.

## 4. Security posture (repo → public)
- No secrets in the working tree or in git history (scanned all blobs).
- `.gitignore` covers `.env`, `.env.*` (keeps `.env.example`), `*.pem`, `*.key`,
  `secrets/`, `credentials*.json`, data/artifact dirs.
- OpenRouter API key held as a `SecretStr` at its single use site
  (`analysis.llm_client`); never stored plaintext on the client, never logged.
- Fixtures are synthetic coding-agent transcripts — no real emails/PII.
- `.env.example` documents every env var the code reads (names only).
- MIT `LICENSE`, `README.md`, `CONTRIBUTING.md` present.

## 5. Remaining (board / owner gate)
- Board/CTO sign-off on the mandatory anti-slop + security gate.
- Flip `github.com/akar5h/kairos-ai` to **public** (irreversible, owner action).
