# Kairos — WORKING DOC (read this first)

> Living anchor for the open-source agent-trace platform line. Purpose: orient a fresh / small-context
> session from ONE file. Decisions, principles, runbook, and what's next live here; deep detail lives in
> the linked docs. Keep this current; link out, don't duplicate.
> Last updated: 2026-06-18.

---

## 0. What Kairos is

Open-source **agent-trace observability + failure-clustering + eval** platform. "Open-source LangSmith **+
a failure-pattern/clustering brain that auto-writes regression evals and gates every change on your own
history.**" The clustering brain + closed eval loop is the moat; storage is deliberately conventional.

Repo: `github.com/akar5h/kairos-ai` (branch `main`, CI green). Local: `/Users/akarshgajbhiye/kairos-ai`.

---

## 1. Status (2026-06-18)

| Phase | State |
|---|---|
| **P1 — own ingestor** (OTLP sink + hooks + spans table + hook-enrichment, Phoenix retired) | ✅ merged to main, **live-validated on real data** |
| **P2 — API-first platform** (read+hierarchy+search API, light console UI, cluster browser, relabel, enrich-default) | ✅ merged to main, **pushed, CI green** |
| **P3.1–P3.4 — eval layer** (floor metrics GATE, cluster eval-sets, regression gate, issue lifecycle) | ✅ **SHIPPED, live-validated** — see `docs/honest-snapshot-3.md` |
| **P3.5** (trajectory diff gate + meta-eval MCC) | ✅ **SHIPPED** — `7ff4465`; 1439 tests green |
| Later | OSS carve (strip Paperclip), polish/hardening, P4 Self-Harness proposer |

`main` is pushed to origin and CI-green. Real session data flows. P3 eval layer is live on 609-entry corpus
(baseline: `known_bad_catch_rate=0.30`, `golden_trajectory_match_rate=1.0`, 73 cluster eval sets frozen).

---

## 2. Locked decisions

**Product / architecture**
- Single **Postgres + canonical IR** (immutable event log, materialized-path tree, JSONB+GIN). Storage is
  conventional ON PURPOSE; **engine-swappable** to ClickHouse only when a real query forces it. Moat =
  outcome/cluster **semantics**, not the store.
- **Source-blind IR.** Claude Code today (hooks + OTel + transcript); generic-OTLP seam for other agents;
  no second concrete source built yet.
- **Ingest = hybrid:** OTel = skeleton (timing, tokens, subagent tree); **hooks = flesh** (full redacted
  I/O + correct `is_error`); transcript = reconcile. **Kairos owns the OTLP sink; Phoenix retired.**
- Join hooks↔spans by **session.id + ordinal-per-tool-name + time-window** (CC spans carry NO `tool_use_id`
  — verified on 500 live spans; same approach as `transcript_join`).
- **Stack:** FastAPI backend (in-process with the Python brain — no cross-lang RPC) + Next.js/React front.
  API-first: the read API is the contract for UI + evals + external consumers.
- **UI:** light, **dense data-console** aesthetic (Honeycomb/Datadog-light, not airy SaaS). Searchable;
  **3-level hierarchy session → traces → spans**; raw spans visible, not just derived steps.
- **Clustering this line = operationalize** existing `discover.py` (surface + relabel + eval-per-cluster),
  not algorithm R&D (fast-follow).
- **OSS boundary:** ingest + clustering + UI + eval = generic; Paperclip coupling stripped/pluggable
  (carve is a later pass; audit gave exact file:lines).

**Eval (P3)** — see `docs/p3-eval-layer-design.md` for full design. Headlines:
- The OSS gap = closed loop **[real traces → verifier-grounded clusters → auto-generated regression evals →
  gate every change held-in+held-out → propose fix]**. Only Braintrust/Latitude (both CLOSED) approximate.
- **Deterministic-first; LLM judge is narrow and residual.**
- **enrich_hooks default ON** (hook-truth by default; raw is an explicit toggle).

---

## 3. Principles / best-practices we adopt

**Engineering discipline**
- Spec before code; feature-by-feature commits; tests alongside (Rule 0/1). Conventional commits, end with
  `Co-Authored-By: Claude Opus 4.8`.
- **Delegate grunt/exploration/code to Sonnet subagents; Opus stays plan/review/synthesis/decisions.**
- **Run tests WITH the DB:** `export KAIROS_PG_DSN=postgresql://kairos:kairos_dev_local@127.0.0.1:5434/kairos`
  — DB-gated tests SKIP without it and have masked real bugs (the `lineno` LogRecord crash lurked from F1.2
  until run with DSN). "Green" without DSN is a lie.
- **Parallel file-writing agents MUST use `isolation:"worktree"` or run sequentially.** Two committing agents
  in one checkout tangled branches once (commits landed on the wrong branch — recoverable but avoid).
- **CI contract (`.github/workflows/ci.yml`, all must pass before push):** `ruff check src/ tests/` **AND**
  `ruff format --check src/ tests/` (agents kept forgetting FORMAT — always run `ruff format` too), `mypy
  src/` (src only — tests/ mypy errors don't gate), `pytest -x --cov` (no DSN → DB tests skip).
- **Secret-test fixtures:** construct secret-shaped strings with `+` concat (`"xox" + "b-..."`), NOT
  adjacent literals (`"xox" "b-..."`) — ruff format FOLDS adjacent literals back into a contiguous token and
  re-trips GitHub push-protection. Never commit a real-shaped secret literal.
- **RSC boundary:** every interactive React component (handlers/hooks/browser APIs) needs `"use client"` —
  `npm run build`/lint do NOT catch the runtime 500. (Bit us twice.) Consider a render-smoke CI guard.

**Eval discipline** (the spine — full rationale in the P3 doc)
- **Verifier-grounded, not judge-grounded** — clusters/labels grounded in real outcome (`is_error`, contract
  completion), never an LLM's opinion.
- **Eval on history after every change — held-in (did it fix the target) + held-out (blast radius).**
- **Fixed evaluator = stable ruler** (k=2 determinism); attribute deltas to the change, not judge drift.
- **Reject → log, don't ship.** The eval is a gate, not advice.
- **Eval-the-evaluator** — auto-generated evals carry an agreement score (MCC/κ) vs reality; retire drifters.
- **LLM-judge hard rules:** never same model family as the agent; never score CoT/verbosity; rubric-decompose
  (no bare 1–5); never feed the agent's own CoT as evidence (~90% FP inflation); never use a judge where an
  oracle exists; PoLL (disjoint families) for *discovery* of new clusters only, not scoring; publish κ before
  any judge goes live.

---

## 4. What's on main (architecture map)

**Ingest / readers**
- `src/kairos/api/otlp.py` — OTLP/HTTP `/v1/traces` receiver → `persist_spans`. Kairos's OTLP sink.
- `src/kairos/ingest/spans.py` — `persist_spans` (writer to `spans` table; populates `session_id`).
- `hooks/kairos_hook.py` — CC hook (PostToolUse/Failure/SessionStart/End) → redact → spool `~/.kairos/spool`.
- `src/kairos/ingest/hook_uploader.py` — `drain_spool` → `hook_events` (idle/SessionEnd-gated).
- `src/kairos/readers/db.py` — `fetch_spans_from_db`, `fetch_envelope_from_db(... enrich_hooks=True default)`,
  `list_trace_ids`.
- `src/kairos/readers/hook_join.py` — enrich envelope steps from `hook_events` (session+ordinal+window).
- `src/kairos/readers/phoenix.py` — **KEEP** `spans_to_envelope` + `_PhoenixSpan*` primitives (PhoenixReader
  class removed; arize-phoenix-client only used by `corpus.py` snapshot now).

**API / UI**
- `src/kairos/api/app.py` — `create_app()`, mounts otlp + read routers, CORS for :3000.
- `src/kairos/api/read.py` — `/v1/traces`, `/v1/traces/{id}`, `/v1/traces/{id}/spans`, `/v1/sessions`,
  `/v1/sessions/{id}`, `/v1/clusters`, `/v1/clusters/{key}/traces`, `/v1/search`, `GET+POST /v1/labels`.
- `ui/` — Next.js light console: `/` sessions, `/sessions/[id]`, `/traces/[id]` (Raw Spans / Conversation /
  Step Timeline + enrich toggle + Labels tab), `/clusters`, `/clusters/[key]`, global search.

**Clustering / detectors / eval (existing, to extend in P3)**
- `src/kairos/loop/discover.py` — clusters: `cluster_key = tool_signature::dominant_feature`.
- `src/kairos/detection/` — D1–D4 session-quality + coordination-context classifier.
- `src/kairos/eval/` — three-tier gate (GATE/REVIEW/INFO), k=2 determinism, worktree `compare`, `eval_runs`.

**DB (migrations/)** — `spans`(0010), `hook_events`(0011), `discovery_queue.cluster_key`(0012),
`spans.session_id`(0013), `labels` nullable(0014), `eval_sets`(0015), `cluster_status`(0016),
`eval_sets.mcc`(0017). Plus sprint-1 tables. DB = `kairos-pg` docker on 127.0.0.1:5434.

---

## 5. Runbook (ops)

```bash
# DSN (always export for tests + scripts)
export KAIROS_PG_DSN=postgresql://kairos:kairos_dev_local@127.0.0.1:5434/kairos

# migrations
uv run python -c "from kairos.loop.db import apply_migrations, _dsn; apply_migrations(_dsn())"

# API + OTLP receiver (read API on :8000; OTLP POST /v1/traces same app)
uv run uvicorn kairos.api.app:create_app --factory --host 127.0.0.1 --port 8000

# UI (light console)
cd ui && NEXT_PUBLIC_KAIROS_API=http://localhost:8000 npm run dev   # :3000

# wire ALL Claude Code sessions into Kairos (edits ~/.claude/settings.json; backs up; reversible)
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:8000 bash scripts/install.sh   # :4318 taken by collector → override to :8000
bash scripts/uninstall.sh           # remove; KAIROS_HOOK_DISABLED=1 to skip a session

# drain hook spool → hook_events (run on a loop or after activity)
uv run python -c "import os; from pathlib import Path; from kairos.ingest.hook_uploader import drain_spool; print(drain_spool(Path.home()/'.kairos'/'spool', os.environ['KAIROS_PG_DSN']))"

# CI gate locally (all four, with DSN)
uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/ && uv run mypy src/ && uv run pytest -x -q
```
Ports: kairos-pg 5434 · collector 4318 (NOT Kairos) · Kairos API 8000 · UI 3000.

---

## 6. Future steps (priority order)

1. **P3 eval layer** (`docs/p3-eval-layer-design.md`) — phased:
   - P3.1 minimal deterministic auto-eval into the GATE (tool-call correctness, known-good/bad floors,
     outcome-grounded, golden-trace replay) — mostly wiring existing signals.
   - P3.2 cluster → held-in/held-out eval-set generation.
   - P3.3 regression-on-history gate (before/after over cluster corpus; blast-radius as GATE signal).
   - P3.4 issue lifecycle (cluster: open→eval→resolved→regressed) on the cluster browser.
   - P3.5 trajectory-diff gate + meta-eval (MCC). ✅ DONE
   - P4 (later) Self-Harness-style bounded-edit proposer, gated by the above.
2. **OSS carve** — strip/abstract Paperclip coupling (audit enumerated file:lines), README, one-command demo.
3. **Polish / hardening** — render-smoke CI guard for the `"use client"` class; OTLP drop-on-DB-down spool
   (currently returns 200 + drops on DB outage); UI density/affordances.

---

## 7. Open risks / gotchas

- OTel tool-content path (`OTEL_LOG_TOOL_CONTENT=1`) is **un-redacted** — only the hook path redacts. Safe
  for localhost; document before any remote endpoint.
- `envelope.metadata["session_id"]` propagates ONLY via the `kairos.task` root span — a trace captured
  without its root → hook enrichment silently no-ops (real CC always has a root).
- OTLP receiver returns 200 + **drops spans on DB-down** (no retry/spool, unlike hooks) — hardening TODO.
- A few 2026 arXiv IDs in the eval research weren't re-verified — confirm before external quoting.

---

## 8. Doc index

- `docs/WORKING.md` — **this file** (orientation, decisions, principles, runbook, next).
- `docs/p3-eval-layer-design.md` — full P3 eval design (competitive scan, LLM-judge, Self-Harness mapping,
  5-component arch, phased build).
- `docs/sprint-progress.md` / `docs/sprint-14day.md` — sprint-1 (self-improvement) resume + plan.
- `docs/system-audit-and-self-improvement-roadmap.md` — audit + post-sprint roadmap.
- `docs/config-guide.md` — correlation_key, OTel sources, environment.
- Owner memory (cross-session): `kairos-sprint2-oss-platform`, `kairos-sprint-docs`,
  `silent-failure-emitter-lie`, `feedback-delegate-coding-to-sonnet`.
