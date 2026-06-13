# Sprint Execution Doc 3/3 — LOOP (Days 8–14), REVISED v2

*Child of `sprint-14day.md`. **Revised 2026-06-13** after Day 7 findings + two owner strategy reviews. The original judge-centric plan is preserved in Appendix A (DEFERRED — post-sprint Phase 4). This v2 restructures the wedge around four decisions the owner locked:*

1. **Deterministic-first.** Exhaust the cheap, reproducible, deterministic path *before* any LLM judge. Day 7 proved unspent deterministic ROI: the silent-failure fix (Bug 1, `4c30a62`) corrected 53 steps across 28 traces, and *contract-completion* could not see any of it — that gap is a deterministic **session-quality** signal, now buildable on honest step data.

2. **Single source of data.** No scatter. **One Postgres** is the source of truth for everything Kairos produces (findings, time series, labels, patterns, discovery queue). ClickHouse is *not* used now — we have not hit columnar-OLAP scale, Postgres is already running, and a second store is exactly the convolution we're removing. ClickHouse becomes a roadmap swap (portable schema) for when discovery aggregations span millions of rows.

3. **Generalize, don't couple.** Unit-of-work outcome is a generic **`correlation_key`** (a declared, *optional*, documented span attribute grouping traces). Paperclip binds `paperclip.issue`; a chat app binds `thread_id`. Engine stays domain-blind. Expectations (which tools "should" appear) are **learned, never declared** — no config burden, no conflict-on-conflict.

4. **Kairos improves *itself*, not the systems it observes.** The loop that closes is on **Kairos's own detection quality**, driven by the **discovery → label → detector flywheel** (the owner's real concern from week 1: "there is no eval system for Kairos itself"). Kairos *surfaces* agent problems with evidence (e.g. "CTO wastes 68% on coordination"); whether anyone *fixes the agent* is the user's governed decision and is **out of scope**. The original Day-13 "coordination diet" (rewriting Paperclip's CTO agent) was a category error — fixing the observed system, not improving Kairos — and is **cut**.

**The LLM judge is DEFERRED** (Appendix A): it is the only stage with circular-validation risk, and we have not yet spent the deterministic budget that shrinks what it must adjudicate. Built later, it inherits a measured error bar (validated against the owner's labels + tau rewards) rather than blind trust.

---

## 0. The nightly pipeline (deterministic-only; zero LLM cost)

```
            03:00 local, launchd
                  │
   ┌──────────────▼──────────────────────────────────────────────────────────┐
   │ nightly_loop.py            (deterministic-only; no LLM in the loop)      │
   │                                                                         │
   │  fetch (26h window) ──► dedupe vs seen_trace_ids                        │
   │        ▼                                                                │
   │  TIER 1   kairos analyze (outcome + tier-1 detectors)        ~0 cost   │
   │        ▼                                                                │
   │  TIER 1.5 session-quality detectors (Day 8)                  ~0 cost   │
   │        │  struggle · unrecovered_error · coordination_waste · work_to_talk │
   │        ▼                                                                │
   │  ROLLUP   correlation_key group → unit-of-work outcome (Day 9)         │
   │        ▼                                                                │
   │  LEARN    per-workflow tool-presence base rates → expectation deltas    │
   │        ▼                                                                │
   │  PERSIST  Postgres: findings + nightly_rollup (Day 10)                 │
   │        │  idempotent by (night_id, trace_id / unit_id)                 │
   │        ▼                                                                │
   │  DISCOVER anomaly + expectation-miss surface → owner-label queue (Day12)│
   │        ▼                                                                │
   │  EMIT     daily report (template-filled, no LLM) + dashboard refresh    │
   │           + decision_ledger rows (improvement.suggested)               │
   └─────────────────────────────────────────────────────────────────────────┘
   Every box that can fail emits a VISIBLE artifact on failure (skip-marker
   report / partial-coverage note). The loop never silently skips a night —
   the F1 lesson applied to the loop itself.
```

**Engine invariant (unchanged):** `KairosEngine.analyze()` never calls an LLM. Tier-1.5 detectors are deterministic, in `src/kairos/detection/`. Loop/persistence/discovery/dashboard live in `src/kairos/loop/` + `scripts/`. The actuator is always a human-gated PR; the loop only *surfaces*.

---

## Day 8 — Deterministic session-quality detectors (`src/kairos/detection/session_quality.py`)

Contract-completion answers "did the unit's signature tool succeed." It is blind to *how badly the agent thrashed*. These detectors fill that gap deterministically, on the now-honest step statuses (Bug 1). All severity-tagged; all measured for precision against the owner's labels (`eval/review/answers.jsonl` + `docs/spotcheck-day4.md`); **a detector under 0.7 precision ships at `info` or is cut — nothing ships dormant.**

```pseudocode
# D1 — unrecovered_error  (sharpens the over-loose recovery rule)
#   current _has_critical_tool_error: ANY same-tool success anywhere = "recovered" —
#   too generous (one Bash success absolves every Bash failure).
fire when: step.status == ERROR
       AND no later step with SAME tool AND jaccard(args_norm) >= 0.9     # same command, not just same tool
           within RECOVERY_WINDOW steps
       AND session-restart boundary is NOT counted as recovery (the haywire pattern)
  severity: error if tool in required_side_effect_tools else warning

# D2 — struggle_ratio  (the Bug-1 class, made visible at trace level)
struggle = (error_steps + redundant_steps + rejected_tool_calls) / max(1, side_effect_successes)
fire when: struggle >= STRUGGLE_T     # T from the label distribution; report the curve, don't guess one number
  severity: warning ; evidence = the churn breakdown

# D3 — coordination_waste  (insight-report-0 M1/M2; now computable with real args)
fire when: >= REPEAT_T identical-arg calls of one tool (inbox poll / token re-derivation)
       OR  fraction of Bash calls matching coordination-curl shapes >= CURL_T
  severity: info→warning by count
  # SURFACING ONLY — Kairos reports the waste; fixing the agent is the user's call.

# D4 — work_to_talk_ratio  (cost without progress)
ratio = side_effect_successes / max(1, llm_tokens_spent / 1000)
fire when: ratio below WTT_T on a trace that is NOT pure research/coordination (op-exempt)
  severity: info
```

**Learned expectations (replaces the cut "mandatory_tools" declaration):** the LEARN stage computes, per workflow, each tool's presence rate across *clean* (outcome-pass, low-struggle) traces. A trace missing a tool whose presence rate ≥ `EXPECT_T` (e.g. 0.9) becomes an **expectation-miss candidate** — it is NOT a fired finding (unlabeled = unmeasured); it is surfaced to discovery for a one-click owner label. Confirmed → tracked as a per-workflow expectation in Postgres. **Zero user declaration, zero config conflict** — the doubt-driven-development silent-skip (trace `6071761a`) is caught this way, not by a hand-written rule.

**Config surface:** detector thresholds live per-op in `context.yaml` (reuse the Day-5 surface) with code defaults commented to the label distribution they came from. **Edge cases:** research/coordination ops are expected low work-to-talk → D4 op-exempt; D1's window crossing a session restart = unrecovered (restart ≠ recovery); a workflow with too few clean traces to learn an expectation (n < `EXPECT_MIN_N`) emits no expectation-miss candidates and says so (no guessing from thin data).

**Validation (blocking for severity ≥ warning):** per-detector precision on the owner's 15 + 20 labels → `eval/reports/session-quality-precision.md`. Demote-or-cut on < 0.7. The deterministic analogue of the judge's validation gate.

---

## Day 9 — Correlation-key rollup (generic, documented, optional)

```pseudocode
# context.yaml gains an OPTIONAL top-level key (documented in the Kairos config guide):
#   correlation_key: "paperclip.issue"    # chat app → "thread_id"; pipeline → "run_id"
# Absent → the unit IS the trace (today's behavior, fully backward compatible).

group traces by correlation_key value (read from TOOL spans; root often lacks it):
    unit_outcome  = outcome of the LAST computable trace (chronological) — intermediate
                    fails on an ultimately-green unit are progress, not failure
    unit_findings = UNION over the group
    unit_cost     = SUM tokens / SUM struggle
orphans (no key value) → "unattributed", scored per-trace
```

**Generic, not coupled:** the engine references a *configured attribute name*, never "issue." Multi-trace units are universal in agent systems. Rollup mode = `last-wins` default; `any-fail`/`all-pass` are per-context, build-when-needed (YAGNI). **Documentation requirement (owner-flagged):** the Kairos config guide MUST state plainly that `correlation_key`, the operation definitions, and detector thresholds are user-provided configuration — what each does, when it's required, and the safe default when omitted. No hidden assumptions.

---

## Day 10 — Persistence: single Postgres source (`src/kairos/loop/persist.py`)

**Decision:** one Postgres, the single source of truth for all Kairos-produced data. A dedicated `kairos` database (new `kairos-pg` container, or a `kairos` DB on an existing PG instance — owner's infra call). Reasons: Postgres is already running; the nightly series is KB/night (no OLAP need); one store removes the scatter; secrets stay local. Schema kept ANSI-portable so a later ClickHouse swap (at discovery-scale) is a connection-string change, not a rewrite.

```sql
-- raw findings (grows with traffic; partition by night when it gets big)
CREATE TABLE findings (
  night_id date, trace_id text, unit_id text, workflow text, agent text,
  detector text, severity text, evidence_steps int[], tokens int,
  struggle real, outcome text, config_hash text, ingested_at timestamptz,
  PRIMARY KEY (night_id, trace_id, detector)          -- idempotent upsert (ON CONFLICT DO UPDATE)
);
-- the time series the dashboard reads (small)
CREATE TABLE nightly_rollup (
  night_id date, workflow text, agent text,
  units int, traces int, outcome_rate real,
  struggle_p50 real, struggle_p90 real, coordination_waste_rate real,
  tokens_per_unit real, finding_counts jsonb, config_hash text,
  PRIMARY KEY (night_id, workflow, agent)
);
-- the flywheel's memory
CREATE TABLE labels        (id text PRIMARY KEY, trace_id text, question text, answer text,
                            verdict text, label_class text, ts timestamptz);  -- owner labels persisted
CREATE TABLE expectations  (workflow text, tool text, presence_rate real, confirmed bool,
                            first_seen date, PRIMARY KEY (workflow, tool));
CREATE TABLE discovery_queue (id text PRIMARY KEY, night_id date, kind text,  -- 'anomaly'|'expectation_miss'
                            trace_id text, features jsonb, labeled bool DEFAULT false);
```

**Idempotency:** `ON CONFLICT … DO UPDATE` keyed as above — re-running a night never double-counts (ledger-cursor discipline). **config_hash gate:** deltas computed only within one hash; a config change writes a `baseline_break` row so the dashboard shows a visible discontinuity, never a fake trend. **Backfill:** one-shot loader replays the last 7 days of live traces so the dashboard opens with history. **Migration:** plain SQL files in `migrations/`, applied idempotently; no ORM heaviness for four tables.

---

## Day 11 — Delta engine + minimal dashboard (`src/kairos/loop/delta.py`, `eval/dashboard/app.py`)

```pseudocode
delta(metric, scope, window_before, window_after, same config_hash):
    return mean(after) - mean(before), with n each side and the raw points
guardrail_check(primary_improved, guardrails=[outcome_rate, escalation_rate]):
    primary improves + any guardrail degrades = REGRESSION (reported, never hidden)
```

**Dashboard — one page (Streamlit, reuse the Day-7 stack), reads `nightly_rollup`:**
- Outcome_rate per workflow over time; baseline-break markers as vertical rules.
- Struggle p50/p90, coordination_waste_rate over time, split by agent (the surfacing view).
- **Kairos's own detection curve** (the self-improvement view): failure-classes-covered and precision-vs-labels over the sprint — this is the thesis artifact.
- Finding-volume by detector; tokens_per_unit trend.
- Sidebar: config_hash timeline; unit-vs-trace granularity toggle.
- No write actions, no auth, nothing beyond these curves. Owner types takeaways; the dashboard shows trends.

---

## Day 12 — Discovery mode + nightly runner

### Discovery (`src/kairos/loop/discover.py`) — the flywheel engine

```pseudocode
# Surfaces what NO detector was told to look for + expectation misses (Day 8 LEARN stage).
features per trace: tool-sequence shape, token z, latency z, coverage, struggle, depth, restart-count
candidates = traces in the tail of any feature (robust z > 3)
           ∪ rare tool-sequence n-grams (freq < 1%)
           ∪ expectation_miss candidates (a near-universal tool absent)
cluster cheaply (tool-signature + dominant feature) → eval/review/discovery_queue.json + Postgres
owner labels them in the SAME review app (Day 7) → confirmed clusters become:
    - a new targeted detector (Day-8 surface), OR
    - a confirmed expectation (expectations table)
```

Discovery never fires findings itself (unlabeled = unmeasured); it only proposes candidates. It `log()`s anything dropped by a cap — no silent truncation. **This is the loop the owner asked for:** today's discovery → tomorrow's label → next week's deterministic rule.

### Runner (`scripts/nightly_loop.py` + launchd) — deterministic, cannot thrash

```pseudocode
STATE_MACHINE (each transition logs a line):
  FETCH    phoenix 26h, dedupe; retry 3×/30min → skip-marker report, EXIT 0 (don't crash launchd)
  ANALYZE  kairos analyze (outcome + tier-1 + tier-1.5); 0 traces → "quiet night" report (valid)
  ROLLUP   correlation_key grouping; key absent → per-trace mode + note (degrade, don't die)
  LEARN    per-workflow presence rates → expectation deltas
  PERSIST  Postgres upserts (findings + rollup); DB down → local parquet fallback + WARN, never lose the night
  DISCOVER anomaly + expectation-miss → discovery_queue (best-effort)
  EMIT     report + dashboard refresh + decision_ledger rows (improvement.suggested)
ANY unexpected exception → traceback to log + skip-marker report. The night is never silent.
```

Env (`.env`, names→`.env.example`): `KAIROS_CONTEXT_PATH`, `KAIROS_PHOENIX_ENDPOINT/PROJECT`, `KAIROS_PG_DSN`, `LEDGER_API_URL`, `KAIROS_LOOP_DATA_DIR`, `KAIROS_LOOP_DISABLED` (kill switch, checked first). **No LLM keys** — the loop calls no model. **First supervised night runs Day 12**; owner skims log + dashboard next morning.

---

## Days 13–14 — The Kairos self-improvement proof (the thesis test)

The thesis is **not** "we fixed an agent." It is **"Kairos measurably got better at its own job, governed by human labels."** Days 13–14 run the flywheel once, end-to-end, on real traffic, and read the result off Kairos's own detection curve.

### Day 13 — run the flywheel live

```
flywheel iteration (live):
  1. discovery surfaces an UNLABELED candidate class (something Day-8 detectors did not target)
  2. owner labels the cluster in the review app (governed step — human decides what's real)
  3. a new deterministic detector (or confirmed expectation) ships for that class,
     validated to ≥0.7 precision on the labels
  4. re-run analyze over the window → Kairos now catches a class it could not catch on Day 8
  measure: classes-covered ↑, precision-vs-labels (held-out), recall on the owner-labeled set
```

**Iteration 0 already happened — cite it as proof the flywheel turns:** Day 7, owner labeled the silent-failure class → the Bug-1 correction + struggle detector shipped (`4c30a62` + Day 8). Days 13–14 run iteration 1 *live and measured*. Candidate classes from discovery (likely): haywire-restart/stale-resume (owner-named, no detector yet), loop-that-looks-like-progress, degenerate-recovery. We do not pre-pick — that's the point; discovery surfaces, owner labels.

### Day 14 — read Kairos's detection curve; case study

```pseudocode
before/after (Kairos itself, Day 8 → Day 14, same corpus window):
  classes_covered:      N → N+k
  precision_vs_labels:  P → P'        (held-out labels, not the ones tuned on)
  recall_on_labeled:    R → R'        (fraction of owner-confirmed failures Kairos now flags)
honesty: small n → direction + magnitude, NOT p-values. Show the traces behind every number.
confounds logged: config_hash changes in the window, traffic shape, model version.
```

`docs/case-study-1.md` (the self-improvement story): a class Kairos *missed* on Day 8 (evidence, linked traces) → discovery surfaced it → owner labeled it → detector shipped (the diff, validated precision) → Kairos's coverage/precision curve moved (dashboard screenshot). **A null/negative result ships under the same template** — "discovery surfaced nothing new this window" or "the new detector didn't beat 0.7, demoted" both prove the *measurement and governance* work, which is the thesis.

**Two-layer claim:**
- **Layer 1 (Kairos works as a product):** on real traffic, Kairos emits truthful, evidence-backed findings — silent failures (53 corrected steps), coordination waste (68%, surfaced not fixed), struggle — persisted as trends.
- **Layer 2 (Kairos improves itself):** the governed flywheel ran end-to-end and Kairos's measured detection quality rose, driven by human labels, validated against ground truth. *That* is the self-improvement — honest because it's human-governed and measured, not self-asserted.

**Explicitly NOT claimed in 14 days:** fixing any agent, recall-completeness across all failure types, cross-org generalization, statistical significance, or the LLM-judge layer.

---

## Security & safety acceptance (blocks Day 12 go-live)

- [ ] Redaction tests green (every pattern class + planted `.env` blob); discovery queue + any persisted excerpt grep-audited.
- [ ] Postgres holds **no raw secrets**: findings store evidence-step indices + redacted digests only, never full tool outputs; DSN via env; instance bound to localhost.
- [ ] No secret in repo: runner config env-only; `.env.example` names-only.
- [ ] Detector/finding outputs are data: nothing parses a finding into a command; suggestions become PRs via human-gated executor, never direct writes.
- [ ] Loop's own traces tagged (`actor_id: kairos-loop`) and excluded from analysis targeting.
- [ ] Kill switch `KAIROS_LOOP_DISABLED=1` checked first in the runner; documented in README.
- [ ] config_hash discipline: deltas only within a hash; baseline-break rows rendered as discontinuities.

---

## Appendix A — DEFERRED: LLM-judge tier-2 (post-sprint Phase 4)

*The original judge design is deferred, not deleted. When the deterministic detectors plateau on precision/recall against labels, OR a class is provably invisible to deterministic rules (e.g. wrong-args-success — the tau-bench 67 FPs), the judge is added with a measured error bar: validated against owner labels + tau rewards (κ ≥ 0.7 gate), judging only deterministically-isolated, evidence-cited questions, voting adversarially for action-driving findings, never auto-acting. Retained design elements (full pseudocode in this file's pre-2026-06-13 git history): triage scoring, versioned `DIGEST_V1` builder with 150-char excerpts, redaction-before-egress, the injection-resistant `<<DATA>>…<<END DATA>>` untrusted-block prompt + `injection_suspected` flag, schema-and-evidence validation loop, and the κ≥0.7 gate on 50 stratified tau digests.*
