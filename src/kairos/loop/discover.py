"""Kairos discovery engine — Day 12, flywheel engine.

Surfaces anomaly candidates and expectation-miss candidates from the
analysis corpus.  NEVER fires findings (unlabeled = unmeasured).

Per-trace features computed:
  - restart_count       : number of session-restart boundary steps
  - post_restart_rework : steps after a restart whose redacted args match a
                          pre-restart step's args (re-doing already-done work).
                          CRITICAL for Day-14 haywire-restart detector.
  - struggle            : (error + redundant + rejected) / max(1, side_effects)
  - token_z             : robust z-score of trace total_tokens vs corpus
  - latency_z           : robust z-score of trace total_latency_ms vs corpus
  - rare_ngram_count    : tool-sequence n-grams (n=2,3) with corpus freq < 1%

Candidates are emitted when any feature is an outlier (robust z > 3) OR
when a rare n-gram is present, OR when an expectation-miss candidate comes
from the Day-8 LEARN stage.

Clusters are formed cheaply by (tool-signature + dominant feature).

Output:
  - discovery_queue Postgres table (via db.py/persist pattern)
  - discovery_queue.json local file

Security:
  - features dict contains ONLY numeric scalars, tool names (not user data),
    and trace_ids.  No raw args, no tool outputs, no free text.
  - Redaction is not needed for numeric features; however all string digests
    that represent args go through _redact_arg_digest() before storage.
  - grep_secrets() is exposed for the acceptance-test audit.

Spec ref: docs/sprint-exec-3-loop.md §"Day 12 — Discovery mode"
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING, Any

import numpy as np

from kairos.detection.session_quality import (
    STRUGGLE_T,
    _count_redundant_steps,
    _find_session_restart_indices,
)
from kairos.log import get_logger
from kairos.models.enums import StepStatus, StepType

if TYPE_CHECKING:
    from datetime import date

    from kairos.detection.session_quality import ExpectationMissCandidate
    from kairos.models.trace import Step, TraceEnvelope

logger = get_logger(__name__)

# ── Corpus n-gram rarity threshold ────────────────────────────────────────────
# A tool-sequence n-gram (bigram or trigram) is "rare" when its corpus frequency
# is below this fraction.  0.01 = present in < 1% of traces.
NGRAM_RARE_T: float = 0.01

# ── Robust z-score outlier threshold ─────────────────────────────────────────
# Robust z uses MAD (median absolute deviation).  3.0 = 4.46× IQR equivalent.
ROBUST_Z_T: float = 3.0

# ── Discovery queue caps ──────────────────────────────────────────────────────
# Maximum candidates emitted per run.  Surplus is dropped and logged.
MAX_CANDIDATES: int = 500

# ── Arg digest length for post-restart rework comparison ─────────────────────
# We compare a truncated hash of normalized args — never raw args.
_ARG_DIGEST_LEN: int = 16  # hex chars

# ── Secret-grep patterns (re-exported for acceptance tests) ──────────────────
_SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"sk-[A-Za-z0-9-]{20,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/-]{8,}=*"),
    re.compile(r"\bghp_[A-Za-z0-9]{36,}"),
    re.compile(r"\bAKIA[A-Z0-9]{16}"),
    re.compile(r"-----BEGIN\s+[A-Z ]+PRIVATE KEY-----"),
    re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}"),
]


def grep_secrets(text: str) -> list[str]:
    """Return matched secret-pattern strings found in *text* (for auditing)."""
    hits: list[str] = []
    for pat in _SECRET_PATTERNS:
        m = pat.search(text)
        if m:
            hits.append(pat.pattern)
    return hits


# ── Redaction for arg digests ─────────────────────────────────────────────────


def _redact_arg_digest(raw_repr: str) -> str:
    """Return a redacted 16-hex-char digest of an args repr string.

    The digest is a SHA-256 prefix — it identifies arg identity without
    revealing arg content.  No raw text crosses the storage boundary.
    """
    return hashlib.sha256(raw_repr.encode()).hexdigest()[:_ARG_DIGEST_LEN]


# ── Tool-signature (canonical sorted set) ────────────────────────────────────


def _tool_signature(trace: TraceEnvelope) -> str:
    """Canonical sorted-unique tool names used in the trace."""
    tools = sorted({s.tool_name for s in trace.steps if s.step_type == StepType.TOOL_CALL and s.tool_name})
    return "|".join(tools)


# ── Robust z-score ────────────────────────────────────────────────────────────


def _robust_z(values: list[float]) -> list[float]:
    """Compute robust z-scores using MAD for each value in *values*.

    Uses 1.4826 × MAD as the scale estimator (consistent estimator for
    Gaussian distributions).  Returns 0.0 when scale is 0 (all identical).
    """
    if not values:
        return []
    arr = np.array(values, dtype=float)
    median = np.median(arr)
    mad = np.median(np.abs(arr - median))
    scale = 1.4826 * mad
    if scale == 0.0:
        return [0.0] * len(values)
    return list((arr - median) / scale)


# ── Post-restart rework detection ─────────────────────────────────────────────


def _post_restart_rework_count(
    steps: list[Step],
    restart_indices: frozenset[int],
) -> int:
    """Count post-restart steps that redo work already done before the restart.

    Algorithm:
      1. Partition steps into pre-restart and post-restart segments.
      2. For each post-restart tool step with real args, compute a digest of
         its (tool_name, args_repr) and compare against the pre-restart digest set.
      3. A match = the agent is redoing work it already did (haywire rework).

    Returns the number of post-restart rework steps found.

    CRITICAL: Day-14 haywire-restart detector depends on this feature being
    computed here.  Do not remove or weaken.
    """
    if not restart_indices:
        return 0

    tool_steps = [s for s in steps if s.step_type == StepType.TOOL_CALL and s.tool_name]
    if not tool_steps:
        return 0

    # Find the first restart boundary in the step list.
    restart_step_indices_sorted = sorted(restart_indices)
    first_restart_idx = restart_step_indices_sorted[0]

    # Pre-restart arg digest set.
    pre_restart_digests: set[str] = set()
    post_restart_steps: list[Step] = []

    for step in tool_steps:
        if step.step_index < first_restart_idx:
            # Pre-restart: build digest set.
            args = step.tool_args_normalized or step.tool_args
            if args:
                key = f"{step.tool_name}:{sorted(args.items())}"
                pre_restart_digests.add(_redact_arg_digest(key))
        else:
            # Post-restart: candidate for rework check.
            post_restart_steps.append(step)

    if not pre_restart_digests or not post_restart_steps:
        return 0

    rework_count = 0
    for step in post_restart_steps:
        args = step.tool_args_normalized or step.tool_args
        if not args:
            continue
        key = f"{step.tool_name}:{sorted(args.items())}"
        digest = _redact_arg_digest(key)
        if digest in pre_restart_digests:
            rework_count += 1

    return rework_count


# ── Per-trace feature extraction ──────────────────────────────────────────────


@dataclass
class TraceFeatures:
    """Per-trace feature vector for discovery."""

    trace_id: str
    restart_count: int
    post_restart_rework: int
    struggle: float
    token_z: float
    latency_z: float
    rare_ngram_count: int
    rare_ngrams: list[str] = field(default_factory=list)
    tool_signature: str = ""
    dominant_feature: str = ""
    """The single feature that most strongly triggered outlier status."""


def _dominant_feature(f: TraceFeatures, z_t: float = ROBUST_Z_T) -> str:
    """Return the feature name with the strongest signal for clustering."""
    candidates: list[tuple[float, str]] = []
    if abs(f.token_z) >= z_t:
        candidates.append((abs(f.token_z), "token_z"))
    if abs(f.latency_z) >= z_t:
        candidates.append((abs(f.latency_z), "latency_z"))
    if f.restart_count > 0:
        candidates.append((float(f.restart_count), "restart_count"))
    if f.post_restart_rework > 0:
        candidates.append((float(f.post_restart_rework), "post_restart_rework"))
    if f.struggle >= STRUGGLE_T:
        candidates.append((f.struggle, "struggle"))
    if f.rare_ngram_count > 0:
        candidates.append((float(f.rare_ngram_count), "rare_ngram"))
    if not candidates:
        return "none"
    return max(candidates, key=lambda t: t[0])[1]


def _extract_ngrams(tool_sequence: list[str], n: int) -> list[tuple[str, ...]]:
    """Extract all n-grams from a tool sequence."""
    return [tuple(tool_sequence[i : i + n]) for i in range(len(tool_sequence) - n + 1)]


def _build_corpus_ngram_freqs(
    traces: list[TraceEnvelope],
    n_values: tuple[int, ...] = (2, 3),
) -> dict[tuple[str, ...], float]:
    """Compute corpus-wide n-gram frequencies (fraction of traces containing n-gram).

    Returns {ngram_tuple: fraction_of_traces_containing_it}.
    """
    total = len(traces)
    if total == 0:
        return {}

    ngram_trace_counts: Counter[tuple[str, ...]] = Counter()
    for trace in traces:
        seen_in_trace: set[tuple[str, ...]] = set()
        for n in n_values:
            for ngram in _extract_ngrams(trace.tool_sequence, n):
                if ngram not in seen_in_trace:
                    seen_in_trace.add(ngram)
                    ngram_trace_counts[ngram] += 1

    return {ngram: count / total for ngram, count in ngram_trace_counts.items()}


def compute_trace_features(
    traces: list[TraceEnvelope],
    ngram_rare_t: float = NGRAM_RARE_T,
    robust_z_t: float = ROBUST_Z_T,
) -> list[TraceFeatures]:
    """Compute per-trace feature vectors over the full corpus.

    Token and latency z-scores are computed corpus-wide (all traces together).
    N-gram rarity is also corpus-wide.  All other features are per-trace.

    Args:
        traces: All traces in the analysis window.
        ngram_rare_t: Fraction threshold below which an n-gram is "rare".
        robust_z_t: Robust z-score threshold for outlier detection (used for
                    dominant feature tagging; not for filtering here).

    Returns:
        List of TraceFeatures, one per trace, in the same order as *traces*.
    """
    if not traces:
        return []

    # Build corpus-wide n-gram frequencies.
    ngram_freqs = _build_corpus_ngram_freqs(traces, n_values=(2, 3))

    # Corpus-wide token and latency lists for robust z.
    token_values = [float(t.total_tokens) for t in traces]
    latency_values = [float(t.total_latency_ms) for t in traces]
    token_zs = _robust_z(token_values)
    latency_zs = _robust_z(latency_values)

    features: list[TraceFeatures] = []
    for i, trace in enumerate(traces):
        steps = trace.steps
        restart_indices = _find_session_restart_indices(steps)
        restart_count = len(restart_indices)
        rework = _post_restart_rework_count(steps, restart_indices)

        # Struggle (D2 formula).
        tool_steps = [s for s in steps if s.step_type == StepType.TOOL_CALL and s.tool_name]
        error_steps = sum(1 for s in tool_steps if s.status == StepStatus.ERROR)
        rejected = sum(
            1
            for s in tool_steps
            if s.status == StepStatus.ERROR
            and s.error_message is not None
            and "tool_use_error" in s.error_message.lower()
        )
        redundant = _count_redundant_steps(steps)
        side_effects = sum(1 for s in tool_steps if s.status == StepStatus.OK)
        struggle = (error_steps + redundant + rejected) / max(1, side_effects)

        # Rare n-grams.
        rare: list[tuple[str, ...]] = []
        for n in (2, 3):
            for ngram in _extract_ngrams(trace.tool_sequence, n):
                freq = ngram_freqs.get(ngram, 0.0)
                if freq < ngram_rare_t:
                    rare.append(ngram)
        # Deduplicate rare ngrams.
        rare_unique = list({g: None for g in rare}.keys())

        f = TraceFeatures(
            trace_id=trace.trace_id,
            restart_count=restart_count,
            post_restart_rework=rework,
            struggle=round(struggle, 4),
            token_z=round(token_zs[i], 4),
            latency_z=round(latency_zs[i], 4),
            rare_ngram_count=len(rare_unique),
            rare_ngrams=[">".join(g) for g in rare_unique[:10]],  # cap for storage
            tool_signature=_tool_signature(trace),
        )
        f.dominant_feature = _dominant_feature(f, z_t=robust_z_t)
        features.append(f)

    return features


# ── Candidate dataclass ───────────────────────────────────────────────────────


@dataclass
class DiscoveryCandidate:
    """A single discovery candidate (anomaly or expectation_miss).

    Features are stored as a dict of numeric scalars + safe string tags.
    NO raw args, outputs, or user content is stored.
    """

    id: str
    kind: str  # 'anomaly' | 'expectation_miss'
    trace_id: str
    cluster_key: str
    features: dict[str, Any]


# ── Build candidates ──────────────────────────────────────────────────────────


def _is_outlier(f: TraceFeatures, z_t: float = ROBUST_Z_T) -> bool:
    """Return True when any feature qualifies the trace as a discovery candidate."""
    return (
        abs(f.token_z) >= z_t
        or abs(f.latency_z) >= z_t
        or f.restart_count > 0
        or f.post_restart_rework > 0
        or f.rare_ngram_count > 0
        # struggle >= threshold is already a D2 finding; include if very high
        or f.struggle >= STRUGGLE_T * 2
    )


def _cluster_key(f: TraceFeatures) -> str:
    """Cheap cluster label: tool_signature + dominant_feature."""
    sig_prefix = f.tool_signature[:60] if f.tool_signature else "llm_only"
    feat = f.dominant_feature if f.dominant_feature else "no_signal"
    return f"{sig_prefix}::{feat}"


def _build_anomaly_candidates(
    features: list[TraceFeatures],
    z_t: float = ROBUST_Z_T,
) -> list[DiscoveryCandidate]:
    """Build anomaly candidates from trace features."""
    candidates: list[DiscoveryCandidate] = []
    for f in features:
        if not _is_outlier(f, z_t):
            continue
        cid = hashlib.sha256(f"anomaly:{f.trace_id}".encode()).hexdigest()[:24]
        candidates.append(
            DiscoveryCandidate(
                id=cid,
                kind="anomaly",
                trace_id=f.trace_id,
                cluster_key=_cluster_key(f),
                features={
                    "restart_count": f.restart_count,
                    "post_restart_rework": f.post_restart_rework,
                    "struggle": f.struggle,
                    "token_z": f.token_z,
                    "latency_z": f.latency_z,
                    "rare_ngram_count": f.rare_ngram_count,
                    "rare_ngrams": f.rare_ngrams,
                    "tool_signature": f.tool_signature,
                    "dominant_feature": f.dominant_feature,
                },
            )
        )
    return candidates


def _build_expectation_miss_candidates(
    miss_candidates: list[ExpectationMissCandidate],
) -> list[DiscoveryCandidate]:
    """Build discovery candidates from expectation-miss candidates (LEARN stage)."""
    candidates: list[DiscoveryCandidate] = []
    for c in miss_candidates:
        cid = hashlib.sha256(f"expectation_miss:{c.trace_id}:{c.missing_tool}".encode()).hexdigest()[:24]
        candidates.append(
            DiscoveryCandidate(
                id=cid,
                kind="expectation_miss",
                trace_id=c.trace_id,
                cluster_key=f"expectation_miss::{c.workflow_name}::{c.missing_tool}",
                features={
                    "workflow_name": c.workflow_name,
                    "missing_tool": c.missing_tool,
                    "presence_rate": round(c.presence_rate, 4),
                    "clean_trace_count": c.clean_trace_count,
                },
            )
        )
    return candidates


# ── Emit: Postgres + JSON ─────────────────────────────────────────────────────


def _persist_candidates_pg(
    candidates: list[DiscoveryCandidate],
    night_id: date,
    conn: Any,
) -> int:
    """Upsert candidates into discovery_queue.  Returns rows upserted.

    Idempotent: ON CONFLICT (id) DO UPDATE.
    Security: features dict contains only numeric scalars, safe string tags,
    trace_ids, and redacted digests — no raw args or outputs.
    """
    from psycopg.types.json import Jsonb

    if not candidates:
        return 0

    rows = [(c.id, night_id, c.kind, c.trace_id, c.cluster_key, Jsonb(c.features)) for c in candidates]

    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO discovery_queue (id, night_id, kind, trace_id, cluster_key, features)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE
                SET night_id    = EXCLUDED.night_id,
                    kind        = EXCLUDED.kind,
                    cluster_key = EXCLUDED.cluster_key,
                    features    = EXCLUDED.features
            """,
            rows,
        )
    conn.commit()
    return len(rows)


def _emit_json(
    candidates: list[DiscoveryCandidate],
    night_id: date,
    output_path: Path,
) -> None:
    """Write discovery_queue.json.

    Security: each candidate's features dict contains only safe, numeric/string
    data (no raw user input).  Secret grep is run on the output in tests.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = [
        {
            "id": c.id,
            "night_id": str(night_id),
            "kind": c.kind,
            "trace_id": c.trace_id,
            "cluster_key": c.cluster_key,
            "features": c.features,
            "labeled": False,
        }
        for c in candidates
    ]
    output_path.write_text(json.dumps(payload, indent=2, default=str))


# ── Main entry point ──────────────────────────────────────────────────────────


@dataclass
class DiscoveryResult:
    """Output of run_discovery()."""

    candidates: list[DiscoveryCandidate]
    anomaly_count: int
    expectation_miss_count: int
    dropped_by_cap: int
    pg_rows_upserted: int
    json_path: Path | None
    cluster_summary: dict[str, int]
    """cluster_key -> count of candidates in that cluster."""
    trace_features: list[TraceFeatures] = field(default_factory=list)
    """Full feature vectors (for live verification / Day-14)."""


def run_discovery(
    traces: list[TraceEnvelope],
    miss_candidates: list[ExpectationMissCandidate],
    night_id: date,
    *,
    conn: Any | None = None,
    json_output_path: Path | None = None,
    ngram_rare_t: float = NGRAM_RARE_T,
    robust_z_t: float = ROBUST_Z_T,
    max_candidates: int = MAX_CANDIDATES,
) -> DiscoveryResult:
    """Run the full discovery pipeline for one night's corpus.

    Steps:
      1. Compute per-trace features (restart, rework, struggle, z-scores, ngrams).
      2. Build anomaly candidates (outlier features).
      3. Fold in expectation-miss candidates from the LEARN stage.
      4. Cap + log any surplus.
      5. Emit to Postgres (if conn provided) and JSON (if path provided).
      6. Return DiscoveryResult with counts, cluster summary, and feature vectors.

    Args:
        traces: All TraceEnvelopes from tonight's analysis window.
        miss_candidates: ExpectationMissCandidate list from learn_tool_expectations().
        night_id: UTC date for this run (written to discovery_queue.night_id).
        conn: Optional psycopg connection (for Postgres emit).
        json_output_path: Path to write discovery_queue.json.
        ngram_rare_t: n-gram corpus frequency threshold for "rare".
        robust_z_t: Robust z-score threshold for outlier tagging.
        max_candidates: Hard cap on total candidates emitted.

    Returns:
        DiscoveryResult.
    """
    logger.info(
        "discover.start",
        night=str(night_id),
        trace_count=len(traces),
        expectation_miss_count=len(miss_candidates),
    )

    # Step 1: feature extraction.
    trace_features = compute_trace_features(traces, ngram_rare_t=ngram_rare_t, robust_z_t=robust_z_t)

    # Step 2: anomaly candidates.
    anomaly_candidates = _build_anomaly_candidates(trace_features, z_t=robust_z_t)

    # Step 3: expectation-miss candidates.
    em_candidates = _build_expectation_miss_candidates(miss_candidates)

    # Step 4: merge + cap.
    all_candidates = anomaly_candidates + em_candidates
    dropped = 0
    if len(all_candidates) > max_candidates:
        dropped = len(all_candidates) - max_candidates
        all_candidates = all_candidates[:max_candidates]
        logger.warning(
            "discover.candidates_capped",
            night=str(night_id),
            cap=max_candidates,
            dropped=dropped,
            anomaly_total=len(anomaly_candidates),
            em_total=len(em_candidates),
        )

    # Cluster summary.
    cluster_counter: Counter[str] = Counter(c.cluster_key for c in all_candidates)
    cluster_summary = dict(cluster_counter.most_common())

    logger.info(
        "discover.candidates_built",
        night=str(night_id),
        anomaly_count=len(anomaly_candidates),
        em_count=len(em_candidates),
        total_after_cap=len(all_candidates),
        dropped=dropped,
        cluster_count=len(cluster_summary),
    )

    # Step 5a: Postgres emit.
    pg_rows = 0
    if conn is not None:
        try:
            pg_rows = _persist_candidates_pg(all_candidates, night_id, conn)
            logger.info("discover.pg_upserted", night=str(night_id), rows=pg_rows)
        except Exception as exc:  # noqa: BLE001
            logger.warning("discover.pg_error", night=str(night_id), error=str(exc))

    # Step 5b: JSON emit.
    effective_json_path: Path | None = json_output_path
    if effective_json_path is not None and all_candidates:
        try:
            _emit_json(all_candidates, night_id, effective_json_path)
            logger.info(
                "discover.json_written",
                night=str(night_id),
                path=str(effective_json_path),
                count=len(all_candidates),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "discover.json_error",
                night=str(night_id),
                path=str(effective_json_path),
                error=str(exc),
            )
    elif json_output_path is not None and not all_candidates:
        logger.info(
            "discover.json_skipped_empty",
            night=str(night_id),
            reason="no candidates after cap",
        )

    anomaly_actual = sum(1 for c in all_candidates if c.kind == "anomaly")
    em_actual = sum(1 for c in all_candidates if c.kind == "expectation_miss")

    return DiscoveryResult(
        candidates=all_candidates,
        anomaly_count=anomaly_actual,
        expectation_miss_count=em_actual,
        dropped_by_cap=dropped,
        pg_rows_upserted=pg_rows,
        json_path=effective_json_path if all_candidates else None,
        cluster_summary=cluster_summary,
        trace_features=trace_features,
    )
