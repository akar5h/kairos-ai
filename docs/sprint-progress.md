# Sprint Progress — living continuation doc

*Updated: 2026-06-13 (Day 5/6 boundary of the 14-day sprint). Purpose: full state capture so any session (human or agent) can resume gracefully. Companion docs: `sprint-14day.md` (mother plan), `sprint-exec-1/2/3-*.md` (execution specs), `insight-report-0.md` (baseline), `system-audit-and-self-improvement-roadmap.md` (findings F1–F10).*

---

## Where we are

**Phase 1 (Truth, Days 1–5): code-complete.** Exit gate (owner 20-trace spot-check) ran once, failed honestly at 11Y/7N/2?, root cause found and fix dispatched (in flight, see below). Expect gate to clear on re-tally without another owner review round.

**Operating model:** Sonnet executor agents implement from spec sections; Fable (this thread) reviews every diff, runs live verification, dispatches fixes. Five review catches so far — all measurement-layer bugs that would have poisoned the loop's reward signal:
1. F10 loop guard per-trace instead of per-run (fixed `f9edfee`)
2. Rung 3 adapter hook defined but never invoked — dead code (fixed `aa1f56d`)
3. Outcome condition 4 demanded readable output → pass structurally impossible on live spans (fixed `d5272c4`, plus emitter discovery: parent tool spans always status OK; truth is on `tool.execution` child's `success` attr, now propagated)
4. Spotcheck evaluated unmapped traces against operations[0] → vacuous fail flood (fixed `ef8108c`, by Fable directly)
5. **BLOCKED-PENDING, fully scoped:** `required_side_effect_tools` is all-of in the engine; Code Implementation's [Edit, Write] needs any-of. Owner labels caught it (every N row = Edit-only or Write-only trace failing `missing_side_effect`; verified live on f788bf6a/bd0ce911/96d0f15c). Executor analyzed and planned the fix but was then permission-denied (background agents can't prompt) — **implement in main thread next session**. Full plan, including the executor's catch of a THIRD call site:
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
| *(pending)* | side_effect_match any/all fix — executor in flight |

Plugin repo (`Xero/kairos-analysis-views`, own git): `b30e7e2` (fail-loud guard, meta/null types) → `c630d60` (outcome rows table) → `8529e0e` (is_primary/secondary count types).

## Current honest numbers (live Phoenix, 345 traces / 7d, pre-any-of fix)

- mean memberships/trace **0.70** (exit bar ≤1.5 ✓; was ~3 before Day 5)
- unmapped **187 (54%)** — Bash-only coordination-curl sessions (the insight-report-0 waste), become mappable after Day 13 intervention
- outcome rates: Code Impl **0.42** (will rise with any-of fix), Research 1.00, Orchestration 0.88, Coordination 1.00
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
- Paperclip source `~/dev/paperclip` (MIT); `packages/mcp-server` ships list_issues/checkout_issue/add_comment/update_issue/approvals — Day 13 intervention = wire it + rewrite CTO AGENTS.md (no fork). Baseline in insight-report-0.
- `scripts/` is outside kairos-ai ruff lint paths. `scripts/node_modules/` untracked, needs .gitignore line.

## Immediate next steps (in order)

1. **Collect in-flight fix** (side_effect_match): verify any-mode semantics in both call sites, check the 7 disputed trace verdicts flip to pass, re-tally owner labels against rerun → expect ≥18/20 equivalent agreement → **Phase 1 CLOSED**.
2. **Day 6** (sprint-exec-2-trust.md): tau-bench agreement harness — pair bundle rewards with traces, κ + confusion matrix, `eval/reports/taubench-agreement.md`. Decision tree: κ≥0.7 proceed; <0.7 = budgeted rework slot for outcome iteration.
3. **Day 7**: organic labeling export (50 findings + 20 clean, stratified, redacted) → owner ~90 min → redundancy redefinition (needs transcript-sourced args since spans carry none) → precision ≥0.7 or demote.
4. Days 8–14 per sprint-exec-3-loop.md (triage → tier-2 judge w/ validation gate → report+store → nightly runner → Day 13 pre-selected coordination-diet intervention on CTO only → delta).

## Standing review discipline (keep)

Executor prompts: spec section is law, ambiguity → STOP, tests alongside, conventional commits, scoped git add, report ambiguities. Fable reviews EVERY report against acceptance criteria + runs one live verification before accepting; fix-or-dispatch on any deviation. Owner gates: spot-check re-tally (imminent), Day 6 κ report, Day 7 labeling, Day 13 intervention approval.
