# P3 — Eval Layer: Design Exploration

> Status: design exploration (pre-build). Author: synthesis from competitive research + LLM-judge
> literature + the Self-Harness paper, grounded in Kairos's existing trace-ingest + failure-clustering stack.
> Companion to the Sprint-2 OSS-platform direction. Nothing here is committed to code yet.

---

## 0. One-line thesis

**Kairos closes the loop that no open-source tool closes: real agent traces → verifier-grounded
failure clusters → auto-generated regression evals → gate every change on replayed history →
(later) propose the fix.** The clustering brain is the moat; the eval layer is what turns clusters
into a forcing function on quality.

---

## 1. The gap (why this is worth building)

From the competitive scan (Braintrust, LangSmith, Langfuse, Arize Phoenix, DeepEval, Ragas, promptfoo,
OpenAI Evals, Inspect AI, Patronus/Galileo/HoneyHive):

**Table stakes (everyone has):** LLM-as-judge scoring, offline dataset experiments, basic CI eval,
human annotation queues, span/trace logging.

**The hole — and it is specific:**

| Capability | OSS status |
|---|---|
| OTel-native agent tracing | Covered (Phoenix, Langfuse) |
| Manual dataset build + annotation | Covered |
| Failure **clustering** from live traces | **Not in OSS** (Arize AX paywalled; Braintrust "Topics" closed) |
| Evals **auto-generated** from failure clusters | **Not in OSS** (Latitude GEPA is closed) |
| Regression eval gating on **historic** traces after every change | **Not in OSS** (Braintrust closed; everyone else = manual curation) |
| Issue lifecycle (cluster → open → eval → resolved → regressed) | **Not in OSS** (Latitude closed) |
| Meta-eval of the auto-generated evals (MCC vs reality) | **Not in OSS** (research/closed only) |
| Causal failure attribution across a trajectory | **Research only** (CHIEF, CausalFlow — not productized) |

The single most valuable intersection — **[real agent traces] × [failure clustering] ×
[auto-generated regression evals gated on every change]** — is almost entirely proprietary
(Braintrust SaaS, Latitude SaaS). Kairos already owns the first two pillars (own ingestor +
`discover.py` clustering). P3 adds the third and converts the whole thing into the OSS standard.

Sources: Braintrust Topics (closed); Latitude GEPA (closed); Langfuse has tracing but no clustering /
no auto-eval-from-failures (their own GitHub discussion #5206 confirms the trajectory-eval gap);
Arize Phoenix OSS lacks clustering (paywalled in AX); UK AISI's own reports document the
"evaluation gap" (benchmark performance overstates real-world utility) but don't close it.

---

## 2. Principles (the spine)

These are non-negotiable; every design decision below follows from them.

1. **Deterministic-first. The LLM judge is narrow and residual.** Anything with a precise spec
   (tool-call validity, schema, retries, latency/token budget, known-cluster membership, outcome
   state) is checked programmatically. The judge is reserved for the *irreducibly subjective*
   (intent satisfaction, helpfulness) — and even then, reference-anchored and bias-corrected.
   This is already Kairos's locked "deterministic-first, LLM judge deferred" decision, and the
   research backs it hard (§4).

2. **Verifier-grounded, not judge-grounded.** Clusters and eval labels are grounded in *real
   outcome signal* (did the contract complete? did the tool error? `is_error` from the hook truth),
   never in an LLM's opinion of the output. This is exactly Self-Harness's "verifier-grounded failure
   patterns" and the reason clustering on judge output is a trap — it inherits every judge bias.

3. **Eval on history after every change — held-in AND held-out.** No detector, prompt, policy, or
   harness change ships without replaying the clustered corpus before/after. **Held-in:** did the
   change help the *targeted* failures. **Held-out:** did anything *else* regress (blast radius).
   Two-sided. This is the EDDOps offline loop, Husain's regression discipline, and Self-Harness's
   "regression test on held-in and held-out splits" — three independent sources converging.

4. **Fixed evaluator = stable ruler.** To attribute a delta to *the change*, the evaluator must be
   frozen and deterministic-enough. Kairos's existing k=2 non-determinism check enforces this. If the
   ruler drifts, the delta is noise. (Self-Harness: model weights + evaluator stay fixed; only the
   harness changes.)

5. **Reject → log, don't ship.** A failed eval is a *gate*, not advice. Rejected changes are recorded
   with their eval evidence; the active system is untouched. (Self-Harness: rejected candidates logged,
   active harness unchanged. Kairos: shadow discipline.)

6. **Eval-the-evaluator.** Auto-generated evals can be wrong, and a miscalibrated eval is *worse than
   none* (false confidence). Every generated eval carries a quality score (agreement vs reality, e.g.
   MCC / Cohen's κ) that is recomputed as new traces arrive; evals that drift below threshold are
   retired. The eval harness is itself observable.

---

## 3. The Kairos eval architecture

Five components. The first three are the OSS-uncontested core; 4–5 are the discipline that keeps them honest.

### 3.1 Cluster → eval-set generation (the GEPA-class move, OSS-first)
For each verifier-grounded failure cluster from `discover.py`, synthesize an **eval case set**:
- **Held-in set** = the cluster's own traces (the failures this change should fix).
- **Held-out set** = a stratified sample of *passing* traces + *other-cluster* traces (the blast-radius guard).
- The eval per case is the **cheapest discriminator that separates the failure mode from passing**:
  prefer a deterministic assertion (tool-call/sequence/schema/outcome check) derived from the cluster's
  `dominant_feature`; fall back to a reference-anchored LLM judge only when the failure is semantic.
- Cases are frozen (golden) with their outcome ground-truth, keyed by `(cluster_key, detector_version)`.

This is the missing bridge between observability and eval infra. We already store the structured trace
(spans + hook truth + steps) and the cluster primitive (`cluster_key = tool_signature::dominant_feature`),
so the inputs exist; P3 is the synthesis + freezing.

### 3.2 Regression-on-history gate (extends the existing three-tier harness)
Kairos already has `src/kairos/eval/` with a **three-tier gate (GATE / REVIEW / INFO)**, k=2 determinism,
worktree-isolated before/after `compare`, and a stored `eval_runs` time series. P3 wires the
cluster-derived sets into it:
- On any detector/prompt/policy change → `compare(pre, post, k=2)` over **held-in + held-out**.
- **GATE (hard):** grounded-quality metrics (outcome precision/recall, tau-style) must not drop > ε → else REGRESS, block.
- **REVIEW (soft):** per-cluster precision/recall surfaced, never auto-blocks.
- **INFO:** fire-rate / severity diagnostics.
- Held-out regression (blast radius) is a *first-class* GATE signal, not an afterthought.

### 3.3 Issue lifecycle (cluster as a first-class entity)
No OSS tool models failure modes with state. Kairos should:
`new cluster detected → ISSUE opened → eval generated → resolved when CI passes the eval N consecutive
runs → REGRESSED (reopened) if the cluster reappears or the eval fails again.`
This is what turns the dashboard from "logs" into a closed observability→development loop, and it maps
cleanly onto our existing `discovery_queue` + `findings` tables (add a status/lifecycle column).

### 3.4 Trajectory diff as a deterministic gate (PRM applied to eval, no judge)
Outcome-only eval can't tell "efficient success" from "stumble-and-recover." We already have the raw
span tree + hook-enriched steps, so we can diff the **trajectory** between runs: tool-call sequence,
arg structure, sub-agent handoffs, restart/recovery shape. A "trajectory regression" is when the causal
path changes unexpectedly even if the final output looks similar. Deterministic, cheap, no LLM. (This is
the process-reward insight — AgentPRM — used for *eval*, not training; EvalView proves the narrow
single-agent version, we extend to the full DAG we already store.)

### 3.5 Meta-eval (MCC-gated eval quality)
Every auto-generated eval gets an agreement score vs reality (Matthews Correlation Coefficient against
the real pass/fail of incoming traces, and Cohen's κ vs owner labels where available). Evals below
threshold are retired or flagged. This is the "eval-the-evaluator" loop applied to our own generated
tests, and it directly answers "why is LLM-as-judge dangerous" — we never trust a judge (or a generated
eval) we haven't calibrated.

---

## 4. LLM-as-judge: failure modes and the residual role

The literature is damning enough that the judge must be *contained*, not central.

**Failure modes (named, evidenced):** position/order bias (κ 0.807→0.639 uncontrolled; >10pp swing on
swap), verbosity/length bias (+17pp for longer-equal-quality), self-preference / same-family narcissism
(10–25% inflation), formatting/sycophancy (6–22%), poor calibration (no uncertainty → coin-flip on
ambiguous), non-determinism (best judges 70–80% self-agreement, weak 40–50%), prompt sensitivity, and —
critically for agents — **gameability: rewriting an agent's chain-of-thought while holding actions fixed
inflates judge false-positives up to ~90%.** On JudgeBench's curated pairs, zero-shot frontier judges
score near chance. (MT-Bench, G-Eval, "Justice or Prejudice"/CALM, JudgeBench, PoLL, "Gaming the Judge",
AgentRewardBench — see §7.)

**The residual role — three tiers:**
- **Tier 0 — no LLM (default):** tool validity, schema, args, retries, timeouts, latency/token, outcome
  state, known-cluster membership. LLM here = pure downside (variance, cost, gameability).
- **Tier 1 — reference-anchored judge:** only where outcome state is observable but *intent satisfaction*
  is ambiguous. Locked rubric (G-Eval/RULERS style, evidence-anchored), position-swap over 2 calls,
  discard inconsistent. **Publish Cohen's κ vs human gold before any Tier-1 judge goes live.**
- **Tier 2 — discovery, not scoring:** when detectors flag a *novel* pattern outside the taxonomy, use a
  **Panel of LLMs (PoLL)** — 3 *disjoint-family* models — to *hypothesize a failure-mode label* (cheaper
  and less biased than one GPT-4-class judge). Output is a candidate cluster name for owner review, never
  a quality score.

**Hard rules (encode as guardrails):** (1) judge never shares model family with the agent; (2) never score
explanation verbosity; (3) never bare pointwise 1–5 without rubric decomposition; (4) **never feed the
agent's own CoT to the judge as evidence** (up to 90% FP inflation); (5) never use a judge where an
instrumentable oracle exists.

---

## 5. Minimal auto-eval we can ship NOW (cheap, deterministic, high-ROI)

These need no LLM and catch real regressions today, reusing what's already in the repo:

1. **Tool-call correctness** — right tool, right args (schema), right order for deterministic workflows.
   We have redacted args (hook truth) + the span tree.
2. **Known-good / known-bad floors** — a small frozen set where the answer is unambiguous: good cases must
   still pass, bad cases must still fail. The binary regression floor. (We have owner labels +
   tau-bench ground truth as seeds.)
3. **Golden-trace replay** — freeze a known-good trajectory; on a new version, diff tool calls/args
   deterministically; allow ≤N valid variants for non-determinism. (§3.4.)
4. **Outcome-grounded checks** — `is_error` (hook truth, post-emitter-lie-fix), contract completion,
   error_count, struggle ratio. Already computed by our detectors.
5. **Cluster-membership regression** — does a previously-fixed cluster reappear? (§3.3 lifecycle.)

All five feed the existing GATE tier. This is the "minimal auto-eval" deliverable and it's mostly wiring
existing signals into the gate — not new science.

---

## 6. Self-Harness, mapped onto Kairos

The paper (Shanghai AI Lab) independently validates our loop. Mapping:

| Self-Harness stage | Kairos equivalent | Status |
|---|---|---|
| Weakness Mining (run → traces → **verifier-grounded** clusters) | own ingestor + `discover.py` clustering | **have it** |
| Harness Proposal (model proposes **bounded** edits from clusters) | propose detector/policy/prompt edits | **future** (P3+/P4) |
| Proposal Validation (regression on **held-in + held-out**, accept/reject) | the eval gate (§3.2) | **building (P3)** |
| Fixed model + fixed evaluator | k=2 stable ruler | **have it** |
| Reject → logged, no change | shadow + issue lifecycle | **building** |

**What we adopt directly:** (a) clusters must be verifier-grounded; (b) the gate is held-in *and*
held-out; (c) the evaluator is frozen during a comparison; (d) rejected changes are logged, never shipped;
(e) proposed edits are *bounded* and reviewable (when we add the proposal stage, constrain it to
middleware/policy/validation-step patches, not arbitrary rewrites).

**Where Kairos goes beyond the paper:** Self-Harness is a closed research loop on benchmark tasks. Kairos
runs on *real production traces*, auto-builds the eval splits *from real clusters*, and (P3) ships this as
the open-source standard with the clustering + meta-eval algorithms transparent and extensible — exactly
what a self-hosted/safety-conscious team can't get from Braintrust or Latitude.

---

## 7. Build plan (phased, reuse-first)

Reuse `src/kairos/eval/` (three-tier gate, k=2, corpus, `eval_runs`), `discover.py` (clusters),
detectors, and the own-ingest trace store. Net-new is the cluster→eval bridge, lifecycle, meta-eval, and
trajectory diff.

- **P3.1 — Minimal auto-eval into the gate (§5).** Wire deterministic checks (tool-call, known-good/bad,
  outcome, golden replay) as GATE inputs. Lowest risk, immediate value.
- **P3.2 — Cluster → eval-set generation (§3.1).** Freeze held-in/held-out sets per `cluster_key`.
- **P3.3 — Regression-on-history gate (§3.2).** Every change → before/after over the cluster corpus,
  blast-radius as a GATE signal. (Extends existing `compare`.)
- **P3.4 — Issue lifecycle (§3.3).** `discovery_queue`/`findings` get status; UI cluster view shows
  open/resolved/regressed (builds on the F2.3 cluster browser).
- **P3.5 — Trajectory diff gate (§3.4)** and **meta-eval / MCC (§3.5).**
- **P4 (later) — Proposal stage** (Self-Harness's bounded-edit proposer), gated by everything above.

---

## 8. Non-goals / risks

- **Not** building a general LLM-judge platform — the judge stays residual (§4). Competing on judge
  features is a losing race.
- **Not** competing on storage/scale yet (Postgres + IR; ClickHouse only when a real query forces it).
- **Risk:** auto-generated evals that don't correlate with reality → mitigated by §3.5 meta-eval (retire
  drifting evals).
- **Risk:** clustering on the wrong grain produces useless eval sets → keep clusters verifier-grounded and
  let the issue lifecycle (resolved/regressed) be the feedback that prunes bad clusters.
- **Citation caveat:** some 2026 arXiv IDs gathered in research were not independently re-verified; confirm
  before quoting specific IDs externally.

---

## 9. The pitch, in one paragraph

Open-source LangSmith gives you traces. Kairos gives you traces **plus a failure-clustering brain that
auto-writes the regression evals and gates every change on your own history** — verifier-grounded,
deterministic-first, with the LLM judge contained to the one job it's good at and calibrated before it's
trusted. That closed loop (`traces → clusters → auto-evals → gate → propose`) exists today only as closed
SaaS. Kairos makes it the open standard.
