# Sprint Progress — living continuation doc

*Updated: 2026-06-13 (Phase 2 closed, Phase 3 reframed — about to start Day 8). Purpose: full state capture so any session (human or agent) can resume gracefully. Companion docs: `sprint-14day.md` (mother plan), `sprint-exec-1/2/3-*.md` (execution specs; exec-3 is v2 — deterministic-first, single-Postgres, Kairos-self-improvement), `insight-report-0.md` (baseline), `system-audit-and-self-improvement-roadmap.md` (findings F1–F10).*

---

## Where we are

**Phase 1 (Truth, Days 1–5): CLOSED 2026-06-13, owner decision (a).** Exit gate ran once (11Y/7N/2?), root cause = all-of side-effect bug, fixed (`30090a9`), rerun flipped all 7 disputed rows. 5 owner-confirmed fails also flipped (loop/bad-recovery class — outcome formula measures contract completion, not session quality). Owner accepted option (a): those 5 are tier-2 detector targets (Days 8–14), outcome metric scoped to contract completion. Code Implementation outcome_rate now 1.00 — discrimination for coding sessions must come from tier-2 mechanisms; recorded as known property, not a bug.

**Phase 2 (Trust, Days 6–7): IN PROGRESS.**
- **Day 6 DONE** (`5f2b7a9`, `c226576`, `b65f8f7`): 161/162 tau-bench pairs, 0% abstention. κ=0.1692 — gate (0.7) failed STRUCTURALLY, not buggy: 67 FPs = wrong-args successes (tool OK, kwargs wrong — invisible to contract completion), 7 FNs = read-only tasks (write-never-performed vs read-only-done-right observationally equivalent at deterministic layer). Inquiry-op fix measured and REJECTED (would zero the FAIL detector: κ→0.0, 29 collateral flips); tuning log in eval/reports/taubench-agreement.md §Rework. Surviving deterministic claim: **FAIL verdicts 0.806 precision / 0.302 recall** — "Kairos FAIL = trustworthy alarm; Kairos PASS = contract done, semantics unverified." Semantic gap formally routed to tier-2 judge (Days 8–14) — now backed by TWO independent ground truths (owner spot-check Y-rows + tau rewards) converging on the same completion≠correctness boundary.
- **Day 7 DONE:** owner labeled 15 live traces in the new Streamlit review app (`eval/review/`, transcript-sourced step digests, 99.5% args coverage, secret-grep clean). Surfaced the #1 finding: **silent failures masked as success**. Split into 3 mechanisms; owner chose to fix Bug 1 deterministically (others → roadmap/learned). Bug 1 fix shipped (`4c30a62`): `transcript_join.py` corrects phantom-OK tool steps (`is_error=true`) on the live-Phoenix path — 53 steps / 28 traces corrected, **0 outcome verdicts flipped** (rejected tools were retried successfully). That zero IS the finding: contract-completion is structurally blind to session quality → motivates the Day-8 deterministic session-quality detectors. Labels persisted in `eval/review/answers.jsonl` = flywheel seed corpus.

**Phase 3 (Loop, Days 8–14): REFRAMED 2026-06-13 (two owner strategy reviews). Spec rewritten: `sprint-exec-3-loop.md` v2.** Four locked decisions:
1. **Deterministic-first** — exhaust deterministic ROI before any LLM judge (judge DEFERRED to Appendix A / post-sprint).
2. **Single Postgres source** — no ClickHouse (premature; we have Postgres, no OLAP-scale need; one store kills the scatter). ClickHouse = roadmap swap, portable schema.
3. **Generalize** — `correlation_key` (optional, documented) for unit-of-work rollup; expectations LEARNED not declared (no mandatory_tools config burden — discovery surfaces a near-universal tool's absence, owner labels once).
4. **Kairos improves ITSELF, not the agents it observes.** The Day-13 "coordination diet" (rewriting Paperclip's CTO agent) was a category error and is CUT. The loop closes on Kairos's own detection quality via the discovery→label→detector flywheel. Kairos *surfaces* agent waste (a valid product output); fixing the agent is the user's governed call, out of scope.

**Operating model:** Sonnet executor agents implement from spec sections; Fable (this thread) reviews every diff, runs live verification, dispatches fixes. Five review catches so far — all measurement-layer bugs that would have poisoned the loop's reward signal:
1. F10 loop guard per-trace instead of per-run (fixed `f9edfee`)
2. Rung 3 adapter hook defined but never invoked — dead code (fixed `aa1f56d`)
3. Outcome condition 4 demanded readable output → pass structurally impossible on live spans (fixed `d5272c4`, plus emitter discovery: parent tool spans always status OK; truth is on `tool.execution` child's `success` attr, now propagated)
4. Spotcheck evaluated unmapped traces against operations[0] → vacuous fail flood (fixed `ef8108c`, by Fable directly)
5. **DONE (main thread, Day 5/6 boundary):** `required_side_effect_tools` was all-of in the engine; Code Implementation's [Edit, Write] needed any-of. Owner labels caught it (every N row = Edit-only or Write-only trace failing `missing_side_effect`). Implemented per the plan below — all 3 call sites + 14 tests (643 total green, ruff/mypy clean). Rerun in `docs/spotcheck-day4-rerun.md`: all 7 owner-disputed N rows flip to pass ✓. **BUT** 5 owner-CONFIRMED fails (Y rows: 8f078036, 79043f7e, b1c3f027, 5eee0136, f07e36e3) ALSO flip to pass, and Code Implementation outcome_rate goes 0.42 → 1.00. Old engine was right-for-wrong-reason on those 5: owner judged them bad for looping / haywire-restart behavior, which the 4-condition outcome formula cannot see (they complete, an Edit succeeds somewhere). Re-tally: 14/20 clear agreement + 1 leaning-agree (?) + 5 disagreements all of ONE class = bad-session-despite-side-effect. Gate ≥18/20 NOT mechanically cleared — owner decision pending: either (a) accept outcome = "contract completion" and route session-quality to tier-2 detectors (Days 8–14; bad-recovery mechanism already named by owner), or (b) hold Phase 1 open until a session-quality condition exists. Recommendation: (a) — the 5 flips are the loop/bad-recovery class the sprint's tier-2 phase is FOR. Original plan kept for record:
   - Schema: `BusinessOperation.side_effect_match: Literal["all","any"] = "all"` (`taxonomy/business_context.py`); `config/context.yaml` Code Implementation gets `side_effect_match: any`.
   - Call site 1 — membership FULL vs ATTEMPTED (`engine/pipeline.py` classify_membership): "any" → FULL when ≥1 required tool succeeded.
   - Call site 2 — outcome condition 4 (`analysis/outcome_metric.py` `_side_effect_result`): "any" three-valued logic → pass if ≥1 tool satisfied; non-computable if none satisfied but ≥1 tool's evidence unknown; else fail (SIDE_EFFECT_OUTPUT_FAILED if a succeeded-but-text-contradicted tool exists, else MISSING_SIDE_EFFECT).
   - **Call site 3 (executor's catch)** — the condition-2 coverage gate (`outcome_metric.py:352-361`) requires required-tool coverage == 1.0 and fails Edit-only traces BEFORE condition 4 runs; under "any" mode skip this gate (condition 4 handles none-succeeded with better evidence).
   - Tests: any-mode Edit-only → PASS, Write-only → PASS, neither → fail missing_side_effect; all-mode tau fixtures unchanged; membership FULL under any-mode.
   - Regenerate: new run to `docs/spotcheck-day4-rerun.md` (NEVER overwrite spotcheck-day4.md — owner's handwritten labels), `honest-snapshot-1.md` as rev 2. Check the 7 disputed traces flip: f788bf6a34304376, bd0ce91137f0f343, 96d0f15c010f64bb, a851f9c219fcad64, 03969588096b5b35, 425764d1beab6b2f, a3bc546c39899e73. Then re-tally owner labels → expect ≥18/20 equivalent → Phase 1 closed.

## Commit ledger (kairos-ai `main`)

| Commit | What |
|---|---|
| `813895f` | Day 1.3: observed_tools micro-lint + archive stale `~/.phoenix/phoenix.db` → `phoenix-taubench-archive-2026-05.db` + README trace topology |
| `ebeb4ce` | Day 1.2: AnalysisMeta, fail-loud zero-op context, null reliability on empty runs |
| `f8dfd22` | Day 2: token usage ladder (live keys: top-level `input_tokens`/`output_tokens`/`cache_read_tokens`), F10 guards; live verify 99.8% instrumented |
| `f9edfee` | F10 loop guard per-run (review fix) |
| `d51584d` | Day 3: outcome evidence ladder (kairos.outcome → otel/success attr → adapter hook → tail-anchored textual w/ negation mask), `status_source`, `failure_reason` enum, HUMAN_ESCALATION mapping (conservative session-end rule), `human_escalation_rate` |
| `aa1f56d` | Rung 3 wiring fix: `apply_step_outcomes` called from transcript normalizers + phoenix live path |
| `1464c04` | Docs suite committed (plans, specs, insight-report-0); duplicate audit doc dropped |
| `e4a920b` | Day 4: orphan-span integrity gate (partial → non-computable), `OutcomeRow` per-trace verdicts in view, spotcheck export script |
| `d5272c4` | Condition-4 fix: structured status satisfies side-effect check; `tool.execution` child `success` propagated to parent |
| `9e5697d` | Day 5: `excluded_tools` schema, context.yaml rewrite (4 coding ops; Bash dropped as distinctive; Coordination = Skill; lead ops → `config/context.lead-pipeline.yaml.disabled`), primary-label finding dedup (tier-1 once per trace), honest_snapshot script |
| `ef8108c` | Spotcheck: unmapped traces get no verdict (Fable direct fix) |
| `514d068` | Phoenix deep links use project NODE id (`UHJvamVjdDox`), not name — name URLs 404 in Phoenix 15.x UI |
| `4a1bd7c` | Spotcheck rows carry redacted transcript digests (spans are skeletons — F10; humans judge from digest) |
| `30090a9` | side_effect_match any/all fix (3 call sites + 14 tests); rerun flipped all 7 disputed N rows |
| `e639050` | Owner Day-4 spot-check labels committed (11Y/7N/2?) |
| `5f2b7a9`, `c226576`, `b65f8f7` | Day 6 tau-bench agreement harness; κ=0.1692 (structural ceiling), FAIL precision 0.806; inquiry-op measured+rejected (tuning log) |
| `d713287`, `aead64a` | Day 7 review app (`eval/review/`) + transcript-join step digests (99.5% args coverage; fixed a real Bearer-token redaction bug) |
| `4c30a62` | Bug 1: `transcript_join.py` corrects phantom-OK tool steps (is_error=true) on live-Phoenix path; 53 steps / 28 traces; 0 verdicts flipped (the finding). TESTBED SCOPE — durable fix is emitter-side (success=false on is_error). |
| `c948edd` → *(this update)* | Phase 3 spec revised: deterministic-first, single-Postgres, correlation_key, learned-expectations, Kairos-self-improvement (agent intervention cut), judge deferred |

Plugin repo (`Xero/kairos-analysis-views`, own git): `b30e7e2` (fail-loud guard, meta/null types) → `c630d60` (outcome rows table) → `8529e0e` (is_primary/secondary count types).

## Current honest numbers (live Phoenix, 345 traces / 7d, post-fixes)

- mean memberships/trace **0.69** (exit bar ≤1.5 ✓; was ~3 before Day 5)
- unmapped **187 (54%)** — Bash-only coordination-curl sessions (the insight-report-0 waste); Kairos SURFACES this, does not fix it
- outcome rates (contract completion): Code Impl **1.00**, Research 1.00, Orchestration 0.88, Coordination 1.00 — high because contract-completion is blind to session quality (the Day-8 detectors add that dimension; this is a known, recorded property)
- Day 6 cross-check: vs tau-bench rewards κ=0.17 (structural ceiling), but **FAIL-verdict precision 0.806** — "Kairos FAIL = trustworthy alarm; PASS = contract done, semantics unverified"
- token instrumentation 99.8%; cache excluded from waste (one trace: 14k real vs 5.16M cache-read — would have inflated 360×)

## Owner spot-check labels (docs/spotcheck-day4.md — PRESERVE, has handwritten comments)

11Y / 7N / 2?. N-cluster = the any-of bug (rows with Edit/Write in last tools). Genuine leftovers:
- `ea9692b9…` N: "was reading Slack docs, not implementation" — membership gray zone, feeds Day 7 labeling.
- Owner-named failure mechanism (multiple rows): **"once the shell terminates, the agent runs haywire / restarts from a stale session without asking, decides from scratch"** — bad-recovery pattern; no tier-1 detector exists; candidate tier-2 mechanism class + intervention #2. Carry into Day 11 pattern store design.

## Known environment facts (verified, save re-discovery)

- Live Phoenix = Docker `deploy-phoenix-1` :6006, project `default` (node id `UHJvamVjdDox`); collector :4317/4318 → :4319. `~/.phoenix/phoenix-taubench-archive-2026-05.db` = May tau-bench corpus for Day 6.
- Live spans: `claude_code.tool` (top-level `tool_name`, `success` on child `tool.execution`), `claude_code.llm_request` (top-level token keys), NO args/outputs on any span (F10) → transcript digests required for human/LLM judgment; transcripts at `~/.claude/projects/*/<session.id>.jsonl`, `session.id` on span attrs. `paperclip.issue`/`run_id` on TOOL spans (absent on root interaction spans — Day 11 join must read tool spans).
- `Xero/config/context.yaml` is a SYMLINK to `kairos-ai/config/context.yaml`.
- tau-bench ground truth: `~/tau-agent/results/ablation_bundles/*.json` (`modes[].checkpoint_rows[].reward` + `kairos_run_dir`).
- Infra running (verified `docker ps`): `deploy-phoenix-1` :6006 (traces), `deploy-otel-collector-1` (OTel ingest, fans out to Phoenix + Jaeger), `ledger-pg` :5432, `infra-cos-postgres` :5433 — **Postgres already available; no new store needed.** Roadmap: Kairos becomes an OTel sink off the existing collector → owns store + view, Phoenix retired.
- Paperclip source `~/dev/paperclip` (MIT) ships `packages/mcp-server` — relevant only as context for the coordination-waste Kairos SURFACES; wiring it is the user's agent-fix decision, OUT OF KAIROS SCOPE.
- `scripts/` is outside kairos-ai ruff lint paths. `scripts/node_modules/` untracked, needs .gitignore line.

## Immediate next steps (in order)

- **DONE — single-source store** (`d600c5c`): dedicated `kairos-pg` container (localhost:5434), 5 tables + migration runner. Ingest/backfill pending (Day 10, needs detectors).
- **DONE — Day 8 detectors** (`2a84b81`, `9975ecf`, `59ac322`): D1–D4 + LEARN stage. **The flywheel turned twice during validation:** (1) Fable live-measurement caught the executor's *estimated* precision was fiction; (2) preparing the owner re-label surfaced that all detectors read EMPTY span args (F10) → fixed by wiring `transcript_join` real (redacted) args into the reader (`9975ecf`). Post-fix measured: **D2 FIXED** (1.0 precision, was false-firing on clean), **D3 FIXED**, **D1 hit the deterministic ceiling** (~0.62 — can't separate "error that mattered" from "benign error agent moved past"; both show later same-tool recovery; SEMANTIC, the empirical LLM-judge trigger). Owner decision: **D1→info, judge stays deferred.** Final slate: **D2 `warning`, D1/D3/D4 `info`.** Detectors NOT yet wired into the loop. Full trail: `eval/reports/session-quality-precision.md`.
- **DONE — Day 9 correlation_key rollup** (`72a052b`): generic unit-of-work grouping (engine never references "paperclip.issue"; reads configured attr). `correlation_key: paperclip.issue` set in context.yaml. Fable live-verified last-wins on real issues (db929b4f F,F,F,F,T→PASS; 812db371 T×6,F→FAIL): 65 issue-traces → 40 units / 5 multi-trace over 168h. Config guide + surfaces/levels doc written (`docs/config-guide.md`). FLAG: last-wins flips a mostly-passing issue to FAIL on a late failing trace — defensible (final state) but ledger-resolved-status is the cleaner future signal (noted, not blocking).
- **DONE — Day 10 Postgres persist + backfill** (`a927012`): `persist.py` + `scripts/backfill.py`; 345 traces / 4 nights / 196 findings (110 traces) / 43 rollup rows; idempotent (3 runs identical); redaction STRUCTURAL (findings table has no text column — only evidence_steps int[]). Fable live-verified row counts + severities (D1 info, D2/redundant warning, D3 info+warning, D4 info). **3 honesty issues found in nightly_rollup → folded into Day 11:** (a) unmapped outcome_rate=0 misrepresents no-verdict as all-fail (must be null); (b) coordination_waste_rate shows >1 (it's a per-trace count, not a 0-1 rate — mislabeled); (c) agent identity fragmented (most traffic under paperclip-claude-<UUID>, not cto/claudecoder/qaengineer — per-agent view noisy until resolved).
- **DONE — Day 11** delta engine + minimal dashboard (`3a45e27`); 3 rollup honesty fixes verified live (unmapped→NULL, coordination_waste_per_trace renamed, agents bucketed).
- **DONE — Day 12 discovery + nightly runner** (`6e1d967`): `discover.py` (anomaly + expectation-miss → discovery_queue: 108 candidates / 55 clusters) + `nightly_loop.py` (deterministic, kill-switch + skip-marker verified, no cron, no LLM). HAYWIRE-SPARSITY FLAG for Day 14: 41 traces have restarts but only 2 have strict post-restart re-work → haywire detector must use the broader signal (≥N restarts + struggle), label the 41 restart traces; likely a thin/null result (fine — Day 14 dogfoods the eval harness regardless).
- **REPRIORITIZED (owner): the EVAL HARNESS is the spine.** Gap: no stored, k-run, before/after eval catching BLAST RADIUS. **Day 13 = eval harness** (`src/kairos/eval/`: fixed corpus + full metric panel + before/after×k=2 + blast-radius gate + eval_runs store + retro-validate Bug-1/args-fix). **Day 14 = haywire flywheel dogfooding the harness** (discovery→owner label→detector→ship THROUGH harness→case-study). Agent-sandbox eval = documented next extension. UI deferred (backend first). Plan: `~/.claude/plans/sunny-scribbling-brooks.md`.
- **Config documentation** (owner-flagged): the Kairos config guide must state what `correlation_key`, operations, detector thresholds are, when required, safe defaults. No hidden assumptions.

**Standing lesson (3× now): executors ESTIMATE precision instead of measuring it — Fable MUST live-run every precision/quality claim before accepting.** Caught fictional numbers on Day-6 inquiry-op, Day-8 initial, and Day-8 post-fix.

## Standing review discipline (keep)

Operating model: Sonnet executors implement from spec sections; Fable (this thread) reviews EVERY diff against acceptance criteria + one live verification before accepting; fix-or-dispatch on any deviation. Executor prompts: spec section is law, ambiguity → STOP, tests alongside, conventional commits, scoped git add. Owner gates: Day-8 detector precision report, Day-12 first-night supervision, Day-14 flywheel-delta + case study. **Nothing ships dormant** — every detector gets a real config + measured precision or it's cut.
