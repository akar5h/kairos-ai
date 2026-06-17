"""Eval harness — run_eval(ref, k) and compare(before_ref, after_ref, k).

Approach (worktree + subprocess):
  1. `git worktree add /tmp/kairos-eval-<ref> <ref>` creates an isolated checkout
     of the repo at the given git ref. The worktree shares git history with the
     main repo but has its own working tree.
  2. We run `uv run python -c "import kairos; ..."` inside that worktree via
     subprocess. `uv` reads the worktree's pyproject.toml + uv.lock so deps
     resolve to that ref's lockfile. Recent refs (days old) share the same
     deps as HEAD — uv's cache means no re-download.
  3. The panel+corpus is the STABLE RULER: same corpus_hash on both sides.
     Only the engine-under-test varies by ref. This prevents confounding panel
     changes with engine changes.
  4. k runs of a deterministic engine MUST be identical. If they differ,
     raise NonDeterminismError — that is a real bug in the engine.

RETRO-VALIDATE usage:
  compare("aead64a", "4c30a62", k=2)  # Bug-1 boundary
  compare("3d6a702", "9975ecf", k=2)  # args-enrichment boundary

Security:
  - Worktrees are created under /tmp and cleaned up after use.
  - No raw tool outputs are passed through subprocess stdout — only the
    serialized MetricPanel (JSON).
  - DSN is forwarded from the calling environment (never hardcoded).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess  # noqa: S404
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kairos.eval.corpus import EvalCorpus, build_corpus

if TYPE_CHECKING:
    from kairos.eval.panel import MetricPanel

_REPO_ROOT = Path(__file__).parent.parent.parent.parent

# ── Errors ────────────────────────────────────────────────────────────────────


class NonDeterminismError(Exception):
    """Raised when k runs of a deterministic engine produce different panels."""


# ── Data structures ───────────────────────────────────────────────────────────


@dataclass
class RefEvalResult:
    """k-run evaluation at one git ref."""

    ref: str
    ref_full: str  # resolved full SHA
    k: int
    panels: list[MetricPanel]
    corpus_hash: str


@dataclass
class MetricDiff:
    """Before/after comparison for one metric."""

    name: str
    before: float | None
    after: float | None
    delta: float | None  # after - before; None if either side is None
    verdict: str  # "improved" | "regressed" | "unchanged" | "unknown"
    tier: str = "info"  # "gate" | "review" | "info" — see metric tiers below


@dataclass
class CompareResult:
    """Full comparison between before and after refs."""

    before_ref: str
    after_ref: str
    before_ref_full: str
    after_ref_full: str
    k: int
    corpus_hash: str
    diffs: list[MetricDiff]
    verdict: str  # "PASS" | "REGRESSED" | "UNKNOWN"
    targeted_metrics: list[str] = field(default_factory=list)
    """Metrics that the change was intended to improve (informational)."""
    regression_metrics: list[str] = field(default_factory=list)
    """GATE-tier metrics that dropped > epsilon (non-empty → REGRESSED)."""
    improved_metrics: list[str] = field(default_factory=list)
    """GATE/REVIEW metrics that improved."""
    review_metrics: list[str] = field(default_factory=list)
    """REVIEW-tier metric changes (detector precision/recall, both directions) —
    surfaced for human review; do NOT fail the gate."""
    info_metrics: list[str] = field(default_factory=list)
    """INFO-tier (volume) metric changes — diagnostic only, never a regression."""


# ── Git helpers ───────────────────────────────────────────────────────────────


def _resolve_ref(ref: str, repo: Path = _REPO_ROOT) -> str:
    """Resolve a git ref to its full SHA."""
    result = subprocess.run(  # noqa: S603
        ["git", "rev-parse", "--verify", ref],  # noqa: S607
        capture_output=True,
        text=True,
        cwd=str(repo),
    )
    if result.returncode != 0:
        raise ValueError(f"Cannot resolve git ref '{ref}': {result.stderr.strip()}")
    return result.stdout.strip()


def _worktree_path(ref_sha: str) -> Path:
    """Return a stable /tmp path for a worktree at ref_sha."""
    short = ref_sha[:12]
    return Path(tempfile.gettempdir()) / f"kairos-eval-{short}"


def _create_worktree(ref_sha: str, worktree_path: Path, repo: Path = _REPO_ROOT) -> None:
    """Create a git worktree at ref_sha if it does not already exist."""
    if worktree_path.exists():
        return
    result = subprocess.run(  # noqa: S603
        ["git", "worktree", "add", "--detach", str(worktree_path), ref_sha],  # noqa: S607
        capture_output=True,
        text=True,
        cwd=str(repo),
    )
    if result.returncode != 0:
        raise RuntimeError(f"git worktree add failed for ref {ref_sha}: {result.stderr.strip()}")


def _remove_worktree(worktree_path: Path, repo: Path = _REPO_ROOT) -> None:
    """Remove a git worktree and prune the ref."""
    with contextlib.suppress(Exception):
        subprocess.run(  # noqa: S603
            ["git", "worktree", "remove", "--force", str(worktree_path)],  # noqa: S607
            capture_output=True,
            cwd=str(repo),
        )
    # Belt-and-suspenders: if the directory still exists, remove it manually.
    if worktree_path.exists():
        shutil.rmtree(worktree_path, ignore_errors=True)
    # Prune stale worktree refs.
    subprocess.run(  # noqa: S603
        ["git", "worktree", "prune"],  # noqa: S607
        capture_output=True,
        cwd=str(repo),
    )


# ── Panel runner in a worktree subprocess ────────────────────────────────────

_PANEL_RUNNER_SCRIPT = """
import sys
import importlib
import importlib.util
import json
import os as _os
from pathlib import Path

_dsn = _os.environ.get("KAIROS_PG_DSN")

worktree_root = Path(sys.argv[1])
host_src = Path(sys.argv[2])

# ── Stable-ruler bootstrap ──────────────────────────────────────────────────
# kairos.eval (corpus.py + panel.py) is the STABLE RULER — always loaded
# from HOST (HEAD), never from the worktree ref under test.
# Strategy: patch sys.modules["kairos.eval.*"] from host src BEFORE
# importing anything else, so the worktree's kairos package (which may not
# have kairos.eval at all) cannot shadow the ruler.

def _load_eval_module(name: str, path: Path, is_pkg: bool = False):
    submodule_search_locations = [str(path.parent)] if is_pkg else None
    spec = importlib.util.spec_from_file_location(
        name, path,
        submodule_search_locations=submodule_search_locations,
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {name} from {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod

_eval_pkg_path = host_src / "kairos" / "eval"
_load_eval_module("kairos.eval", _eval_pkg_path / "__init__.py", is_pkg=True)
_load_eval_module("kairos.eval.corpus", _eval_pkg_path / "corpus.py")
_load_eval_module("kairos.eval.panel", _eval_pkg_path / "panel.py")

# ── Worktree engine injection ───────────────────────────────────────────────
# After ruler is locked in sys.modules, add worktree src to path so
# kairos.detection / kairos.analysis / etc. come from the ref under test.
src_path = worktree_root / "src"
if str(src_path) not in sys.path:
    sys.path.insert(0, str(src_path))

from kairos.eval.corpus import build_corpus
from kairos.eval.panel import compute_panel

# The CORPUS is the stable ruler — its fixed inputs (tau-bench envelopes,
# raw-span snapshots, resolved map, live ids) ALWAYS come from HOST, never the
# worktree. Only the engine (spans_to_envelope, detectors) varies by ref.
host_root = host_src.parent
taubench_dir = host_root / "eval" / "corpus" / "taubench"
live_ids = host_root / "eval" / "corpus" / "live_trace_ids.txt"
snapshot_dir = host_root / "eval" / "corpus" / "live"

# argv[3]/argv[4] override host paths if explicitly provided (kept for compat).
if len(sys.argv) > 3 and sys.argv[3]:
    taubench_dir = Path(sys.argv[3])
if len(sys.argv) > 4 and sys.argv[4]:
    live_ids = Path(sys.argv[4])

corpus = build_corpus(
    taubench_dir=taubench_dir, live_ids_file=live_ids, snapshot_dir=snapshot_dir, dsn=_dsn
)
panel = compute_panel(corpus, taubench_dir=taubench_dir, snapshot_dir=snapshot_dir, dsn=_dsn)
print(panel.to_json())
"""


def _run_panel_in_worktree(
    worktree_path: Path,
    host_src: Path,
    taubench_dir: Path,
    live_ids_file: Path,
) -> MetricPanel:
    """Run compute_panel inside a worktree subprocess using uv run.

    The worktree's engine (src/kairos/) is used for detection/pipeline.
    The host's eval ruler (panel.py + corpus.py) is injected via sys.path.

    Returns the MetricPanel from the subprocess stdout.
    """
    # Write the runner script to a temp file
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, prefix="kairos_eval_") as f:
        f.write(_PANEL_RUNNER_SCRIPT)
        runner_path = f.name

    env = dict(os.environ)

    try:
        result = subprocess.run(  # noqa: S603
            [  # noqa: S607
                "uv",
                "run",
                "--project",
                str(worktree_path),
                "python",
                runner_path,
                str(worktree_path),
                str(host_src),
                str(taubench_dir),
                str(live_ids_file),
            ],
            capture_output=True,
            text=True,
            cwd=str(worktree_path),
            env=env,
            timeout=300,  # 5 minutes per panel run
        )
    finally:
        Path(runner_path).unlink(missing_ok=True)

    if result.returncode != 0:
        raise RuntimeError(
            f"Panel runner failed in worktree {worktree_path}:\n"
            f"stdout: {result.stdout[:2000]}\n"
            f"stderr: {result.stderr[:2000]}"
        )

    # Parse the JSON panel from stdout (last JSON block in output)
    stdout = result.stdout.strip()
    # Find the JSON block (skip any uv output before it)
    json_match = re.search(r"\{.*\}", stdout, re.DOTALL)
    if not json_match:
        raise RuntimeError(f"No JSON found in panel runner stdout:\n{stdout[:2000]}")

    panel_dict = json.loads(json_match.group())
    return _panel_from_dict(panel_dict)


# ── Panel serialization ───────────────────────────────────────────────────────


def _panel_from_dict(d: dict[str, Any]) -> MetricPanel:
    """Reconstruct a MetricPanel from its dict representation."""
    from kairos.eval.panel import DetectorMetrics, MetricPanel, OutcomeMetrics

    outcome_d = d["outcome"]
    outcome = OutcomeMetrics(
        owner_precision=outcome_d.get("owner_precision"),
        owner_recall=outcome_d.get("owner_recall"),
        owner_tp=outcome_d.get("owner_tp", 0),
        owner_fp=outcome_d.get("owner_fp", 0),
        owner_fn=outcome_d.get("owner_fn", 0),
        owner_tn=outcome_d.get("owner_tn", 0),
        owner_labeled_count=outcome_d.get("owner_labeled_count", 0),
        tau_kappa=outcome_d.get("tau_kappa"),
        tau_fail_precision=outcome_d.get("tau_fail_precision"),
        tau_fail_recall=outcome_d.get("tau_fail_recall"),
        tau_abstention_rate=outcome_d.get("tau_abstention_rate"),
        tau_total=outcome_d.get("tau_total", 0),
        tau_computable=outcome_d.get("tau_computable", 0),
        tau_a=outcome_d.get("tau_a", 0),
        tau_b=outcome_d.get("tau_b", 0),
        tau_c=outcome_d.get("tau_c", 0),
        tau_d=outcome_d.get("tau_d", 0),
    )

    detectors = {}
    for name, det_d in d.get("detectors", {}).items():
        detectors[name] = DetectorMetrics(
            name=name,
            precision=det_d.get("precision"),
            recall=det_d.get("recall"),
            fire_count=det_d.get("fire_count", 0),
            fire_rate=det_d.get("fire_rate", 0.0),
            tp=det_d.get("tp", 0),
            fp=det_d.get("fp", 0),
            fn=det_d.get("fn", 0),
            tn=det_d.get("tn", 0),
            labeled_count=det_d.get("labeled_count", 0),
        )

    return MetricPanel(
        corpus_hash=d["corpus_hash"],
        corpus_size=d["corpus_size"],
        outcome=outcome,
        detectors=detectors,
        classes_covered=d.get("classes_covered", 0),
        severity_error_count=d.get("severity_error_count", 0),
        severity_warning_count=d.get("severity_warning_count", 0),
        severity_info_count=d.get("severity_info_count", 0),
        total_findings=d.get("total_findings", 0),
    )


def _panels_identical(p1: MetricPanel, p2: MetricPanel) -> bool:
    """Return True if two panels are identical (determinism check)."""
    return p1.to_dict() == p2.to_dict()


# ── run_eval ─────────────────────────────────────────────────────────────────


def run_eval(
    ref: str,
    k: int = 2,
    *,
    corpus: EvalCorpus | None = None,
    cleanup_worktree: bool = True,
    repo: Path = _REPO_ROOT,
) -> RefEvalResult:
    """Evaluate the Kairos engine at a git ref over the fixed corpus k times.

    Parameters
    ----------
    ref:
        Git ref (SHA, branch, tag) to evaluate.
    k:
        Number of runs. For a deterministic engine, all k runs must be identical.
        If any two runs differ, NonDeterminismError is raised.
    corpus:
        Pre-built corpus. If None, build_corpus() is called.
    cleanup_worktree:
        Remove the worktree after evaluation (default True).
    repo:
        Repo root path.

    Returns
    -------
    RefEvalResult with k panels (all identical for a deterministic engine).

    Raises
    ------
    NonDeterminismError
        If two runs produce different panels (k >= 2). This is a real engine bug.
    ValueError
        If ref cannot be resolved.
    RuntimeError
        If the panel runner subprocess fails.
    """
    if corpus is None:
        corpus = build_corpus(dsn=os.environ.get("KAIROS_PG_DSN"))

    ref_sha = _resolve_ref(ref, repo)
    wt_path = _worktree_path(ref_sha)

    host_src = repo / "src"
    taubench_dir = repo / "eval" / "corpus" / "taubench"
    live_ids_file = repo / "eval" / "corpus" / "live_trace_ids.txt"

    try:
        _create_worktree(ref_sha, wt_path, repo)

        panels: list[MetricPanel] = []
        for run_idx in range(k):
            panel = _run_panel_in_worktree(wt_path, host_src, taubench_dir, live_ids_file)
            # Verify corpus_hash stability
            if panel.corpus_hash != corpus.corpus_hash:
                raise RuntimeError(
                    f"Corpus hash mismatch on run {run_idx}: "
                    f"expected {corpus.corpus_hash}, got {panel.corpus_hash}. "
                    "The corpus must be the same stable ruler across all runs."
                )
            panels.append(panel)

        # Nondeterminism check: all k panels must be identical.
        if k >= 2:
            for i in range(1, len(panels)):
                if not _panels_identical(panels[0], panels[i]):
                    raise NonDeterminismError(
                        f"Nondeterminism detected: run 0 and run {i} at ref {ref} ({ref_sha[:12]}) "
                        "produced different panels. This is a real engine bug — the engine "
                        "is not deterministic. Investigate before shipping."
                    )

    finally:
        if cleanup_worktree:
            _remove_worktree(wt_path, repo)

    return RefEvalResult(
        ref=ref,
        ref_full=ref_sha,
        k=k,
        panels=panels,
        corpus_hash=corpus.corpus_hash,
    )


# ── compare ──────────────────────────────────────────────────────────────────


def _extract_metric_values(panel: MetricPanel) -> dict[str, float | None]:
    """Flatten a MetricPanel to a {metric_name: value} dict."""
    values: dict[str, float | None] = {
        "outcome.owner_precision": panel.outcome.owner_precision,
        "outcome.owner_recall": panel.outcome.owner_recall,
        "outcome.tau_kappa": panel.outcome.tau_kappa,
        "outcome.tau_fail_precision": panel.outcome.tau_fail_precision,
        "outcome.tau_fail_recall": panel.outcome.tau_fail_recall,
        "outcome.tau_abstention_rate": panel.outcome.tau_abstention_rate,
        "aggregate.classes_covered": float(panel.classes_covered),
        "aggregate.total_findings": float(panel.total_findings),
        "aggregate.severity_error": float(panel.severity_error_count),
        "aggregate.severity_warning": float(panel.severity_warning_count),
        "aggregate.severity_info": float(panel.severity_info_count),
    }
    for det_name, dm in panel.detectors.items():
        prefix = f"detector.{det_name}"
        values[f"{prefix}.precision"] = dm.precision
        values[f"{prefix}.recall"] = dm.recall
        values[f"{prefix}.fire_count"] = float(dm.fire_count)
        values[f"{prefix}.fire_rate"] = dm.fire_rate
    return values


# ── Three-tier metric classification ──────────────────────────────────────────
#
# The gate must distinguish a real quality regression from an INTENDED volume
# change (e.g. F10 args-enrichment suppressing false-positive fires). Flagging a
# false-positive fix as a regression makes the harness cry wolf. So:
#
#   GATE   (hard): grounded quality metrics. A drop > epsilon here = REGRESSED.
#                  These are the metrics validated against ground-truth labels /
#                  tau-bench rewards — the only signals that mean "detection got
#                  worse". Higher is better for all of them.
#   REVIEW (soft): per-detector precision/recall vs labels, BOTH directions.
#                  A recall drop after a precision fix may be a real miss OR
#                  over-firing being corrected — a human decides. Surfaced, never
#                  auto-fails the gate.
#   INFO   (diag): volume metrics (fire_count, fire_rate, severity_*,
#                  total_findings, classes_covered). Pure diagnostics — a change
#                  is never a regression by itself.
#
# Gate verdict = REGRESSED iff any GATE metric dropped > _GATE_EPSILON, else PASS.

# Epsilon below which a GATE-metric delta is treated as noise (no regression).
_GATE_EPSILON: float = 0.01

# GATE tier — grounded quality metrics, higher-is-better. A drop fails the gate.
_GATE_METRICS: frozenset[str] = frozenset(
    {
        "outcome.owner_precision",
        "outcome.owner_recall",
        "outcome.tau_kappa",
        "outcome.tau_fail_precision",
        "outcome.tau_fail_recall",
    }
)


def _metric_tier(name: str) -> str:
    """Return the tier for a metric name: 'gate' | 'review' | 'info'."""
    if name in _GATE_METRICS:
        return "gate"
    # Detector precision/recall vs labels → REVIEW (human decides).
    if name.startswith("detector.") and (name.endswith(".precision") or name.endswith(".recall")):
        return "review"
    # Everything else (volume, severity counts, abstention, aggregate) → INFO.
    return "info"


def _classify_delta(name: str, delta: float | None) -> str:
    """Classify a metric delta as improved / regressed / unchanged / unknown.

    GATE metrics: higher-is-better; a drop beyond epsilon is a regression.
    REVIEW metrics: directional (improved/regressed) but never fail the gate —
        the verdict aggregation only acts on GATE-tier regressions.
    INFO metrics: never 'regressed' — a directional move is reported as
        'increased'/'decreased' diagnostics, classified here as 'unchanged'
        for gate purposes (they carry no pass/fail meaning).
    """
    if delta is None:
        return "unknown"
    tier = _metric_tier(name)
    if tier == "gate":
        if abs(delta) <= _GATE_EPSILON:
            return "unchanged"
        return "improved" if delta > 0 else "regressed"
    if tier == "review":
        if abs(delta) < 1e-9:
            return "unchanged"
        return "improved" if delta > 0 else "regressed"
    # INFO tier — volume/diagnostic. Never a regression.
    return "unchanged"


def compare(
    before_ref: str,
    after_ref: str,
    k: int = 2,
    *,
    targeted_metrics: list[str] | None = None,
    corpus: EvalCorpus | None = None,
    report_dir: Path | None = None,
    repo: Path = _REPO_ROOT,
) -> CompareResult:
    """Run run_eval on both refs and diff the panel with three-tier gating.

    Gate: PASS iff no GATE-tier (grounded-quality) metric dropped > epsilon.
    REVIEW-tier (detector precision/recall) and INFO-tier (volume) changes are
    surfaced but never fail the gate — an intended false-positive suppression
    (volume drop) must not read as a regression. The panel+corpus is the stable
    ruler; only the engine varies by ref.

    Parameters
    ----------
    before_ref, after_ref:
        Git refs to compare.
    k:
        Runs per ref (nondeterminism check).
    targeted_metrics:
        Names of metrics the change was intended to improve (informational;
        does not affect PASS/REGRESSED gate — blast radius is the gate).
    corpus:
        Pre-built corpus. Built once and shared across both evals if provided.
    report_dir:
        If given, write eval/reports/eval-<before>..<after>.md there.
    repo:
        Repo root.

    Returns
    -------
    CompareResult with verdict "PASS" or "REGRESSED".
    """
    if corpus is None:
        corpus = build_corpus(dsn=os.environ.get("KAIROS_PG_DSN"))

    before_result = run_eval(before_ref, k=k, corpus=corpus, repo=repo)
    after_result = run_eval(after_ref, k=k, corpus=corpus, repo=repo)

    # Use first panel (all k are identical per nondeterminism check)
    before_panel = before_result.panels[0]
    after_panel = after_result.panels[0]

    # Verify corpus hash stability across refs
    if before_panel.corpus_hash != after_panel.corpus_hash:
        raise RuntimeError(
            f"Corpus hash mismatch between refs: before={before_panel.corpus_hash}, "
            f"after={after_panel.corpus_hash}. The ruler must be identical on both sides."
        )

    before_values = _extract_metric_values(before_panel)
    after_values = _extract_metric_values(after_panel)

    # Build diff
    diffs: list[MetricDiff] = []
    all_metrics = sorted(set(before_values) | set(after_values))

    for name in all_metrics:
        bv = before_values.get(name)
        av = after_values.get(name)
        delta = (av - bv) if (av is not None and bv is not None) else None
        verdict = _classify_delta(name, delta)
        tier = _metric_tier(name)
        diffs.append(MetricDiff(name=name, before=bv, after=av, delta=delta, verdict=verdict, tier=tier))

    # GATE: only grounded-quality drops fail the gate.
    regression_metrics = [d.name for d in diffs if d.tier == "gate" and d.verdict == "regressed"]
    # IMPROVEMENTS: GATE/REVIEW metrics that rose (volume rises are not credited).
    improved_metrics = [d.name for d in diffs if d.tier in {"gate", "review"} and d.verdict == "improved"]
    # REVIEW: detector precision/recall changes (both directions) — human review.
    review_metrics = [d.name for d in diffs if d.tier == "review" and d.verdict in {"regressed", "improved"}]
    # INFO: any volume metric that actually moved — diagnostic only.
    info_metrics = [d.name for d in diffs if d.tier == "info" and d.delta is not None and abs(d.delta) > 1e-9]

    # Verdict = REGRESSED iff a GATE metric dropped > epsilon; else PASS.
    verdict = "REGRESSED" if regression_metrics else "PASS"

    result = CompareResult(
        before_ref=before_ref,
        after_ref=after_ref,
        before_ref_full=before_result.ref_full,
        after_ref_full=after_result.ref_full,
        k=k,
        corpus_hash=corpus.corpus_hash,
        diffs=diffs,
        verdict=verdict,
        targeted_metrics=targeted_metrics or [],
        regression_metrics=regression_metrics,
        improved_metrics=improved_metrics,
        review_metrics=review_metrics,
        info_metrics=info_metrics,
    )

    if report_dir is not None:
        _write_compare_report(result, before_panel, after_panel, report_dir)

    return result


# ── Report writer ─────────────────────────────────────────────────────────────


def _fmt(v: float | None, precision: int = 4) -> str:
    if v is None:
        return "—"
    return f"{v:.{precision}f}"


def _write_compare_report(
    result: CompareResult,
    before_panel: MetricPanel,
    after_panel: MetricPanel,
    report_dir: Path,
) -> None:
    """Write eval/reports/eval-<before>..<after>.md."""
    report_dir.mkdir(parents=True, exist_ok=True)

    before_short = result.before_ref_full[:7]
    after_short = result.after_ref_full[:7]
    filename = f"eval-{before_short}..{after_short}.md"
    path = report_dir / filename

    lines: list[str] = [
        f"# Eval Report: {result.before_ref} → {result.after_ref}",
        "",
        f"- Before: `{result.before_ref}` ({result.before_ref_full[:12]})",
        f"- After:  `{result.after_ref}` ({result.after_ref_full[:12]})",
        f"- k={result.k} runs (nondeterminism check: {'PASS' if result.k >= 2 else 'skipped'})",
        f"- Corpus hash: `{result.corpus_hash[:16]}...`",
        f"- Corpus size: {before_panel.corpus_size} entries",
        "",
        f"## Verdict: **{result.verdict}**",
        "",
    ]

    by_name = {d.name: d for d in result.diffs}

    def _delta_line(m: str, sign: bool = False) -> str:
        diff = by_name[m]
        d = diff.delta
        plus = "+" if (sign and d is not None and d > 0) else ""
        return f"- `{m}`: {_fmt(diff.before)} → {_fmt(diff.after)} (Δ{plus}{_fmt(d, 4)})"

    lines.append(
        "_Tiers — GATE: grounded quality (fails the gate); "
        "REVIEW: detector precision/recall (human decides); "
        "INFO: volume diagnostics (never a regression)._"
    )
    lines.append("")

    if result.regression_metrics:
        lines.append("### GATE regressions (gate FAILED):")
        for m in result.regression_metrics:
            lines.append(_delta_line(m))
        lines.append("")
    else:
        lines.append("### GATE: no grounded-quality regression (gate PASSED).")
        lines.append("")

    if result.improved_metrics:
        lines.append("### Improvements (GATE/REVIEW):")
        for m in result.improved_metrics:
            lines.append(_delta_line(m, sign=True))
        lines.append("")

    if result.review_metrics:
        lines.append("### Needs human review (detector precision/recall):")
        for m in result.review_metrics:
            lines.append(_delta_line(m, sign=True))
        lines.append("")

    if result.info_metrics:
        lines.append("### Informational deltas (volume — not a regression):")
        for m in result.info_metrics:
            lines.append(_delta_line(m, sign=True))
        lines.append("")

    if result.targeted_metrics:
        lines.append(f"### Targeted metrics: {', '.join(result.targeted_metrics)}")
        lines.append("")

    lines += [
        "## Full Panel Diff",
        "",
        "| Metric | Tier | Before | After | Delta | Verdict |",
        "|--------|------|--------|-------|-------|---------|",
    ]
    for diff in result.diffs:
        delta_str = f"Δ{_fmt(diff.delta, 4)}" if diff.delta is not None else "—"
        lines.append(
            f"| `{diff.name}` | {diff.tier} | {_fmt(diff.before)} | {_fmt(diff.after)} | {delta_str} | {diff.verdict} |"
        )
    lines.append("")
    lines += [
        "## Outcome Detail",
        "",
        "**Before:**",
        f"- owner_precision={_fmt(before_panel.outcome.owner_precision)}, "
        f"owner_recall={_fmt(before_panel.outcome.owner_recall)} "
        f"(n={before_panel.outcome.owner_labeled_count})",
        f"- tau_kappa={_fmt(before_panel.outcome.tau_kappa)}, "
        f"abstention={_fmt(before_panel.outcome.tau_abstention_rate)}",
        "",
        "**After:**",
        f"- owner_precision={_fmt(after_panel.outcome.owner_precision)}, "
        f"owner_recall={_fmt(after_panel.outcome.owner_recall)} "
        f"(n={after_panel.outcome.owner_labeled_count})",
        f"- tau_kappa={_fmt(after_panel.outcome.tau_kappa)}, "
        f"abstention={_fmt(after_panel.outcome.tau_abstention_rate)}",
        "",
        "_Generated by `scripts/eval_run.py`. Provable-credit record._",
    ]

    path.write_text("\n".join(lines) + "\n")
