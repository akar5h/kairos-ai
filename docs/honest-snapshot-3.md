# Honest Snapshot 3 — P3 Eval Layer Baseline

**Date:** 2026-06-18  **Phase:** P3.1–P3.4 shipped and live-validated

**Corpus:** tau=161, spotcheck=20, answers=39, live=389 → 609 total entries

---

## What was built

**P3.1 — Floor metrics (GATE signals)**

Four deterministic checks wired into the eval GATE. Any drop > 0.01 blocks a commit via `compare()`.

**P3.2 — Cluster → eval-set generation**

73 eval sets frozen from live `discovery_queue` clusters. Each set has a held-in (cluster's own traces) and held-out (labeled-pass corpus entries + other-cluster traces). Discriminator derived from `dominant_feature` (latency_z, restart_count, rare_ngram, struggle, token_z, outcome_only).

**P3.3 — Regression-on-history gate**

`compare(eval_sets=...)` extends the main panel compare with per-cluster blast-radius: `held_out_new_fires` (fraction of held-out traces where a detector fires after the change but not before) is a first-class GATE signal. `trace_detector_fires` (per-trace fire map) is stored in `MetricPanel` so no extra worktree runs are needed.

**P3.4 — Issue lifecycle**

`discovery_queue` clusters now carry `status` (open / resolved / regressed). API endpoints `POST /v1/clusters/{key}/resolve` and `/regress`. UI cluster browser shows amber/green/red status badges with Resolve/Regress buttons.

---

## Live baseline (2026-06-18)

| Metric | Value | n |
|--------|-------|---|
| `floor.known_good_pass_rate` | 0.892 | 65 |
| `floor.known_bad_catch_rate` | 0.302 | 96 |
| `floor.tau_required_tool_hit_rate` | 0.578 | 161 |
| `floor.golden_trajectory_match_rate` | 1.000 | 5 |
| `outcome.owner_precision` | 0.464 | — |
| `outcome.tau_kappa` | 0.169 | — |
| traces with at least one finding | 323 | 609 corpus |
| eval sets frozen | 73 | — |

---

## Interpretation

**`known_bad_catch_rate = 0.30`** is the primary improvement target. D1 (unrecovered_error), D2 (struggle_ratio), and D3 (coordination_waste) are pattern-specific detectors — they are not general failure classifiers. This number will rise as new detectors cover more failure modes. It is now a GATE floor: any regression from 0.30 blocks.

**`tau_kappa = 0.169`** independently confirms the catch rate. Low agreement with tau-bench ground truth is real underperformance. The eval is calibrated correctly; the detectors are the gap.

**`known_good_pass_rate = 0.892`** means ~11% of owner-confirmed good sessions are classified as failures. Likely false-positive fires from D1/D2 on sessions with transient errors that ultimately succeeded.

**`tau_required_tool_hit_rate = 0.578`** reflects that agents complete all required side-effect tools on 58% of tau-bench tasks. Consistent with the tau-bench fail rate.

**`golden_trajectory_match_rate = 1.0`** is the normalizer canary. The 5 frozen tau-bench trajectories (1–2 tools each) still reproduce exactly. Any drop here means `spans_to_envelope` or arg normalization changed.

---

## Bug found during E2E

`list_clusters_by_status(None, dsn)` crashed: psycopg3 uses `%s` placeholders; the query used PostgreSQL-native `$1` syntax. Fixed in `da3145a` — split into two explicit queries (one for `status IS NULL`, one filtered).

---

## Next

P3.5: trajectory diff gate (deterministic sequence diff between before/after → GATE if causal path changes unexpectedly) + meta-eval (MCC / Cohen's κ on auto-generated evals; retire drifters below threshold).
