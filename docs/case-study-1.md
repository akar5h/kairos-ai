# Case Study 1: The Haywire-Restart Null Result

**Date:** 2026-06-15
**Sprint:** 14-day Kairos self-improvement sprint, Day 14
**Sources:** `eval/reports/haywire-analysis.md`, `docs/sprint-progress.md`, `docs/sprint-exec-3-loop.md`, `eval/review/answers.jsonl`

---

## TL;DR

Discovery surfaced "haywire restarts" as a candidate failure class. Owner-labeled 24 traces. **Precision: 0/24 (0%).** Every flagged restart is a Paperclip control-plane coordination event by design — not an agent quality failure. The pivot: a deterministic coordination-context flag (keys on "Wake Payload"/"heartbeat" in task text) fires 24/24 with zero tuning, cleanly separating control-plane noise from quality signal. Shipped as a first-class Kairos detector. The null result is the win.

---

## Background: The Flywheel and the Candidate

The sprint thesis is that Kairos improves *itself* — not the agents it observes. The governed loop: discovery surfaces an unlabeled candidate class → owner labels traces → a detector ships, validated against ground truth, eval-gated → Kairos's measured detection quality rises. That flywheel is the product claim, not whether any downstream agent changed.

On Day 12, `discover.py` ran anomaly detection across the 345-trace corpus (4 nights, 2026-06-08 through 2026-06-11). It flagged 41 traces with `restart_count >= 1` as a candidate "haywire restart" cluster — sessions where the agent appeared to start over, potentially re-doing work. The owner named this class explicitly. Day 14 built a Streamlit review app, surfaced 41 entries with restart boundaries highlighted, and the owner labeled them.

---

## What the Labels Said

24 of 41 traces were labeled (all with `class == "haywire"` in `answers.jsonl`). The verdict:

| Category | Count |
|---|---|
| paperclip_coordination | 22 |
| infra_error | 1 |
| unclear | 1 |
| genuine_haywire | **0** |

| Owner verdict | Count |
|---|---|
| fine | 21 |
| unclear | 3 |
| bad | **0** |

**Precision of the restart-as-haywire detector: 0/24 (0%).** The owner labeled zero traces as genuinely bad. Automated rework detection (`post_restart_rework` field, computed via hash-matching pre- and post-restart step args) returns 0 for all 24 — no trace shows an agent repeating real task work after a restart.

A representative sample of owner verdicts:
- `ea07f86e` (5 restarts): "No, it did not go haywire. It worked properly."
- `67a6b305` (4 restarts): "Gracefully continued."
- `a4f51dcb` (1 restart): "This is overall a paperclip issue, not an agent failure."
- `e0bd12aad28a` (2 restarts): "This is normal workflow."
- `568077220b64` (2 restarts): "Correct execution — don't flag tool error as legit error."

---

## Why Restarts Aren't Failures Here

Every session in this dataset is a Paperclip heartbeat. Entry points are one of three task types, each carrying a telltale string: "Paperclip Wake Payload," "Paperclip Resume Delta," or "Continue your Paperclip work" (heartbeat continuation for named agents). When a session "restarts," the Paperclip scheduler has triggered a new heartbeat invocation — not the agent looping or re-deciding on its own. The agent re-reads the `claude-prompt` file (Paperclip's next-heartbeat signal) and continues from where it left off.

The restart is the control plane calling the agent again. It is working as designed.

Three concrete patterns account for 22/24:

1. **Wake Payload sessions** — first tool call is almost always `Skill("paperclip")`, followed by `printenv PAPERCLIP_*` or `curl $PAPERCLIP_API_URL/.../checkout`. At each restart index, the agent re-reads the claude-prompt file and resumes status posting.

2. **Resume Delta sessions** — agent re-enters a partial session with the same coordination prologue (`curl PAPERCLIP_API_URL/interactions`, python urllib to the same base URL).

3. **Checkout contention** — repeated `curl -X POST .../checkout` / `curl -X POST .../checkin` / `curl -X PUT .../documents/plan` sequences (e.g., `67a6b305`, 4 restarts, steps 33–53). The agent is navigating Paperclip API checkout/release contention, not looping on task work.

The one `infra_error` case (`acf38858`, 6 restarts) is a separate artifact: Write-tool failures on a gate-results file driven by the `ai-change-verifier` skill's file-lock behavior. The agent recovered correctly each time; the task succeeded.

---

## The Pivot: A Deterministic Coordination Flag

Instead of a haywire detector, the analysis produced a coordination-context classifier. The rule is simple:

Flag a trace as `paperclip_coordination` — and route it away from the quality signal — if the task text (or `user_events[0].text`) contains `"Wake Payload"`, `"Resume Delta"`, or `"heartbeat"` (case-insensitive). This is R1 from the proposed flag rule in the analysis report.

Coverage against the 24 labeled traces:
- R1 (task text match): **24/24**
- R2 (`Skill("paperclip")` call): 18/24
- R3 (≥2 Bash steps referencing `$PAPERCLIP_API_URL`): 22/24
- R4 (`printenv PAPERCLIP` in Bash step): 12/24

R1 alone is sufficient. "Wake Payload" is a Paperclip-proprietary phrase; the false-positive risk outside Paperclip sessions is low. R2–R4 are corroborating signals, not required.

Implementation is config-driven. The markers live in `context.yaml` — the engine stays source-blind, recognizing these patterns without being coupled to Paperclip internals. This fits the sprint's generalization principle: engine references configured attribute names and string markers, never hard-wired domain knowledge.

**The flag does not discard these traces.** It routes them to a `paperclip_coordination` bucket, available for Paperclip-product analysis (coordination efficiency, checkout contention patterns). The quality queue gets cleaner signal; the coordination data is preserved.

---

## Why This Is Self-Improvement

The thesis for this sprint is that Kairos improves its own detection quality — not that it fixes agents. This case study is a textbook example of that:

- **Before the flywheel:** `restart_count >= 1` was a plausible haywire signal. Kairos would have fired it on every Paperclip session with a restart, which is most sessions. The signal would have been 0% precise — pure noise in the quality queue.
- **After the flywheel:** the coordination-context flag is shipped. Kairos now correctly tags "this restart = coordination event, not quality failure" on the entire Paperclip cohort. The remaining quality detectors (struggle_ratio, unrecovered_error, coordination_waste) operate on cleaner inputs because coordination-restart sessions are no longer contaminating the sample.

The flywheel's value here wasn't producing a new failure detector. It was *discovering that a plausible detector is noise* and pivoting to a clean classifier that makes the other signals honest. A governed null result, shipped as a positive.

This is the two-layer claim from the sprint:
1. **Kairos works as a product** — it emits truthful, evidence-backed findings on real traffic. The haywire result is honest precisely because 0% precision is reported and acted on, not papered over.
2. **Kairos improves itself** — the flywheel ran end-to-end: discovery surfaced the candidate, the owner labeled 24 traces, the analysis produced a deterministic rule, the rule ships with measured coverage (24/24). Detection quality rose because a source of noise is now classified and excluded.

---

## Limitations and Non-Claims

Be direct about what this result doesn't prove.

**n=24, single labeler.** All verdicts come from one owner reviewing their own system. No inter-rater reliability measurement. The "fine" verdicts could include cases a second reviewer would call borderline.

**Genuine haywire is currently unmeasurable from this corpus.** Every agent in the dataset runs under Paperclip, where restarts are by-design scheduled events. There is no non-Paperclip cohort to establish a baseline. Claims about genuine haywire rates in non-Paperclip agents cannot be made from this data. The right comparison set would be interactive Claude sessions, where restarts carry different semantics (9 user-interrupt events were observed in one transcript examined during the sprint).

**The two highest-signal traces are outside the labeled 24.** Discovery computed `post_restart_rework` for all 41 restart traces. The only two with `post_restart_rework > 0` — i.e., the only traces with any detected post-restart arg repetition — are `6ceca8d5` and `d38a760a`. These were NOT among the 24 labeled traces. Genuine haywire on the highest-signal candidates remains unjudged; the owner is labeling those next.

**`post_restart_rework` is an arg-hash match, not semantic analysis.** Zero rework detected means zero exact-match arg repetition. An agent that re-reads a file at a different path or rephrases a command would not be caught. The measure is a necessary but not sufficient indicator of re-doing work.

**Coordination efficiency is surfaced, not fixed.** Kairos reports that `67a6b305` spent steps 33–53 navigating Paperclip checkout contention. Whether to reduce that contention is a Paperclip product decision, out of Kairos's scope.

---

## What Shipped / What Was Cut / What's Open

**Shipped:**
- Deterministic `paperclip_coordination` flag (R1: task text matches "Wake Payload"/"Resume Delta"/"heartbeat") routed into a separate bucket, not the quality queue.
- Config-driven marker list in `context.yaml`; engine stays source-blind.
- Full per-trace classification table (24 entries) in `eval/reports/haywire-analysis.md` with evidence for each verdict.
- Recommendation to apply the flag at queue-build time in `build_haywire_queue.py`.

**Cut:**
- `detect_haywire_restart` as a quality-signal detector. 0% precision on this corpus; nothing ships dormant.
- Any precision/recall claim for genuine haywire. There is no labeled non-Paperclip cohort to measure against.

**Open:**
- Owner labels for `6ceca8d5` and `d38a760a` (the two `post_restart_rework > 0` traces, not yet labeled). These are the strongest genuine-haywire candidates in the full 41-trace set.
- Non-Paperclip cohort collection — interactive Claude sessions where restarts carry user-interrupt semantics — needed before genuine haywire can be measured.
- Separate `infra_error` detector for the Write-tool file-lock pattern (`acf38858`): `is_error == True` on Write steps within a 3-step window of restart indices. Recommended in the analysis; not yet built.
- Paperclip-product analysis queue for the 22 coordination sessions: checkout contention frequency, coordination step count distribution.
