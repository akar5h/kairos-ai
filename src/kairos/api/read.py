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
import uuid
from datetime import UTC, datetime  # noqa: TC003 — pydantic needs the runtime type
from typing import Any

import fastapi
import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel, field_validator

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
    """One row from the ``labels`` table.

    ``question``, ``verdict`` and ``label_class`` are nullable (migration 0014
    relaxed the original NOT NULL); only ``trace_id`` and ``answer`` are required.
    """

    id: str
    trace_id: str
    question: str | None
    answer: str
    verdict: str | None
    label_class: str | None
    ts: datetime


class LabelCreate(BaseModel):
    """Request body for POST /v1/labels (append-only label write).

    Locked contract: ``trace_id`` and ``answer`` are required; ``question``,
    ``verdict`` and ``label_class`` are optional. ``verdict`` — when supplied —
    must be one of ``tp`` / ``fp`` / ``fn`` (invalid → 422).
    """

    trace_id: str
    answer: str
    question: str | None = None
    verdict: str | None = None
    label_class: str | None = None

    @field_validator("verdict")
    @classmethod
    def _verdict_in_enum(cls, v: str | None) -> str | None:
        if v is not None and v not in {"tp", "fp", "fn"}:
            msg = "verdict must be one of 'tp', 'fp', 'fn', or null"
            raise ValueError(msg)
        return v


class SessionSummary(BaseModel):
    """Aggregate row returned by GET /v1/sessions.

    Groups spans by session_id; NULL session_id rows are excluded.
    """

    session_id: str
    trace_count: int
    span_count: int
    error_count: int
    started_at: datetime | None
    ended_at: datetime | None
    tools: list[str]


class TraceInSession(BaseModel):
    """One trace inside a session returned by GET /v1/sessions/{session_id}."""

    trace_id: str
    span_count: int
    error_count: int
    started_at: datetime | None
    ended_at: datetime | None
    tools: list[str]


class RawSpan(BaseModel):
    """One raw span returned by GET /v1/traces/{trace_id}/spans.

    ``attributes`` contains a compact subset by default; pass ``?full=true``
    to include all attributes.
    """

    span_id: str
    parent_span_id: str | None
    name: str
    tool_name: str | None
    status_code: str | None
    start_time: datetime | None
    end_time: datetime | None
    attributes: dict[str, Any]


class SearchHits(BaseModel):
    """Grouped search results returned by GET /v1/search."""

    sessions: list[dict[str, Any]]
    traces: list[dict[str, Any]]
    spans: list[dict[str, Any]]


# Compact attribute keys included by default in RawSpan.
_COMPACT_ATTR_KEYS: frozenset[str] = frozenset({"tool_name", "session.id", "span.type", "kairos.span.kind"})


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


@router.post("/labels", response_model=LabelRow, status_code=201)
def create_label(body: LabelCreate) -> LabelRow:
    """Append a new label row (write path for the review UI).

    APPEND-ONLY: every call inserts a fresh row — labels are never updated or
    deleted. ``id`` is a generated uuid hex; ``ts`` is set to now(). The trace
    is NOT required to exist (labels can be recorded for any trace_id).

    Returns the created row (201). Invalid ``verdict`` is rejected at the
    boundary by ``LabelCreate`` (422 before this body runs).
    """
    label_id = uuid.uuid4().hex
    ts = datetime.now(UTC)
    try:
        with _connect() as conn:
            row = conn.execute(
                "INSERT INTO labels "
                "  (id, trace_id, question, answer, verdict, label_class, ts) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "RETURNING id, trace_id, question, answer, verdict, label_class, ts",
                (
                    label_id,
                    body.trace_id,
                    body.question,
                    body.answer,
                    body.verdict,
                    body.label_class,
                    ts,
                ),
            ).fetchone()
            conn.commit()
    except Exception:
        logger.exception("read.create_label failed trace_id=%s", body.trace_id)
        raise fastapi.HTTPException(status_code=500, detail="Database error") from None

    if row is None:  # defensive — RETURNING always yields a row on success
        logger.error("read.create_label no row returned trace_id=%s", body.trace_id)
        raise fastapi.HTTPException(status_code=500, detail="Database error")

    return LabelRow(
        id=row["id"],
        trace_id=row["trace_id"],
        question=row["question"],
        answer=row["answer"],
        verdict=row["verdict"],
        label_class=row["label_class"],
        ts=row["ts"],
    )


# ─── Session hierarchy routes ─────────────────────────────────────────────────


@router.get("/sessions", response_model=list[SessionSummary])
def get_sessions(
    q: str | None = fastapi.Query(
        None,
        description="Filter sessions by session_id prefix or ILIKE match.",
    ),
    since: str | None = fastapi.Query(
        None,
        description="ISO-8601 timestamp; only sessions started at or after this time.",
    ),
    limit: int = fastapi.Query(
        100,
        ge=1,
        le=1000,
        description="Maximum number of sessions to return (1–1000, default 100).",
    ),
) -> list[SessionSummary]:
    """List sessions grouped from the spans table.

    Groups spans BY session_id (NULL rows excluded — they have no session
    context).  Per session: trace_count, span_count, error_count, started_at,
    ended_at, tools (distinct tool names, limited to 20).

    ``q`` filters on session_id using ILIKE (parameterized: ``%q%``).
    ``since`` filters on started_at (min start_time in the group).
    Orders by started_at DESC.
    """
    clauses: list[str] = ["session_id IS NOT NULL"]
    params: list[object] = []

    if q is not None:
        clauses.append("session_id ILIKE %s")
        params.append(f"%{q}%")

    where_clause = "WHERE " + " AND ".join(clauses)

    having_clauses: list[str] = []
    if since is not None:
        having_clauses.append("min(start_time) >= %s")
        params.append(since)

    having_clause = ("HAVING " + " AND ".join(having_clauses)) if having_clauses else ""
    params.append(limit)

    sql = f"""
        SELECT
            session_id,
            count(DISTINCT trace_id)                                        AS trace_count,
            count(*)                                                        AS span_count,
            count(*) FILTER (WHERE status_code = 'ERROR')                  AS error_count,
            min(start_time)                                                 AS started_at,
            max(end_time)                                                   AS ended_at,
            (
                SELECT array_agg(DISTINCT t)
                FROM unnest(
                    array_agg(attributes->>'tool_name')
                    FILTER (WHERE attributes->>'tool_name' IS NOT NULL)
                ) AS t
            )                                                               AS tools
        FROM spans
        {where_clause}
        GROUP BY session_id
        {having_clause}
        ORDER BY min(start_time) DESC
        LIMIT %s
    """  # noqa: S608 — no user data interpolated; only structural SQL

    try:
        with _connect() as conn:
            rows = conn.execute(sql, params).fetchall()
    except Exception:
        logger.exception("read.get_sessions failed q=%s since=%s", q, since)
        raise fastapi.HTTPException(status_code=500, detail="Database error") from None

    return [
        SessionSummary(
            session_id=row["session_id"],
            trace_count=int(row["trace_count"]),
            span_count=int(row["span_count"]),
            error_count=int(row["error_count"]),
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            tools=list(row["tools"] or [])[:20],
        )
        for row in rows
    ]


@router.get("/sessions/{session_id}", response_model=list[TraceInSession])
def get_session_traces(
    session_id: str = fastapi.Path(..., description="Session ID to fetch traces for."),
) -> list[TraceInSession]:
    """Return all traces within a session (the middle hierarchy level).

    Each trace_id within the session is returned with aggregated span_count,
    error_count, time range, and distinct tool names.

    404 when no spans exist for the session_id.
    """
    try:
        with _connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    trace_id,
                    count(*)                                                    AS span_count,
                    count(*) FILTER (WHERE status_code = 'ERROR')               AS error_count,
                    min(start_time)                                             AS started_at,
                    max(end_time)                                               AS ended_at,
                    (
                        SELECT array_agg(DISTINCT t)
                        FROM unnest(
                            array_agg(attributes->>'tool_name')
                            FILTER (WHERE attributes->>'tool_name' IS NOT NULL)
                        ) AS t
                    )                                                           AS tools
                FROM spans
                WHERE session_id = %s
                GROUP BY trace_id
                ORDER BY min(start_time) ASC
                """,
                (session_id,),
            ).fetchall()
    except Exception:
        logger.exception("read.get_session_traces failed session_id=%s", session_id)
        raise fastapi.HTTPException(status_code=500, detail="Database error") from None

    if not rows:
        raise fastapi.HTTPException(
            status_code=404,
            detail=f"Session {session_id!r} not found",
        )

    return [
        TraceInSession(
            trace_id=row["trace_id"],
            span_count=int(row["span_count"]),
            error_count=int(row["error_count"]),
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            tools=list(row["tools"] or [])[:20],
        )
        for row in rows
    ]


@router.get("/traces/{trace_id}/spans", response_model=list[RawSpan])
def get_trace_spans(
    trace_id: str = fastapi.Path(..., description="Hex trace ID."),
    full: bool = fastapi.Query(
        False,
        description="When true, include all attributes instead of the compact subset.",
    ),
) -> list[RawSpan]:
    """Return raw spans for a trace (the leaf level of the hierarchy).

    Ordered by start_time ASC so the UI can render the span tree in order.
    By default a compact attribute subset is returned (tool_name, session.id,
    span.type, kairos.span.kind); pass ``?full=true`` for all attributes.

    404 when no spans exist for the trace_id.
    """
    try:
        with _connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    span_id,
                    parent_span_id,
                    name,
                    attributes->>'tool_name'    AS tool_name,
                    status_code,
                    start_time,
                    end_time,
                    attributes
                FROM spans
                WHERE trace_id = %s
                ORDER BY start_time ASC NULLS LAST
                """,
                (trace_id,),
            ).fetchall()
    except Exception:
        logger.exception("read.get_trace_spans failed trace_id=%s", trace_id)
        raise fastapi.HTTPException(status_code=500, detail="Database error") from None

    if not rows:
        raise fastapi.HTTPException(
            status_code=404,
            detail=f"Trace {trace_id!r} not found",
        )

    def _attrs(row: dict[str, Any]) -> dict[str, Any]:
        raw: dict[str, Any] = row["attributes"] or {}
        if full:
            return raw
        return {k: v for k, v in raw.items() if k in _COMPACT_ATTR_KEYS}

    return [
        RawSpan(
            span_id=row["span_id"],
            parent_span_id=row["parent_span_id"],
            name=row["name"],
            tool_name=row["tool_name"],
            status_code=row["status_code"],
            start_time=row["start_time"],
            end_time=row["end_time"],
            attributes=_attrs(row),
        )
        for row in rows
    ]


@router.get("/search", response_model=SearchHits)
def search(
    q: str = fastapi.Query(..., min_length=1, description="Search query string."),
    types: str = fastapi.Query(
        "sessions,traces,spans",
        description="Comma-separated result types to include: sessions, traces, spans.",
    ),
    limit: int = fastapi.Query(
        20,
        ge=1,
        le=200,
        description="Max results per group (1–200, default 20).",
    ),
) -> SearchHits:
    """Unified search across sessions, traces, and spans.

    Searches four dimensions (all parameterized — no injection surface):

    1. **IDs**: session_id / trace_id / span_id prefix match.
    2. **Tool names**: attributes->>'tool_name' ILIKE query.
    3. **Content**: spans.attributes::text ILIKE query OR hook_events
       (tool_input_redacted::text / tool_output) ILIKE query → resolved back
       to session/trace.  v1 uses a sequential scan; a trigram GIN index on
       attributes::text (migration 0013) accelerates this when pg_trgm is
       installed.
    4. **Status**: if query matches 'error'/'ok'/'unset', filter by status_code.

    Returns grouped hits capped by ``limit`` per group.  Each hit includes
    enough context to navigate (ids + snippet + counts).

    Content-search note: at v1 scale this is a full scan on spans and
    hook_events.  Install pg_trgm + re-run migration 0013 to enable the
    GIN trigram index for ILIKE acceleration.
    """
    requested: set[str] = {t.strip().lower() for t in types.split(",")}
    like_q = f"%{q}%"

    # Determine if q looks like a status code filter.
    status_match: str | None = None
    q_lower = q.strip().lower()
    if q_lower in {"error", "ok", "unset"}:
        status_match = q_lower.upper()

    sessions_hits: list[dict[str, Any]] = []
    traces_hits: list[dict[str, Any]] = []
    spans_hits: list[dict[str, Any]] = []

    try:
        with _connect() as conn:
            # ── Sessions (group by session_id) ────────────────────────────────
            if "sessions" in requested:
                # Dimension 1: session_id ILIKE
                # Dimension 2: tool_name ILIKE
                # Dimension 4: status
                filter_parts: list[str] = [
                    "session_id ILIKE %s",
                ]
                filter_params: list[object] = [like_q]

                filter_parts.append("attributes->>'tool_name' ILIKE %s")
                filter_params.append(like_q)

                if status_match:
                    filter_parts.append("status_code = %s")
                    filter_params.append(status_match)

                # Dimension 3: content — attributes::text ILIKE
                filter_parts.append("attributes::text ILIKE %s")
                filter_params.append(like_q)

                session_where = (
                    "WHERE session_id IS NOT NULL AND ("
                    + " OR ".join(filter_parts)
                    + ")"
                )
                session_params: list[object] = list(filter_params) + [limit]

                session_sql = f"""
                    SELECT
                        session_id,
                        count(DISTINCT trace_id)                                AS trace_count,
                        count(*)                                                AS span_count,
                        min(start_time)                                         AS started_at
                    FROM spans
                    {session_where}
                    GROUP BY session_id
                    ORDER BY min(start_time) DESC
                    LIMIT %s
                """  # noqa: S608
                session_rows = conn.execute(session_sql, session_params).fetchall()
                sessions_hits = [
                    {
                        "session_id": r["session_id"],
                        "trace_count": int(r["trace_count"]),
                        "span_count": int(r["span_count"]),
                        "started_at": r["started_at"].isoformat() if r["started_at"] else None,
                    }
                    for r in session_rows
                ]

            # ── Traces ────────────────────────────────────────────────────────
            if "traces" in requested:
                trace_filter_parts: list[str] = [
                    "trace_id ILIKE %s",
                    "attributes->>'tool_name' ILIKE %s",
                    "attributes::text ILIKE %s",
                ]
                trace_filter_params: list[object] = [like_q, like_q, like_q]

                if status_match:
                    trace_filter_parts.append("status_code = %s")
                    trace_filter_params.append(status_match)

                trace_where = "WHERE (" + " OR ".join(trace_filter_parts) + ")"
                trace_params = list(trace_filter_params) + [limit]

                trace_sql = f"""
                    SELECT
                        trace_id,
                        session_id,
                        count(*)                                                AS span_count,
                        count(*) FILTER (WHERE status_code = 'ERROR')           AS error_count,
                        min(start_time)                                         AS started_at
                    FROM spans
                    {trace_where}
                    GROUP BY trace_id, session_id
                    ORDER BY min(start_time) DESC
                    LIMIT %s
                """  # noqa: S608
                trace_rows = conn.execute(trace_sql, trace_params).fetchall()
                traces_hits = [
                    {
                        "trace_id": r["trace_id"],
                        "session_id": r["session_id"],
                        "span_count": int(r["span_count"]),
                        "error_count": int(r["error_count"]),
                        "started_at": r["started_at"].isoformat() if r["started_at"] else None,
                    }
                    for r in trace_rows
                ]

                # Dimension 3 content from hook_events → resolve to trace.
                # hook_events has session_id but not trace_id directly; join via
                # spans.session_id to surface the trace hit.
                hook_filter_parts: list[str] = [
                    "he.tool_input_redacted::text ILIKE %s",
                    "he.tool_output ILIKE %s",
                ]
                hook_filter_params: list[object] = [like_q, like_q]

                hook_sql = f"""
                    SELECT DISTINCT s.trace_id, s.session_id
                    FROM hook_events he
                    JOIN spans s ON s.session_id = he.session_id
                    WHERE ({" OR ".join(hook_filter_parts)})
                    LIMIT %s
                """  # noqa: S608
                hook_params = hook_filter_params + [limit]
                hook_rows = conn.execute(hook_sql, hook_params).fetchall()

                existing_trace_ids = {h["trace_id"] for h in traces_hits}
                for hr in hook_rows:
                    if hr["trace_id"] not in existing_trace_ids:
                        traces_hits.append(
                            {
                                "trace_id": hr["trace_id"],
                                "session_id": hr["session_id"],
                                "span_count": None,
                                "error_count": None,
                                "started_at": None,
                                "snippet": f"content match in hook_events for session {hr['session_id']}",
                            }
                        )
                        existing_trace_ids.add(hr["trace_id"])

                traces_hits = traces_hits[:limit]

            # ── Spans ─────────────────────────────────────────────────────────
            if "spans" in requested:
                span_filter_parts: list[str] = [
                    "span_id ILIKE %s",
                    "attributes->>'tool_name' ILIKE %s",
                    "attributes::text ILIKE %s",
                ]
                span_filter_params: list[object] = [like_q, like_q, like_q]

                if status_match:
                    span_filter_parts.append("status_code = %s")
                    span_filter_params.append(status_match)

                span_where = "WHERE (" + " OR ".join(span_filter_parts) + ")"
                span_params = list(span_filter_params) + [limit]

                span_sql = f"""
                    SELECT
                        span_id,
                        trace_id,
                        session_id,
                        name,
                        attributes->>'tool_name'    AS tool_name,
                        status_code,
                        start_time
                    FROM spans
                    {span_where}
                    ORDER BY start_time DESC
                    LIMIT %s
                """  # noqa: S608
                span_rows = conn.execute(span_sql, span_params).fetchall()
                spans_hits = [
                    {
                        "span_id": r["span_id"],
                        "trace_id": r["trace_id"],
                        "session_id": r["session_id"],
                        "name": r["name"],
                        "tool_name": r["tool_name"],
                        "status_code": r["status_code"],
                        "started_at": r["start_time"].isoformat() if r["start_time"] else None,
                    }
                    for r in span_rows
                ]

    except Exception:
        logger.exception("read.search failed q=%r types=%s", q, types)
        raise fastapi.HTTPException(status_code=500, detail="Database error") from None

    return SearchHits(
        sessions=sessions_hits,
        traces=traces_hits,
        spans=spans_hits,
    )
