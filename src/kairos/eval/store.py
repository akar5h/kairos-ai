"""Eval runs store — eval_runs table in kairos-pg.

Schema (see migration 0009_eval_runs.sql):
  eval_runs(
    run_id text PRIMARY KEY,  — deterministic: sha256(ref+corpus_hash+config_hash+ts_iso)
    ref text,                 — git ref evaluated
    ref_full text,            — resolved full SHA
    corpus_hash text,         — stable ruler hash
    config_hash text,         — hash of context.yaml (None if not used)
    k int,                    — number of runs
    panel jsonb,              — MetricPanel serialized as JSON
    verdict text,             — "PASS" | "REGRESSED" | "NONDETERMINISM_ERROR" | "run"
    ts timestamptz            — evaluation timestamp
  )

Security contract:
  - DSN read exclusively from KAIROS_PG_DSN env var (enforced by db.py).
  - panel jsonb holds only aggregated metrics — no raw tool outputs, no PII.
  - run_id is a content-addressed hash; collisions are idempotent (ON CONFLICT DO NOTHING).
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from kairos.loop.db import get_connection

if TYPE_CHECKING:
    from kairos.eval.panel import MetricPanel

# ── Data structures ───────────────────────────────────────────────────────────


@dataclass
class EvalRunRecord:
    """One row in eval_runs."""

    run_id: str
    ref: str
    ref_full: str
    corpus_hash: str
    config_hash: str | None
    k: int
    panel: dict[str, Any]  # MetricPanel.to_dict()
    verdict: str
    ts: datetime


# ── run_id ───────────────────────────────────────────────────────────────────


def _make_run_id(ref_full: str, corpus_hash: str, config_hash: str | None, ts: datetime) -> str:
    """Deterministic run_id: SHA-256 of (ref_full, corpus_hash, config_hash, ts_iso)."""
    payload = f"{ref_full}|{corpus_hash}|{config_hash or ''}|{ts.isoformat()}"
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


# ── Store API ─────────────────────────────────────────────────────────────────


def store_run(
    ref: str,
    ref_full: str,
    corpus_hash: str,
    k: int,
    panel: MetricPanel,
    verdict: str,
    config_hash: str | None = None,
    ts: datetime | None = None,
) -> str:
    """Insert an eval run row into kairos-pg. Returns the run_id.

    Idempotent: ON CONFLICT (run_id) DO NOTHING.
    DSN via KAIROS_PG_DSN env var.

    Raises RuntimeError if KAIROS_PG_DSN is not set (fail loud).
    """
    ts = ts or datetime.now(UTC)
    run_id = _make_run_id(ref_full, corpus_hash, config_hash, ts)
    panel_json = json.dumps(panel.to_dict())

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO eval_runs
              (run_id, ref, ref_full, corpus_hash, config_hash, k, panel, verdict, ts)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
            ON CONFLICT (run_id) DO NOTHING
            """,
            (
                run_id,
                ref,
                ref_full,
                corpus_hash,
                config_hash,
                k,
                panel_json,
                verdict,
                ts,
            ),
        )
        conn.commit()

    return run_id


def load_run(run_id: str) -> EvalRunRecord | None:
    """Load one eval_runs row by run_id. Returns None if not found."""
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT run_id, ref, ref_full, corpus_hash, config_hash, k,
                   panel::text, verdict, ts
            FROM eval_runs
            WHERE run_id = %s
            """,
            (run_id,),
        ).fetchone()

    if row is None:
        return None

    return EvalRunRecord(
        run_id=str(row[0]),
        ref=str(row[1]),
        ref_full=str(row[2]),
        corpus_hash=str(row[3]),
        config_hash=str(row[4]) if row[4] is not None else None,
        k=int(str(row[5])),
        panel=json.loads(str(row[6])),
        verdict=str(row[7]),
        ts=cast("datetime", row[8]),
    )


def load_recent_runs(limit: int = 50) -> list[EvalRunRecord]:
    """Load recent eval_runs rows ordered by ts descending."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT run_id, ref, ref_full, corpus_hash, config_hash, k,
                   panel::text, verdict, ts
            FROM eval_runs
            ORDER BY ts DESC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()

    return [
        EvalRunRecord(
            run_id=str(row[0]),
            ref=str(row[1]),
            ref_full=str(row[2]),
            corpus_hash=str(row[3]),
            config_hash=str(row[4]) if row[4] is not None else None,
            k=int(str(row[5])),
            panel=json.loads(str(row[6])),
            verdict=str(row[7]),
            ts=cast("datetime", row[8]),
        )
        for row in rows
    ]


def is_db_available() -> bool:
    """Return True if KAIROS_PG_DSN is set and the DB is reachable."""
    dsn = os.environ.get("KAIROS_PG_DSN", "").strip()
    if not dsn:
        return False
    try:
        with get_connection() as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:  # noqa: BLE001
        return False
