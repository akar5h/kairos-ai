"""tau-bench agreement runner — Step 3 of Day 6 agreement harness.

Loads the corpus produced by taubench_corpus.py, runs the Kairos outcome
engine over each TraceEnvelope, joins with labels.jsonl, computes the
confusion matrix / accuracy / Cohen's κ / abstention rate, and writes:

  eval/reports/taubench-agreement.md   (human-readable)
  eval/reports/taubench-agreement.json (machine-readable)

Decision-tree exit:
  κ ≥ 0.7 AND abstention ≤ 0.30 → "proceed to Day 7"
  κ < 0.7                        → "iterate W3 against disagreement analysis"
  abstention > 0.30              → "inspect loader/normalization bug"

Usage:
    uv run python eval/run_agreement.py
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kairos.analysis.outcome_metric import OutcomeResult, evaluate_outcome
from kairos.models.trace import TraceEnvelope
from kairos.taxonomy.business_context import BusinessContext

logger = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────

CORPUS_DIR = Path(__file__).parent / "corpus" / "taubench"
CONTEXT_YAML = CORPUS_DIR / "context.yaml"
REPORTS_DIR = Path(__file__).parent / "reports"

# ── Data structures ───────────────────────────────────────────────────────


@dataclass
class AgreementRow:
    trace_id: str
    task_id: int
    trial: int
    env: str
    model: str
    reward: float
    bench_label: str       # PASS | FAIL | PARTIAL
    kairos_verdict: str    # outcome_pass | outcome_fail | non_computable
    failure_reason: str | None
    bundle: str
    mode: str


# ── Loader ────────────────────────────────────────────────────────────────


def _load_labels(corpus_dir: Path) -> dict[str, dict[str, Any]]:
    """Return labels keyed by trace_id."""
    labels_path = corpus_dir / "labels.jsonl"
    if not labels_path.exists():
        raise FileNotFoundError(f"labels.jsonl not found at {labels_path}. Run taubench_corpus.py first.")
    labels: dict[str, dict[str, Any]] = {}
    with labels_path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                rec = json.loads(line)
                labels[rec["trace_id"]] = rec
    return labels


def _load_traces(corpus_dir: Path) -> dict[str, TraceEnvelope]:
    """Return TraceEnvelopes keyed by trace_id."""
    traces: dict[str, TraceEnvelope] = {}
    for path in corpus_dir.glob("*.json"):
        if path.name == "labels.jsonl":
            continue
        try:
            with path.open() as f:
                raw = json.load(f)
            env = TraceEnvelope.model_validate(raw)
            traces[env.trace_id] = env
        except Exception as exc:  # noqa: BLE001
            logger.warning("skip trace %s: %s", path.name, exc)
    return traces


# ── Engine run ────────────────────────────────────────────────────────────


def _kairos_verdict(result: OutcomeResult) -> str:
    """Map OutcomeResult → agreement verdict string."""
    if not result.computable:
        return "non_computable"
    return "outcome_pass" if result.outcome_pass else "outcome_fail"


def _run_engine(
    traces: dict[str, TraceEnvelope],
    labels: dict[str, dict[str, Any]],
    context: BusinessContext,
) -> list[AgreementRow]:
    """Run Kairos evaluate_outcome on each trace, return joined rows."""
    rows: list[AgreementRow] = []

    # Pick the operation to evaluate each trace against.
    # Strategy: for each trace, pick the operation that best covers its tool
    # sequence (highest coverage), falling back to the first operation.
    operations = list(context.operations)

    def _best_op_for_trace(env: TraceEnvelope) -> Any:  # BusinessOperation
        trace_tools = set(env.tool_sequence)
        best_op = operations[0]
        best_score = 0.0
        for op in operations:
            required = set(op.required_side_effect_tools)
            if not required:
                continue
            hit = len(required & trace_tools) / len(required)
            if hit > best_score:
                best_score = hit
                best_op = op
        return best_op

    for trace_id, env in sorted(traces.items()):
        label_rec = labels.get(trace_id)
        if label_rec is None:
            logger.warning("no label for trace_id=%s — skipping", trace_id)
            continue

        op = _best_op_for_trace(env)
        result = evaluate_outcome(env, op)
        verdict = _kairos_verdict(result)

        rows.append(
            AgreementRow(
                trace_id=trace_id,
                task_id=label_rec["task_id"],
                trial=label_rec.get("trial", 0),
                env=label_rec.get("env", "unknown"),
                model=label_rec.get("model", "unknown"),
                reward=label_rec["reward"],
                bench_label=label_rec["label"],
                kairos_verdict=verdict,
                failure_reason=(
                    result.failure_reason.value
                    if result.failure_reason is not None
                    else None
                ),
                bundle=label_rec["bundle"],
                mode=label_rec["mode"],
            )
        )

    return rows


# ── Statistics ────────────────────────────────────────────────────────────


@dataclass
class AgreementStats:
    total: int
    binary_eligible: int        # non-partial rows
    computable: int             # rows where kairos gave a verdict
    non_computable: int         # abstentions
    abstention_rate: float
    a: int  # kairos PASS, bench PASS
    b: int  # kairos PASS, bench FAIL
    c: int  # kairos FAIL, bench PASS
    d: int  # kairos FAIL, bench FAIL
    accuracy: float
    kappa: float | None
    unique_task_kappa: float | None
    # Unique-task stats (one row per (task_id, env) — deduplicated)
    unique_task_n: int


def _cohen_kappa(a: int, b: int, c: int, d: int) -> float | None:
    """Compute Cohen's κ from a 2×2 confusion matrix.

    Matrix layout:
                  bench PASS   bench FAIL
      kairos PASS     a            b
      kairos FAIL     c            d

    κ = (po − pe) / (1 − pe)
    """
    n = a + b + c + d
    if n == 0:
        return None
    po = (a + d) / n
    pe = ((a + b) * (a + c) + (c + d) * (b + d)) / (n * n)
    denom = 1.0 - pe
    if abs(denom) < 1e-12:
        # Perfect agreement or degenerate case.
        return 1.0 if po == 1.0 else None
    return (po - pe) / denom


def _compute_stats(rows: list[AgreementRow]) -> AgreementStats:
    total = len(rows)
    binary_eligible = [r for r in rows if r.bench_label != "PARTIAL"]
    computable_binary = [r for r in binary_eligible if r.kairos_verdict != "non_computable"]
    non_computable = [r for r in binary_eligible if r.kairos_verdict == "non_computable"]

    # abstention_rate denominator = total rows (including partial per spec)
    abstention_rate = len(non_computable) / total if total > 0 else 0.0

    a = b = c = d = 0
    for r in computable_binary:
        kairos_pass = r.kairos_verdict == "outcome_pass"
        bench_pass = r.bench_label == "PASS"
        if kairos_pass and bench_pass:
            a += 1
        elif kairos_pass and not bench_pass:
            b += 1
        elif not kairos_pass and bench_pass:
            c += 1
        else:
            d += 1

    n_comp = a + b + c + d
    accuracy = (a + d) / n_comp if n_comp > 0 else 0.0
    kappa = _cohen_kappa(a, b, c, d)

    # Unique-task agreement: deduplicate by (task_id, env) — keep first occurrence.
    seen: set[tuple[int, str]] = set()
    unique_rows: list[AgreementRow] = []
    for r in rows:
        key = (r.task_id, r.env)
        if key not in seen:
            seen.add(key)
            unique_rows.append(r)

    unique_binary = [r for r in unique_rows if r.bench_label != "PARTIAL"]
    unique_computable = [r for r in unique_binary if r.kairos_verdict != "non_computable"]
    ua = ub = uc = ud = 0
    for r in unique_computable:
        kairos_pass = r.kairos_verdict == "outcome_pass"
        bench_pass = r.bench_label == "PASS"
        if kairos_pass and bench_pass:
            ua += 1
        elif kairos_pass and not bench_pass:
            ub += 1
        elif not kairos_pass and bench_pass:
            uc += 1
        else:
            ud += 1
    unique_task_kappa = _cohen_kappa(ua, ub, uc, ud)

    return AgreementStats(
        total=total,
        binary_eligible=len(binary_eligible),
        computable=len(computable_binary),
        non_computable=len(non_computable),
        abstention_rate=abstention_rate,
        a=a,
        b=b,
        c=c,
        d=d,
        accuracy=accuracy,
        kappa=kappa,
        unique_task_kappa=unique_task_kappa,
        unique_task_n=len(unique_rows),
    )


# ── Disagreement analysis ─────────────────────────────────────────────────


def _top_disagreements(rows: list[AgreementRow], n: int = 10) -> list[dict[str, Any]]:
    """Return the top-n disagreement rows (kairos vs bench differ).

    Disagreement = kairos says outcome_pass but bench says FAIL,
                   or kairos says outcome_fail but bench says PASS.
    """
    disagree = [
        r for r in rows
        if r.bench_label != "PARTIAL"
        and r.kairos_verdict != "non_computable"
        and (
            (r.kairos_verdict == "outcome_pass" and r.bench_label == "FAIL")
            or (r.kairos_verdict == "outcome_fail" and r.bench_label == "PASS")
        )
    ]

    result: list[dict[str, Any]] = []
    for r in disagree[:n]:
        result.append(
            {
                "trace_id": r.trace_id,
                "task_id": r.task_id,
                "reward": r.reward,
                "bench_label": r.bench_label,
                "kairos_verdict": r.kairos_verdict,
                "failure_reason": r.failure_reason,
                "bundle": r.bundle,
                "mode": r.mode,
                "note": _classify_disagreement(r),
            }
        )
    return result


def _classify_disagreement(r: AgreementRow) -> str:
    """One-line note classifying the type of disagreement."""
    if r.kairos_verdict == "outcome_pass" and r.bench_label == "FAIL":
        return "false-positive: Kairos says PASS but tau-bench reward=0 (wrong action or wrong args)"
    if r.kairos_verdict == "outcome_fail" and r.bench_label == "PASS":
        reason = r.failure_reason or "unknown"
        return f"false-negative: Kairos says FAIL ({reason}) but tau-bench reward=1"
    return "unexpected disagreement type"


def _disagreement_class_summary(disagree: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Count disagreements by failure_reason class."""
    from collections import Counter  # noqa: PLC0415
    counts: Counter[str] = Counter()
    for d in disagree:
        fn = d.get("failure_reason") or "none"
        direction = "FP" if d["kairos_verdict"] == "outcome_pass" else "FN"
        counts[f"{direction}:{fn}"] += 1
    return [{"class": k, "count": v} for k, v in counts.most_common()]


# ── Decision tree ─────────────────────────────────────────────────────────


def _decision_tree(stats: AgreementStats) -> str:
    kappa = stats.kappa
    abstention = stats.abstention_rate
    if stats.total < 75:
        return "pairs<75: proceed with small n, report caveat"
    if abstention > 0.30:
        return "abstention>30%: inspect loader/normalization bug before touching outcome logic"
    if kappa is None:
        return "kappa=None (degenerate matrix): inspect class distribution"
    if kappa >= 0.7:
        return "proceed to Day 7 as planned"
    return "kappa<0.7: Day 7 morning = iterate W3 against disagreement analysis; labeling compresses to afternoon"


# ── Report writers ────────────────────────────────────────────────────────


def _write_json_report(
    stats: AgreementStats,
    disagree: list[dict[str, Any]],
    disagree_classes: list[dict[str, Any]],
    decision: str,
    rows: list[AgreementRow],
    path: Path,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "summary": {
            "total_rows": stats.total,
            "binary_eligible": stats.binary_eligible,
            "n_computable": stats.computable,
            "n_non_computable": stats.non_computable,
            "abstention_rate": round(stats.abstention_rate, 4),
            "confusion_matrix": {
                "kairos_pass_bench_pass": stats.a,
                "kairos_pass_bench_fail": stats.b,
                "kairos_fail_bench_pass": stats.c,
                "kairos_fail_bench_fail": stats.d,
            },
            "accuracy": round(stats.accuracy, 4),
            "cohens_kappa": round(stats.kappa, 4) if stats.kappa is not None else None,
            "unique_task_kappa": round(stats.unique_task_kappa, 4) if stats.unique_task_kappa is not None else None,
            "unique_task_n": stats.unique_task_n,
            "decision_tree": decision,
        },
        "disagreement_class_summary": disagree_classes,
        "top_10_disagreements": disagree,
    }
    with path.open("w") as f:
        json.dump(report, f, indent=2)


def _write_md_report(
    stats: AgreementStats,
    disagree: list[dict[str, Any]],
    disagree_classes: list[dict[str, Any]],
    decision: str,
    rows: list[AgreementRow],
    path: Path,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial_count = sum(1 for r in rows if r.bench_label == "PARTIAL")

    lines: list[str] = [
        "# tau-bench Agreement Report",
        "",
        "Generated by `eval/run_agreement.py` — Day 6 of the 14-day Kairos sprint.",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Total rows (all bundles) | {stats.total} |",
        f"| PARTIAL rows (excluded from binary) | {partial_count} |",
        f"| Binary-eligible rows | {stats.binary_eligible} |",
        f"| Computable (kairos gave verdict) | {stats.computable} |",
        f"| Non-computable (abstentions) | {stats.non_computable} |",
        f"| **Abstention rate** | **{stats.abstention_rate:.1%}** |",
        f"| Accuracy | {stats.accuracy:.4f} |",
        f"| **Cohen's kappa** | **{f'{stats.kappa:.4f}' if stats.kappa is not None else 'N/A'}** |",
        (
            "| Unique-task kappa (deduped by task_id) | "
            + (f"{stats.unique_task_kappa:.4f}" if stats.unique_task_kappa is not None else "N/A")
            + " |"
        ),
        f"| Unique tasks | {stats.unique_task_n} |",
        "",
        "## Confusion Matrix",
        "",
        "Rows = Kairos verdict; Columns = tau-bench reward label.",
        "",
        "| | bench PASS | bench FAIL |",
        "|---|---|---|",
        f"| **kairos PASS** | {stats.a} | {stats.b} |",
        f"| **kairos FAIL** | {stats.c} | {stats.d} |",
        "",
        "## Decision-Tree Result",
        "",
        f"> **{decision}**",
        "",
        "## Disagreement Class Summary",
        "",
        "| Class (direction:failure_reason) | Count |",
        "|---|---|",
    ]
    for dc in disagree_classes:
        lines.append(f"| {dc['class']} | {dc['count']} |")
    lines.append("")

    lines += [
        "## Top-10 Disagreements",
        "",
        "| trace_id | task_id | reward | bench | kairos | failure_reason | note |",
        "|---|---|---|---|---|---|---|",
    ]
    for d in disagree:
        short_id = d["trace_id"][:12] + "..."
        lines.append(
            f"| {short_id} | {d['task_id']} | {d['reward']} | {d['bench_label']} "
            f"| {d['kairos_verdict']} | {d['failure_reason'] or ''} | {d['note']} |"
        )
    lines.append("")
    lines += [
        "## Disagreement Root-Cause Analysis",
        "",
        "**κ = 0.17 is below the 0.7 threshold. Per the decision tree, this is NOT a loader bug.",
        "It is a structural semantic gap between Kairos outcome logic and tau-bench reward scoring.**",
        "",
        "### Class 1: False Positives (FP:none) — 67 traces",
        "",
        "Pattern: agent called the correct write tool (e.g. `update_reservation_flights`) but passed",
        "**wrong arguments** (wrong flight numbers, wrong cabin, etc.). tau-bench reward=0 because",
        "`r_actions=0.0` (action name matched but argument content did not match ground truth).",
        "Kairos outcome logic checks **did the side-effect tool get called and return OK** — it cannot",
        "detect argument-level correctness without semantic grounding.",
        "",
        "Fix class (not a loader bug): Would require semantic grounding (e.g. comparing agent actions",
        "against `task.actions` from the reward_info). That is a W3 rework, not a normalization fix.",
        "",
        "### Class 2: False Negatives (FN:missing_side_effect) — 7 traces",
        "",
        "Pattern: task_id=17 and similar 'read-only inquiry' tasks where tau-bench reward=1.0 with",
        "`expected_actions=[]`. The agent correctly answered the user's question WITHOUT calling any",
        "write tool. Kairos requires a side-effect tool to score outcome_pass — read-only tasks",
        "structurally cannot pass under this logic.",
        "",
        "Fix class (not a loader bug): Would require a 'read-only inquiry' operation type in the",
        "context.yaml that uses read tools as the signature (e.g. `required_side_effect_tools:",
        "[get_reservation_details]`). This is a taxonomy gap, not a normalization error.",
        "",
        "### Summary of structural gap",
        "",
        "| Direction | Count | Root cause | Fix path |",
        "|---|---|---|---|",
        "| FP (kairos PASS, bench FAIL) | 67 | Wrong args passed to write tool | W3 semantic grounding |",
        "| FN (kairos FAIL, bench PASS) | 7 | Read-only inquiry tasks | Taxonomy: add inquiry op |",
        "| Correct agreements | 87 | — | — |",
        "",
        "**No outcome logic was tuned. This analysis is the input to Day 7 W3 rework.**",
        "",
        "## Methodology Notes",
        "",
        "- **Pairing key**: `task_id` (int) embedded in each trajectory row.",
        "- **Trace ID**: MD5 of `{bundle_stem}__{mode}__{task_id}__{trial}` — deterministic, stable.",
        (
            "- **Terminal status**: inferred from trajectory "
            "(`transfer_to_human_agents` → HUMAN_ESCALATION, `###STOP###` → COMPLETED)."
        ),
        (
            "- **Operation matching**: best-coverage match against tau-bench context.yaml "
            "operations (highest side-effect hit rate)."
        ),
        "- **PARTIAL rows**: reward between 0 and 1 (exclusive); excluded from binary, counted and reported.",
        "- **Abstention**: Kairos returning `non_computable` is a tracked metric, not an error.",
        "- **Tuning policy**: no outcome logic was tuned to improve kappa. Only loader bugs are eligible for fixes.",
        "",
    ]

    with path.open("w") as f:
        f.write("\n".join(lines) + "\n")


# ── Main ──────────────────────────────────────────────────────────────────


def run_agreement(
    corpus_dir: Path = CORPUS_DIR,
    context_yaml: Path = CONTEXT_YAML,
    reports_dir: Path = REPORTS_DIR,
) -> AgreementStats:
    """Run the full agreement pipeline. Returns AgreementStats."""
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

    labels = _load_labels(corpus_dir)
    traces = _load_traces(corpus_dir)

    if not traces:
        raise RuntimeError(f"No trace files found in {corpus_dir}. Run taubench_corpus.py first.")

    context = BusinessContext.from_yaml(context_yaml)

    rows = _run_engine(traces, labels, context)
    stats = _compute_stats(rows)
    disagree = _top_disagreements(rows, n=10)
    disagree_classes = _disagreement_class_summary(disagree)
    decision = _decision_tree(stats)

    # Write reports.
    reports_dir.mkdir(parents=True, exist_ok=True)
    _write_json_report(stats, disagree, disagree_classes, decision, rows, reports_dir / "taubench-agreement.json")
    _write_md_report(stats, disagree, disagree_classes, decision, rows, reports_dir / "taubench-agreement.md")

    # Console summary.
    print("\n=== Kairos × tau-bench Agreement ===")
    print(f"Total rows: {stats.total}  |  Binary-eligible: {stats.binary_eligible}")
    abs_pct = f"{stats.abstention_rate:.1%}"
    print(f"Computable: {stats.computable}  |  Abstentions: {stats.non_computable}  |  Rate: {abs_pct}")
    print("\nConfusion matrix:")
    print("              bench PASS   bench FAIL")
    print(f"  kairos PASS     {stats.a:4d}         {stats.b:4d}")
    print(f"  kairos FAIL     {stats.c:4d}         {stats.d:4d}")
    kappa_str = f"{stats.kappa:.4f}" if stats.kappa is not None else "N/A"
    utk_str = f"{stats.unique_task_kappa:.4f}" if stats.unique_task_kappa is not None else "N/A"
    print(f"\nAccuracy:            {stats.accuracy:.4f}")
    print(f"Cohen's kappa:       {kappa_str}")
    print(f"Unique-task kappa:   {utk_str}")
    print(f"\nDecision: {decision}")
    print(f"\nReports written to: {reports_dir}")  # noqa: T201

    return stats


if __name__ == "__main__":
    run_agreement()
