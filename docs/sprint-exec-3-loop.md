# Sprint Execution Doc 3/3 — LOOP (Days 8–14), REVISED

*Child of `sprint-14day.md`. **Revised 2026-06-13** after Day 7 findings + owner strategy review. The original judge-centric plan is preserved verbatim in Appendix A (DEFERRED — post-sprint Phase 4). This revision reorders the wedge around three decisions the owner locked:*

1. **Deterministic-first.** Exhaust the cheap, reproducible, deterministic path *before* any LLM judge. Day 7 proved there is unspent deterministic ROI: the silent-failure fix (Bug 1, `4c30a62`) corrected 53 steps across 28 traces, and the *contract-completion* metric could not see any of it — that gap is a deterministic **session-quality** signal we have not built yet, with honest inputs now available.
2. **Persist + delta + dashboard as the spine.** "Self-improving" requires memory of self. A nightly *report* is repeated snapshots; the thesis needs a **time series** with interventions as markers on a curve. Store = **ClickHouse (local Docker)**.
3. **Generalize, don't couple.** "Issue-level outcome" is a special case of a generic **`correlation_key`** (a declared span attribute grouping traces into one logical unit of work). Paperclip binds `paperclip.issue`; a chat app binds `thread_id`. Engine stays domain-blind.

**The LLM judge is DEFERRED** (Appendix A). Reason: it is the only stage with circular-validation risk ("LLM judging LLM"), and we have not yet spent the deterministic budget that reduces what the judge must adjudicate. We build it after the deterministic loop is proven and persisted — at which point the judge inherits a measured error bar (validated against the owner's labels + tau rewards) instead of being trusted blind.

---

## 0. The nightly pipeline (revised — no LLM in the loop)

```
            03:00 local, launchd
                  │
   ┌──────────────▼──────────────────────────────────────────────────────────┐
   │ nightly_loop.py            (deterministic-only; zero LLM cost)           │
   │                                                                         │
   │  fetch (26h window) ──► dedupe vs seen_trace_ids                        │
   │        │                                                                │
   │        ▼                                                                │
   │  TIER 1   kairos analyze (outcome + tier-1 detectors)        ~0 cost   │
   │        │                                                                │
   │        ▼                                                                │
   │  TIER 1.5 session-quality detectors (Day 8)                  ~0 cost   │
   │        │  struggle · unrecovered_error · mandatory_tool_missing ·      │
   │        │  coordination_waste · work_to_talk                            │
   │        ▼                                                                │
   │  ROLLUP   correlation_key group → unit-of-work outcome (Day 9)         │
   │        │                                                                │
   │        ▼                                                                │
   │  PERSIST  ClickHouse: raw findings + nightly aggregates (Day 10)       │
   │        │  idempotent by (night_id, trace_id / unit_id)                 │
   │        ▼                                                                │
   │  DISCOVER anomaly surface → owner-label queue (Day 12)                 │
   │        │                                                                │
   │        ▼                                                                │
   │  EMIT     daily report (md+json, template-filled, no prose-LLM)        │
   │           + dashboard refresh + decision_ledger rows                   │
   └─────────────────────────────────────────────────────────────────────────┘
   Every box that can fail produces a VISIBLE artifact on failure
   (skip-marker report / partial-coverage note). The loop never silently
   skips a night — the F1 lesson applied to the loop itself.
```

**Engine invariant (unchanged):** `KairosEngine.analyze()` never calls an LLM. Tier-1.5 detectors are deterministic and live in `src/kairos/detection/`. The loop, persistence, discovery, and dashboard live in `src/kairos/loop/` + `scripts/`. The CLAUDE.md dropped-modules list stays dropped — this is analysis tooling; the actuator is a human-approved PR.

---

## Day 8 — Deterministic session-quality detectors (`src/kairos/detection/session_quality.py`)

The contract-completion metric answers "did the unit of work's signature tool succeed." It is blind to *how badly the agent thrashed getting there*. These detectors fill that gap deterministically, using the now-honest step statuses (Bug 1). All are severity-tagged and measured for precision against the owner's Day-7 labels (`eval/review/answers.jsonl`) where applicable; a detector under 0.7 precision ships at severity `info`, same discipline as `redundant_execution`.

```pseudocode
# D1 — unrecovered_error  (sharpens the over-loose recovery rule)
#   current _has_critical_tool_error: ANY same-tool success anywhere = recovered.
#   too generous for ubiquitous tools (one Bash success absolves all Bash fails).
fire when: step.status == ERROR
       AND no later step with SAME tool AND jaccard(args_norm) >= 0.9   # same command, not just same tool
           within RECOVERY_WINDOW steps
  severity: error if tool in op.required_side_effect_tools else warning

# D2 — struggle_ratio  (the Bug-1 class, made visible)
#   corrected/error+retry churn relative to productive work.
struggle = (error_steps + redundant_steps + rejected_tool_calls) / max(1, side_effect_successes)
fire when: struggle >= STRUGGLE_T   # T tuned on labels; report distribution, don't guess one number
  severity: warning ; attach the churn breakdown as evidence

# D3 — mandatory_tool_missing  (Bug 3, GENERIC + deterministic — not a judgment call)
#   context.yaml op gains:  mandatory_tools: [<tool/skill names>]
#   a declared-required step that never fired (or fired but is_error) = contract violation.
fire when: op.mandatory_tools present AND any m in mandatory_tools has no successful step
  severity: error    # it's a declared contract; absence is unambiguous

# D4 — coordination_waste  (insight-report-0 M1/M2, now computable: real args)
fire when: >= REPEAT_T identical-arg calls of the same tool (poll/credential rituals)
       OR  fraction of Bash calls matching known coordination-curl shapes >= CURL_T
  severity: info→warning by count ; this is the Day-13 intervention's target metric

# D5 — work_to_talk_ratio  (cost without progress)
ratio = side_effect_successes / max(1, llm_tokens_spent / 1000)
fire when: ratio below WTT_T on a trace that is NOT pure research/coordination
  severity: info ; a cost-efficiency signal, not a correctness one
```

**Config surface:** thresholds live per-op in `context.yaml` (reuse the Day-5 surface), never hardcoded; defaults in code with a comment citing the label distribution they came from. **Edge cases:** research/coordination ops are *expected* low work-to-talk → D5 exempt by op; D1 recovery window crossing a session-restart boundary (the haywire pattern) counts as *unrecovered* (the restart is not recovery); D3 with a mandatory tool that is itself optional in some modes → that's a context.yaml modelling error, fail loud at load.

**Validation (blocking for severity ≥ warning):** measure each detector's precision on the owner's 15 labels + the 20 spot-check labels. Record in `eval/reports/session-quality-precision.md`. This is the deterministic analogue of the judge's validation gate — a detector that cries wolf gets demoted, not shipped loud.

---

## Day 9 — Correlation-key rollup (generic unit-of-work outcome)

```pseudocode
# context.yaml gains a top-level optional key:
#   correlation_key: "paperclip.issue"     # Paperclip; chat app would use "thread_id"
# The engine reads the named attr off spans (tool spans carry it; root may not — read tool spans).
# When absent from config, the unit IS the trace (today's behavior) — backward compatible.

group traces by correlation_key value:
    unit_outcome  = outcome of the LAST computable trace in the group (chronological)
                    # intermediate fails on an ultimately-green unit are progress, not failure
    unit_findings = UNION of findings across the group
    unit_cost     = SUM tokens / SUM struggle across the group
    unit_span     = first_trace.start .. last_trace.end
orphans (no key value) → reported under "unattributed", scored per-trace
```

**Why this is generic, not Paperclip-coupled:** the engine never references "issue." It references a configured attribute name and groups by its value. Multi-trace units are universal in agent systems (a chat = many request-traces under one thread; a pipeline = many step-traces under one run_id). Paperclip is one binding of a generic feature — exactly as `business_context.yaml` is generic with Paperclip as one tenant. Rollup mode is `last-wins` by default; `any-fail` / `all-pass` selectable per-context later (do not build until needed — YAGNI).

**Output:** `UnitOutcomeSummary` alongside the existing per-trace summary; both flow to persistence. The dashboard can show outcome at trace OR unit granularity.

---

## Day 10 — Persistence: ClickHouse (local Docker)

**Decision:** ClickHouse, self-hosted in Docker locally for the sprint. Rationale: (a) these traces carry live tokens in Bash args — local keeps the redaction surface local, no cloud egress; (b) one container beside Phoenix, no signup/auth/latency; (c) columnar OLAP is the correct substrate for the 100k-trace vision (discovery-mode aggregations over millions of rows); (d) schema kept cloud-portable → ClickHouse Cloud / Tinybird later is a connection-string swap. Free-cloud reality logged: ClickHouse Cloud is trial-credits not forever-free; Tinybird (ClickHouse-based) has a real free tier if managed-free becomes a hard requirement post-sprint — but revisit only once data is redaction-clean by construction.

```
deploy: docker compose service `clickhouse` (ports 8123 http / 9000 native), volume-backed,
        same compose file / pattern as deploy-phoenix. Env: CLICKHOUSE_DSN (names→.env.example).

Two-tier schema (raw + aggregate):

  TABLE findings  (raw, grows with traffic — ClickHouse's job)
    night_id Date, trace_id String, unit_id String, workflow LowCardinality(String),
    detector LowCardinality(String), severity Enum, evidence_steps Array(UInt32),
    tokens UInt32, struggle Float32, outcome Enum('pass','fail','noncomputable'),
    config_hash String, agent LowCardinality(String), ingested_at DateTime
    ENGINE = MergeTree ORDER BY (night_id, workflow, detector)
    -- idempotent reload: ReplacingMergeTree keyed on (night_id, trace_id, detector)

  TABLE nightly_rollup  (small, the time series the dashboard reads)
    night_id Date, workflow LowCardinality(String), agent LowCardinality(String),
    units UInt32, traces UInt32,
    outcome_rate Float32, struggle_p50 Float32, struggle_p90 Float32,
    coordination_waste_rate Float32, tokens_per_unit Float32,
    finding_counts Map(String, UInt32),
    config_hash String,
    intervention_marker String DEFAULT ''   -- set on nights an intervention applied to `agent`
    ENGINE = ReplacingMergeTree ORDER BY (night_id, workflow, agent)
```

**Ingest** (`src/kairos/loop/persist.py`): write raw findings then the computed rollup; idempotent by (night_id, trace/unit) so re-running a night never double-counts (the ledger-cursor discipline). **config_hash gate:** deltas are only computed within the same config_hash; a config change appends a `baseline_break` marker row so the dashboard renders a visible discontinuity instead of a fake trend. **Backfill:** one-shot loader replays the last 7 days of live traces into ClickHouse so the dashboard opens with history, not an empty chart.

---

## Day 11 — Delta engine + minimal dashboard (`src/kairos/loop/delta.py`, `eval/dashboard/app.py`)

```pseudocode
delta(metric, agent, window_before, window_after, same config_hash):
    return mean(after) - mean(before), with n each side and the raw points
    # interventions annotated from nightly_rollup.intervention_marker
guardrail_check(primary_metric_improved, guardrails=[outcome_rate, escalation_rate]):
    primary improves + any guardrail degrades = REGRESSION (report as such, never hide)
```

**Dashboard — minimal, one page (Streamlit, reuse the Day-7 app stack):**
- Top: outcome_rate per workflow over time (line). Intervention markers as vertical rules with labels.
- Mid: struggle_p50/p90 and coordination_waste_rate over time, **split by agent** (CTO vs controls) — this is where the Day-13 intervention must show as a bend for CTO only.
- Bottom: finding-volume trend by detector; tokens_per_unit trend.
- Sidebar: config_hash timeline (baseline-break markers), unit vs trace granularity toggle.
- **No write actions, no auth, no charts beyond these.** It reads `nightly_rollup`. The owner types takeaways; the dashboard shows curves.

The dashboard *is* the thesis artifact: Day 14's "did it work" is read off it — CTO's coordination/struggle curves bend down after the marker, controls flat, outcome guardrail unbroken.

---

## Day 12 — Discovery mode + nightly runner

### Discovery (`src/kairos/loop/discover.py`) — the flywheel

```pseudocode
# Deterministic anomaly surfacing: find what NO detector was told to look for.
features per trace: tool-sequence shape, token z-score, latency z-score,
                    coverage, struggle, depth, restart-count
outliers = traces in the tail of any feature distribution (robust z > 3) OR
           rare tool-sequence n-grams (frequency < 1%)
cluster the outliers (cheap: by tool-signature + dominant feature)
emit eval/review/discovery_queue.json  → the SAME review app (Day 7)
owner labels them → confirmed clusters become next round's targeted detector (Day 8 surface)
```

This closes the loop the owner asked for: today's discovery → tomorrow's label → next week's deterministic rule. Discovery never fires findings on its own (unlabeled = unmeasured); it only *proposes candidates for labeling*. Honesty: it `log()`s how many outliers were dropped by any cap — no silent truncation.

### Runner (`scripts/nightly_loop.py` + launchd) — deterministic, cannot thrash

```pseudocode
STATE_MACHINE (each transition logs a line):
  FETCH    phoenix 26h, dedupe vs seen_trace_ids; retry 3×/30min → skip-marker report, EXIT 0
  ANALYZE  kairos analyze (outcome + tier-1 + tier-1.5); 0 traces → "quiet night" report (valid)
  ROLLUP   correlation_key grouping; key attr absent → per-trace mode + coverage note (degrade)
  PERSIST  ClickHouse write (raw + rollup), atomic, idempotent; DB down → write local parquet
           fallback + WARN, never lose the night
  DISCOVER anomaly surface → discovery_queue (best-effort; failure = empty queue + note)
  EMIT     report file + dashboard data refresh + decision_ledger rows (improvement.suggested)
ANY unexpected exception → traceback to log + skip-marker report. The night is never silent.
```

Env (`.env`, names→`.env.example`): `KAIROS_CONTEXT_PATH`, `KAIROS_PHOENIX_ENDPOINT/PROJECT`, `CLICKHOUSE_DSN`, `LEDGER_API_URL`, `PAPERCLIP_DB_URL` (ledger_ro fallback for the key join), `KAIROS_LOOP_DATA_DIR`, `KAIROS_LOOP_DISABLED` (kill switch checked first). **No `ANTHROPIC_API_KEY` / `JUDGE_MODEL`** — the revised loop calls no LLM. **First supervised night runs Day 12**; owner skims log + dashboard next morning.

---

## Days 13–14 — Intervention + measurement (the thesis test)

### Day 13 — the coordination diet (pre-selected, baseline in `insight-report-0.md`)

Unchanged in substance from the original plan — it is the perfect deterministic-loop intervention because its target metric (`coordination_waste_rate`, D4) is now a first-class persisted series:

```
I1: add paperclip MCP server to CTO agent session config (.mcp.json / adapter config)
I2: rewrite CTO AGENTS.md coordination section:
    - coordination via MCP tools, never curl
    - never poll: inbox empty → end turn (orchestrator wakes you)
    - never re-derive tokens: env provides them
    - Grep/Glob/Read over Bash equivalents
I1+I2 = ONE PR, CTO agent only; claudecoder/qaengineer untouched = CONTROLS
on apply: stamp nightly_rollup.intervention_marker='coordination_diet' for agent=cto, this night
```

Selection criteria for intervention #2+ (and override test if discovery surfaces something stronger) are retained from Appendix A §Day-13.

### Day 14 — delta read off the dashboard

```pseudocode
primary   = coordination_waste_rate(cto): mean(3 nights before) vs mean(nights after)
            AND struggle_p50(cto) same windows
control   = same metrics for claudecoder/qaengineer — MUST stay flat (attribution)
guardrail = outcome_rate AND escalation_rate (cto) — PAIRED; primary improves + guardrail
            degrades = REGRESSION, reported as such
honesty: ~7 units/night × few nights = direction + magnitude, NOT p-values. No stats theater.
confounds logged: model version, traffic shape, other PRs to cto instructions (git log).
```

`docs/case-study-1.md` (written once ≥3 post-nights exist): pattern (evidence, linked traces) → intervention (the diff, PR link) → delta (dashboard screenshot + table, primary + guardrails, CTO vs controls) → confounds → verdict. **A null/negative result ships under the same template** — for a thesis about governed self-improvement, "we measured honestly, the curve didn't move, we rolled back" is itself a positive result about the *measurement*.

---

## What the loop's before/after will be (the thesis claim)

> Kairos deterministically detected a quantified coordination-waste pattern, surfaced a specific fix, a human applied it to one agent via PR, and the persisted ClickHouse time series shows that agent's coordination-waste and struggle curves dropping after the intervention marker while untouched control agents stayed flat — with no regression in outcome or escalation rate.

Target deltas (CTO, from `insight-report-0.md` §4): Bash share 75%→<30%; identical-command repeats pervasive→~0; tool-calls/heartbeat ↓30–50%; struggle ↓; outcome/escalation flat (guardrail). **Deliberately NOT claimed in 14 days:** recall across all failure types, cross-org generalization, statistical significance (n=1 intervention), or the LLM-judge layer. Existence proof of a closed, governed, *persisted* self-improvement loop — a case study, and we say so.

---

## Security & safety acceptance (blocks Day 12 go-live)

- [ ] Redaction tests green (every pattern class + planted `.env` blob) — discovery queue + any persisted excerpt grep-audited.
- [ ] ClickHouse holds **no raw secrets**: findings store evidence-step indices + redacted digests only, never full tool outputs; DSN via env; container not exposed beyond localhost.
- [ ] No secret in repo: runner config env-only; `.env.example` names-only.
- [ ] Detector outputs are data: nothing parses a finding into a command; suggestions become PRs via human-gated executor, never direct writes.
- [ ] Loop's own traces tagged (`actor_id: kairos-loop`) and excluded from intervention targeting.
- [ ] Kill switch `KAIROS_LOOP_DISABLED=1` checked first in the runner; documented in README.
- [ ] config_hash discipline: deltas only within a hash; baseline-break rows rendered as discontinuities.

---

## Appendix A — DEFERRED: LLM-judge tier-2 (post-sprint Phase 4)

*The original Days 9–10 judge design is preserved here verbatim-in-intent for when the deterministic loop is proven and we choose to add semantic adjudication. It is deferred, not deleted. When built, it inherits a measured error bar: validated against the owner's labels + tau rewards (κ ≥ 0.7 gate), judges only deterministically-isolated evidence-cited questions, votes adversarially for action-driving findings, and never auto-acts. Build triggers: (1) deterministic detectors plateau on precision/recall against labels; (2) a class of failure (e.g. wrong-args-success — the tau-bench 67 FPs) is provably invisible to deterministic rules and worth the circular-validation risk to catch.*

Key elements retained for that phase: triage scoring (`triage.py`), digest builder with versioned `DIGEST_V1` format + 150-char excerpts, redaction-before-egress, the injection-resistant judge prompt (`<<DATA>>…<<END DATA>>` untrusted-block contract, `injection_suspected` flag), the schema-and-evidence validation loop, and the κ≥0.7 validation gate on 50 stratified tau digests. The full original pseudocode lives in this file's git history (pre-2026-06-13 revision) and in `sprint-progress.md`'s commit ledger.
