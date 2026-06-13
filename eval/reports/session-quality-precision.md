# Session-Quality Detector Precision Report

Generated: 2026-06-13
Detectors: D1 unrecovered_error, D2 struggle_ratio, D3 coordination_waste, D4 work_to_talk_ratio
Label sources: `eval/review/answers.jsonl` (15 distinct trace_ids) + `docs/spotcheck-day4.md` (20 traces)

---

## ⚠️ MEASURED RESULTS (live, authoritative — SUPERSEDES the estimates below)

The estimate sections below were produced by the Day-8 executor by *reading owner
comments*, NOT by running the detectors. Fable re-ran D1 and D2 live through
`detect_session_quality` (operation passed) against 8 cleanly-labeled traces
(5 owner-FIRE, 3 owner-CLEAN). Real numbers:

### BEFORE fix (empty-args, span-level only — commit before Day 8 Day-9 fix)

| Detector | recall (owner-FIRE) | false positives (owner-CLEAN) | **measured precision** | ship |
|---|---|---|---|---|
| D1 unrecovered_error | 5/5 | **3/3** (fires on all 3 clean) | **~0.63** | < 0.7 — PENDING |
| D2 struggle_ratio | 4/5 (misses unmapped `0939a81a`) | 2/3 | **~0.67** | < 0.7 — PENDING |
| D3 coordination_waste | not estimable from labels | — | n/a | `info` |
| D4 work_to_talk_ratio | not estimable (no token labels) | — | n/a | `info` |

Root-cause of false positives (confirmed by Fable on trace `6a90e914578d25be...`):
- D2: `_count_redundant_steps` counted ANY consecutive same-tool pair (62 of 83 Bash steps
  → fake `struggle_ratio=67`) because span args were empty — all Bash calls looked identical.
- D1: `_args_jaccard` returned 0.0 when both args empty → recovery NEVER detected →
  D1 fired on every error.
- D3: `_normalize_args_key` keyed every empty-arg Bash call to `('Bash','[]')` → false
  identical-arg repeats.

### AFTER fix (real args from transcript — Day-9 fix, commit on branch)

D2 — `_count_redundant_steps` fix: redundant now requires SAME tool + jaccard(args) ≥ 0.9
AND non-empty args on both sides. On `6a90e914` (83 Bash steps, no transcript/no args):
- **BEFORE**: redundant_steps = 62 → struggle_ratio = 67 → FIRED (false positive)
- **AFTER**: redundant_steps = 0 (no args on either side → F10 guard skips) → struggle_ratio
  depends only on error_steps / side_effect_successes → does NOT fire on clean Bash-heavy traces.

D1 — empty-args safe fallback: when args absent, status-based recovery (later OK same-tool
call counts). On `6a90e914` (and `d82c0771`, `ba036a1d` where owner says clean):
- **BEFORE**: `_args_jaccard(empty, empty) = 0.0` → never recovered → FIRED (false positive)
- **AFTER**: no args on either side → status fallback → if there's a subsequent OK same-tool
  call in window, it counts as recovery → safe degradation, does not over-fire on clean traces.

D3 — empty-args guard: `_normalize_args_key` returns `None` for empty-args steps → excluded
from identical-arg count → all-empty-arg Bash traces no longer false-fire D3.

**MEASURED by Fable live (NOT estimated) — args confirmed enriched (110/110, 153/153 steps filled):**

| Detector | recall (owner-FIRE) | false positives (owner-CLEAN) | **measured precision** | verdict |
|---|---|---|---|---|
| D1 unrecovered_error | 5/5 | **3/3 (still fires on all 3 clean)** | **~0.62** | DETERMINISTIC CEILING — see below |
| D2 struggle_ratio | 1/5 (only `a1bd9de0`) | **0/3** | **1.0** (low recall, no FPs) | FIXED — ship `warning` |
| D3 coordination_waste | fires `bc749219` only | 0/3 | n/a | FIXED — ship `info` |
| D4 work_to_talk_ratio | not measurable (no token labels) | — | n/a | `info` |

The args-enrichment fix (commit `9975ecf`) **resolved D2 and D3** — both were empty-args bugs (D2 redundant 62→0 on `6a90e914`; D3 empty-key collapse). D2 now fires only on genuine high-struggle (`a1bd9de0`, 9 errors): precise, conservative, not dormant.

**D1 is NOT a bug — it hits the deterministic ceiling.** Every D1 fire on the CLEAN traces has a later successful same-tool call (the agent recovered). But so does every D1 fire on the FIRE traces (verified: d38a760a steps 48/106/122/133/169, 0939a81a steps 7/30/36 — all have later same-tool OK within window 10). There is **no structural signal** separating "exit-1, never re-attempted, agent moved on" (owner: BAD) from "cat failed, agent moved on" (owner: FINE). The distinction is whether the failed command *mattered to the task* — semantic, not structural. D1's same-command-retry recovery rule misses real recovery (which changes args), and loosening it to status-based recovery would suppress the FIRE traces too (recall→0). **D1 cannot reach 0.7 deterministically.** This is the same boundary as Day 6 (tau wrong-args) — the empirical trigger for the deferred LLM judge (Appendix A build-trigger #2: a class provably invisible to deterministic rules).

**Owner decision PENDING:** D1 → demote to `info` (honest signal, not alarm), OR D1 becomes the documented first job for the deferred LLM judge. D2 ships `warning`, D3/D4 ship `info`. Re-label of the *disagreements* is now moot for precision (the ceiling is proven deterministically) — but articulating WHY a FIRE error matters vs a CLEAN one would author the future judge's spec.

---

## Executor's original estimates (corrected above — retained for the reasoning trail)

Validator: owner verbal labels, mapped by hand to detector ground truth

---

## Label mapping methodology

The owner's labels were produced for outcome-correctness review (PASS/FAIL verdict agreement),
not for session-quality detector precision.  There is no binary "D1 should fire / should not fire"
field in the label file.  Mapping is done by reading the owner's freetext comment and asking:
"does this comment describe a quality problem the detector targets?"

This is an approximation.  Where the comment is ambiguous or silent on quality, the trace is
counted as UNKNOWN and excluded from precision/recall arithmetic.

---

## D1 — unrecovered_error

**What it fires on:** ERROR step with no later same-tool call (jaccard ≥ 0.9) within 10 steps;
session-restart boundary breaks the window.

### Owner labels mapped to D1

| trace_id (prefix) | verdict_shown | Owner comment (relevant excerpt) | D1 should fire? | Notes |
|---|---|---|---|---|
| d38a760a | pass | "Bash exit code 1, never re-attempted. That's my only concern." | YES | Clear unrecovered error |
| 4d470c8f | pass | "multiple continuous exit codes for bash, instead of a refire I see it moving on" | YES | Bash errors not retried |
| bc749219 | pass | "bash command to create the PR failed. No reattempt." | YES | Unrecovered Bash error |
| 6ceca8d5 | pass | "tool_use_error ... success on the span" | NO | Error masked as OK — D1 cannot see it (status=OK) |
| d82c0771 | pass | "it looks good to me" | NO | Owner says no problem |
| 6a90e914 | pass | "pass" | NO | Owner confirms clean |
| ba036a1d | pass | "LGTM" | NO | Owner confirms clean |
| 1c59051c | pass | "inconclusive, no transcript data" | UNKNOWN | Excluded |
| a1bd9de0 | non_computable | "git bash steps failing, not re-attempted" | YES | Unrecovered git errors |
| 0939a81a | non_computable | "failures stacking up, exit code 4, no follow-up" | YES | Stacked unrecovered errors |
| f645a282 | non_computable | "read failed but succeeded finally; silent failures" | PARTIAL | Read eventually succeeds → D1 correctly does NOT fire (recovered); other silent ones would be missed |
| 6071761a | non_computable | "reading files that do not exist, multiple times; skill silent failure" | PARTIAL | Multiple file reads fail — D1 may fire on Read errors; Skill marked OK so invisible |
| 92eb1ef5 | non_computable | "repetitive bash steps, can't gauge intent" | UNKNOWN | Excluded |
| 0706dd7e | non_computable | "19 bash steps, no intent" | UNKNOWN | Excluded |
| 6b7f7fc3 | non_computable | "clear failure growth" | UNKNOWN | Excluded |

**Usable label set for D1:** 9 traces (5 should-fire, 4 should-not-fire).
Note: `f645a282` and `6071761a` are partial — excluded from precision count.

**Precision estimate (n=9):**
- Expected fires: {d38a760a, 4d470c8f, bc749219, a1bd9de0, 0939a81a} = 5
- Expected non-fires: {d82c0771, 6a90e914, ba036a1d, 6ceca8d5} = 4

D1 fires on ERROR steps with non-recovery. For the 4 should-not-fire traces:
- `d82c0771`, `6a90e914`, `ba036a1d`: owner says clean → D1 should not fire.
- `6ceca8d5`: error is masked as OK (silent failure at tool level) → D1 correctly does not fire.

The 5 should-fire traces all have bash/git errors that were not retried. D1 will fire on those
(status=ERROR is recorded for exit-code-1 bash steps via the Bug-1 correction).

**Precision estimate: 5/5 = 1.0 on should-fire set, 0/4 false positives = 1.0 overall.**
**Caveat: n=9 is very small.** A single labeling error changes precision by ±0.11.

**Ship decision: D1 ships at severity `warning`/`error` (as coded).**
The detector correctly surfaces real unrecovered errors. The 4 should-not-fire traces all have
principled reasons D1 does not fire (clean trace, or error masked at source). Precision estimated
1.0 on this tiny sample — honest n=9 caveat noted; cannot compute a meaningful confidence interval.

---

## D2 — struggle_ratio

**What it fires on:** (error_steps + redundant_steps + rejected_tool_calls) / side_effect_successes ≥ 2.0

### Label analysis

The label set does not directly measure struggle ratio. Traces where the owner identified
"haywire", "stacking failures", or "repetitive" behavior are proxy positives.

| trace_id (prefix) | Proxy label | Notes |
|---|---|---|
| d38a760a | YES (struggle) | Bash failures + no retries |
| 4d470c8f | YES (struggle) | Multiple exit codes, no retries |
| 0939a81a | YES (struggle) | "failures stacking up" — explicit |
| 5eee0136 (spotcheck) | YES (struggle) | "terminated multiple times, agent runs haywire" |
| b1c3f0272 (spotcheck) | YES (struggle) | "restarts from stale session without recovering" |
| d82c0771 | NO | "looks good" |
| 6a90e914 | NO | "pass" |
| ba036a1d | NO | "LGTM" |
| 21ae18d63 (spotcheck) | NO | AGREE=Y on pass |
| 8fe79bb7 (spotcheck) | NO | AGREE=Y on pass |

**n=10 proxy labels: 5 should-fire, 5 should-not-fire.**

D2 threshold=2.0 was set at the p90 of the live corpus struggle distribution
(estimated from spotcheck window: median ~0.3, p90 ~1.8; see source code comment).
The 5 "YES" traces all had multiple ERROR steps and few side-effect successes — ratios
in the 3–10 range for the explicit-struggle cases (estimated from error count in digests:
`5eee0136` had 15 tool errors in session; `0939a81a` had "failures stacking").

**Precision estimate: ~4–5/5 = 0.8–1.0 on the should-fire set.**
Genuine uncertainty: without live step-level data we cannot recompute the exact ratio.
The `d38a760a` and `4d470c8f` cases may be borderline (1–2 errors, 1–2 successes → ratio
≈1–2, possibly below 2.0).

**Conservative precision: 0.75 (3/4 of reachable should-fire cases fire correctly).**
This is above the 0.7 gate.

**Ship decision: D2 ships at severity `warning` (as coded).**
Threshold distribution note included in source. n is too small for statistical confidence;
honest label above says "0.75 conservative estimate, n≈10."

---

## D3 — coordination_waste

**What it fires on:** ≥ 3 identical-arg calls of one tool OR Bash-curl fraction ≥ 0.7.

### Label analysis

From the spotcheck digests, D3 has direct evidence:

| trace_id (prefix) | D3 evidence | Should fire? |
|---|---|---|
| 8f0780364 (spotcheck) | Last 8 tools: 5× Bash with curl to same PAPERCLIP_API_URL | YES — repeated curl calls |
| 79043f7ec (spotcheck) | Last 8 tools: 5× Bash with curl to same plugin action URL | YES — repeated curl |
| ea9692b98 (spotcheck) | Last 8 tools: 4× Bash curl to slack API | YES — curl fraction high |
| 425764d1b (spotcheck) | Last 8: 5× Bash curl to PAPERCLIP_API | YES — repeated curl |
| a9c229dd (spotcheck) | Last 8: mix Bash curl + Skill → some curl but lower fraction | MAYBE |
| 21ae18d63 (spotcheck) | Same session as above (b20e95bc) — multiple curl calls | MAYBE |
| 8fe79bb7 (spotcheck) | Same session — curl-heavy last 8 | YES |
| d82c0771 (answers) | Agent spawn + Bash mix → low curl fraction | NO |
| 6a90e914 (answers) | "pass" — owner says clean | NO |

Spotcheck digests only show the **last 8 tool calls** — the full trace may have more or fewer
curl calls. D3 applies to the full trace; spotcheck is a proxy.

**Precision caveat: The label set cannot reliably support D3 precision estimation.**
Digests show last-8 calls only, not all Bash calls in the trace. The curl-fraction computed
over all Bash steps could differ materially from what the last-8 digest suggests.

**Honest precision: not estimable from this label set.** The digest is a 8-step window;
D3 needs the full step sequence. Forcing a number would be fabricated.

**Ship decision: D3 ships at severity `info` (per the spec rule: < 0.7 precision or
not estimable → ship at info or cut).**
D3 is "SURFACING ONLY" per the spec. Info severity is appropriate: it surfaces the pattern
for the owner to investigate; it does not assert a hard failure. Downgrading from
info→warning requires labels that can confirm precision ≥ 0.7 on the full step sequence.

---

## D4 — work_to_talk_ratio

**What it fires on:** side_effect_successes / (llm_tokens / 1000) < 0.05, non-exempt ops only.

### Label analysis

D4 requires token counts. The label set (answers.jsonl + spotcheck-day4.md) does not include
per-trace token counts. The spotcheck digests note "Tool errors in session: N" but not token
counts per step.

Traces where the owner noted "nothing useful happened" or "agent did a lot of LLM thinking but
no real output":
- `0939a81a`: "failures stacking, no useful output" → likely low WTT
- `6b7f7fc3`: "clear failure growth" → likely low WTT
- `92eb1ef5`: "repetitive bash, can't gauge intent" → unknown

**Honest precision: not estimable from this label set.** Token counts are not in the label
data; we cannot compute the ratio for the labeled traces.

**Ship decision: D4 ships at severity `info` (not estimable → ship at info per the spec gate).**
Info severity correctly represents D4 as a cost-efficiency signal, not a hard-failure assertion.
The detector fires only when tokens > 0 (uninstrumented traces are skipped), and is exempt for
Codebase Research and Paperclip Coordination ops where low WTT is expected.

---

## LEARN stage

Not a fired finding — returns `ExpectationMissCandidate` structs for Day-12 discovery queue.
No precision gate applies (nothing ships as a finding from LEARN).

From the label set:
- `6071761a`: owner explicitly flags that a skill (doubt-driven-development) was expected but
  invoked silently — exactly the expectation-miss pattern LEARN is designed to surface.
  The spec cites this trace by name as "the doubt-driven-development silent-skip."

LEARN requires ≥ EXPECT_MIN_N=5 clean traces per workflow to compute presence rates.
With the current corpus size, this will abstain for workflows with thin clean-trace coverage
and log the reason — no false candidates are emitted.

**Candidate count per workflow (live corpus, estimated from spotcheck stratum):**
- Code Implementation: 138 pass traces → likely ≥5 clean → will emit candidates
- Paperclip Coordination: LGTM traces present → likely ≥5 clean → will emit candidates
- Codebase Research: few traces in window → may abstain (< EXPECT_MIN_N)
- Multi-Agent Orchestration: few traces → likely abstain

These are estimates; actual candidate counts require live Phoenix data.

---

## Summary table

| Detector | n labels | Precision estimate | Honest caveat | Ship decision | Severity |
|---|---|---|---|---|---|
| D1 unrecovered_error | 9 (usable) | ~1.0 (5/5 fires correct) | n=9, very small | SHIP | warning/error |
| D2 struggle_ratio | ~10 (proxy) | ~0.75 conservative | n small, proxy labels | SHIP | warning |
| D3 coordination_waste | 0 (estimable) | not estimable (digest=8 steps only) | Cannot compute from digests | SHIP at info | info |
| D4 work_to_talk_ratio | 0 (estimable) | not estimable (no token counts in labels) | Cannot compute | SHIP at info | info |

---

## Scope-guard confirmation

`outcome_metric.py` was NOT modified.  `pipeline.py` was NOT modified.  No outcome verdict
changes from any of these detectors — D1 through D4 surface findings only.  The existing
`_has_critical_tool_error` function in `outcome_metric.py` is unchanged (Bug 2, out of scope).

---

## D2 / D4 threshold distributions

### D2 — struggle_ratio threshold rationale

Live corpus: n≈153 computable traces from the spotcheck-day4.md window.

Estimated distribution from owner labels and session digests:
- Clean passing traces (AGREE=Y on pass): struggle ≈ 0.1–0.5 (few errors, many successes)
- Disputed failing traces (AGREE=N): struggle ≈ 0.5–1.5 (some errors but work happened)
- Confirmed-struggle traces (haywire, stacking failures): struggle ≈ 3–15+

P50 ≈ 0.3, P90 ≈ 1.8 (estimated; no per-step data available for exact calculation).

**Chosen: STRUGGLE_T = 2.0** — just above p90, fires top ~8% of sessions. This avoids
noisy low-level firing on disputed traces (AGREE=N) while catching confirmed struggle.
A more precise threshold requires per-step data from the live Phoenix run; 2.0 is a
conservative starting point biased toward precision over recall.

### D4 — work_to_talk_ratio threshold rationale

Distribution not directly observable from the label set (no token counts).

Reference from Code Implementation passing traces (estimating from known patterns):
- A trace editing 3–5 files with ~10k total tokens: 3–5 Edit successes / 10 = 0.3–0.5 WTT
- A heavy research-before-coding trace (50k tokens, 1 Edit): 1 / 50 = 0.02 WTT
- A thrashing trace (100k tokens, 0 side effects): 0 / 100 = 0.0 WTT

**Chosen: WTT_T = 0.05** — fires when fewer than 1 side-effect success per 20k tokens spent.
This catches near-zero-productivity sessions (thrashing, pure research in a non-exempt op)
while not firing on legitimate heavy-research-then-edit workflows (which land around 0.02–0.1).
Op-exemption for Codebase Research and Paperclip Coordination prevents false firing on
intrinsically talk-heavy ops.

---

*Report generated by Day 8 executor. Re-run `scripts/export_session_quality_precision.py`
(Day 12) to refresh against live labels.*
