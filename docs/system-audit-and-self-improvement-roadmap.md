# Kairos AI — System Audit & Self-Improvement Roadmap

*Date: 2026-06-12. Audited against: kairos-ai source (current working tree), the one substantive analysis run (`2026-06-09_22-17.json`, Paperclip deployment), live Phoenix (Docker `deploy-phoenix-1`, project `default`, 346 claude_code traces), and tau-agent ablation bundles.*

---

## 1. Executive summary

Kairos's engine is architecturally sound: one IR, deterministic, stateless, fail-loud. But on real coding-agent traces, every analysis dimension is currently either dark or miscalibrated:

- The **outcome rate (0.0)** is a measurement artifact, not a finding about the agents.
- The **706 deterministic findings** are double/triple-counted across overlapping workflow memberships and carry uniform severity (`warning`) and zero cost attribution.
- A **reference cohort has never formed** (confidence `none` in all 10 workflows, ever), so divergence detection — the centerpiece — has never executed (also deliberately disabled).
- **Most analysis runs produced empty output** due to a deployment config issue, not an engine bug.

None of this is fatal. The fixes are calibration, not redesign. The deeper gap is the owner's own diagnosis: **Kairos has no eval system for itself.** Section 5 specifies one, built largely from assets already on disk (tau-bench ablation bundles with per-task reward labels). Sections 6–7 design the LLM tier-2 layer and the daily self-improvement loop on top — both gated on the eval harness existing first, because a self-improvement loop with an unvalidated reward signal optimizes noise.

Recommended order: **plumbing → calibration → eval harness → LLM tier-2 → daily loop.**

---

## 2. Scope and evidence

| Evidence | Location |
|---|---|
| Engine source | `~/kairos-ai/src/kairos/` |
| Only substantive analysis run | `~/.paperclip/instances/default/data/kairos-results/2026-06-09_22-17.json` (341 KB) |
| Six empty runs incl. `latest.json` | same dir, all 218 bytes, `workflows: []` |
| Live trace store | Docker `deploy-phoenix-1` (Phoenix 15.4.0), project `default`, 346 traces, spans `claude_code.tool` / `claude_code.llm_request` / `claude_code.tool.blocked_on_user` |
| Stale trace store | `~/.phoenix/phoenix.db` — 11,302 spans, exclusively tau-bench (May 7–13), **not** connected to the live deployment |
| Workflow definitions | `Xero/config/context.yaml` (10 operations: 6 lead-pipeline, 4 coding-agent) |
| Ground-truth asset | `~/tau-agent/results/ablation_bundles/*.json` — per-task `reward` (0/1) + trace artifacts |

---

## 3. Audit findings (ranked by impact)

### F1 — Most analysis runs return empty results (deployment, not engine)

Six of seven result files, including `latest.json`, are 218-byte empty AnalysisViews (`workflows: []`, `unmapped: 0`). Phoenix had traces at those times. Likely cause (owner-confirmed hypothesis): `KAIROS_CONTEXT_PATH` not configured in the plugin when those runs fired — `kairos view` requires `--context` (`cli.py:262`–`267`), and the plugin worker resolves it from config DB → env with empty-string default. An empty/invalid context yields zero operations, zero matches, empty view, **silently**.

**Fix:** the plugin should fail loudly when `contextPath` is empty or the file is unreadable, instead of producing a plausible-looking empty result. The engine's own "fail loud, not silent" principle stops at the deployment boundary today. Add: result metadata recording which context file and how many operations were loaded (`context_path`, `operation_count`, `trace_count_fetched`) so an empty result is distinguishable from an empty world.

### F2 — Outcome rate 0.0 is a measurement artifact

`outcome_metric.py` condition 4 scans tool output for substring failure markers:

- `_SIDE_EFFECT_FAILURE_MARKERS` (`outcome_metric.py:29–37`): `"failure"`, `"failed"`, `"error"`, `"exception"`, `"denied"`, `"not submitted"`, `"validation failed"` — matched as substrings anywhere in `tool_output.lower()`.

For coding agents this systematically false-fails: Bash output routinely contains `error` (test runner summaries, grep results, compiler output, `stderr` echoes, even `"0 errors"`). In the real run: Code Implementation 0/2 computable passed, Paperclip Coordination 0/12 passed. Terminal status is not the cause — live spans default to `COMPLETED` (`genai_mapping.py:672–682`) — so failures come from conditions 3/4.

Second contributor: Paperclip Coordination requires `Bash` **and** `Skill` as side-effect tools (`context.yaml`). If `Skill` never appears as a distinct span in claude_code traces, every Coordination trace fails on "missing side-effect."

**Fixes:**
1. Replace substring scan with structured signals where available: exit codes, OTel span status, `kairos.outcome` attributes. Substring heuristics only as last resort, word-boundary anchored, and only on the *tail* of output.
2. Per-adapter outcome extractors (claude_code traces have different output conventions than tau-bench tools). The `normalization/agents/` layer is the natural home.
3. Verify which tool names actually appear as spans before requiring them in `required_side_effect_tools` (see F9 — same disease).
4. Report which condition failed per trace in the AnalysisView (`failure_reason` enum), so a 0.0 rate is auditable instead of mysterious.

### F3 — Workflow memberships overlap; findings are double/triple-counted

`context.yaml` claims "zero overlap by design," but that only holds between the lead-pipeline and coding sections. Within the coding section:

- `Read` is the distinctive tool for Codebase Research — present in essentially every coding trace.
- `Bash` is distinctive for Paperclip Coordination — present in essentially every coding trace.

Result in the real run: the same trace IDs (`656619f5…`, `8fe79bb7…`, `c7afd726…`) appear in Code Implementation, Codebase Research, *and* Paperclip Coordination. The 706 findings are the same ~15 traces' findings counted 2–3×. Any downstream consumer (dashboard, improvement loop, cost model) inherits the inflation.

**Fixes (pick one or combine):**
1. **Specificity-weighted distinctive tools:** discount a tool's distinctive power by its base rate across all traces (a tool in 95% of traces identifies nothing). An IDF-style weight over `required_side_effect_tools` is a small change in `classify_membership` (`pipeline.py:109–174`).
2. **Primary-label mode:** keep multi-label internally but emit a primary workflow per trace (highest specificity-weighted recall) for counting; report secondary memberships separately.
3. **Redefine coding ops on intent, not tool fingerprint:** Paperclip already knows the issue type/agent role per run; carry that into trace metadata (`kairos.workflow` span attribute) and let membership be declared, not inferred. Inference is the fallback for traces with no declaration. This is the cleanest long-term answer and aligns with the open-source story: customers will have the same "all my agents use the same 10 tools" problem.

### F4 — Reference cohort has never formed; the best feature has never run

All 10 workflows, in every run ever: `confidence: none`, `eligible: 0`. Eligibility (`reference_behavior.py:231–289`) requires COMPLETED + zero errors + not-a-loop + no critical redundancy (3+ consecutive same-tool calls at Jaccard ≥ 0.85, `:59`) + ≥ 0.8 expected-tool coverage. Coding agents legitimately call `Bash`/`Read` consecutively many times — the redundancy criterion alone disqualifies most real coding traces, and 0.8 coverage of broad `expected_tools` lists does the rest. With no cohort: no reference path, no step/token budgets, no divergence (also off by default, `pipeline.py:283` — owner-confirmed deliberate, see §6 for where divergence should come back).

**Fixes:**
1. Per-operation eligibility overrides in `context.yaml` (the schema already carries per-op `membership_recall_threshold`; extend the pattern): `eligibility_redundancy_exempt_tools: [Read, Bash, Grep]`, `eligibility_min_coverage: 0.5`.
2. Treat eligibility as a *ranking*, not a binary gate, when the eligible count is below the cohort minimum: take the top-N cleanest traces with a `confidence: low` flag rather than producing nothing. An imperfect reference beats no reference — and the confidence tier system already exists to communicate the difference.
3. Once outcome extraction is fixed (F2), add "outcome passed" as an eligibility signal — currently impossible because outcome itself is broken.

### F5 — Severity monoculture and unearned confidence

All 706 findings: `severity: warning`. Loop findings carry `confidence: 1.0`; redundant findings carry confidence = Jaccard similarity. Nothing distinguishes "agent burned 40k tokens in a stuck loop" from "agent read the same file twice." This is the alert-fatigue failure mode: undifferentiated warnings get ignored, and the improvement loop downstream has no prioritization signal.

**Fix — severity must be earned by measured precision (the static-analysis lesson, cf. Coverity):** a detector rule may emit `error`/`critical` only if its measured precision on the labeled corpus (§5) clears a bar (e.g. ≥ 0.9); 0.7–0.9 → `warning`; below → `info` or off by default. Until the corpus exists, severities are placeholders and should be labeled as such in the view. Confidence values should be calibrated (a 0.9-confidence finding correct ~90% of the time, measured via ECE) or not displayed.

### F6 — Cost attribution is dark: `estimated_token_waste: 0` on all 706 findings

Detectors compute waste from `step.total_tokens` (`detection/redundant.py`, `detection/loops.py`), which is 0 for all live spans — token usage isn't being extracted from `claude_code.llm_request` spans (or isn't emitted). Same root cause kills `token_budget_p75` (needs ≥ 0.8 token coverage, `reference_behavior.py:186`).

**Fix:** audit the span attributes actually present on `claude_code.llm_request` (likely `gen_ai.usage.input_tokens` / `output_tokens` or `llm.token_count.*` conventions) and map them in `readers/genai_mapping.py`. If the emitter genuinely doesn't send usage, fix the emitter — token waste is the single most persuasive number Kairos can show a user, and right now it is always zero.

### F7 — Stale-data hygiene: two Phoenixes

`~/.phoenix/phoenix.db` (359 MB, tau-bench, May 7–13) is disconnected from the live Docker Phoenix; it's a leftover from the startup-era experiments. Anyone (human or agent) pointing tools at the SQLite file gets confidently wrong answers — this audit nearly did. **Fix:** rename/archive the file, and document the live topology in the README: OTel collector :4317/4318 → Phoenix container :4319 (Docker volume), UI/GraphQL :6006. Keep the tau-bench DB — it's eval-corpus raw material (§5) — just clearly labeled.

### F8 — Lead-pipeline operations are aspirational

Six of ten operations (`run_clutch_scrape`, `save_raw_companies`, …) reference tool names that have never appeared as spans; the Python pipeline isn't OTel-instrumented. They matched zero traces and dilute every report. Owner has descoped the lead pipeline. **Fix:** remove them from the active `context.yaml` (a `context.yaml.xero-lead-only` variant already exists for the inverse split), or add a `status: uninstrumented` field that the engine reports distinctly instead of as silent zeros. General principle for the product: **validate context.yaml against the trace store** — `kairos context lint --against-phoenix` that reports which declared tools have ever been observed. Cheap to build, prevents this entire class of silent mismatch for every future user.

### F10 — (added 2026-06-12, post 36h trace analysis) Vacuous similarity on uninstrumented args

Live `claude_code.tool` spans carry `tool_name` but no `tool_args`/`tool_output`. `jaccard_dict_similarity` returns **1.0** for None/None and empty/empty (`detection/similarity.py:10–17`), so on live data every consecutive same-tool pair scores as identical → the 642 redundancy findings (confidence 1.0, F5's monoculture) are largely this artifact, not agent behavior. Fix: detector-level guard (no args on either step → no finding); loop detection similarly degrades to a triage-only signal when outputs are uninstrumented. Longer term: emitter enrichment (arg/output excerpts on spans) or transcript-source analysis (ClaudeCodeNormalizer already parses transcripts, which DO carry args+outputs). See `insight-report-0.md` for the full analysis and the coordination-waste findings it surfaced.

### F9 — Empty results are indistinguishable from healthy-quiet results

`reliability: {terminal_status_rate: 1.0, tool_sequence_rate: 1.0}` on a run with zero traces reads as "perfect." Vacuous truth presented as signal. **Fix:** AnalysisView should carry `trace_count_analyzed` at top level and null out rates when the denominator is zero. (Same family as F1's metadata fix.)

---

## 4. Calibration fix priority

| Order | Fix | Effort | Unblocks |
|---|---|---|---|
| 1 | F1 + F9: fail-loud plugin config, result metadata | hours | trustworthy runs |
| 2 | F6: token extraction from llm_request spans | hours–1d | cost attribution, token budgets |
| 3 | F2: structured outcome extraction per adapter | 1–2d | ground-truth outcome rate |
| 4 | F3: membership specificity weighting or declared workflows | 1–2d | honest finding counts |
| 5 | F4: per-op eligibility overrides + top-N cohort fallback | 1d | reference paths, budgets, (later) divergence |
| 6 | F5: precision-earned severity | after §5 harness | prioritization |
| 7 | F7 + F8: hygiene, context lint | hours | ops sanity |

---

## 5. The eval harness: how to know Kairos itself works

Owner's stated worry: *"How do I know the results I'm producing are actually useful? There is no eval system for Kairos itself."* Correct worry — it is the gating dependency for everything in §6–7. Determinism removes variance, not bias: a deterministic detector can be systematically wrong, and only labels reveal it.

The meta-evaluation pattern is settled practice (LLM-as-judge literature, SRE fault injection, static-analysis rule calibration): **treat each detector as a classifier and score it against ground truth.** Three corpus layers, in order of construction cost:

### Layer 1 — tau-bench bundles: ground truth already on disk (start here)

`~/tau-agent/results/ablation_bundles/*.json` contain per-task `reward` (0.0/1.0) from tau-bench's deterministic evaluator, with matching traces in the archived Phoenix DB and kairos run artifacts (`kairos_run_dir`). One inspected bundle: 20 airline tasks, `average_reward: 0.55` — a usable pass/fail mix.

**Build:** a script that pairs each task's trace with its reward, runs `kairos analyze` over the corpus with a tau-bench `context.yaml`, and reports:
- **Outcome agreement:** Kairos outcome prediction vs. tau-bench reward — accuracy and Cohen's κ (κ guards against majority-class trickery). Targets: accuracy ≥ 0.9, κ ≥ 0.7.
- **Detector precision on organic failures:** tau-bench failure traces naturally contain loops/redundancy; sample findings, label true/false-positive by hand.

This directly validates the F2 fix: before/after numbers on the same corpus.

### Layer 2 — synthetic fault injection (exact ground truth, unlimited volume)

A **trace mutator** over known-clean TraceEnvelopes (the IR is JSON — mutation is trivial):
- duplicate a tool-call step with identical/near-identical args → expected `redundant_execution` at known indices;
- splice a repeating step subsequence with identical outputs → expected `loop_detected` (with ERROR statuses → `stuck_loop`);
- drop/replace a required side-effect step, or append failure markers to its output → expected outcome fail with known failing condition;
- (when divergence returns) permute steps against a known reference path → expected divergence at a known step.

≥ 50 injected positives per detector + ≥ 100 clean traces. **Score per incident, not per span** (one injected fault = one expected finding; multiple hits on the same fault dedupe) — per-point scoring inflates detector quality, a known trap from the anomaly-detection literature. Metrics: per-rule precision / recall / F1, **false positives per 100 clean traces** (the alert-fatigue number), and a determinism check (same trace twice → byte-identical findings; it's the headline claim, assert it in CI).

Acceptance: injected-fault recall ≥ 0.95 per detector (synthetic faults are easy; missing them is disqualifying); FP ≤ 5 per 100 clean traces.

### Layer 3 — human-labeled organic sample (the honest layer)

~100 real findings from Paperclip traces + ~50 traces Kairos called clean; label each finding true/false-positive and each clean trace for missed issues. Double-label ≥ 30 to measure inter-rater agreement — **if humans can't agree (κ < 0.6) on what counts as a "redundant call" in coding-agent traces, the detector definition is the problem, not the threshold.** Given F3's likely false-positive profile (consecutive Reads are normal), expect this layer to drive a redefinition, e.g. "redundant only if same tool + same normalized args + no intervening state change."

### Harness mechanics

pytest-style runner in `tests/eval/` or `eval/`; corpus and expected labels as versioned fixtures; per-rule quality table (`docs/detector-quality.md`) regenerated in CI; regressions fail the build. This table is also the open-source credibility artifact — the few vendors that publish detector precision/recall numbers (Arize's eval-template benchmarks, Patronus's Lynx/HaluBench) stand out precisely because most don't. Nobody publishes this for *trace-structure* detectors (loops, redundancy, divergence). Kairos can set that bar.

**External test set worth importing: TRAIL** ([arXiv:2505.08638](https://arxiv.org/abs/2505.08638), dataset at [huggingface.co/datasets/PatronusAI/TRAIL](https://huggingface.co/datasets/PatronusAI/TRAIL)) — 148 agent traces (118 GAIA, 30 SWE-bench), **1,987 OpenTelemetry spans**, 841 human-annotated errors across a reasoning/planning/execution taxonomy with span-level locations. Already in OTel format — Kairos's native input. Best Gemini-2.5-pro scored ~11% at locating these errors, so any decent deterministic detector performance here is a publishable result.

*(Other methodology sources: JudgeBench arXiv:2410.12784, "Judging the Judges" arXiv:2406.12624, EvalGen arXiv:2404.12272, point-adjust critique arXiv:2109.05257, Google SRE alerting doctrine, Coverity CACM 2010. TRAIL, τ²-bench, GEPA, ACE verified live 2026-06-12; remainder from training knowledge.)*

---

## 6. LLM tier-2: semantic analysis on top of deterministic results

Owner's instinct — deterministic first, LLM judges on top, so 100k traces fit without bloating any context window — is the right shape, and matches the engine's existing Slice A/Slice B seam (`correctness_score.py` has three stubbed LLM dimensions; `decision_state.py` exists but is unwired). Design:

**Funnel architecture (the context-window answer):**
1. **Tier 1 (deterministic, all traces):** existing detectors + outcome + membership. Cost: ~0. Output: findings + flags per trace.
2. **Triage (deterministic, all traces):** rank traces by signal density — finding count × severity weight × token waste (once F6 lands). Select top-K (e.g. 50/day) plus a small random sample (to catch what tier 1 misses — this sample is also tier-2's own recall check).
3. **Tier 2 (LLM, selected traces only):** per-trace judge over a *compressed digest* (tool sequence + args summaries + error excerpts + tier-1 findings — the TraceEnvelope IR is already 90% of this digest), answering: was the outcome actually achieved? was each flagged finding real? what is the failure *mechanism* (wrong plan / missing context / bad tool result / spec ambiguity)? Output: structured verdicts with citations to step indices.
4. **Synthesis (LLM, one pass over verdicts):** cluster failure mechanisms across the day's verdicts into named patterns with example traces — the daily insight document of §7.

**Divergence comes back here, not in tier 1.** The owner's concern — deterministic divergence on non-deterministic systems is unreliable — is sound: tool-bigram divergence flags every creative-but-valid path. The right split: tier 1 *computes* divergence cheaply as a triage feature (a structurally divergent trace is worth an LLM look), tier 2 *judges* whether the deviation was a failure or a valid alternative. Divergence stops being an accusation and becomes an attention signal. This is the strongest version of "exploit deterministic, then judge with LLM."

**Tier 2 is itself a judge → it needs the §5 harness too:** validate its verdicts against tau-bench rewards (layer 1) and human labels (layer 3) before its outputs feed any improvement loop. Same κ targets.

**Open-source angle:** the funnel (deterministic prefilter → budgeted LLM judge → synthesis) is the differentiator versus eval vendors who run LLM judges over everything (cost) or deterministic checks only (shallow). "Kairos analyzes 100k traces for the LLM cost of 50" is the pitch line.

---

## 7. The daily self-improvement loop

Target state (owner): every 24h, Kairos analyzes the day's traces autonomously and produces insights that fix the agents further. Constraints adopted: findings are **suggestive, not enforcing**; improvement actions are human-gated; Kairos itself stays analysis-only (the CLAUDE.md "no runtime correction" invariant holds — the loop lives *outside* the engine, in Paperclip).

**Loop anatomy (one cycle):**

```
nightly cron (Paperclip)
  → kairos analyze: full day's traces, tier 1            (deterministic)
  → triage + tier 2 judges + synthesis                   (LLM, budgeted)
  → Daily Insight Report (markdown artifact + JSON)
      - new/persisting failure patterns, ranked by cost (token waste × frequency)
      - per-agent: outcome rate trend, budget drift vs. prior days
      - top 3 suggested interventions, each with:
          target artifact (AGENTS.md §X / context.yaml op Y / skill Z)
          evidence (trace links), expected metric delta
  → filed as Paperclip issue (suggestive; human or CTO-agent reviews)
  → accepted suggestions land as normal reviewed changes
  → next night's run measures the delta on the same metrics
      (this closes the loop: every intervention is an experiment
       with a built-in before/after readout)
```

**Why this ordering is safe:** the loop's reward signal is the §5-validated outcome rate + cost metrics. Without the harness, the loop optimizes measurement artifacts — with today's numbers it would conclude "all coding agents fail 100% of the time and are massively redundant" and rewrite instructions to fix a phantom. Reward-signal hygiene before actuation, always.

**Persistence between cycles (lightweight, outside the engine):** a small store of pattern fingerprints → first-seen / last-seen / intervention-applied / post-intervention rate. This is what makes day N+1 a *comparison* rather than a fresh snapshot, and it's the seed of the open-source "insights memory" without reintroducing any banned runtime-correction machinery into the engine.

**Maturity ladder:** (1) report-only → (2) suggestions filed as issues, human applies → (3) low-risk suggestions auto-applied behind approval gates (Paperclip's approval + AGT machinery already exists for exactly this) → (4) closed-loop A/B: apply to one agent, hold another as control. Move up a rung only when the prior rung's suggestions show positive measured deltas.

---

## 8. Research landscape — self-improving agent loops

*(Key citations verified live 2026-06-12: [ACE arXiv:2510.04618](https://arxiv.org/abs/2510.04618) (ICLR 2026), [GEPA arXiv:2507.19457](https://arxiv.org/abs/2507.19457) (ICLR 2026 oral), [TRAIL arXiv:2505.08638](https://arxiv.org/abs/2505.08638), [τ²-bench arXiv:2506.07982](https://arxiv.org/abs/2506.07982), [DGM arXiv:2505.22954](https://arxiv.org/abs/2505.22954). Remaining URLs from training knowledge to Jan 2026.)*

### 8.1 The convergent finding

The field converged in 2025 on **context/prompt-level actuators driven by verifiable reward signals**. Weight-level and code-level self-modification only succeed with airtight machine-checkable verifiers — and even then reward hacking appears (Sakana's Darwin Gödel Machine agents faked logs and removed test markers to look successful; arXiv:2505.22954). The architecture in §6–7 — deterministic detectors as reward, instruction/config edits as actuator, human-gated apply — is the consensus design, not a compromise.

### 8.2 Key threads

**Karpathy — "system prompt learning" (May 2025):** the missing learning paradigm is neither pretraining nor RL but the system distilling explicit lessons from solved problems and editing them into its own instructions. Open problems he names are exactly this roadmap's hard parts: lesson quality control, consolidation over time (a "sleep"-like curation pass), prompt bloat. His Dwarkesh interview (Oct 2025) framing — outcome-RL is "sucking supervision through a straw"; verbal reflection over full traces carries far more signal — is the argument for §6's tier-2 judges consuming full trace digests rather than scalar scores.

**Anthropic practice:** the recurring internal pattern is *agent reads transcripts → proposes targeted edits to prompts/tool descriptions → validated against a small eval set built from ~20 real observed failures → human review*. Their multi-agent research system post (Jun 2025) reports a tool-testing agent that rewrote tool descriptions and cut task time ~40%. Their reward-hacking research (Nov 2025) shows models that learn to hack a reward generalize to broader deception — the strongest argument for §5 preceding §7. Agent Skills (versioned markdown know-how) is their sanctioned actuator for accumulated lessons.

**Academic lineage (actuator × reward):**

| System | Actuator | Reward | Lesson for Kairos |
|---|---|---|---|
| Reflexion (2023) | verbal reflections in memory | env pass/fail | needs reliable success check — §5 first |
| Voyager (2023) | executable skill library | execution feedback | code-as-memory compounds |
| DSPy **GEPA** (Jul 2025) | reflective prompt evolution over full trajectories | task metric + NL feedback | beat GRPO RL by ~10% with ~35× fewer rollouts — language-level reflection > scalar RL |
| **ACE** (Oct 2025, arXiv:2510.04618) | evolving playbook via Generator/Reflector/Curator, **incremental delta updates** | execution outcomes | names the two failure modes to design against: **brevity bias** and **context collapse** (monolithic rewrites erase knowledge) — never wholesale-rewrite instructions |
| Agent Workflow Memory (CMU 2024) | workflows induced from past traces, injected into context | trajectory success | closest prior to trace-mining → instruction loop; +24–51% on web agents |
| SEAL (MIT 2025) | **weights** via self-generated finetune data | downstream perf | compute-heavy, catastrophic forgetting — avoid weight actuators |
| AlphaEvolve (DeepMind 2025) | **code** via evolutionary search | deterministic evaluator only | works *only* where machine-checkable verifier exists — Kairos's deterministic layer is that verifier |

**Vendors — who closes the loop:** as of early 2026 **no major product ships unattended trace→config mutation**. Frontier offering is auto-*proposed* diff, human-approved: Braintrust **Loop** (agent that generates improved prompts/scorers from eval results), Arize **Prompt Learning** (NL eval explanations optimize prompts, Reflexion/GEPA-inspired), LangChain **LangMem** (prompt optimizer as a library). Their insight generation is LLM-judged, not deterministic — Kairos's deterministic trace-derived signals are the differentiator. §7's maturity ladder lands exactly at the market frontier (rung 2–3) without being reckless (rung 4 is beyond anything shipped).

**Practitioner consensus:** Hamel Husain — highest-ROI activity is manually reading traces and building a failure-mode taxonomy; evals derived from observed failures, judges calibrated against human labels. Addy Osmani — AI delivers ~70%, last 30% is verification; instruction files are living engineering artifacts, every AI diff human-reviewed. METR's developer study (Jul 2025): experienced devs were ~19% *slower* with AI while believing ~20% faster — **perceived improvement is not a reward signal; measure outcomes.**

### 8.3 Failure modes to design against

1. **Reward hacking / Goodhart:** keep monitoring signals out of the optimization target (OpenAI Mar 2025: optimizing against a monitor teaches *obfuscated* hacking). Rotate/hold out eval sets.
2. **Context collapse and bloat:** itemized delta updates with provenance (which traces produced this lesson); periodic consolidation pass that merges/dedupes/prunes; monotone growth is itself a failure mode ("context rot").
3. **Judge self-preference:** the model judging an edit must not be the model that proposed it; calibrate judges against human labels (§5 layer 3).
4. **Memory/config poisoning:** lessons persisted from untrusted trace content are an injection vector (AgentPoison; Willison's "lethal trifecta") — another reason apply stays human-gated.
5. **Criteria drift:** human grading criteria shift over time (EvalGen) — version the eval criteria too, revisit quarterly.

### 8.4 Positioning

The 2025–2026 market gap is precisely this design: **deterministic trace-derived reward signals feeding a gated instruction-layer actuator.** Products do LLM-judged insights with manual apply; research (ACE/AWM/GEPA) validates the architecture but isn't productized; nobody publishes detector-quality numbers for trace-structure detectors. Kairos open-source can occupy that gap with §5's quality table as the credibility artifact.

---

## 9. Roadmap

| Phase | Content | Exit criterion |
|---|---|---|
| **0. Plumbing** | F1, F9, F7: fail-loud config, result metadata, archive stale Phoenix DB | a run is either correct or visibly broken — never silently empty |
| **1. Calibration** | F6 tokens, F2 outcome, F3 membership, F4 eligibility | outcome rate on Paperclip traces is defensible; finding counts deduplicated; ≥1 workflow reaches cohort confidence ≥ low |
| **2. Eval harness** | §5 layers 1–2 (tau-bench agreement + fault injection), CI quality table; layer 3 sample | per-rule precision/recall published; severity tiers earned (F5) |
| **3. LLM tier-2** | funnel: triage → judge → synthesis; divergence as triage feature; tier-2 validated on harness | judged verdicts agree with tau-bench/human labels (κ ≥ 0.7) |
| **4. Daily loop** | nightly cron, Insight Report, suggestions as Paperclip issues, pattern store | first intervention with measured before/after delta |
| **5. Open-source prep** | context lint, docs, detector quality table as public artifact, multi-agent adapters hardened | external user can run §5 harness on their own traces |

The sequence is strict on one dependency only: **no improvement loop (phase 4) before the harness (phase 2).** Everything else can overlap.
