"""P3.4 — Cluster lifecycle management.

Status transitions:
  open → resolved: cluster passes N consecutive eval checks (N=3 default)
  resolved → regressed: cluster reappears (new discovery_queue rows after resolved)
  any → open: manual re-open or new detection

All writes are idempotent. Status is per cluster_key (not per row).
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row

_VALID_STATUSES: frozenset[str] = frozenset({"open", "resolved", "regressed"})


def get_cluster_status(cluster_key: str, dsn: str) -> str | None:
    """Return the current status for *cluster_key*, or None if not found."""
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT status FROM discovery_queue WHERE cluster_key = %s LIMIT 1",
            (cluster_key,),
        ).fetchone()
    return row["status"] if row is not None else None


def set_cluster_status(cluster_key: str, status: str, dsn: str) -> None:
    """Set *status* on all rows for *cluster_key*.

    Raises ``ValueError`` when *status* is not in ``{'open', 'resolved', 'regressed'}``.
    No-op (no rows updated) when *cluster_key* does not exist.
    """
    if status not in _VALID_STATUSES:
        msg = f"status must be one of {sorted(_VALID_STATUSES)!r}, got {status!r}"
        raise ValueError(msg)

    with psycopg.connect(dsn) as conn:
        conn.execute(
            "UPDATE discovery_queue SET status = %s, status_updated_at = now() WHERE cluster_key = %s",
            (status, cluster_key),
        )
        conn.commit()


def resolve_cluster(cluster_key: str, dsn: str) -> None:
    """Transition *cluster_key* to ``resolved`` if currently ``open`` or ``regressed``."""
    current = get_cluster_status(cluster_key, dsn)
    if current in {"open", "regressed"}:
        set_cluster_status(cluster_key, "resolved", dsn)


def regress_cluster(cluster_key: str, dsn: str) -> None:
    """Transition *cluster_key* to ``regressed`` if currently ``resolved``."""
    current = get_cluster_status(cluster_key, dsn)
    if current == "resolved":
        set_cluster_status(cluster_key, "regressed", dsn)


def list_clusters_by_status(
    status: str | None,
    dsn: str,
) -> list[dict[str, Any]]:
    """Return cluster aggregates, optionally filtered by *status*.

    If *status* is ``None``, all clusters are returned.

    Each dict contains:
        cluster_key: str
        status: str
        trace_count: int
        status_updated_at: str | None  (ISO timestamp)
    """
    sql = """
        SELECT
            cluster_key,
            status,
            COUNT(*) AS trace_count,
            MAX(status_updated_at) AS status_updated_at
        FROM discovery_queue
        WHERE ($1::text IS NULL OR status = $1)
        GROUP BY cluster_key, status
        ORDER BY trace_count DESC
    """
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        rows = conn.execute(sql, (status,)).fetchall()

    return [
        {
            "cluster_key": row["cluster_key"],
            "status": row["status"],
            "trace_count": int(row["trace_count"]),
            "status_updated_at": (
                row["status_updated_at"].isoformat() if row["status_updated_at"] is not None else None
            ),
        }
        for row in rows
    ]
