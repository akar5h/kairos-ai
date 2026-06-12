# Kairos 14-Day Sprint — Thesis-Critical Path

*Date: 2026-06-12. This document supersedes the schedule in `self-improvement-plan.md`; that document and `self-improvement-engineering-spec.md` remain the full roadmap and the detailed specs. Work items below reference spec sections (W1–W16) with KEEP / SIMPLIFY / CUT decisions.*

> **Update 2026-06-12 (post manual 36h trace analysis — see `insight-report-0.md`):**
> 1. **Day 2 discovery is done.** Live `llm_request` spans carry top-level `input_tokens` / `output_tokens` / `cache_read_tokens` / `cache_creation_tokens`; mapping starts immediately.
> 2. **Day 11's issue join is trivial.** Spans carry `paperclip.issue` + `paperclip.run_id`; activity_log correlation is the fallback only.
> 3. **New finding F10:** live `claude_code.tool` spans carry NO args/output, and `jaccard_dict_similarity(∅,∅) == 1.0` (`similarity.py:10-17`) — the historical 642 redundancy findings are vacuous-similarity artifacts. Day 3 gains a guard (detector must not fire on uninstrumented args); Day 7's labeling will partly measure a name-only signal — transcript cross-reference compensates; emitter arg/output enrichment is roadmap.
> 4. **Day 13's intervention is pre-selected with evidence:** the coordination diet (wire Paperclip's existing MCP server + rewrite the coordination section of AGENTS.md; CTO agent first, others held as control). Baseline numbers live in `insight-report-0.md`.

## The thesis being tested

**A governed self-improvement loop on agent traces works:** nightly deterministic analysis + budgeted LLM judging produces an intervention that, once applied through review, measurably moves an agent's outcome or cost metric. Fourteen days buys one honest pass through that loop — not a launchable product. Everything cut here is cut because the thesis survives without it; the cut list IS the post-sprint roadmap.

## What got cut and why the thesis survives

| Cut | Why thesis survives | Returns |
|---|---|---|
| W5 cohort/divergence | Loop runs on findings + outcome + token waste alone; reference paths add precision, not existence | Roadmap phase 2 |
| W8 fault-injection harness | Trust for 14 days = tau-bench agreement + one human labeling pass; injection corpus is the *durable* trust artifact, mandatory before market, not before thesis | First post-sprint item |
| W10 CI quality table / earned severity | Same — severity stays `provisional` everywhere, view says so | With W8 |
| W11 TRAIL | Pure market credibility | Launch prep |
| W4 specificity-weighting machinery | At one-deployment scale, hand-fixing the 4 coding op definitions achieves the same dedup; the algorithm generalizes it later | OSS prep |
| Full `context lint` | Micro version (observed-tool report) suffices to fix the ops | OSS prep |
| W3 full integrity gating | Simple orphan-parent check only | Hardening |

**Accepted risk, stated plainly:** detector trust will rest on ~250 tau-bench labels + ~50 human labels + one self-consistency check. Good enough to believe a measured delta; not good enough to publish numbers. Do not market the sprint output as validated — that's what the roadmap is for.

## Day-by-day

**Day 1 — Stop the lying (W1 simplified + hygiene).**
Plugin hard-errors on missing/empty context. `meta` block in AnalysisView (engine_version, context_sha256, operation_count, trace_count_fetched/analyzed). Null reliability when zero traces. Archive `~/.phoenix/phoenix.db` → `phoenix-taubench-archive-2026-05.db`. Micro-lint: one script printing observed span tool names + per-tool trace base rate from live Phoenix (feeds Day 5). *Skip: batch-mode partial-merge handling (batch path unused at current volume).*
**Exit:** empty-world run visibly empty; misconfig run errors; tool base-rate table in hand.

**Day 2 — Tokens (W2 full, discovery pre-done).**
Keys confirmed on live spans (top-level `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`) → mapping in `genai_mapping.py` directly. `total_tokens` = output + uncached input; cache reads separate field, never waste. Freed discovery time goes to the F10 guard: redundancy/loop detectors must skip (not fire at confidence 1.0) when both steps' args are uninstrumented.
**Exit:** ≥80% of LLM steps carry nonzero tokens; F10 guard unit-tested.

**Days 3–4 — Outcome v2 (W3 core).**
Evidence chain: `kairos.outcome` attr → OTel span status → claude_code adapter extractor → word-boundary markers on last 500 chars only. `blocked_on_user` → HUMAN_ESCALATION (counts as pass; `human_escalation_rate` reported separately — OWNER-DECISION default adopted). `failure_reason` enum per trace surfaced in view. Orphan-parent check → `partial` → non-computable. The `"0 errors"` fixture test ships.
**Exit:** owner spot-checks 20 live trace verdicts, ≥90% agreement. *This is the sprint's load-bearing day — if it slips, slip Day 5, not this.*

**Day 5 — Membership dedup (W4 simplified) + Honest Snapshot.**
Hand-rewrite the 4 coding ops: Code Implementation distinctive `[Edit, Write]`; Research distinctive `[Read]` + new `excluded_tools: [Edit, Write]` field; Coordination distinctive `[Skill]` (drop Bash — Day 1 base rates will confirm it's ubiquitous; if Skill spans don't exist, Coordination goes honest-unmapped for the sprint rather than building arg-pattern matching). Primary-label emission: findings counted once under primary workflow. Lead-pipeline ops moved to `context.lead-pipeline.yaml.disabled`. Then: full re-run → `docs/honest-snapshot-1.md` — the baseline.
**Exit:** mean memberships/trace ≤1.5; snapshot committed.

**Day 6 — tau-bench agreement (W7 compressed).**
Loader pairs bundle `checkpoint_rows` rewards with normalized traces from `kairos_run_dir` artifacts (skip archived-DB spelunking; bundles' own artifacts first). Target ≥150 pairs. Report: accuracy, Cohen's κ, confusion matrix, top-10 disagreement analysis.
**Exit:** κ measured. κ ≥0.7 → proceed. κ <0.7 → Day 7 morning becomes a W3 iteration using the disagreement analysis; labeling shifts to afternoon. (Budgeted: this is the sprint's one planned rework slot.)

**Day 7 — Human labeling + redundancy fix (W9 lite).**
Export 50 stratified findings + 20 clean traces with Phoenix links. **Owner labels: ~90 min.** Measure redundancy precision. Expected: low → redefinition (same tool + args Jaccard ≥0.85 + output similarity ≥0.9 + exempt list + retry guard) tuned on the labels, re-measured same day. Rules below 0.7 precision demote to `info`.
**Exit:** per-rule precision known; `redundant_execution` either ≥0.7 or demoted.

**Day 8 — Triage (W12 lite).**
Score = severity-weighted findings (primary workflow only) + token waste + outcome_fail. No divergence term. Top-K + 5 random clean traces (judge's recall probe). Deterministic, seeded by run date.
**Exit:** ranked list reproducible.

**Days 9–10 — Tier-2 judge (W13 — no cuts on safety).**
Digest builder (≤5k tokens, first/last 150-char output excerpts, findings inline). Redaction pass (planted-secret tests) — not skippable, digests leave the machine. Injection guard (fenced data blocks; instruction-following from trace content = flagged finding) — not skippable. Strict JSON verdict schema, step-index validation, one retry. **Validation gate compressed:** 50 tau-bench digests (not 100), κ vs reward ≥0.7 required before any verdict feeds Day 11. Judge model ≠ future intervention-proposer model, or at minimum distinct prompt + logged model ID.
**Exit:** gate report committed; judge trusted or iterated.

**Day 11 — Report + pattern memory (W14 simplified).**
Issue-level aggregation KEPT (single `run_id`→issue join via activity_log — per-heartbeat verdicts mislead; this is cheap and load-bearing). Pattern store as JSON file (not sqlite): fingerprint → first/last_seen, count, examples, config_hash, intervention_ref. Daily report (md+json): patterns ranked by occurrences × waste, outcome/escalation rates per agent, top-3 suggested interventions with evidence links + expected delta. LLM writes prose; numbers come from the store; template enforces it. Config_hash covers context.yaml + detector config, NOT agent instructions (so the intervention itself doesn't break baselines).
**Exit:** report renders on live data and on an empty day.

**Day 12 — Nightly runner (W15 compressed) + first live night.**
Standalone script under launchd (sidecar pattern). 26h overlapping window, dedupe vs store. Hard token cap, logged truncation. Failure → skip-marker report, never silence. Files report as Paperclip issue (`kairos-daily` label); suggestions logged to decision_ledger (`improvement.suggested`). `.env.example` updated.
**Exit:** first unattended night produces an issue.

**Day 13 — Apply the pre-selected intervention (W16 start).**
Candidate already evidence-backed by `insight-report-0.md`: the **coordination diet** — (I1) wire Paperclip's existing MCP server (`~/dev/paperclip/packages/mcp-server`: list_issues, checkout_issue, add_comment, update_issue, approvals) into the CTO agent's session; (I2) rewrite CTO AGENTS.md coordination section (MCP tools not curl; never poll — inbox empty → end turn; never re-derive tokens; Grep/Glob/Read over shell). I1+I2 land together as one PR; other agents held as controls. If the Day 12 report surfaces something even stronger, owner may override — but the default is set. Intervention stamped in store (commit hash).
**Exit:** intervention live on CTO only.

**Day 14 — Second night + direction check.**
Verify the loop survived its second night, targeted pattern's occurrence didn't obviously invert, paired metrics (outcome + cost together — never cost alone) collected. **The full ≥3-night delta and `case-study-1.md` complete themselves passively over the following week — no active work, just nights passing.** Day 14 closes the sprint; the case study closes the thesis.

## Owner time budget (total ≈ 5h across 14 days)

- Day 0: confirm the 5 OWNER-DECISIONs (defaults pre-adopted above where the sprint forced them) — 30 min
- Day 4: 20-trace outcome spot-check — 45 min
- Day 7: labeling — 90 min
- Day 13: report review + intervention approval — 60 min
- Day 14+: case-study read — 30 min

## Slip policy

Single-track plan, no parallel slack. If a day slips: protect Days 3–4 (outcome), Days 9–10 (judge safety), Day 12 (first night). Sacrifice in order: Day 8 sophistication (triage can be "sort by finding count"), Day 11 store richness (counts only), Day 6 pair count (75 pairs still beats zero). Never sacrifice: redaction, injection guard, the judge validation gate, the spot-check. A loop that's late is a delay; a loop trusted without checks is a thesis-invalidating result you can't detect.

## Post-sprint roadmap (the cuts, in return order)

1. W8 fault-injection harness + W10 CI quality table + earned severity — the durable trust artifact; first thing after the case study.
2. W5 cohorts + divergence as triage signal — precision upgrade to the loop.
3. W11 TRAIL benchmark + full `context lint` + W4 specificity algorithm — OSS/launch credibility.
4. Auto-apply graduation, A/B canary, multi-adapter — per the master plan's far roadmap.
