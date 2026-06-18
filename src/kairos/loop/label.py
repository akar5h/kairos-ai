"""P4.1 — Semantic cluster labeling via LLM.

Reads representative trace envelopes per discovery_queue cluster, calls an
LLM to name the failure pattern, and stores the result as a ClusterInsight
row.  The LLM is a *labeler only* — clustering, gating, and detection remain
deterministic.

Security:
  - Only tool_sequence (tool names), error_message text, and aggregate
    metrics (token count, latency) are sent to the LLM.
  - tool_args / tool_output / user_input are NEVER included in the prompt.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

import psycopg
from pydantic import BaseModel

from kairos.analysis.llm_client import LLMClient
from kairos.log import get_logger
from kairos.loop.outcomes import load_outcome_labels
from kairos.models.enums import StepStatus
from kairos.readers.db import fetch_envelope_from_db

if TYPE_CHECKING:
    from kairos.models.trace import TraceEnvelope

logger = get_logger(__name__)

# auto_approve threshold: confidence must exceed this AND is_coherent must be True.
AUTO_APPROVE_CONFIDENCE_T: float = 0.8


# ── LLM response schema ───────────────────────────────────────────────────────


class ClusterInsightResponse(BaseModel):
    """JSON schema the LLM must return. auto_approve is always overwritten."""

    pattern_name: str
    description: str
    discriminator_hint: str
    root_cause: str
    confidence: float
    is_coherent: bool
    auto_approve: bool  # ignored — Python recomputes from confidence + is_coherent


# ── Stored dataclass ──────────────────────────────────────────────────────────


@dataclass
class ClusterInsight:
    """A persisted cluster insight row."""

    id: str
    cluster_key: str
    pattern_name: str | None
    description: str | None
    discriminator_hint: str | None
    root_cause: str | None
    confidence: float | None
    is_coherent: bool | None
    auto_approve: bool
    model_used: str | None


# ── Prompt builder ────────────────────────────────────────────────────────────


def _build_prompt(
    cluster_key: str,
    envelopes: list[TraceEnvelope],
    outcome_labels: dict[str, str],
) -> str:
    """Build the LLM labeling prompt for a cluster.

    Only tool names, error messages, and aggregate metrics are included.
    Raw args and user content are never sent.
    """
    lines: list[str] = [
        "You are analyzing a cluster of agent execution traces to identify failure patterns.",
        "",
        f"Cluster: {cluster_key}",
        f"Traces analyzed: {len(envelopes)}",
        "",
        "For each trace, here is the tool sequence and outcome:",
    ]

    for i, envelope in enumerate(envelopes, start=1):
        outcome = outcome_labels.get(envelope.trace_id, "unknown")
        error_tool_names = [s.tool_name for s in envelope.steps if s.status == StepStatus.ERROR and s.tool_name][:10]
        lines += [
            "",
            f"--- Trace {i} ({envelope.trace_id[:8]}) [outcome: {outcome}] ---",
            f"Tool sequence: {' → '.join(envelope.tool_sequence[:30])}",
            f"Steps with errors: {error_tool_names}",
            f"Token count: {envelope.total_tokens}",
        ]

    lines += [
        "",
        "Based on these traces, identify the common failure pattern (if any).",
        "",
        "Respond with JSON only (no markdown fences, no commentary):",
        "{",
        '  "pattern_name": "snake_case_name_under_5_words",',
        '  "description": "One sentence: what the agent does wrong in this cluster.",',
        '  "discriminator_hint": "One sentence: a deterministic rule that would detect this.",',
        '  "root_cause": "One sentence: why the agent fails.",',
        '  "confidence": 0.0,',
        '  "is_coherent": true,',
        '  "auto_approve": false',
        "}",
    ]

    return "\n".join(lines)


# ── Core labeling functions ───────────────────────────────────────────────────


def label_cluster(
    cluster_key: str,
    dsn: str,
    *,
    model: str | None = None,
) -> ClusterInsight | None:
    """Label one cluster: fetch envelopes, call LLM, persist insight.

    Returns ClusterInsight on success; None on any failure (no traces, no API
    key, LLM exhausted retries, DB error).
    """
    # 1. Fetch up to 5 trace_ids for this cluster.
    try:
        with psycopg.connect(dsn) as conn:
            rows = conn.execute(
                "SELECT trace_id FROM discovery_queue WHERE cluster_key = %s ORDER BY night_id DESC LIMIT 5",
                (cluster_key,),
            ).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning("label_cluster.db_error", cluster_key=cluster_key, error=str(exc))
        return None

    if not rows:
        logger.info("label_cluster.no_traces", cluster_key=cluster_key)
        return None

    trace_ids = [str(row[0]) for row in rows]

    # 2. Fetch envelopes — skip traces that fail to load.
    envelopes: list[TraceEnvelope] = []
    for tid in trace_ids:
        try:
            env = fetch_envelope_from_db(tid, dsn, enrich_hooks=False)
            envelopes.append(env)
        except Exception as exc:  # noqa: BLE001
            logger.warning("label_cluster.envelope_fetch_failed", trace_id=tid[:16], error=str(exc))

    if not envelopes:
        logger.warning("label_cluster.all_envelopes_failed", cluster_key=cluster_key)
        return None

    # 3. Load outcome labels for context.
    outcome_labels = load_outcome_labels(dsn)

    # 4. Build prompt.
    prompt = _build_prompt(cluster_key, envelopes, outcome_labels)

    # 5. Call LLM — gracefully skip when API key is absent.
    try:
        client = LLMClient(model=model)
    except ValueError as exc:
        logger.warning("label_cluster.no_api_key", cluster_key=cluster_key, error=str(exc))
        return None

    raw_response = client.generate(prompt, ClusterInsightResponse)
    if raw_response is None:
        logger.warning("label_cluster.llm_failed", cluster_key=cluster_key)
        return None
    assert isinstance(raw_response, ClusterInsightResponse)
    response: ClusterInsightResponse = raw_response

    # 6. Compute auto_approve from our rule — NEVER trust the LLM's field.
    auto_approve = response.confidence > AUTO_APPROVE_CONFIDENCE_T and response.is_coherent

    # 7. Persist to cluster_insights.
    insight_id = str(uuid.uuid4())
    resolved_model = client.model
    try:
        with psycopg.connect(dsn) as conn:
            conn.execute(
                """
                INSERT INTO cluster_insights
                  (id, cluster_key, pattern_name, description, discriminator_hint,
                   root_cause, confidence, is_coherent, auto_approve, model_used)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    insight_id,
                    cluster_key,
                    response.pattern_name,
                    response.description,
                    response.discriminator_hint,
                    response.root_cause,
                    response.confidence,
                    response.is_coherent,
                    auto_approve,
                    resolved_model,
                ),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("label_cluster.insert_failed", cluster_key=cluster_key, error=str(exc))
        return None

    logger.info(
        "label_cluster.done",
        cluster_key=cluster_key,
        pattern_name=response.pattern_name,
        confidence=response.confidence,
        is_coherent=response.is_coherent,
        auto_approve=auto_approve,
    )

    return ClusterInsight(
        id=insight_id,
        cluster_key=cluster_key,
        pattern_name=response.pattern_name,
        description=response.description,
        discriminator_hint=response.discriminator_hint,
        root_cause=response.root_cause,
        confidence=response.confidence,
        is_coherent=response.is_coherent,
        auto_approve=auto_approve,
        model_used=resolved_model,
    )


def label_all_unlabeled(
    dsn: str,
    *,
    model: str | None = None,
) -> list[ClusterInsight]:
    """Label all clusters that have no entry in cluster_insights yet.

    Returns the list of successfully created ClusterInsight objects.
    """
    try:
        with psycopg.connect(dsn) as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT cluster_key FROM discovery_queue
                WHERE cluster_key NOT IN (SELECT DISTINCT cluster_key FROM cluster_insights)
                ORDER BY cluster_key
                """
            ).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning("label_all_unlabeled.db_error", error=str(exc))
        return []

    cluster_keys = [str(row[0]) for row in rows]
    logger.info("label_all_unlabeled.start", unlabeled_count=len(cluster_keys))

    results: list[ClusterInsight] = []
    for i, ck in enumerate(cluster_keys, start=1):
        logger.info("label_all_unlabeled.progress", current=i, total=len(cluster_keys), cluster_key=ck)
        insight = label_cluster(ck, dsn, model=model)
        if insight is not None:
            results.append(insight)

    logger.info("label_all_unlabeled.done", labeled=len(results), total=len(cluster_keys))
    return results


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import os
    import sys

    dsn = os.environ.get("KAIROS_PG_DSN", "").strip()
    if not dsn:
        print("KAIROS_PG_DSN not set", file=sys.stderr)  # noqa: T201
        sys.exit(1)
    _model = os.environ.get("KAIROS_LLM_MODEL")
    _results = label_all_unlabeled(dsn, model=_model)
    print(f"Labeled {len(_results)} clusters")  # noqa: T201
    for r in _results:
        _status = "AUTO-APPROVED" if r.auto_approve else "needs review"
        _conf = f"{r.confidence:.2f}" if r.confidence is not None else "?"
        print(f"  {r.cluster_key[:60]}: {r.pattern_name} (conf={_conf}, {_status})")  # noqa: T201
