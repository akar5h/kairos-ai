# Haywire-Restart Detector: Analysis of 24 Labeled Traces

**Date:** 2026-06-15
**Analyst:** Kairos data-analysis agent
**Sources:** `eval/review/answers.jsonl` (24 haywire-class records, last-wins), `eval/review/haywire_queue.json` (40 entries)

---

## TL;DR

- **22/24 traces are Paperclip-coordination** — the "restart" is a Paperclip session re-entry event (new heartbeat, wake payload, or resume delta), not an agent quality failure.
- **0/24 are genuine haywire** — no trace shows an agent re-doing real task work post-restart. Automated rework detection confirms this (`post_restart_rework = 0` for all 24).
- **Precision of the restart-as-haywire detector: 0/24 (0%)** — the owner labeled zero of the 24 as genuinely bad.
- **Recommendation:** Suppress all Paperclip-coordination restarts from the haywire queue using the deterministic rule below. Defer shipping genuine-haywire as a Kairos signal until non-Paperclip traces are collected.
- **Traces missing from current queue:** 0 — all 24 labeled traces are present in `haywire_queue.json`.

---

## Method

**Data loading:** `answers.jsonl` filtered to `class == haywire`, last record wins per `trace_id` (full UUID). Matched to queue by 8-character hex prefix of trace_id. All 24 matched.

**Category assignment:** Each trace classified by examining (a) `task` text and `user_events[0].text` for Paperclip orchestration markers, (b) step-level tool calls (`Skill` tool, `Bash` args), (c) restart step indices and surrounding steps, (d) `post_restart_rework` field (automated hash-match count).

**Owner verdict inference from free text:**
- `fine` — owner says recovered/continued/correct/normal/no issue/lgtm/graceful/good
- `bad` — owner says haywire/redo/waste/failure (used honestly — none found)
- `unclear` — ambiguous ("inconclusive", "you don't know", "cannot figure out")

---

## Per-Trace Table

| trace_id (12hex) | restart / rework | category | owner_verdict | evidence |
|---|---|---|---|---|
| 8f2b4e722321 | 2 / 0 | paperclip_coordination | fine | Wake Payload task; Skill(paperclip) step 1; printenv PAPERCLIP step 3; owner: "agent was able to recover well" |
| b9ccdc5b9ff8 | 1 / 0 | paperclip_coordination | fine | Wake Payload task; QA agent; Skill(paperclip) step 49; owner: "continued work properly, no degradation" |
| 2de5a083fec3 | 1 / 0 | unclear | unclear | Resume Delta task; only 5 steps total (curl PAPERCLIP API then immediate restart); owner: "inconclusive" |
| 8ff8ca757d02 | 1 / 0 | paperclip_coordination | fine | Wake Payload task; Skill(paperclip) step 1; 9 Bash steps reference $PAPERCLIP_API_URL; owner: "able to do the thing properly" |
| a8cbcf088d02 | 1 / 0 | paperclip_coordination | fine | Wake Payload task; reads memory files then Slack API; PAPERCLIP env at step 8; owner: "Correctly done" |
| acf388589262 | 6 / 0 | infra_error | fine | Wake Payload; 6 restarts; Write-tool errors on .mlo/diff-audit.md (steps 63-64 ERR, retried ok); restarts driven by skill verification file-lock; owner: "failing in same write-error pattern again and again"—task succeeded |
| edc594417ec7 | 5 / 0 | paperclip_coordination | unclear | Wake Payload; 5 restarts; reads claude-prompt file at each restart (Paperclip next-heartbeat trigger); owner: "task did succeed with silent failures—you need to prioritize" |
| ea07f86e83f2 | 5 / 0 | paperclip_coordination | fine | Wake Payload; 5 restarts; Skill(paperclip) step 1; curl PAPERCLIP_API_URL checkout+status; owner: "did not go haywire, worked properly" |
| 67a6b3050022 | 4 / 0 | paperclip_coordination | fine | Wake Payload; 4 restarts; curl heartbeat-context step 5; checkout/release/re-checkout loop (steps 33-53) exhausting Paperclip API; owner: "Gracefully continued" |
| ed733386ecc3 | 3 / 0 | paperclip_coordination | unclear | Wake Payload; 3 restarts; Skill(paperclip)+curl checkout step 3; restarts interleaved with skills-file search; owner: "cannot figure out if restarted—only error is file not found in tool return" |
| 0e4cebe220eb | 3 / 0 | paperclip_coordination | fine | Wake Payload; 3 restarts; Skill(paperclip)+printenv PAPERCLIP step 4; reads claude-prompt file at steps 74-78; owner: "worked fine" |
| b0d1f1c23087 | 2 / 0 | paperclip_coordination | fine | Wake Payload; 2 restarts; Skill(paperclip)+printenv PAPERCLIP step 3; reads claude-prompt file at steps 96-97; owner: "lgtm" |
| 3d1e8efd9043 | 2 / 0 | paperclip_coordination | fine | Resume Delta; 2 restarts; Skill(paperclip-dev) step 101; curl PAPERCLIP_API_URL/plugins step 77; owner: "literally no issue with restart" |
| 163081933d31 | 2 / 0 | paperclip_coordination | fine | Wake Payload; 2 restarts; Skill(paperclip) step 1; printenv PAPERCLIP step 4; reads claude-prompt file at restart steps 64-66; owner: "same tool error"—restart not the problem |
| 29fcf7d07417 | 2 / 0 | paperclip_coordination | fine | Resume Delta; 2 restarts; curl PAPERCLIP_API_URL interactions step 32; reads api-reference.md at restart; owner: "correct only" |
| 3f45d8d4a84e | 2 / 0 | paperclip_coordination | fine | Heartbeat; 2 restarts; curl PAPERCLIP_API_URL checkout at steps 3 and 5; reads paperclip api-reference at restart; owner: "Correctly done" |
| 657f364af09e | 2 / 0 | paperclip_coordination | fine | Resume Delta; 2 restarts; python3 urllib.request to PAPERCLIP_API_URL at steps 1, 3, 7, 9; owner: "worked well" |
| 0b15fdc586ec | 2 / 0 | paperclip_coordination | fine | Wake Payload; 2 restarts; Skill(paperclip) step 1; reads claude-prompt file at restart steps 58-60; owner: "worked fine, idk what error" |
| 6674825ab5cc | 2 / 0 | paperclip_coordination | fine | Wake Payload; 2 restarts; reads Leadzo memory file then curl PAPERCLIP_API_URL checkout at step 9; owner: "No" (no haywire) |
| e0bd12aad28a | 2 / 0 | paperclip_coordination | fine | Wake Payload; 2 restarts; Skill(paperclip) step 7; curl PAPERCLIP API interactions; owner: "this is normal workflow" |
| 757a72ed9255 | 2 / 0 | paperclip_coordination | fine | Resume Delta; 2 restarts; python3 urllib.request PAPERCLIP_API_URL at steps 1, 3, 5, 7; owner: "normal workflow" |
| 568077220b64 | 2 / 0 | paperclip_coordination | fine | Wake Payload; 2 restarts; Skill(paperclip) step 1; 33 Bash steps reference $PAPERCLIP env; owner: "correct execution dont flag tool error as legit error" |
| a4f51dcbdcd9 | 1 / 0 | paperclip_coordination | fine | Wake Payload; 1 restart; Skill(paperclip) step 1; curl checkout step 6; owner explicitly: "overall a paperclip issue, not an agent failure" |
| c5d7dd9714ec | 1 / 0 | paperclip_coordination | fine | Wake Payload; 1 restart; Skill(paperclip) step 1; curl checkout http://127.0.0.1:3100 step 5; reads claude-prompt file at restart step 61; owner: "No issue" |

---

## Tallies

### Category distribution (n=24)

| Category | Count |
|---|---|
| paperclip_coordination | 22 |
| infra_error | 1 |
| unclear | 1 |
| genuine_haywire | 0 |
| benign_restart | 0 |

### Owner verdict distribution (n=24)

| Verdict | Count |
|---|---|
| fine | 21 |
| unclear | 3 |
| bad | 0 |

### Precision arithmetic

The detector fires on every trace where `restart_count >= 1` (the queue's membership criterion). Using owner_verdict as ground truth:

```
Precision = (# owner says bad) / (# detector fires)
          = 0 / 24
          = 0.0%
```

Precision for genuine-haywire subset specifically:
```
Genuine haywire count = 0
Precision (genuine subset) = 0 / 24 = 0.0%
```

All 24 traces have `post_restart_rework = 0` (automated hash-match found zero post-restart steps whose args matched a pre-restart step). This corroborates the owner verdicts.

---

## The Paperclip-Coordination Finding

Every session in this dataset is a Paperclip heartbeat. The entry point is one of three task types:

1. **"Paperclip Wake Payload"** — agent enters a new heartbeat scoped to a specific issue. The first tool call is almost always `Skill("paperclip")` followed by `printenv PAPERCLIP_*` or `curl $PAPERCLIP_API_URL/api/issues/.../checkout`.

2. **"Paperclip Resume Delta"** — agent re-enters a partial session; same coordination prologue.

3. **"Continue your Paperclip work"** — heartbeat continuation for named agents (Atlas, etc.).

When the session "restarts," it is the Paperclip scheduler triggering a new heartbeat invocation — not the agent looping or re-deciding. The agent re-reads the claude-prompt file (Paperclip's next-heartbeat signal file) and continues from where it left off. This is working as designed.

**Concrete examples:**

**Example 1 — `ea07f86e` (5 restarts, CTO agent, fine):**
Task: "Paperclip Wake Payload ... heartbeat is scoped to the issue below."
Step 1: `Skill("paperclip")` — loads coordination skill.
Steps 3-15: `echo "Agent: $PAPERCLIP_AGENT_ID" ... curl -X POST "$PAPERCLIP_API_URL/api/issues/54cc4f0c-.../checkout"`.
At each restart index, the agent reads the claude-prompt file and resumes status posting.
Owner: "No, it did not go haywire. It worked properly."

**Example 2 — `67a6b305` (4 restarts, CTO agent, fine):**
Task: "Paperclip Wake Payload."
Steps 3-13: `echo "API: $PAPERCLIP_API_URL" ... curl ".../heartbeat-context"`.
Steps 33-53: Repeated `curl -X POST ".../checkout"`, `curl -X POST ".../checkin"`, `curl -X PUT ".../documents/plan"` — the agent is navigating Paperclip checkout/release contention, not looping on task work.
Owner: "Gracefully continued."

**Example 3 — `a4f51dcb` (1 restart, named agent, fine):**
Task: "Paperclip Wake Payload."
Step 1: `Skill("paperclip")`. Step 6: `curl -X POST "$PAPERCLIP_API_URL/api/issues/.../checkout"`.
Post-restart: agent continues writing a consolidated analysis file.
Owner explicitly: "This is overall a paperclip issue, not an agent failure."

---

## Proposed Deterministic Flag Rule

Flag a trace as `paperclip_coordination` — and suppress from the haywire signal — if **any** of the following are true:

| Signal | Implementation | Rationale |
|---|---|---|
| **R1** | `task` text (or `user_events[0].text`) contains `"Wake Payload"` or `"Resume Delta"` or `"heartbeat"` (case-insensitive) | All Paperclip heartbeat sessions carry one of these strings verbatim |
| **R2** | Any step has `tool == "Skill"` and `input_full == "paperclip"` (or `args_digest == "paperclip"`) | The `Skill("paperclip")` call is the canonical coordination entry point |
| **R3** | Two or more `Bash` steps reference `$PAPERCLIP_API_URL` or `$PAPERCLIP` env vars in `args_digest` | Coordination work always references these env vars in API calls |
| **R4** | `Bash` step contains `printenv PAPERCLIP` | Token re-derivation at session start — pure coordination signal |

**Recommended minimum signal to fire: R1 alone** (covers all 24; R2/R3/R4 are redundant strengtheners).

### How many of the 24 this rule catches (real count)

Applied to all 24 traces:
- R1 (Wake Payload / Resume Delta / heartbeat in task text): **24/24**
- R2 (Skill="paperclip"): **18/24**
- R3 (≥2 Bash steps reference $PAPERCLIP env): **22/24**
- R4 (printenv PAPERCLIP in Bash step): **12/24**

**R1 alone catches 24/24.** Any single signal from R2-R4 would catch the same or a subset.

### False-positive risk

**Low for Paperclip sessions, unknown outside them.** All agents in this dataset run under Paperclip, so R1 is essentially a tautology here. The realistic FP risk arises if:
- A non-Paperclip session task text happens to contain the word "heartbeat" (possible but unlikely — the exact phrase "Wake Payload" is Paperclip-proprietary).
- A Paperclip session genuinely goes haywire on its task work while also being a coordination session (would be correctly flagged as coordination, suppressing a real signal — but this would require `post_restart_rework > 0` to detect).

**Mitigation:** Apply R1 only as a routing/bucketing rule, not suppression. Route to `paperclip_coordination` class, keep in a separate queue for Paperclip product analysis. Do not discard.

---

## Recommendation

**1. Suppress Paperclip-coordination restarts from the haywire quality signal.**
Apply R1 (task contains "Wake Payload"/"Resume Delta"/"heartbeat") at queue-build time to bucket these into a `paperclip_coordination` class. Route to a separate Paperclip-product review queue, not the agent-quality queue. This removes 22-24/24 of the current haywire-queue noise.

**2. Defer shipping genuine-haywire as a Kairos quality signal.**
0/24 labeled traces represent genuine agent haywire. The current detector (restart_count >= 1) has 0% precision against the owner's ground truth. There is no evidence of genuine haywire in this sample. This is likely because the entire labeled cohort is Paperclip agents where restarts are by design.

**To build a genuine signal, you need:**
- A non-Paperclip labeled set (agents where restarts are not by-design scheduled events).
- Or a Paperclip-specific signal: `post_restart_rework > 0` combined with the rework steps being on domain code files (not Paperclip API calls), which would distinguish a real redo from an API retry.

**3. The `infra_error` case (`acf38858`) warrants its own detector.**
6 restarts driven by Write-tool failures on a gate-results file. The agent recovered correctly each time. This is a file-lock / concurrency artifact of the `ai-change-verifier` skill, not haywire. Flag with: `is_error_struct == True` on Write steps within a 3-step window of restart indices.

---

## Honest Non-Claims and Limitations

- **n=24, single labeler.** All verdicts come from one owner reviewing their own system. No inter-rater reliability measurement. "Fine" could include cases a second reviewer would call borderline.
- **Conversation window caveats.** Steps are accessed via `args_digest` (a short hash/excerpt) and `collapsed_runs`. The full tool output is often not available. Classifications are based on tool call patterns, not full semantic analysis of what the agent produced.
- **`post_restart_rework` is an arg-hash match.** It detects exact arg repetition. An agent that re-reads a file with a different path or rephrases a git command would not be caught. Zero rework here means zero exact-match repetition — genuinely low confidence there's no semantic rework.
- **All 24 are Paperclip agents.** The dataset has no non-Paperclip traces to establish a baseline. Any claim about "genuine haywire" rates in non-Paperclip agents cannot be made from this data.
- **No secret values appear in this report.** Auth tokens, API keys, and bearer values were redacted in the source data and are not reproduced here.
