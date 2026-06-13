"""Kairos Postgres persistence layer — Day 10.

Writes per-trace findings and per-workflow nightly rollups to the kairos-pg
Postgres instance.  All writes are idempotent (ON CONFLICT … DO UPDATE) so
re-running a night never double-counts.

Security contract (enforced here — do NOT weaken):
  • evidence_steps holds STEP INDICES (int[]) only.  Full tool outputs, raw
    args, secrets, and PII are NEVER written to the findings table.
  • No raw text from the analysis pipeline crosses the INSERT boundary.
  • Redaction of any free-text field is the caller's responsibility BEFORE
    calling these functions; this module does not re-redact because it never
    receives free text in the first place.

config_hash discipline:
  • Deltas are only meaningful within a single config_hash.  On a hash change
    versus the most-recently-persisted rollup, persist_nightly_rollup() writes
    a sentinel row with baseline_break=true so the Day-11 dashboard renders a
    vertical discontinuity rule.
  • compute_config_hash() produces a stable SHA-256 hex digest over the
    context.yaml content + detector threshold values + severity map.

Agent derivation:
  • Span attrs carry ``paperclip.agent_id`` (e.g. "claudecoder", "cto",
    "qaengineer") on live traces via the root span's ``paperclip`` dict.
    The PhoenixReader root-span meta (collected by _fetch_root_trace_ids in
    export_spotcheck.py) exposes this as the ``agent`` field.  When absent,
    the value "unknown" is used.
  • The TraceEnvelope carries no dedicated agent field today; callers pass
    agent identity from the meta_by_trace dict built during Phoenix fetch.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from typing import TYPE_CHECKING, Any

import psycopg  # noqa: TCH002 — used at runtime via psycopg.Connection type
from psycopg.types.json import Jsonb

from kairos.log import get_logger
from kairos.loop.db import apply_migrations, get_connection

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    from kairos.detection.models import Finding
    from kairos.engine.pipeline import AnalysisResult
    from kairos.models.trace import TraceEnvelope

logger = get_logger(__name__)

# ── Secrets grep patterns (for the acceptance-test helper) ────────────────────

_SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"sk-[A-Za-z0-9-]{20,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/-]{8,}=*"),
    re.compile(r"\bghp_[A-Za-z0-9]{36,}"),
    re.compile(r"\bAKIA[A-Z0-9]{16}"),
    re.compile(r"-----BEGIN\s+[A-Z ]+PRIVATE KEY-----"),
    re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}"),  # JWT
]


def grep_secrets(text: str) -> list[str]:
    """Return a list of matched secret patterns found in *text* (for auditing)."""
    hits: list[str] = []
    for pat in _SECRET_PATTERNS:
        if pat.search(text):
            hits.append(pat.pattern)
    return hits


# ── config_hash ────────────────────────────────────────────────────────────────


def compute_config_hash(
    context: Any,  # BusinessContext — typed as Any to avoid heavy import
    detector_thresholds: dict[str, Any] | None = None,
    severity_map: dict[str, str] | None = None,
) -> str:
    """Compute a stable SHA-256 hex digest over the analysis config.

    Inputs:
      context            — BusinessContext (serialised as JSON-stable repr)
      detector_thresholds — optional {threshold_name: value} dict
      severity_map        — optional {detector: severity} dict

    Deltas are only meaningful within a single config_hash.  When the hash
    changes between consecutive nights, a baseline_break sentinel row is
    written to nightly_rollup so the dashboard renders a discontinuity.
    """
    parts: list[Any] = []

    # Serialise the business context in a stable, hash-friendly way.
    ops = []
    for op in context.operations:
        ops.append({
            "name": op.name,
            "expected_tools": sorted(op.expected_tools),
            "required_side_effect_tools": sorted(op.required_side_effect_tools),
            "side_effect_match": op.side_effect_match,
            "excluded_tools": sorted(op.excluded_tools),
        })
    parts.append({
        "agent_name": context.agent_name,
        "correlation_key": context.correlation_key,
        "operations": sorted(ops, key=lambda o: o["name"]),
    })

    if detector_thresholds:
        parts.append({"thresholds": dict(sorted(detector_thresholds.items()))})
    if severity_map:
        parts.append({"severity_map": dict(sorted(severity_map.items()))})

    stable = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(stable.encode()).hexdigest()[:16]


def _latest_persisted_hash(night_id: date, conn: psycopg.Connection[Any]) -> str | None:
    """Return the most recent config_hash in nightly_rollup before night_id.

    Returns None when no prior rows exist.
    """
    row = conn.execute(
        "SELECT config_hash FROM nightly_rollup "
        "WHERE night_id < %s ORDER BY night_id DESC LIMIT 1",
        (night_id,),
    ).fetchone()
    return row[0] if row else None


# ── Redaction guard ────────────────────────────────────────────────────────────


def _safe_evidence_steps(finding: Finding) -> list[int]:
    """Return only integer step indices from a Finding's affected_step_indices.

    Security: this is the ONLY place a finding's indices cross the DB boundary.
    Only integers are returned; any non-int value is silently dropped.
    """
    return [idx for idx in finding.affected_step_indices if isinstance(idx, int)]


# ── Per-trace workflow lookup ──────────────────────────────────────────────────


def _build_trace_to_workflow(result: AnalysisResult) -> dict[str, str]:
    """Build a trace_id → workflow_name lookup from an AnalysisResult."""
    mapping: dict[str, str] = {}
    for ws in result.workflows:
        for tid in ws.primary_trace_ids:
            mapping[tid] = ws.operation_name
    return mapping


def _build_trace_to_struggle(
    envelopes: Sequence[TraceEnvelope],
) -> dict[str, float]:
    """Build trace_id → struggle scalar (error_count / max(1, step_count))."""
    return {
        e.trace_id: e.error_count / max(1, e.step_count)
        for e in envelopes
    }


def _build_trace_to_tokens(envelopes: Sequence[TraceEnvelope]) -> dict[str, int]:
    """Build trace_id → total_tokens from TraceEnvelopes."""
    return {e.trace_id: e.total_tokens for e in envelopes}


# ── findings ───────────────────────────────────────────────────────────────────


def persist_findings(
    *,
    night_id: date,
    result: AnalysisResult,
    envelopes: Sequence[TraceEnvelope],
    agent_by_trace: dict[str, str],
    config_hash: str,
    conn: psycopg.Connection[Any],
) -> int:
    """Write one findings row per (trace, detector) for all session-quality findings.

    Idempotent: ON CONFLICT (night_id, trace_id, detector) DO UPDATE.

    Security:
      • evidence_steps holds ONLY integer step indices (_safe_evidence_steps).
      • tokens is a scalar integer count.
      • No free text, no raw tool output, no secrets cross this boundary.

    Returns the number of rows upserted.
    """
    trace_to_workflow = _build_trace_to_workflow(result)
    trace_to_struggle = _build_trace_to_struggle(envelopes)
    trace_to_tokens = _build_trace_to_tokens(envelopes)

    # Build outcome lookup from per_trace_results across all workflow summaries.
    outcome_by_trace: dict[str, str] = {}
    for ws in result.workflows:
        for r in ws.outcome.per_trace_results:
            if not r.computable:
                outcome_by_trace[r.trace_id] = "non_computable"
            elif r.outcome_pass:
                outcome_by_trace[r.trace_id] = "pass"
            else:
                outcome_by_trace[r.trace_id] = "fail"

    # Collect all findings from unit_summaries (union across all traces).
    all_findings: list[Finding] = []
    for us in result.unit_summaries:
        all_findings.extend(us.unit_findings)

    # De-duplicate by (trace_id, pattern_name).
    seen: set[tuple[str, str]] = set()
    deduped: list[Finding] = []
    for f in all_findings:
        key = (f.trace_id, f.pattern_name)
        if key not in seen:
            seen.add(key)
            deduped.append(f)

    # Also pick up tier-1 deterministic findings not already in unit_findings.
    for ws in result.workflows:
        for f in ws.deterministic_findings:
            key = (f.trace_id, f.pattern_name)
            if key not in seen:
                seen.add(key)
                deduped.append(f)

    if not deduped:
        logger.info("persist.findings.no_findings", night=str(night_id))
        return 0

    # Build unit_id lookup: trace_id → unit_id
    unit_id_by_trace: dict[str, str] = {}
    for us in result.unit_summaries:
        for tid in us.trace_ids:
            unit_id_by_trace[tid] = us.unit_id

    rows = []
    for finding in deduped:
        tid = finding.trace_id
        workflow = trace_to_workflow.get(tid, "unmapped")
        agent = agent_by_trace.get(tid, "unknown")
        unit_id = unit_id_by_trace.get(tid, tid)
        tokens = trace_to_tokens.get(tid, 0)
        struggle = round(trace_to_struggle.get(tid, 0.0), 4)
        outcome = outcome_by_trace.get(tid, "non_computable")

        # SECURITY: only integer indices, never raw output.
        evidence_steps = _safe_evidence_steps(finding)

        rows.append((
            night_id,
            tid,
            unit_id,
            workflow,
            agent,
            finding.pattern_name,  # detector
            finding.severity,
            evidence_steps,
            tokens,
            struggle,
            outcome,
            config_hash,
        ))

    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO findings
                (night_id, trace_id, unit_id, workflow, agent,
                 detector, severity, evidence_steps, tokens, struggle,
                 outcome, config_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (night_id, trace_id, detector) DO UPDATE
                SET unit_id       = EXCLUDED.unit_id,
                    workflow      = EXCLUDED.workflow,
                    agent         = EXCLUDED.agent,
                    severity      = EXCLUDED.severity,
                    evidence_steps= EXCLUDED.evidence_steps,
                    tokens        = EXCLUDED.tokens,
                    struggle      = EXCLUDED.struggle,
                    outcome       = EXCLUDED.outcome,
                    config_hash   = EXCLUDED.config_hash,
                    ingested_at   = now()
            """,
            rows,
        )
    conn.commit()

    logger.info("persist.findings.upserted", night=str(night_id), count=len(rows))
    return len(rows)


# ── nightly_rollup ─────────────────────────────────────────────────────────────


def _percentile(values: list[float], p: float) -> float:
    """Return the p-th percentile of *values* (0 ≤ p ≤ 100).

    Uses linear interpolation (same as numpy percentile default).
    Returns 0.0 for empty input.
    """
    if not values:
        return 0.0
    n = len(values)
    if n == 1:
        return values[0]
    sorted_vals = sorted(values)
    # Linear interpolation index
    idx = (p / 100.0) * (n - 1)
    lo = int(idx)
    hi = lo + 1
    if hi >= n:
        return sorted_vals[-1]
    frac = idx - lo
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


def persist_nightly_rollup(
    *,
    night_id: date,
    result: AnalysisResult,
    envelopes: Sequence[TraceEnvelope],
    agent_by_trace: dict[str, str],
    config_hash: str,
    conn: psycopg.Connection[Any],
) -> int:
    """Write one nightly_rollup row per (workflow, agent).

    Idempotent: ON CONFLICT (night_id, workflow, agent) DO UPDATE.

    config_hash discipline:
      When config_hash differs from the most recently persisted hash, writes a
      baseline_break=true sentinel row (zero-value metrics, baseline_break set)
      so the dashboard sees a discontinuity.  The normal data rows are ALSO
      written with their real metrics — the baseline_break row is a signal,
      not a replacement.

    Returns the number of rows upserted (including any baseline_break rows).
    """
    trace_to_workflow = _build_trace_to_workflow(result)
    trace_to_struggle = _build_trace_to_struggle(envelopes)
    trace_to_tokens = _build_trace_to_tokens(envelopes)

    # config_hash change check — write baseline_break sentinel if needed.
    prior_hash = _latest_persisted_hash(night_id, conn)
    hash_changed = prior_hash is not None and prior_hash != config_hash

    # Per-workflow, per-agent accumulators.
    buckets: dict[tuple[str, str], dict[str, Any]] = defaultdict(lambda: {
        "unit_ids": set(),
        "trace_ids": set(),
        "outcomes": [],       # bool per computable unit
        "struggles": [],      # float per trace
        "tokens": [],         # int per unit
        "finding_counts": Counter(),
    })

    # Walk unit_summaries to fill buckets.
    for us in result.unit_summaries:
        # Determine workflow + agent for this unit (first trace wins).
        workflow = "unmapped"
        agent = "unknown"
        for tid in us.trace_ids:
            wf = trace_to_workflow.get(tid)
            if wf:
                workflow = wf
                agent = agent_by_trace.get(tid, "unknown")
                break

        key = (workflow, agent)
        b = buckets[key]

        b["unit_ids"].add(us.unit_id)
        b["trace_ids"].update(us.trace_ids)

        if us.unit_computable and us.unit_outcome_pass is not None:
            b["outcomes"].append(us.unit_outcome_pass)

        # Tokens: sum across unit traces.
        unit_tokens = sum(trace_to_tokens.get(tid, 0) for tid in us.trace_ids)
        b["tokens"].append(unit_tokens)

        # Struggle: per-trace scalar → collect all for p50/p90.
        for tid in us.trace_ids:
            b["struggles"].append(trace_to_struggle.get(tid, 0.0))

        # Finding counts: per detector.
        for f in us.unit_findings:
            b["finding_counts"][f.pattern_name] += 1

    rows_upserted = 0

    # Write baseline_break sentinel rows before data rows so the dashboard
    # sees the discontinuity signal.
    if hash_changed:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO nightly_rollup
                    (night_id, workflow, agent, units, traces, outcome_rate,
                     struggle_p50, struggle_p90, coordination_waste_rate,
                     tokens_per_unit, finding_counts, config_hash, baseline_break)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (night_id, workflow, agent) DO UPDATE
                    SET config_hash    = EXCLUDED.config_hash,
                        baseline_break = true
                """,
                (
                    night_id,
                    "_config_change_",
                    "_",
                    0, 0, 0.0, 0.0, 0.0, 0.0, 0.0,
                    Jsonb({}),
                    config_hash,
                    True,
                ),
            )
        conn.commit()
        rows_upserted += 1
        logger.warning(
            "persist.nightly_rollup.baseline_break",
            night=str(night_id),
            prior_hash=prior_hash,
            new_hash=config_hash,
        )

    # Write real data rows.
    for (workflow, agent), b in buckets.items():
        units = len(b["unit_ids"])
        traces = len(b["trace_ids"])
        outcomes: list[bool] = b["outcomes"]
        outcome_rate = (sum(outcomes) / len(outcomes)) if outcomes else 0.0
        struggles: list[float] = b["struggles"]
        struggle_p50 = _percentile(struggles, 50)
        struggle_p90 = _percentile(struggles, 90)
        tokens_list: list[int] = b["tokens"]
        tokens_per_unit = (sum(tokens_list) / len(tokens_list)) if tokens_list else 0.0
        finding_counts_dict = dict(b["finding_counts"])

        # coordination_waste_rate: fraction of units that have a coordination_waste finding.
        coord_waste_units = sum(
            1
            for us in result.unit_summaries
            if any(f.pattern_name == "coordination_waste" for f in us.unit_findings)
            and any(trace_to_workflow.get(tid) == workflow for tid in us.trace_ids)
        )
        coordination_waste_rate = coord_waste_units / max(1, units)

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO nightly_rollup
                    (night_id, workflow, agent, units, traces, outcome_rate,
                     struggle_p50, struggle_p90, coordination_waste_rate,
                     tokens_per_unit, finding_counts, config_hash, baseline_break)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (night_id, workflow, agent) DO UPDATE
                    SET units                   = EXCLUDED.units,
                        traces                  = EXCLUDED.traces,
                        outcome_rate            = EXCLUDED.outcome_rate,
                        struggle_p50            = EXCLUDED.struggle_p50,
                        struggle_p90            = EXCLUDED.struggle_p90,
                        coordination_waste_rate = EXCLUDED.coordination_waste_rate,
                        tokens_per_unit         = EXCLUDED.tokens_per_unit,
                        finding_counts          = EXCLUDED.finding_counts,
                        config_hash             = EXCLUDED.config_hash,
                        baseline_break          = EXCLUDED.baseline_break
                """,
                (
                    night_id,
                    workflow,
                    agent,
                    units,
                    traces,
                    round(outcome_rate, 6),
                    round(struggle_p50, 6),
                    round(struggle_p90, 6),
                    round(coordination_waste_rate, 6),
                    round(tokens_per_unit, 2),
                    Jsonb(finding_counts_dict),
                    config_hash,
                    False,
                ),
            )
        conn.commit()
        rows_upserted += 1

    logger.info(
        "persist.nightly_rollup.upserted",
        night=str(night_id),
        count=rows_upserted,
        hash_changed=hash_changed,
    )
    return rows_upserted


# ── Public entry point ─────────────────────────────────────────────────────────


def persist_night(
    *,
    night_id: date,
    result: AnalysisResult,
    envelopes: Sequence[TraceEnvelope],
    agent_by_trace: dict[str, str],
    context: Any,  # BusinessContext — typed Any to avoid heavy import
    detector_thresholds: dict[str, Any] | None = None,
    severity_map: dict[str, str] | None = None,
    conn: psycopg.Connection[Any] | None = None,
) -> dict[str, int]:
    """Persist one night's analysis: findings + nightly_rollup.

    This is the single entry point for the nightly loop.  Idempotent.

    Parameters
    ----------
    night_id:
        UTC date that owns this batch (e.g. ``date.today()``).
    result:
        AnalysisResult from run_pipeline() — contains workflows, unit_summaries.
    envelopes:
        All TraceEnvelopes from that night's window.
    agent_by_trace:
        trace_id → agent name (from Phoenix root span meta, ``paperclip.agent_id``
        or ``service.name``).  Use "unknown" when absent.
    context:
        BusinessContext used for this run — drives config_hash computation.
    detector_thresholds:
        Optional threshold dict for config_hash (e.g. STRUGGLE_T, RECOVERY_WINDOW).
    severity_map:
        Optional {detector: severity} for config_hash.
    conn:
        Optional pre-opened psycopg connection (for tests).  When None, a new
        connection is opened from KAIROS_PG_DSN.

    Returns
    -------
    dict with keys: findings_rows, rollup_rows.
    """
    apply_migrations()

    cfg_hash = compute_config_hash(context, detector_thresholds, severity_map)

    _conn: psycopg.Connection[Any] = get_connection() if conn is None else conn
    own_conn = conn is None

    try:
        findings_rows = persist_findings(
            night_id=night_id,
            result=result,
            envelopes=envelopes,
            agent_by_trace=agent_by_trace,
            config_hash=cfg_hash,
            conn=_conn,
        )
        rollup_rows = persist_nightly_rollup(
            night_id=night_id,
            result=result,
            envelopes=envelopes,
            agent_by_trace=agent_by_trace,
            config_hash=cfg_hash,
            conn=_conn,
        )
    finally:
        if own_conn:
            _conn.close()

    return {"findings_rows": findings_rows, "rollup_rows": rollup_rows}
