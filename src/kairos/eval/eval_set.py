"""P3.2 — Cluster → eval-set generation.

For each cluster_key in discovery_queue, generate a frozen eval set:
  held_in:  traces belonging to this cluster (the failure pattern to fix)
  held_out: labeled-pass corpus traces + other-cluster traces (blast-radius guard)

Discriminator is derived from the cluster's dominant_feature:
  latency_z     → type="latency_z_threshold",  config={"threshold_z": min_cluster_latency_z}
  restart_count → type="restart_count_gt",      config={"threshold": min_cluster_restart_count}
  rare_ngram    → type="rare_ngram_present",    config={"ngrams": union_of_cluster_ngrams}
  struggle      → type="struggle_gt",           config={"threshold": min_cluster_struggle}
  token_z       → type="token_z_threshold",     config={"threshold_z": min_cluster_token_z}
  (anything else) → type="outcome_only", config={}
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from kairos.eval.corpus import CorpusEntry


# ── Data structures ───────────────────────────────────────────────────────────


@dataclass
class EvalSetRecord:
    """One row in eval_sets."""

    eval_set_id: str
    cluster_key: str
    detector_version: str
    frozen_at: datetime
    held_in: list[dict[str, Any]]
    held_out: list[dict[str, Any]]
    discriminator_type: str
    discriminator_config: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable dict; frozen_at as ISO string."""
        return {
            "eval_set_id": self.eval_set_id,
            "cluster_key": self.cluster_key,
            "detector_version": self.detector_version,
            "frozen_at": self.frozen_at.isoformat(),
            "held_in": self.held_in,
            "held_out": self.held_out,
            "discriminator_type": self.discriminator_type,
            "discriminator_config": self.discriminator_config,
        }


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_eval_set_id(cluster_key: str, detector_version: str, frozen_at: datetime) -> str:
    """SHA-256 of cluster_key|detector_version|frozen_at_iso, hex[:32]."""
    payload = f"{cluster_key}|{detector_version}|{frozen_at.isoformat()}"
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def _dominant_feature_from_cluster_key(cluster_key: str) -> str:
    """Return suffix after the last '::'; if no '::' return cluster_key as-is."""
    if "::" in cluster_key:
        return cluster_key.rsplit("::", 1)[-1]
    return cluster_key


def _discriminator_from_features(
    dominant_feature: str, all_features: list[dict[str, Any]]
) -> tuple[str, dict[str, Any]]:
    """Derive (discriminator_type, config) from the cluster's feature dicts."""
    if dominant_feature == "latency_z":
        threshold = min(float(f["latency_z"]) for f in all_features)
        return "latency_z_threshold", {"threshold_z": threshold}
    if dominant_feature == "restart_count":
        threshold = min(int(f["restart_count"]) for f in all_features)
        return "restart_count_gt", {"threshold": threshold}
    if dominant_feature in {"rare_ngram", "rare_ngrams"}:
        ngrams: set[str] = set()
        for f in all_features:
            ngrams.update(f.get("rare_ngrams") or [])
        return "rare_ngram_present", {"ngrams": sorted(ngrams)}
    if dominant_feature == "struggle":
        threshold = min(float(f["struggle"]) for f in all_features)
        return "struggle_gt", {"threshold": threshold}
    if dominant_feature == "token_z":
        threshold = min(float(f["token_z"]) for f in all_features)
        return "token_z_threshold", {"threshold_z": threshold}
    return "outcome_only", {}


# ── Core API ──────────────────────────────────────────────────────────────────


def generate_eval_set(
    cluster_key: str,
    dsn: str,
    *,
    detector_version: str = "HEAD",
    held_out_limit: int = 30,
    corpus_pass_entries: list[CorpusEntry] | None = None,
) -> EvalSetRecord:
    """Generate a frozen eval set for `cluster_key`.

    1. Fetch held_in traces from discovery_queue WHERE cluster_key = ?
    2. Compute discriminator from held_in features
    3. Build held_out: labeled-pass corpus entries + other-cluster traces
    4. Return EvalSetRecord (not stored — call store_eval_set to persist)

    Raises ValueError if no traces found for the given cluster_key.
    """
    import psycopg

    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT trace_id, features FROM discovery_queue WHERE cluster_key = %s",
            (cluster_key,),
        ).fetchall()

    if not rows:
        raise ValueError(f"No traces found for cluster_key={cluster_key!r}")

    held_in: list[dict[str, Any]] = []
    all_features: list[dict[str, Any]] = []
    for trace_id, features in rows:
        feat = features if isinstance(features, dict) else json.loads(features)
        held_in.append({"trace_id": str(trace_id), "features": feat})
        all_features.append(feat)

    held_in_ids = {entry["trace_id"] for entry in held_in}

    dominant_feature = _dominant_feature_from_cluster_key(cluster_key)
    disc_type, disc_config = _discriminator_from_features(dominant_feature, all_features)

    # Build held_out
    held_out: list[dict[str, Any]] = []

    # a. labeled-pass corpus entries
    if corpus_pass_entries:
        for entry in corpus_pass_entries:
            if entry.trace_id not in held_in_ids:
                held_out.append(
                    {
                        "trace_id": entry.trace_id,
                        "outcome_truth": "pass",
                        "source": "labeled",
                    }
                )

    # b. supplement with other-cluster traces up to held_out_limit
    remaining = held_out_limit - len(held_out)
    if remaining > 0:
        with psycopg.connect(dsn) as conn:
            other_rows = conn.execute(
                "SELECT trace_id FROM discovery_queue WHERE cluster_key != %s",
                (cluster_key,),
            ).fetchall()
        seen_in_held_out = {e["trace_id"] for e in held_out}
        for (other_trace_id,) in other_rows:
            tid = str(other_trace_id)
            if tid not in held_in_ids and tid not in seen_in_held_out:
                held_out.append(
                    {
                        "trace_id": tid,
                        "outcome_truth": "unknown",
                        "source": "other_cluster",
                    }
                )
                seen_in_held_out.add(tid)
                remaining -= 1
                if remaining <= 0:
                    break

    # c. limit total held_out
    held_out = held_out[:held_out_limit]

    frozen_at = datetime.now(UTC)
    eval_set_id = _make_eval_set_id(cluster_key, detector_version, frozen_at)

    return EvalSetRecord(
        eval_set_id=eval_set_id,
        cluster_key=cluster_key,
        detector_version=detector_version,
        frozen_at=frozen_at,
        held_in=held_in,
        held_out=held_out,
        discriminator_type=disc_type,
        discriminator_config=disc_config,
    )


def store_eval_set(record: EvalSetRecord, dsn: str) -> str:
    """Insert record into eval_sets. ON CONFLICT DO NOTHING. Returns eval_set_id."""
    import psycopg

    with psycopg.connect(dsn) as conn:
        conn.execute(
            """
            INSERT INTO eval_sets
              (eval_set_id, cluster_key, detector_version, frozen_at,
               held_in, held_out, discriminator_type, discriminator_config)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s::jsonb)
            ON CONFLICT (eval_set_id) DO NOTHING
            """,
            (
                record.eval_set_id,
                record.cluster_key,
                record.detector_version,
                record.frozen_at,
                json.dumps(record.held_in),
                json.dumps(record.held_out),
                record.discriminator_type,
                json.dumps(record.discriminator_config),
            ),
        )
        conn.commit()

    return record.eval_set_id


def load_eval_set(eval_set_id: str, dsn: str) -> EvalSetRecord | None:
    """Load one eval_sets row by primary key. Returns None if not found."""
    import psycopg

    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            """
            SELECT eval_set_id, cluster_key, detector_version, frozen_at,
                   held_in::text, held_out::text, discriminator_type, discriminator_config::text
            FROM eval_sets
            WHERE eval_set_id = %s
            """,
            (eval_set_id,),
        ).fetchone()

    if row is None:
        return None

    return EvalSetRecord(
        eval_set_id=str(row[0]),
        cluster_key=str(row[1]),
        detector_version=str(row[2]),
        frozen_at=cast("datetime", row[3]),
        held_in=json.loads(str(row[4])),
        held_out=json.loads(str(row[5])),
        discriminator_type=str(row[6]),
        discriminator_config=json.loads(str(row[7])),
    )


def list_eval_sets(dsn: str) -> list[EvalSetRecord]:
    """Load all eval_sets rows ordered by frozen_at DESC."""
    import psycopg

    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            """
            SELECT eval_set_id, cluster_key, detector_version, frozen_at,
                   held_in::text, held_out::text, discriminator_type, discriminator_config::text
            FROM eval_sets
            ORDER BY frozen_at DESC
            """,
        ).fetchall()

    return [
        EvalSetRecord(
            eval_set_id=str(row[0]),
            cluster_key=str(row[1]),
            detector_version=str(row[2]),
            frozen_at=cast("datetime", row[3]),
            held_in=json.loads(str(row[4])),
            held_out=json.loads(str(row[5])),
            discriminator_type=str(row[6]),
            discriminator_config=json.loads(str(row[7])),
        )
        for row in rows
    ]


def generate_all_eval_sets(
    dsn: str,
    *,
    detector_version: str = "HEAD",
    held_out_limit: int = 30,
) -> list[str]:
    """Generate + store eval sets for all distinct cluster_keys in discovery_queue.

    Returns list of eval_set_ids (one per cluster_key).
    """
    import psycopg

    with psycopg.connect(dsn) as conn:
        rows = conn.execute("SELECT DISTINCT cluster_key FROM discovery_queue").fetchall()

    cluster_keys = [str(row[0]) for row in rows]
    eval_set_ids: list[str] = []

    for ck in cluster_keys:
        record = generate_eval_set(
            ck,
            dsn,
            detector_version=detector_version,
            held_out_limit=held_out_limit,
        )
        eid = store_eval_set(record, dsn)
        eval_set_ids.append(eid)

    return eval_set_ids
