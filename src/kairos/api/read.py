"""F2.1 Read API — READ-ONLY routes over Kairos Postgres.

Six routes that form the contract layer between Postgres and the UI / evals:

    GET /traces                          — lightweight trace list (no N+1)
    GET /traces/{trace_id}               — full TraceEnvelope (404 if missing)
    GET /clusters                        — cluster aggregates from discovery_queue
    GET /clusters/{cluster_key}/traces   — trace_ids in a cluster
    GET /findings                        — findings rows (filter required)
    GET /labels                          — labels rows for a trace

Design rules enforced here
--------------------------
- DSN exclusively from _dsn() (never hardcoded, never logged).
- ALL SQL uses parameterized queries (no f-string interpolation of params).
- DB errors → clean HTTP 500 (logged) — exception text is never exposed.
- Empty results → 200 + empty list (not 404), EXCEPT /traces/{id} which 404s.
- At least one filter is required for /findings to avoid full-table scans.
"""

from __future__ import annotations

import logging
from datetime import datetime  # noqa: TC003 — pydantic needs the runtime type
from typing import Any

import fastapi
import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel

from kairos.loop.db import _dsn
from kairos.readers.db import fetch_envelope_from_db

logger = logging.getLogger(__name__)

router = fastapi.APIRouter(prefix="/v1", tags=["read"])


# ─── Response models ──────────────────────────────────────────────────────────


class TraceSummary(BaseModel):
    """Lightweight trace summary returned by GET /traces.

    Built from a single aggregate query over the ``spans`` table — no N+1.
    We intentionally avoid building full TraceEnvelopes for the list view.
    """

    trace_id: str
    started_at: datetime | None
    span_count: int
    error_count: int


class ClusterSummary(BaseModel):
    """Aggregate row returned by GET /clusters."""

    cluster_key: str
    trace_count: int
    min_night_id: str | None
    kinds: list[str]
    sample_features: dict[str, Any]


class ClusterTraceMember(BaseModel):
    """One trace in a cluster returned by GET /clusters/{cluster_key}/traces."""

    trace_id: str
    labeled: bool


class FindingRow(BaseModel):
    """One row from the ``findings`` table."""

    night_id: str
    trace_id: str
    unit_id: str
    workflow: str
    agent: str
    detector: str
    severity: str
    evidence_steps: list[int]
    tokens: int
    struggle: float
    outcome: str
    config_hash: str
    ingested_at: datetime


class LabelRow(BaseModel):
    """One row from the ``labels`` table."""

    id: str
    trace_id: str
    question: str
    answer: str
    verdict: str
    label_class: str
    ts: datetime


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _connect() -> psycopg.Connection[dict[str, Any]]:
    """Open a dict-row psycopg connection using _dsn().

    Raises RuntimeError (propagated as 500) when KAIROS_PG_DSN is absent.
    """
    return psycopg.connect(_dsn(), row_factory=dict_row)


# ─── Routes ───────────────────────────────────────────────────────────────────


@router.get("/traces", response_model=list[TraceSummary])
def get_traces(
    since: str | None = fastapi.Query(
        None,
        description="ISO-8601 timestamp; only traces started at or after this time.",
    ),
    limit: int = fastapi.Query(
        100,
        ge=1,
        le=1000,
        description="Maximum number of traces to return (1–1000, default 100).",
    ),
) -> list[TraceSummary]:
    """List traces with a lightweight per-trace summary.

    Uses a single aggregate query over ``spans`` — one round-trip, no N+1.
    For each trace we compute: started_at (min start_time), span_count,
    error_count (spans with status_code = 'ERROR').
    """
    # Build parameterized query.
    #
    # We extend list_trace_ids' filtering logic but return richer aggregates
    # in the same query so we never fetch trace_ids then query each separately.
    params: list[object] = []
    having: list[str] = []

    if since is not None:
        having.append("min(start_time) >= %s")
        params.append(since)

    having_clause = ("HAVING " + " AND ".join(having)) if having else ""
    params.append(limit)

    sql = f"""
        SELECT
            trace_id,
            min(start_time)                                    AS started_at,
            count(*)                                           AS span_count,
            count(*) FILTER (WHERE status_code = 'ERROR')     AS error_count
        FROM spans
        GROUP BY trace_id
        {having_clause}
        ORDER BY min(start_time) DESC
        LIMIT %s
    """  # noqa: S608 — no user data interpolated; only structural SQL

    try:
        with _connect() as conn:
            rows = conn.execute(sql, params).fetchall()
    except Exception:
        logger.exception("read.get_traces failed")
        raise fastapi.HTTPException(status_code=500, detail="Database error") from None

    return [
        TraceSummary(
            trace_id=row["trace_id"],
            started_at=row["started_at"],
            span_count=int(row["span_count"]),
            error_count=int(row["error_count"]),
        )
        for row in rows
    ]


@router.get("/traces/{trace_id}")
def get_trace(
    trace_id: str = fastapi.Path(..., description="Hex trace ID."),
    enrich_hooks: bool = fastapi.Query(
        False,
        description="When true, overwrite step fields from hook_events table.",
    ),
) -> dict[str, Any]:
    """Return the full TraceEnvelope for a single trace.

    Uses ``fetch_envelope_from_db`` — returns steps (conversation/timeline)
    and all aggregated metrics.  404 when no spans exist for the trace_id.

    TraceEnvelope and Step are Pydantic BaseModels so FastAPI serializes them
    directly; no additional response model wrapper needed.
    """
    try:
        dsn = _dsn()
    except RuntimeError:
        logger.exception("read.get_trace dsn_error trace_id=%s", trace_id)
        raise fastapi.HTTPException(status_code=500, detail="Database error") from None

    # Check existence first with a cheap COUNT to return a clean 404.
    try:
        with psycopg.connect(dsn, row_factory=dict_row) as conn:
            row = conn.execute(
                "SELECT count(*) AS n FROM spans WHERE trace_id = %s",
                (trace_id,),
            ).fetchone()
    except Exception:
        logger.exception("read.get_trace count_failed trace_id=%s", trace_id)
        raise fastapi.HTTPException(status_code=500, detail="Database error") from None

    if row is None or int(row["n"]) == 0:
        raise fastapi.HTTPException(
            status_code=404,
            detail=f"Trace {trace_id!r} not found",
        )

    try:
        envelope = fetch_envelope_from_db(
            trace_id,
            dsn,
            enrich_hooks=enrich_hooks,
        )
    except Exception:
        logger.exception("read.get_trace build_failed trace_id=%s", trace_id)
        raise fastapi.HTTPException(status_code=500, detail="Database error") from None

    # TraceEnvelope is a Pydantic BaseModel — .model_dump() for full JSON output.
    return envelope.model_dump(mode="json")


@router.get("/clusters", response_model=list[ClusterSummary])
def get_clusters() -> list[ClusterSummary]:
    """Return cluster aggregates from the discovery_queue table.

    One row per cluster_key, ordered by trace count descending.
    Includes a sample features blob from one member of the cluster.

    Query: single pass over discovery_queue — no N+1.
    """
    sql = """
        SELECT
            cluster_key,
            count(DISTINCT trace_id)            AS trace_count,
            min(night_id)::text                 AS min_night_id,
            array_agg(DISTINCT kind)            AS kinds,
            (array_agg(features ORDER BY id))[1] AS sample_features
        FROM discovery_queue
        GROUP BY cluster_key
        ORDER BY count(DISTINCT trace_id) DESC
    """  # noqa: S608 — no user data interpolated

    try:
        with _connect() as conn:
            rows = conn.execute(sql).fetchall()
    except Exception:
        logger.exception("read.get_clusters failed")
        raise fastapi.HTTPException(status_code=500, detail="Database error") from None

    return [
        ClusterSummary(
            cluster_key=row["cluster_key"],
            trace_count=int(row["trace_count"]),
            min_night_id=row["min_night_id"],
            kinds=list(row["kinds"] or []),
            sample_features=dict(row["sample_features"] or {}),
        )
        for row in rows
    ]


@router.get("/clusters/{cluster_key}/traces", response_model=list[ClusterTraceMember])
def get_cluster_traces(
    cluster_key: str = fastapi.Path(..., description="Cluster key."),
) -> list[ClusterTraceMember]:
    """Return trace_ids and labeled status for all members of a cluster."""
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT trace_id, labeled "
                "FROM discovery_queue "
                "WHERE cluster_key = %s "
                "ORDER BY night_id DESC, id",
                (cluster_key,),
            ).fetchall()
    except Exception:
        logger.exception("read.get_cluster_traces failed cluster_key=%s", cluster_key)
        raise fastapi.HTTPException(status_code=500, detail="Database error") from None

    return [
        ClusterTraceMember(trace_id=row["trace_id"], labeled=bool(row["labeled"]))
        for row in rows
    ]


@router.get("/findings", response_model=list[FindingRow])
def get_findings(
    trace_id: str | None = fastapi.Query(None, description="Filter by trace_id."),
    night_id: str | None = fastapi.Query(
        None, description="Filter by night_id (YYYY-MM-DD)."
    ),
) -> list[FindingRow]:
    """Return findings rows.

    At least one of ``trace_id`` or ``night_id`` must be supplied to avoid
    full-table scans.  Returns 400 if neither is provided.
    """
    if trace_id is None and night_id is None:
        raise fastapi.HTTPException(
            status_code=400,
            detail="At least one of 'trace_id' or 'night_id' must be provided.",
        )

    clauses: list[str] = []
    params: list[object] = []

    if trace_id is not None:
        clauses.append("trace_id = %s")
        params.append(trace_id)
    if night_id is not None:
        clauses.append("night_id = %s")
        params.append(night_id)

    sql = (
        "SELECT night_id::text, trace_id, unit_id, workflow, agent, detector, "
        "       severity, evidence_steps, tokens, struggle, outcome, config_hash, ingested_at "
        "FROM findings "
        "WHERE " + " AND ".join(clauses) + " "
        "ORDER BY ingested_at DESC"
    )

    try:
        with _connect() as conn:
            rows = conn.execute(sql, params).fetchall()
    except Exception:
        logger.exception("read.get_findings failed trace_id=%s night_id=%s", trace_id, night_id)
        raise fastapi.HTTPException(status_code=500, detail="Database error") from None

    return [
        FindingRow(
            night_id=row["night_id"],
            trace_id=row["trace_id"],
            unit_id=row["unit_id"],
            workflow=row["workflow"],
            agent=row["agent"],
            detector=row["detector"],
            severity=row["severity"],
            evidence_steps=list(row["evidence_steps"] or []),
            tokens=int(row["tokens"]),
            struggle=float(row["struggle"]),
            outcome=row["outcome"],
            config_hash=row["config_hash"],
            ingested_at=row["ingested_at"],
        )
        for row in rows
    ]


@router.get("/labels", response_model=list[LabelRow])
def get_labels(
    trace_id: str = fastapi.Query(..., description="trace_id to fetch labels for."),
) -> list[LabelRow]:
    """Return all labels for a given trace_id."""
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT id, trace_id, question, answer, verdict, label_class, ts "
                "FROM labels "
                "WHERE trace_id = %s "
                "ORDER BY ts DESC",
                (trace_id,),
            ).fetchall()
    except Exception:
        logger.exception("read.get_labels failed trace_id=%s", trace_id)
        raise fastapi.HTTPException(status_code=500, detail="Database error") from None

    return [
        LabelRow(
            id=row["id"],
            trace_id=row["trace_id"],
            question=row["question"],
            answer=row["answer"],
            verdict=row["verdict"],
            label_class=row["label_class"],
            ts=row["ts"],
        )
        for row in rows
    ]
