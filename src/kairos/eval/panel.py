"""Metric panel — compute the FULL metric vector for a Kairos engine against the corpus.

Blast radius definition: ANY panel metric that regresses counts as regression,
not just the targeted metric. The panel is the stable ruler; only the engine-under-
test changes between before/after refs.

Panel metric vector (all deterministic):

Outcome metrics (vs owner labels where labeled, tau-bench corpus):
  outcome_precision     — TP / (TP + FP) on labeled set (pass/fail only)
  outcome_recall        — TP / (TP + FN) on labeled set
  tau_kappa             — Cohen's κ on tau-bench binary subset (reuses run_agreement logic)
  tau_fail_precision    — precision for FAIL class on tau-bench
  tau_fail_recall       — recall for FAIL class on tau-bench
  tau_abstention_rate   — fraction non-computable on tau-bench

Per-detector metrics (D1, D2, D3, D4, redundant_execution):
  {det}_precision       — TP / (TP+FP) where labeled (None if no labels)
  {det}_recall          — TP / (TP+FN) where labeled (None if no labels)
  {det}_fire_count      — absolute fires across entire corpus
  {det}_fire_rate       — fire_count / corpus_size (stability signal)

Aggregate metrics:
  classes_covered       — count of detectors with at least one fire in corpus
  severity_error_count  — findings with severity=error
  severity_warning_count — findings with severity=warning
  severity_info_count   — findings with severity=info
  total_findings        — all findings

All metrics are deterministic given the same corpus + engine. The engine is
imported from the CURRENT Python path (set by the harness to point at the ref
under evaluation).
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kairos.eval.corpus import CorpusEntry, EvalCorpus

# Detectors in the panel — order is stable.
DETECTOR_NAMES: list[str] = [
    "unrecovered_error",    # D1
    "struggle_ratio",       # D2
    "coordination_waste",   # D3
    "work_to_talk_ratio",   # D4
    "redundant_execution",  # tier-1 redundant detector
]

# Detector label keys in CorpusEntry.detector_truth map to pattern_name values.
_CORPUS_KEY_TO_PATTERN: dict[str, str] = {
    "D1": "unrecovered_error",
    "D2": "struggle_ratio",
    "D3": "coordination_waste",
    "D4": "work_to_talk_ratio",
    "redundant_execution": "redundant_execution",
}


# ── Data structures ───────────────────────────────────────────────────────────


@dataclass
class DetectorMetrics:
    """Precision/recall + fire-rate for one detector."""

    name: str
    precision: float | None    # None if no labeled entries for this detector
    recall: float | None
    fire_count: int            # absolute fires across corpus
    fire_rate: float           # fire_count / corpus_size

    # Confusion matrix cells (where labeled)
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0
    labeled_count: int = 0     # entries with True/False truth for this detector


@dataclass
class OutcomeMetrics:
    """Outcome precision/recall and tau-bench agreement stats."""

    # vs owner labels (pass/fail ground truth from spotcheck + answers)
    owner_precision: float | None
    owner_recall: float | None
    owner_tp: int = 0
    owner_fp: int = 0
    owner_fn: int = 0
    owner_tn: int = 0
    owner_labeled_count: int = 0

    # tau-bench (reuses AgreementStats logic)
    tau_kappa: float | None = None
    tau_fail_precision: float | None = None
    tau_fail_recall: float | None = None
    tau_abstention_rate: float | None = None
    tau_total: int = 0
    tau_computable: int = 0
    # Confusion matrix for tau-bench binary
    tau_a: int = 0   # kairos PASS, tau PASS
    tau_b: int = 0   # kairos PASS, tau FAIL
    tau_c: int = 0   # kairos FAIL, tau PASS
    tau_d: int = 0   # kairos FAIL, tau FAIL


@dataclass
class MetricPanel:
    """Full metric vector for one engine-at-ref run against the corpus."""

    corpus_hash: str
    corpus_size: int
    outcome: OutcomeMetrics
    detectors: dict[str, DetectorMetrics]  # keyed by pattern_name
    classes_covered: int          # detectors with >= 1 fire
    severity_error_count: int
    severity_warning_count: int
    severity_info_count: int
    total_findings: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


# ── Engine runner ─────────────────────────────────────────────────────────────


def _run_engine_on_corpus(
    entries: list[CorpusEntry],
    taubench_dir: Path,
) -> dict[str, Any]:
    """Run the Kairos engine against all corpus entries that have loaded traces.

    Returns a dict:
      "outcome_results": dict[trace_id, {"outcome_pass": bool, "computable": bool}]
      "findings": dict[trace_id, list[{"pattern_name": str, "severity": str}]]

    The engine is imported from the current Python path (set by harness to the ref
    under evaluation). For tau-bench traces, loads TraceEnvelopes from disk.
    For live traces, we only compute what we can without Phoenix access
    (fire-count is 0 for live entries without an envelope).

    IMPORTANT: this function never modifies outcome_metric.py, session_quality.py,
    pipeline.py, or any detector logic. It is a pure consumer.
    """
    from kairos.models.trace import TraceEnvelope
    from kairos.taxonomy.business_context import BusinessContext

    # Optional callables — may not exist at all git refs (graceful degradation).
    # Typed as Any so mypy accepts dynamic assignment from conditional imports.
    _evaluate_outcome: Any = None
    _detect_tier1: Any = None
    _detect_session_quality: Any = None
    with contextlib.suppress(ImportError):
        from kairos.analysis.outcome_metric import evaluate_outcome as _evaluate_outcome
    with contextlib.suppress(ImportError):
        from kairos.detection.runner import detect_tier1 as _detect_tier1
    with contextlib.suppress(ImportError):
        from kairos.detection.session_quality import (
            detect_session_quality as _detect_session_quality,
        )

    # Load taubench context for tau-bench traces
    context_yaml = taubench_dir / "context.yaml"
    tau_context = BusinessContext.from_yaml(context_yaml) if context_yaml.exists() else None

    # Load tau-bench trace envelopes
    tau_envelopes: dict[str, TraceEnvelope] = {}
    if taubench_dir.exists():
        for json_path in taubench_dir.glob("*.json"):
            if json_path.name in {"labels.jsonl", "context.yaml"}:
                continue
            with contextlib.suppress(Exception):
                raw = json.loads(json_path.read_text())
                env = TraceEnvelope.model_validate(raw)
                tau_envelopes[env.trace_id] = env

    tau_operations = list(tau_context.operations) if tau_context else []

    def _best_op(env: TraceEnvelope) -> Any:
        """Pick the tau-bench operation that best covers this trace's tools."""
        if not tau_operations:
            return None
        trace_tools = set(env.tool_sequence)
        best_op = tau_operations[0]
        best_score = 0.0
        for op in tau_operations:
            required = set(op.required_side_effect_tools)
            if not required:
                continue
            hit = len(required & trace_tools) / len(required)
            if hit > best_score:
                best_score = hit
                best_op = op
        return best_op

    outcome_results: dict[str, dict[str, Any]] = {}
    findings_by_trace: dict[str, list[dict[str, Any]]] = {}

    for entry in entries:
        tid = entry.trace_id
        maybe_env: TraceEnvelope | None = tau_envelopes.get(tid)

        if maybe_env is None:
            # No envelope available (spotcheck, answers, or live without Phoenix)
            # Cannot run engine — record as not-computable
            outcome_results[tid] = {"outcome_pass": False, "computable": False}
            findings_by_trace[tid] = []
            continue

        trace_env: TraceEnvelope = maybe_env

        # Outcome
        if entry.source == "taubench" and tau_operations and _evaluate_outcome is not None:
            op = _best_op(trace_env)
            result = _evaluate_outcome(trace_env, op)
            outcome_results[tid] = {
                "outcome_pass": result.outcome_pass,
                "computable": result.computable,
            }
        else:
            outcome_results[tid] = {"outcome_pass": False, "computable": False}

        # Detectors — run on the envelope (session_quality + redundant).
        # Guard: detectors may not exist at old refs (graceful degradation).
        trace_findings: list[dict[str, Any]] = []
        with contextlib.suppress(Exception):
            # Tier-1 redundant detector (redundant_execution)
            if tau_operations and _detect_tier1 is not None:
                op = _best_op(trace_env)
                cluster_median = float(len(trace_env.steps))
                tier1_findings = _detect_tier1([trace_env], cluster_median)
                for f in tier1_findings:
                    trace_findings.append({"pattern_name": f.pattern_name, "severity": f.severity})
            # Session-quality detectors (D1–D4)
            if _detect_session_quality is not None:
                if tau_operations:
                    sq_findings = _detect_session_quality([trace_env], _best_op(trace_env))
                else:
                    sq_findings = _detect_session_quality([trace_env], None)
                for f in sq_findings:
                    trace_findings.append({"pattern_name": f.pattern_name, "severity": f.severity})
        findings_by_trace[tid] = trace_findings

    return {
        "outcome_results": outcome_results,
        "findings": findings_by_trace,
    }


# ── Cohen's κ ────────────────────────────────────────────────────────────────


def _cohen_kappa(a: int, b: int, c: int, d: int) -> float | None:
    """Compute Cohen's κ from 2×2 confusion matrix."""
    n = a + b + c + d
    if n == 0:
        return None
    po = (a + d) / n
    pe = ((a + b) * (a + c) + (c + d) * (b + d)) / (n * n)
    denom = 1.0 - pe
    if abs(denom) < 1e-12:
        return 1.0 if po == 1.0 else None
    return (po - pe) / denom


# ── Panel computation ─────────────────────────────────────────────────────────


def _safe_div(num: int, denom: int) -> float | None:
    if denom == 0:
        return None
    return num / denom


def _compute_outcome_metrics(
    entries: list[CorpusEntry],
    outcome_results: dict[str, dict[str, Any]],
) -> OutcomeMetrics:
    """Compute outcome precision/recall vs owner labels and tau-bench κ."""
    # Owner labels (from spotcheck + answers; truth in {pass, fail})
    owner_tp = owner_fp = owner_fn = owner_tn = 0
    owner_labeled = 0

    for entry in entries:
        if entry.outcome_truth not in {"pass", "fail"}:
            continue
        result = outcome_results.get(entry.trace_id, {})
        if not result.get("computable", False):
            continue  # abstain → excluded from owner precision
        owner_labeled += 1
        truth_pass = entry.outcome_truth == "pass"
        pred_pass = result.get("outcome_pass", False)
        if truth_pass and pred_pass:
            owner_tp += 1
        elif not truth_pass and pred_pass:
            owner_fp += 1
        elif truth_pass and not pred_pass:
            owner_fn += 1
        else:
            owner_tn += 1

    # Tau-bench: entries with tau_reward defined
    tau_entries = [e for e in entries if e.source == "taubench" and e.outcome_truth != "partial"]
    tau_total = len(tau_entries)
    tau_a = tau_b = tau_c = tau_d = 0
    tau_non_computable = 0

    for entry in tau_entries:
        result = outcome_results.get(entry.trace_id, {})
        if not result.get("computable", False):
            tau_non_computable += 1
            continue
        truth_pass = entry.outcome_truth == "pass"
        pred_pass = result.get("outcome_pass", False)
        if truth_pass and pred_pass:
            tau_a += 1
        elif not truth_pass and pred_pass:
            tau_b += 1
        elif truth_pass and not pred_pass:
            tau_c += 1
        else:
            tau_d += 1

    tau_kappa = _cohen_kappa(tau_a, tau_b, tau_c, tau_d)
    tau_abstention_rate = tau_non_computable / tau_total if tau_total > 0 else None

    # FAIL precision/recall: kairos FAIL = positive class (catching actual failures)
    # tau_fail_precision = tau_d / (tau_d + tau_c) → of all kairos FAILs, how many are actually FAILs
    # tau_fail_recall    = tau_d / (tau_d + tau_b) → of all actual FAILs, how many did kairos catch
    tau_fail_precision = _safe_div(tau_d, tau_d + tau_c)
    tau_fail_recall = _safe_div(tau_d, tau_d + tau_b)

    return OutcomeMetrics(
        owner_precision=_safe_div(owner_tp, owner_tp + owner_fp),
        owner_recall=_safe_div(owner_tp, owner_tp + owner_fn),
        owner_tp=owner_tp,
        owner_fp=owner_fp,
        owner_fn=owner_fn,
        owner_tn=owner_tn,
        owner_labeled_count=owner_labeled,
        tau_kappa=tau_kappa,
        tau_fail_precision=tau_fail_precision,
        tau_fail_recall=tau_fail_recall,
        tau_abstention_rate=tau_abstention_rate,
        tau_total=tau_total,
        tau_computable=tau_a + tau_b + tau_c + tau_d,
        tau_a=tau_a,
        tau_b=tau_b,
        tau_c=tau_c,
        tau_d=tau_d,
    )


def _compute_detector_metrics(
    detector_name: str,
    corpus_key: str | None,
    entries: list[CorpusEntry],
    findings_by_trace: dict[str, list[dict[str, Any]]],
    corpus_size: int,
) -> DetectorMetrics:
    """Compute precision/recall + fire-rate for one detector."""
    # Fire count across entire corpus
    fire_count = sum(
        1 for tid, findings in findings_by_trace.items()
        if any(f["pattern_name"] == detector_name for f in findings)
    )

    # Precision/recall vs labels (where available)
    tp = fp = fn = tn = 0
    labeled_count = 0

    if corpus_key is not None:
        for entry in entries:
            truth = entry.detector_truth.get(corpus_key)
            if truth is None:
                continue
            labeled_count += 1
            fired = any(
                f["pattern_name"] == detector_name
                for f in findings_by_trace.get(entry.trace_id, [])
            )
            if truth is True and fired:
                tp += 1
            elif truth is False and fired:
                fp += 1
            elif truth is True and not fired:
                fn += 1
            else:
                tn += 1

    precision = _safe_div(tp, tp + fp)
    recall = _safe_div(tp, tp + fn)

    return DetectorMetrics(
        name=detector_name,
        precision=precision,
        recall=recall,
        fire_count=fire_count,
        fire_rate=fire_count / corpus_size if corpus_size > 0 else 0.0,
        tp=tp,
        fp=fp,
        fn=fn,
        tn=tn,
        labeled_count=labeled_count,
    )


def compute_panel(corpus: EvalCorpus, taubench_dir: Path | None = None) -> MetricPanel:
    """Compute the full metric panel for the current engine against the corpus.

    Parameters
    ----------
    corpus:
        The versioned EvalCorpus to evaluate against.
    taubench_dir:
        Path to eval/corpus/taubench/ for loading trace envelopes.
        Defaults to the standard repo path.
    """
    if taubench_dir is None:
        taubench_dir = Path(__file__).parent.parent.parent.parent / "eval" / "corpus" / "taubench"

    engine_results = _run_engine_on_corpus(corpus.entries, taubench_dir)
    outcome_results = engine_results["outcome_results"]
    findings_by_trace = engine_results["findings"]

    corpus_size = len(corpus.entries)

    # Outcome metrics
    outcome = _compute_outcome_metrics(corpus.entries, outcome_results)

    # Per-detector metrics
    # Map detector names to corpus truth keys
    detector_to_key: dict[str, str | None] = {
        "unrecovered_error": "D1",
        "struggle_ratio": "D2",
        "coordination_waste": "D3",
        "work_to_talk_ratio": "D4",
        "redundant_execution": "redundant_execution",
    }
    detectors: dict[str, DetectorMetrics] = {}
    for det_name in DETECTOR_NAMES:
        corpus_key = detector_to_key.get(det_name)
        detectors[det_name] = _compute_detector_metrics(
            det_name, corpus_key, corpus.entries, findings_by_trace, corpus_size
        )

    # Aggregate metrics
    classes_covered = sum(1 for dm in detectors.values() if dm.fire_count > 0)

    all_findings = [f for findings in findings_by_trace.values() for f in findings]
    severity_error = sum(1 for f in all_findings if f.get("severity") == "error")
    severity_warning = sum(1 for f in all_findings if f.get("severity") == "warning")
    severity_info = sum(1 for f in all_findings if f.get("severity") == "info")

    return MetricPanel(
        corpus_hash=corpus.corpus_hash,
        corpus_size=corpus_size,
        outcome=outcome,
        detectors=detectors,
        classes_covered=classes_covered,
        severity_error_count=severity_error,
        severity_warning_count=severity_warning,
        severity_info_count=severity_info,
        total_findings=len(all_findings),
    )
