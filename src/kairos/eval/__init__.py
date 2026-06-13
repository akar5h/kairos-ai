"""Kairos eval harness — versioned ground-truth corpus + metric panel + compare gate.

Package layout:
  corpus.py  — versioned fixed corpus (tau-bench + owner labels + live snapshot)
  panel.py   — full metric panel (outcome + per-detector + aggregate)
  harness.py — run_eval(ref, k) + compare(before, after, k)
  store.py   — eval_runs Postgres table + round-trip helpers
"""
