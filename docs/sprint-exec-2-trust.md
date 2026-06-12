# Sprint Execution Doc 2/3 — TRUST (Days 6–7)

*Child of `sprint-14day.md`. Covers Day 6 (tau-bench agreement) and Day 7 (human labeling + redundancy redefinition). Shortest doc, highest leverage: these two days are what separates "Kairos prints numbers" from "I believe the numbers enough to act on them."*

---

## 0. The trust model for a 14-day sprint

```
                 what we can afford          what it buys
 ┌─────────────────────────────────────────────────────────────────┐
 │ Day 6: ~150–250 tau-bench        outcome logic validated        │
 │        reward labels (free,      against an INDEPENDENT,        │
 │        already on disk)          deterministic ground truth     │
 ├─────────────────────────────────────────────────────────────────┤
 │ Day 7: ~50 human labels          detector precision measured    │
 │        (90 min owner time)       on the ACTUAL target domain    │
 │                                  (coding agents)                │
 └─────────────────────────────────────────────────────────────────┘
 NOT bought (roadmap): recall on organic failures, fault-injection
 coverage, severity calibration, drift tracking. The sprint trusts
 one measured delta — it does not certify detectors. Say so in
 anything public.
```

Why both layers: tau-bench validates the *outcome* dimension but its agents (tool-calling, short traces) don't exercise the coding-agent failure surface; the human labels validate *findings* on coding traces but n=50 is too small for outcome rates. Together they cover what Day 13's intervention decision actually consumes: "this workflow's outcome rate is real" + "these findings are mostly not noise."

---

## Day 6 — tau-bench agreement harness

### Data inventory (verified during audit)

```
~/tau-agent/results/ablation_bundles/*.json     ← ~10+ bundles
  └─ modes[]: each has
       checkpoint_rows[]: {task_id, reward, info{...}}   ← GROUND TRUTH (reward 0.0–1.0)
       kairos_run_dir: data/runs/{ts}_{env}_{model}      ← per-run kairos artifacts
       kairos_artifacts: {manifest...}
       average_reward
~/tau-agent/data/runs/{run_dir}/                ← normalized traces / raw artifacts (verify layout first)
~/.phoenix/phoenix-taubench-archive-2026-05.db  ← 11,302 spans, fallback source only
```

### Step 1 — pairing loader (`kairos-ai/eval/taubench_corpus.py`)

```pseudocode
for bundle in glob("~/tau-agent/results/ablation_bundles/*.json"):
    for mode in bundle.modes:
        run_dir = mode.kairos_run_dir
        rows = mode.checkpoint_rows                       # [{task_id, reward, info}]
        artifacts = inspect(run_dir)                      # FIRST ACTION OF THE DAY:
                                                          # open one run_dir by hand, learn the layout.
                                                          # Expect normalized TraceEnvelope JSON or raw
                                                          # transcripts; pairing key is task_id or ordering.
        for row in rows:
            trace = locate_trace(artifacts, row.task_id)  # exact mechanism depends on layout —
            if trace is None: skipped += 1; continue      # COUNT and REPORT skips, never silently drop
            emit eval/corpus/taubench/{trace_id}.json
            emit labels.jsonl: {trace_id, task_id, env: bundle.args.env,
                                model: bundle.args.model, reward: row.reward,
                                bundle: bundle.filename, mode: mode.mode}
coverage_report: pairs found / rows total, per bundle
```

**Label semantics:**

```pseudocode
reward == 1.0          → label PASS
reward == 0.0          → label FAIL
0 < reward < 1.0       → label PARTIAL → excluded from binary agreement, counted+reported
duplicate task (same task_id across bundles/modes) → keep all runs (they are distinct
    executions); ALSO report unique-task agreement to show duplicates don't carry the result
```

**Caveat — escalation tasks:** tau-bench rewards correct `transfer_to_human_agents` behavior through its own scoring. No special-casing: the reward is the truth, including for escalations. (This conveniently also tests Day 3's HUMAN_ESCALATION mapping in the opposite domain.)

### Step 2 — context for the corpus

The corpus needs a tau-bench `context.yaml` (operations keyed to `tool.*` span names: `book_reservation`, `update_reservation_flights`, etc.). Look first in `~/kairos` / `~/tau-agent` for a startup-era one. If absent, write minimal: one operation per domain (airline, retail), `required_side_effect_tools` = the write-action tools (`book_reservation`, `cancel_reservation`, `update_*`, `return_*`, `exchange_*`, `modify_*`), expected = observed read+write set (pull from the archive DB span-name table in the audit — it's already enumerated). Half an hour, not a project. Commit as `eval/corpus/taubench/context.yaml`.

### Step 3 — agreement run (`eval/run_agreement.py`)

```pseudocode
result = kairos analyze --normalized-dir eval/corpus/taubench --context .../context.yaml
for trace in result:   kairos_verdict = outcome_pass | outcome_fail | non_computable
join with labels.jsonl on trace_id

confusion matrix over computable ∩ binary-labeled:
                    bench PASS    bench FAIL
  kairos PASS           a             b
  kairos FAIL           c             d
accuracy = (a+d)/(a+b+c+d)
kappa: po = accuracy
       pe = ((a+b)(a+c) + (c+d)(b+d)) / n²
       κ  = (po - pe) / (1 - pe)
abstention_rate = non_computable / total          # Kairos saying "can't tell" is a tracked
                                                  # metric, NOT an error — but >30% abstention
                                                  # makes κ unrepresentative; report both
top-10 disagreements: trace link, reward, kairos failure_reason, 1-line note each
→ eval/reports/taubench-agreement.md  (+ .json for machines)
```

### Decision tree at end of Day 6

```
κ ≥ 0.7  AND abstention ≤ 30%  ──►  proceed to Day 7 as planned
κ < 0.7                        ──►  Day 7 morning = iterate W3 against the
                                    disagreement analysis (the budgeted rework slot);
                                    labeling compresses to the afternoon
abstention > 30%               ──►  inspect: usually a pairing/normalization bug
                                    (truncated artifacts), not an outcome bug —
                                    fix the loader before touching outcome logic
pairs < 75                     ──►  fall back to archive-DB span extraction for
                                    the largest bundle only; if still <75, proceed
                                    with what exists and SAY SO in the report —
                                    a small honest n beats a fabricated large one
```

**Architect's note:** resist the urge to "fix" disagreements one by one until κ looks good — that's fitting the test set. Fix only *classes* of disagreement (e.g. "all failures with reason=missing_side_effect trace back to a normalization gap"), and log every change made while looking at this corpus. The corpus is burned as a neutral benchmark the moment you tune on it; that's fine for the sprint (it's a development corpus), but it's why the roadmap's W8 injection harness — which regenerates fresh — is the durable artifact.

---

## Day 7 — Human labeling + `redundant_execution` redefinition

### Step 1 — export (`eval/export_labeling.py`, morning, ~1h build)

```pseudocode
source = latest post-Day-5 live analysis (the Honest Snapshot run)
sample = stratified(findings, by=(pattern_name, primary_workflow), n=50)
       + sample(traces with zero findings, n=20)
emit labeling.csv:
  id | phoenix_url | pattern | primary_workflow | steps | context | verdict | note
where context = for each affected step: "tool(args≤120ch) → output≤120ch", joined
       (enough to judge WITHOUT opening Phoenix for most rows; link for the rest)
SECURITY: run the Day 9 redaction patterns over context BEFORE writing the CSV —
       it gets committed; tokens/keys in Bash args are live in these traces.
       If redaction isn't built yet (it's Day 9), grep-audit the CSV by hand. Do not skip.
```

### Step 2 — labeling protocol (owner, ~90 min)

Written at the top of the CSV so future labelers (or future-you) apply the same standard:

```
verdict ∈ {TP, FP, UNSURE}
A redundancy/loop finding is TP iff: knowing what the agent knew at that step,
the repeated call could have been skipped with NO loss of correctness.
  - Re-reading a file AFTER editing it           → FP (state changed)
  - Re-running tests after a fix                  → FP (that's the point of tests)
  - Identical Read, no intervening writes         → TP
  - Polling a status endpoint until ready         → FP unless count is absurd (>5: judgment)
UNSURE is a legitimate verdict; do not force it. >20% UNSURE = the finding
definition is ambiguous — that itself is the finding.
Clean traces: skim, note anything that obviously should have been flagged ("missed: ...").
```

### Step 3 — measure + redefine (afternoon)

```pseudocode
precision(rule) = TP / (TP + FP)                  # UNSURE excluded, reported
if precision(redundant_execution) < 0.7:          # expected outcome
    redefine:
      fire iff  same tool
            AND jaccard(args_norm)   ≥ 0.85       # raise floor from current behavior
            AND output_similarity    ≥ 0.90       # NEW: same question + same answer = waste;
                                                  #      same question + new answer = progress/polling
                                                  # similarity = jaccard over output token sets,
                                                  # or exact-match on sha1 for outputs >4KB (cheap)
            AND tool not in op.redundancy_exempt  # reuse the Day 5 per-op config surface
            AND prev.status == OK                 # existing retry guard, keep
    re-measure on the SAME labels; iterate threshold once if needed (document both runs)
if still < 0.7: demote rule to severity "info" (one-line config/constant change)
                — it keeps running, keeps appearing in triage features, stops
                claiming attention it hasn't earned
loop_detected / stuck_loop: same measurement; expected to hold up better
                (identical-output × 3+ is a strong signal); demote likewise if not
```

**Self-consistency check (scheduled now, executed Day 14):** owner relabels 15 random rows blind; agreement <80% → tighten the protocol text before any roadmap-scale labeling.

**Edge cases:** fewer than 50 findings exist post-Day-5 dedup (likely — the 706 was inflation) → label all of them, report n, no padding; a finding spanning steps the CSV context window misses → labeler uses the Phoenix link, notes it (signals the digest builder on Day 9 needs those steps too); labels disagreeing with tau-bench-domain behavior → exemptions are per-op config (Day 5 surface), never hardcoded — coding ops get exemptions, tau ops don't.

### Day 7 exit

- `eval/corpus/organic/labels.csv` committed (redacted).
- Per-rule precision recorded in `eval/reports/organic-precision.md`.
- `redundant_execution` at ≥0.7 precision or demoted.
- One paragraph appended to the Honest Snapshot: "what changed in detector definitions and why" — the snapshot's numbers shift when the definition does; the delta chain must record the break (same config-hash discipline as everything else).

---

## What Days 6–7 hand to Phase 3

1. A trusted (κ-measured) outcome signal → Day 11's report can rank workflows by outcome_rate without lying.
2. Precision-known findings → Day 8's triage weights mean something; demoted rules contribute features, not severity.
3. A labeling protocol + corpus seed → roadmap W8/W9 start from here, not zero.
4. A disagreement habit: every number that feeds a decision has a "show me the traces" path. Keep that property through the loop — it's the difference between this and every dashboard nobody trusts.
```
