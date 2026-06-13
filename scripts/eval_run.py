"""eval_run.py — CLI for the Kairos eval harness.

Usage:
    # Run panel at a single ref
    uv run scripts/eval_run.py run --ref <git-ref> [--k 2]

    # Compare two refs (before → after)
    uv run scripts/eval_run.py compare --before <ref> --after <ref> [--k 2]

    # Retro-validate: run both canonical retro boundaries
    uv run scripts/eval_run.py retro

Environment:
    KAIROS_PG_DSN   optional — if set, results are stored in kairos-pg.
                    If unset, results are printed only (no store write).

Output:
    Panel JSON + human-readable summary to stdout.
    eval/reports/eval-<before>..<after>.md written for compare runs.
    eval_runs row inserted when KAIROS_PG_DSN is set.

Exit codes:
    0 — PASS (or single ref run completed)
    1 — REGRESSED (compare gate failed)
    2 — NONDETERMINISM_ERROR
    3 — error (exception)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO / "src"))

from kairos.eval.corpus import build_corpus
from kairos.eval.harness import NonDeterminismError, compare, run_eval
from kairos.eval.panel import compute_panel
from kairos.eval.store import is_db_available, store_run


def _fmt(v: float | None, digits: int = 4) -> str:
    if v is None:
        return "None"
    return f"{v:.{digits}f}"


def _print_panel_summary(label: str, panel: object) -> None:
    """Print a concise panel summary to stdout."""
    print(f"\n=== Panel: {label} ===")
    print(f"Corpus hash:    {panel.corpus_hash[:16]}...")
    print(f"Corpus size:    {panel.corpus_size}")
    print(f"Classes covered: {panel.classes_covered}")
    print(f"Total findings: {panel.total_findings}  "
          f"(error={panel.severity_error_count}, "
          f"warning={panel.severity_warning_count}, "
          f"info={panel.severity_info_count})")
    print()
    print("Outcome metrics:")
    o = panel.outcome
    print(f"  owner precision={_fmt(o.owner_precision)}, recall={_fmt(o.owner_recall)} "
          f"(n={o.owner_labeled_count})")
    print(f"  tau_kappa={_fmt(o.tau_kappa)}, "
          f"abstention={_fmt(o.tau_abstention_rate)}, "
          f"tau_fail_precision={_fmt(o.tau_fail_precision)}, "
          f"tau_fail_recall={_fmt(o.tau_fail_recall)}")
    print(f"  confusion: PASS/PASS={o.tau_a} PASS/FAIL={o.tau_b} FAIL/PASS={o.tau_c} FAIL/FAIL={o.tau_d}")
    print()
    print("Per-detector (precision / recall / fire_rate):")
    for det_name, dm in panel.detectors.items():
        print(f"  {det_name:30s}  "
              f"prec={_fmt(dm.precision)}  "
              f"rec={_fmt(dm.recall)}  "
              f"fire_rate={_fmt(dm.fire_rate, 3)}  "
              f"fires={dm.fire_count}")


def cmd_run(args: argparse.Namespace) -> int:
    """Evaluate engine at a single ref."""
    print(f"[eval_run] Building corpus...", flush=True)
    corpus = build_corpus()
    print(f"[eval_run] Corpus: {len(corpus.entries)} entries, hash={corpus.corpus_hash[:16]}...")
    print(f"[eval_run] Running panel at ref={args.ref} k={args.k}...", flush=True)

    try:
        result = run_eval(args.ref, k=args.k, corpus=corpus)
    except NonDeterminismError as e:
        print(f"\nNONDETERMINISM_ERROR: {e}", file=sys.stderr)
        return 2

    panel = result.panels[0]
    _print_panel_summary(f"{args.ref} ({result.ref_full[:12]})", panel)

    # Store if DB available
    if is_db_available():
        run_id = store_run(
            ref=args.ref,
            ref_full=result.ref_full,
            corpus_hash=result.corpus_hash,
            k=result.k,
            panel=panel,
            verdict="run",
        )
        print(f"\n[eval_run] Stored eval_run: run_id={run_id}")
    else:
        print("\n[eval_run] KAIROS_PG_DSN not set — skipping store.", file=sys.stderr)

    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    """Compare two refs and report blast radius."""
    report_dir = _REPO / "eval" / "reports"

    print(f"[eval_run] Building corpus...", flush=True)
    corpus = build_corpus()
    print(f"[eval_run] Corpus: {len(corpus.entries)} entries, hash={corpus.corpus_hash[:16]}...")
    print(f"[eval_run] Comparing {args.before} → {args.after} (k={args.k})...", flush=True)

    try:
        result = compare(
            args.before,
            args.after,
            k=args.k,
            corpus=corpus,
            report_dir=report_dir,
        )
    except NonDeterminismError as e:
        print(f"\nNONDETERMINISM_ERROR: {e}", file=sys.stderr)
        return 2

    print(f"\n=== Compare: {args.before} → {args.after} ===")
    print(f"Before: {result.before_ref_full[:12]}  After: {result.after_ref_full[:12]}")
    print(f"Corpus hash: {result.corpus_hash[:16]}...")
    print(f"k={result.k} runs — nondeterminism check: PASS")
    print(f"\nVerdict: {result.verdict}")

    if result.regression_metrics:
        print("\nREGRESSIONS:")
        for m in result.regression_metrics:
            diff = next(d for d in result.diffs if d.name == m)
            print(f"  {m}: {_fmt(diff.before)} → {_fmt(diff.after)} (Δ{_fmt(diff.delta)})")

    if result.improved_metrics:
        print("\nIMPROVEMENTS:")
        for m in result.improved_metrics:
            diff = next(d for d in result.diffs if d.name == m)
            print(f"  {m}: {_fmt(diff.before)} → {_fmt(diff.after)} (Δ+{_fmt(diff.delta)})")

    unchanged = [d for d in result.diffs if d.verdict == "unchanged"]
    print(f"\n{len(unchanged)} metrics unchanged, {len(result.improved_metrics)} improved, "
          f"{len(result.regression_metrics)} regressed.")

    # Store both runs if DB available
    if is_db_available():
        # Load before/after panels from the compare result
        # Re-run single evals to get the panel objects (the compare already ran them)
        print("[eval_run] Storing runs in eval_runs...", flush=True)
        before_single = run_eval(args.before, k=1, corpus=corpus)
        after_single = run_eval(args.after, k=1, corpus=corpus)
        run_id_b = store_run(
            ref=args.before, ref_full=result.before_ref_full,
            corpus_hash=result.corpus_hash, k=result.k,
            panel=before_single.panels[0], verdict="run",
        )
        run_id_a = store_run(
            ref=args.after, ref_full=result.after_ref_full,
            corpus_hash=result.corpus_hash, k=result.k,
            panel=after_single.panels[0], verdict=result.verdict,
        )
        print(f"[eval_run] Stored: before={run_id_b}, after={run_id_a}")
    else:
        print("[eval_run] KAIROS_PG_DSN not set — skipping store.", file=sys.stderr)

    return 0 if result.verdict == "PASS" else 1


def cmd_retro(args: argparse.Namespace) -> int:
    """Retro-validate the two canonical sprint boundaries."""
    print("=== RETRO-VALIDATE ===")
    print("Boundary 1: Bug-1 silent-failure fix (aead64a → 4c30a62)")
    print("Boundary 2: args-enrichment F10 fix (3d6a702 → 9975ecf)")
    print()

    corpus = build_corpus()
    print(f"Corpus: {len(corpus.entries)} entries, hash={corpus.corpus_hash[:16]}...")
    print()

    report_dir = _REPO / "eval" / "reports"
    exit_code = 0

    for label, before, after in [
        ("Bug-1 silent-failure fix", "aead64a", "4c30a62"),
        ("args-enrichment F10 fix", "3d6a702", "9975ecf"),
    ]:
        print(f"--- {label} ---")
        print(f"    Comparing {before} → {after} ...", flush=True)
        try:
            result = compare(
                before, after, k=args.k, corpus=corpus, report_dir=report_dir
            )
        except NonDeterminismError as e:
            print(f"    NONDETERMINISM_ERROR: {e}")
            exit_code = 2
            continue
        except Exception as e:
            print(f"    ERROR: {e}")
            exit_code = 3
            continue

        print(f"    Verdict: {result.verdict}")
        for m in result.improved_metrics:
            diff = next(d for d in result.diffs if d.name == m)
            print(f"    IMPROVED  {m}: {_fmt(diff.before)} → {_fmt(diff.after)}")
        for m in result.regression_metrics:
            diff = next(d for d in result.diffs if d.name == m)
            print(f"    REGRESSED {m}: {_fmt(diff.before)} → {_fmt(diff.after)}")
        if not result.improved_metrics and not result.regression_metrics:
            print("    No metric changes detected (corpus may lack coverage at these refs).")
        print()
        if result.verdict != "PASS":
            exit_code = 1

    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Kairos eval harness — run and compare panels across git refs.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="Evaluate engine at a single ref")
    run_p.add_argument("--ref", required=True, help="Git ref (SHA, branch, tag)")
    run_p.add_argument("--k", type=int, default=2, help="Number of runs (nondeterminism check)")

    cmp_p = sub.add_parser("compare", help="Compare before → after refs")
    cmp_p.add_argument("--before", required=True, help="Before ref")
    cmp_p.add_argument("--after", required=True, help="After ref")
    cmp_p.add_argument("--k", type=int, default=2, help="Number of runs per ref")

    retro_p = sub.add_parser("retro", help="Retro-validate canonical sprint boundaries")
    retro_p.add_argument("--k", type=int, default=2)

    args = parser.parse_args()

    try:
        if args.cmd == "run":
            return cmd_run(args)
        elif args.cmd == "compare":
            return cmd_compare(args)
        elif args.cmd == "retro":
            return cmd_retro(args)
        else:
            parser.print_help()
            return 3
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 3


if __name__ == "__main__":
    sys.exit(main())
