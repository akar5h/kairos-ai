# Kairos Sprint — Living Progress Doc (resume after compaction)

*Updated 2026-06-14. Canonical resume doc. Read this FIRST. Companions: `sprint-14day.md` (mother plan), `sprint-exec-1/2/3-*.md` (execution specs; exec-3 is v2 — deterministic-first, single-Postgres, eval-harness-spine), `insight-report-0.md` (coordination-waste baseline), `system-audit-and-self-improvement-roadmap.md` (findings F1–F10). Approved Day 12–14 plan: `~/.claude/plans/sunny-scribbling-brooks.md`.*

---

## TL;DR — where we are

14-day Kairos self-improvement sprint. **Phases 1 & 2 CLOSED. Phase 3 (Days 8–14) nearly done.** Days 8–13 complete and Fable-verified. **Day 14 is in progress and BLOCKED ON THE OWNER: the haywire-restart label session.**

**Right now:** the haywire review app is running at **http://localhost:8502** (`QUEUE_PATH=eval/review/haywire_queue.json uv run streamlit run eval/review/app.py --server.headless true --server.port 8502`). 41 restart traces await owner labels (haywire vs benign). After labels land → build `detect_haywire_restart` → ship it THROUGH the eval harness (before/after k=2, blast-radius) → write `docs/case-study-1.md`. Then the sprint is done.

---

## The thesis (reframed — read this, it changed mid-sprint)

Kairos = deterministic AI-agent trace observability. The sprint proves Kairos **improves ITSELF**, not the agents it watches. The loop that closes: **discovery → owner label → ship a deterministic detector → eval-gated → measured**. Kairos *surfaces* agent problems with evidence (e.g. "CTO wastes 68% on coordination"); whether anyone fixes the agent is the user's governed decision and is OUT OF SCOPE. Two-layer claim: (1) Kairos works as a product (truthful evidence-backed findings on real traffic), (2) Kairos improves itself (governed flywheel raises its own detection quality, measured against ground truth, eval-gated).

## Locked decisions (chronological, all owner-confirmed)

1. **Deterministic-first.** Exhaust deterministic ROI before any LLM. The **LLM judge is DEFERRED** to post-sprint (Appendix A of exec-3) — it's the only stage with circular-validation risk; build it once deterministic detectors plateau, with a measured error bar.
2. **Phase-1 outcome = contract completion**, not session quality (owner option (a), Day 5). Code Implementation outcome_rate ~1.00 is a known property, not a bug; session quality is the tier-1.5 detectors' job.
3. **Single Postgres source** (`kairos-pg`), NO ClickHouse (premature; one store kills the scatter). ClickHouse = roadmap swap at OLAP scale.
4. **Generalize, don't couple:** `correlation_key` (optional, documented span attr) for unit-of-work rollup. Paperclip → `paperclip.issue`; standalone Claude → `session.id`; chat → `thread_id`. Nesting: issue ⊃ session ⊃ trace ⊃ span; the key picks the level. Engine is source-blind via the `TraceEnvelope` IR firewall (two ingestion families exist: OTel reader `readers/phoenix.py` + native normalizers `normalization/agents/*`).
5. **Expectations LEARNED, not declared** (no mandatory_tools config burden). Discovery surfaces a near-universal tool's absence; owner labels once.
6. **Agent intervention CUT.** The original "coordination diet" (rewrite Paperclip CTO's AGENTS.md) was a category error — fixing the observed system, not Kairos.
7. **The EVAL HARNESS is the spine** (owner, Day 13). No Kairos change ships without a stored, repeated (k=2), before/after eval that catches BLAST RADIUS across the full metric panel. Agent-sandbox eval (run the agent k times before/after a suggestion, tau-bench-style) = documented immediate next extension, not built this sprint.
8. **Flywheel iteration-1 targets the haywire-restart class** (owner-named twice in labels). Nightly runner built but **launchd cron deferred** (traffic frozen ~2026-06-11). **UI deferred** (backend first; the Day-11 dashboard covers minimal viewing meanwhile).

## STANDING DISCIPLINE (critical — keep doing this)

**Executors ESTIMATE precision/coverage instead of measuring it. Fable MUST live-run every quality/coverage/eval claim before accepting.** Caught fictional/vacuous numbers ~5×: Day-6 inquiry-op, Day-8 initial precision, Day-8 post-fix precision, **Day-13 harness was VACUOUS** (ran detectors only on tau traces, skipped all live/owner traces), Day-13 gate cried wolf on volume. Also: executors leave red tests calling them "pre-existing" after partial-suite runs (Day-14 caplog test). Operating model: Sonnet executors implement from a spec section (spec is law, ambiguity→STOP, tests alongside, conventional commits, scoped `git add` — NEVER `-A`); Fable reviews every diff + one live verification; fix-or-dispatch on any deviation.

---

## Phase status

**Phase 1 (Truth, Days 1–5): CLOSED.** Honest measurement on live Paperclip traces. Key fixes: outcome evidence ladder (`d51584d`), `side_effect_match` any/all (`30090a9`), excluded_tools + context.yaml rewrite (`9e5697d`). Spot-check gate closed via owner option (a).

**Phase 2 (Trust, Days 6–7): CLOSED.**
- Day 6 tau-bench agreement (`5f2b7a9`/`b65f8f7`): κ=0.169 — STRUCTURAL ceiling (67 wrong-args-success FPs + 7 read-only FNs, observationally equivalent to deterministic layer). Surviving claim: **Kairos FAIL = 0.806 precision** ("FAIL = trustworthy alarm; PASS = contract done, semantics unverified"). Inquiry-op fix measured + REJECTED (tuning log).
- Day 7: owner labeled 15 live traces in the new Streamlit review app → #1 finding **silent failures masked as success**. **Bug 1** (`4c30a62`): OTel emitter stamps `success=true` on `is_error` tool_results (rejected Edit/Write); `transcript_join.py` corrects phantom-OK steps on the live path. 53 steps / 28 traces corrected, **0 outcome verdicts flipped** — proving contract-completion is blind to session quality (→ motivates Day-8 detectors). Durable fix is emitter-side (flagged for roadmap).

**Phase 3 (Loop, Days 8–14): Days 8–13 DONE, Day 14 in progress.**

- **Day 8 — session-quality detectors** (`2a84b81`, `9975ecf`, `59ac322`). Final slate: **D2 struggle_ratio `warning`; D1 unrecovered_error / D3 coordination_waste / D4 work_to_talk `info`.** The flywheel turned twice during validation: (a) Fable live-measurement caught the executor's estimated precision was fiction; (b) ALL detectors were reading EMPTY span args (F10) → fixed by wiring `transcript_join` real redacted args into the reader (`9975ecf`). After fix: D2/D3 fixed (no longer false-fire on clean traces); **D1 hit the deterministic ceiling** (~0.62 — can't separate "error that mattered" from "benign error agent moved past"; both show later same-tool recovery; SEMANTIC → owner chose D1→`info`, judge stays deferred). LEARN stage = per-workflow tool presence → expectation-miss candidates. Full trail: `eval/reports/session-quality-precision.md`. **Scope guard:** `outcome_metric.py` / `pipeline.py` recovery logic UNTOUCHED (that's deferred "Bug 2").
- **Day 9 — correlation_key rollup** (`72a052b`): generic unit-of-work grouping, `last-wins` outcome. Fable live-verified on real issues (db929b4f F,F,F,F,T→PASS; 812db371 T×6,F→FAIL): 65 issue-traces → 40 units / 5 multi-trace. `config/context.yaml` has `correlation_key: paperclip.issue`. `docs/config-guide.md` documents it + surfaces/levels. FLAG: last-wins flips a mostly-passing issue to FAIL on a late failing trace — ledger-resolved-status is the cleaner future signal (noted, not blocking).
- **Day 10 — Postgres persist + backfill** (`d600c5c` store foundation, `a927012` ingest). `kairos-pg` (localhost:5434), 5 tables. Backfill: 4 nights (2026-06-08..11) / 345 traces / 196 findings / 43 rollup rows; idempotent; redaction STRUCTURAL (findings table has no text column).
- **Day 11 — delta engine + minimal dashboard** (`3a45e27`) + 3 rollup honesty fixes (Fable-flagged, verified live): unmapped outcome_rate→NULL (was fake-0), `coordination_waste_rate`→`coordination_waste_per_trace` (was a count mislabeled as a rate), agents bucketed (claudecoder/cto/qaengineer/other/unknown; UUIDs collapsed — no deterministic name map).
- **Day 12 — discovery + nightly runner** (`6e1d967`): `discover.py` (anomaly + expectation-miss → discovery_queue: 108 candidates / 55 clusters; computes restart + post_restart_rework features) + `nightly_loop.py` (deterministic state machine, kill-switch `KAIROS_LOOP_DISABLED` + skip-marker verified, no cron, no LLM). HAYWIRE SPARSITY: 41 traces have restarts, only 2 have strict post-restart re-work → haywire likely thin/null.
- **Day 13 — THE EVAL HARNESS** (`a94b487` → `a9b2cd2` non-vacuous fix → `b29ce73` three-tier gate). `src/kairos/eval/`: corpus (507 entries: 161 tau + 20 spotcheck + 15 answers + 311 live; **raw-span snapshot** so each git ref normalizes the SAME fixed input via ITS OWN code; `corpus_hash 72fb137f`) + 25-metric panel + `compare(before,after,k)` + **three-tier gate** + `eval_runs` append-only store. Tiers: **GATE** = grounded quality (outcome owner_precision/recall, tau_kappa, tau_fail_precision/recall) — drop>0.01 = REGRESSED; **REVIEW** = per-detector precision/recall (surfaced, doesn't fail gate); **INFO** = fire_count/fire_rate/severity/total (diagnostic, never a regression). Retro-validated LIVE: args-fix `3d6a702→9975ecf` **PASS** (struggle fires 78→24 = F10 false-positive suppression in INFO; unrecovered_error precision 0.538→0.583 improved; outcome/tau flat); Bug-1 `aead64a→4c30a62` **PASS**. k=2 nondeterminism check passes. **Already surfaced a REVIEW signal for the owner: `coordination_waste.recall 1.0→0.5`** across the args boundary. THE GAP IS CLOSED.
- **Day 14 — haywire flywheel (IN PROGRESS, OWNER-BLOCKED).** Review queue built (`b7d37c2`): `eval/review/build_haywire_queue.py` → `eval/review/haywire_queue.json`, 41 restart traces, restart points + post-restart steps highlighted with redacted transcript digests, secret-grep clean, app boots (200). Caplog test brittleness fixed (`63486df`). **PENDING: owner labels at http://localhost:8502.**

## What's LEFT (post-label, the remaining sprint)

1. **Owner labels the 41 haywire traces** (http://localhost:8502 → answers.jsonl, `class: haywire`).
2. **Build `detect_haywire_restart`** (new `src/kairos/detection/haywire.py` or extend session_quality.py): signature = session-restart boundary + post-restart re-work (redacted-arg match / re-read already-read files) and/or ≥N restarts/unit. Reuse `_find_session_restart_indices` + transcript args. Validate precision on the owner labels (Fable live-measures).
3. **Ship it THROUGH the harness**: `scripts/eval_run.py compare <pre-haywire-ref> <haywire-ref> --k 2` → prove haywire precision ≥0.7 on its labels AND no GATE-tier regression. Severity = earned (≥0.7 `warning`, else `info`/cut — nothing dormant). Add haywire to the corpus labels + panel.
4. **`docs/case-study-1.md`**: class Kairos missed Day 8 → discovery surfaced → owner labeled → detector shipped → eval shows targeted-up + no blast radius + curve moved. Two-layer claim. **Null-result ships under the same template** (haywire <0.7 → demoted; harness + governance still proven). Expect thin/null given 2/41 strict re-work.
5. **Dashboard eval-history panel** (small, deferred from Day 11): read `eval_runs` → Kairos's own quality trajectory. Optional polish.
6. Minor: `scripts/` not in ruff scope (60 pre-existing T201 prints in eval_run.py — out of scope); launchd cron one-line follow-up when trace collection resumes.

## Roadmap (post-sprint, documented)

- LLM-judge tier-2 (exec-3 Appendix A) — when deterministic plateaus; first job = D1's "did this error matter" + the tau wrong-args class.
- Agent-sandbox eval (tau-agent substrate) — run the agent k times before/after a suggestion.
- Emitter-side fix for the is_error→success lie (Bug 1's durable fix).
- Kairos owns ingest (OTel sink off the existing collector) → owns store + view, Phoenix retired; ClickHouse at scale.
- Dedicated eval UI (after backend proven + real eval_runs history).
- Ledger-resolved-status as the unit outcome signal (cleaner than last-wins).

## Environment facts (verified, save rediscovery)

- `kairos-pg` = `deploy-kairos-pg-1` localhost:**5434**, db `kairos`, user `kairos`. Tables: findings, nightly_rollup, labels, expectations, discovery_queue, eval_runs, schema_migrations. Connect: `docker exec deploy-kairos-pg-1 psql -U kairos -d kairos`. DSN via env `KAIROS_PG_DSN`.
- Live Phoenix = `deploy-phoenix-1` :6006, project `default` (node id `UHJvamVjdDox`). OTel collector `deploy-otel-collector-1` fans out to Phoenix + Jaeger. **Traffic FROZEN ~2026-06-11** (owner fixing collection separately) → eval/flywheel run on the fixed 4-night/345-trace corpus.
- Live spans: `claude_code.tool` (top-level `tool_name`, `success` on child `tool.execution`), no args/outputs (F10) → `transcript_join.py` enriches redacted args from `~/.claude/projects/*/<session.id>.jsonl`. `paperclip.issue`/`service.name` (agent id) on spans.
- tau-bench ground truth: `~/tau-agent/results/ablation_bundles/*.json`. `eval/corpus/taubench/`.
- Owner labels: `docs/spotcheck-day4.md` (20, handwritten — NEVER overwrite) + `eval/review/answers.jsonl` (15 + haywire). Raw eval span snapshots git-ignored (29M, regenerable from manifest).
- `Xero/config/context.yaml` is a SYMLINK to `kairos-ai/config/context.yaml`.

## Commit ledger (kairos-ai `main`, newest first)

`63486df` test caplog fix · `b7d37c2` Day-14 haywire queue · `cd9ec0e` docs · `b29ce73` three-tier gate · `a9b2cd2` harness non-vacuous · `a94b487` Day-13 eval harness · `6b3f722` docs · `6e1d967` Day-12 discovery+runner · `3a45e27` Day-11 delta+dashboard · `a927012`/`d600c5c` Day-10 persist+store · `72a052b` Day-9 rollup · `02e58f2`/`59ac322`/`9975ecf`/`2a84b81` Day-8 detectors · `3d6a702` disagreement queue · `bbf441d` precision report · `bc7354c`/`c948edd` Phase-3 reframe · `4c30a62` Bug-1 · `aead64a`/`d713287` Day-7 review app · `b65f8f7`/`5f2b7a9` Day-6 tau · earlier = Days 1–5.

Suite: **1058 passed / 28 skipped**, ruff + mypy clean on `src tests`.
