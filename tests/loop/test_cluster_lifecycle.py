"""P3.4 — Unit tests for cluster_lifecycle.py.

DB-gated: all tests are skipped unless KAIROS_PG_DSN is set.
Each test inserts rows under a ``test_*`` cluster_key and cleans up afterward.
"""

from __future__ import annotations

import datetime
import os
import uuid

import psycopg
import pytest
from psycopg.types.json import Jsonb

from kairos.loop.cluster_lifecycle import (
    get_cluster_status,
    list_clusters_by_status,
    regress_cluster,
    resolve_cluster,
    set_cluster_status,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("KAIROS_PG_DSN"),
    reason="KAIROS_PG_DSN not set",
)


# ─── Fixture ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def dsn() -> str:
    return os.environ["KAIROS_PG_DSN"]


@pytest.fixture()
def test_cluster(dsn: str):
    """Insert one discovery_queue row with a unique test_* cluster_key.

    Yields (cluster_key, trace_id). Deletes all rows with that cluster_key
    after the test.
    """
    cluster_key = f"test_{uuid.uuid4().hex[:16]}"
    trace_id = uuid.uuid4().hex + uuid.uuid4().hex[:0]
    row_id = uuid.uuid4().hex[:24]
    night = datetime.date(2026, 6, 17)
    features: dict[str, object] = {"tool_signature": "bash"}

    with psycopg.connect(dsn) as conn:
        conn.execute(
            "INSERT INTO discovery_queue "
            "(id, night_id, kind, trace_id, cluster_key, features) "
            "VALUES (%s, %s, 'anomaly', %s, %s, %s) "
            "ON CONFLICT DO NOTHING",
            (row_id, night, trace_id, cluster_key, Jsonb(features)),
        )
        conn.commit()

    yield cluster_key, trace_id

    with psycopg.connect(dsn) as conn:
        conn.execute(
            "DELETE FROM discovery_queue WHERE cluster_key LIKE 'test_%'",
        )
        conn.commit()


# ─── Tests ────────────────────────────────────────────────────────────────────


def test_resolve_cluster_sets_status(dsn: str, test_cluster: tuple[str, str]) -> None:
    cluster_key, _ = test_cluster

    # Default status is 'open'.
    assert get_cluster_status(cluster_key, dsn) == "open"

    resolve_cluster(cluster_key, dsn)
    assert get_cluster_status(cluster_key, dsn) == "resolved"


def test_regress_cluster_sets_status(dsn: str, test_cluster: tuple[str, str]) -> None:
    cluster_key, _ = test_cluster

    # open → resolved → regressed
    resolve_cluster(cluster_key, dsn)
    assert get_cluster_status(cluster_key, dsn) == "resolved"

    regress_cluster(cluster_key, dsn)
    assert get_cluster_status(cluster_key, dsn) == "regressed"


def test_set_cluster_status_invalid_raises(dsn: str, test_cluster: tuple[str, str]) -> None:
    cluster_key, _ = test_cluster

    with pytest.raises(ValueError, match="status must be one of"):
        set_cluster_status(cluster_key, "invalid_status", dsn)

    # Status must remain unchanged.
    assert get_cluster_status(cluster_key, dsn) == "open"


def test_list_clusters_by_status(dsn: str) -> None:
    """Insert rows with different statuses, then list by each status."""
    dsn_val = dsn
    night = datetime.date(2026, 6, 17)
    features: dict[str, object] = {"tool_signature": "bash"}

    keys: dict[str, str] = {
        "open": f"test_{uuid.uuid4().hex[:12]}_open",
        "resolved": f"test_{uuid.uuid4().hex[:12]}_resolved",
        "regressed": f"test_{uuid.uuid4().hex[:12]}_regressed",
    }

    try:
        with psycopg.connect(dsn_val) as conn:
            for _status, cluster_key in keys.items():
                row_id = uuid.uuid4().hex[:24]
                trace_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO discovery_queue "
                    "(id, night_id, kind, trace_id, cluster_key, features) "
                    "VALUES (%s, %s, 'anomaly', %s, %s, %s) "
                    "ON CONFLICT DO NOTHING",
                    (row_id, night, trace_id, cluster_key, Jsonb(features)),
                )
            conn.commit()

        # Set statuses (open is default; set the others explicitly).
        resolve_cluster(keys["resolved"], dsn_val)
        resolve_cluster(keys["regressed"], dsn_val)
        regress_cluster(keys["regressed"], dsn_val)

        # List all — all three should appear.
        all_clusters = list_clusters_by_status(None, dsn_val)
        all_keys = {c["cluster_key"] for c in all_clusters}
        for k in keys.values():
            assert k in all_keys

        # List by 'open' — only the open key.
        open_clusters = list_clusters_by_status("open", dsn_val)
        open_keys = {c["cluster_key"] for c in open_clusters}
        assert keys["open"] in open_keys
        assert keys["resolved"] not in open_keys
        assert keys["regressed"] not in open_keys

        # List by 'resolved'.
        resolved_clusters = list_clusters_by_status("resolved", dsn_val)
        resolved_keys = {c["cluster_key"] for c in resolved_clusters}
        assert keys["resolved"] in resolved_keys
        assert keys["open"] not in resolved_keys

        # List by 'regressed'.
        regressed_clusters = list_clusters_by_status("regressed", dsn_val)
        regressed_keys = {c["cluster_key"] for c in regressed_clusters}
        assert keys["regressed"] in regressed_keys

        # Each result dict has the expected shape.
        sample = next(c for c in all_clusters if c["cluster_key"] == keys["open"])
        assert "trace_count" in sample
        assert sample["trace_count"] >= 1

    finally:
        with psycopg.connect(dsn_val) as conn:
            for cluster_key in keys.values():
                conn.execute(
                    "DELETE FROM discovery_queue WHERE cluster_key = %s",
                    (cluster_key,),
                )
            conn.commit()
