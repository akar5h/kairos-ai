"""Tests for P4.2 cluster insight endpoints.

GET  /v1/clusters/{key}/insights
POST /v1/clusters/{key}/insights/{id}/approve

Unit tests mock the DB layer. DB-gated integration tests seed real rows.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from fastapi.testclient import TestClient

from kairos.api.app import create_app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app())


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _insight_row(
    *,
    insight_id: str | None = None,
    cluster_key: str = "Bash|Read::token_z",
    approved_at: datetime | None = None,
) -> dict[str, Any]:
    return {
        "id": insight_id or str(uuid.uuid4()),
        "cluster_key": cluster_key,
        "pattern_name": "stale_date_rebooking",
        "description": "Agent calls with past date",
        "discriminator_hint": "check departure_date < today",
        "root_cause": "No date validation",
        "confidence": 0.85,
        "is_coherent": True,
        "auto_approve": True,
        "approved_at": approved_at,
        "approved_by": None,
        "model_used": "anthropic/claude-sonnet-4.5",
        "created_at": _utcnow(),
    }


# ─── GET /v1/clusters/{key}/insights ─────────────────────────────────────────


class TestGetClusterInsights:
    def test_empty_returns_list(self, client: TestClient) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: s
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = []

        with (
            patch("kairos.api.read._dsn", return_value="postgresql://x"),
            patch("psycopg.connect", return_value=mock_conn),
        ):
            resp = client.get("/v1/clusters/Bash%7CRead%3A%3Atoken_z/insights")

        assert resp.status_code == 200
        assert resp.json() == []

    def test_returns_insight_rows(self, client: TestClient) -> None:
        row = _insight_row(cluster_key="Bash|Read::token_z")
        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: s
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = [row]

        with (
            patch("kairos.api.read._dsn", return_value="postgresql://x"),
            patch("psycopg.connect", return_value=mock_conn),
        ):
            resp = client.get("/v1/clusters/Bash%7CRead%3A%3Atoken_z/insights")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["pattern_name"] == "stale_date_rebooking"
        assert data[0]["confidence"] == 0.85


# ─── POST /v1/clusters/{key}/insights/{id}/approve ───────────────────────────


class TestApproveInsight:
    def _mock_conn_not_found(self) -> MagicMock:
        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: s
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchone.return_value = None
        return mock_conn

    def test_unknown_insight_returns_404(self, client: TestClient) -> None:
        mock_conn = self._mock_conn_not_found()
        with (
            patch("kairos.api.read._dsn", return_value="postgresql://x"),
            patch("psycopg.connect", return_value=mock_conn),
        ):
            resp = client.post("/v1/clusters/Bash%7CRead%3A%3Atoken_z/insights/nonexistent-id/approve")
        assert resp.status_code == 404

    def test_approve_valid_insight(self, client: TestClient) -> None:
        iid = str(uuid.uuid4())
        row = _insight_row(insight_id=iid, approved_at=None)

        call_count = 0

        class FakeConn:
            def __enter__(self) -> FakeConn:
                return self

            def __exit__(self, *a: Any) -> bool:
                return False

            def execute(self, sql: str, params: Any = None) -> FakeConn:
                nonlocal call_count
                call_count += 1
                self._last_sql = sql
                return self

            def fetchone(self) -> dict[str, Any] | None:
                # First call: fetch the insight; subsequent calls for update
                if call_count == 1:
                    return row
                return None

            def commit(self) -> None:
                pass

        with (
            patch("kairos.api.read._dsn", return_value="postgresql://x"),
            patch("psycopg.connect", return_value=FakeConn()),
            patch(
                "kairos.api.read.generate_eval_set",
                side_effect=ValueError("No traces"),
            ),
        ):
            resp = client.post(f"/v1/clusters/Bash%7CRead%3A%3Atoken_z/insights/{iid}/approve")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "approved"

    def test_already_approved_returns_already_approved(self, client: TestClient) -> None:
        iid = str(uuid.uuid4())
        row = _insight_row(insight_id=iid, approved_at=_utcnow())

        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: s
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchone.return_value = row

        with (
            patch("kairos.api.read._dsn", return_value="postgresql://x"),
            patch("psycopg.connect", return_value=mock_conn),
        ):
            resp = client.post(f"/v1/clusters/Bash%7CRead%3A%3Atoken_z/insights/{iid}/approve")

        assert resp.status_code == 200
        assert resp.json()["status"] == "already_approved"

    def test_eval_set_valueerror_still_returns_approved(self, client: TestClient) -> None:
        """generate_eval_set ValueError (no traces) → still 200, status=approved."""
        iid = str(uuid.uuid4())
        row = _insight_row(insight_id=iid, approved_at=None)

        call_count = 0

        class FakeConn2:
            def __enter__(self) -> FakeConn2:
                return self

            def __exit__(self, *a: Any) -> bool:
                return False

            def execute(self, sql: str, params: Any = None) -> FakeConn2:
                nonlocal call_count
                call_count += 1
                return self

            def fetchone(self) -> dict[str, Any] | None:
                if call_count == 1:
                    return row
                return None

            def commit(self) -> None:
                pass

        with (
            patch("kairos.api.read._dsn", return_value="postgresql://x"),
            patch("psycopg.connect", return_value=FakeConn2()),
            patch(
                "kairos.api.read.generate_eval_set",
                side_effect=ValueError("cluster has no traces"),
            ),
        ):
            resp = client.post(f"/v1/clusters/some_key/insights/{iid}/approve")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "approved"
        assert "eval set not generated" in data["message"]
        assert data["eval_set_id"] is None


# ─── DB-gated integration tests ──────────────────────────────────────────────


@pytest.mark.integration
class TestClusterInsightsIntegration:
    """Requires KAIROS_PG_DSN. Seeds real rows."""

    @pytest.fixture(autouse=True)
    def require_dsn(self) -> None:
        if not os.environ.get("KAIROS_PG_DSN"):
            pytest.skip("KAIROS_PG_DSN not set")

    @pytest.fixture()
    def dsn(self) -> str:
        return os.environ["KAIROS_PG_DSN"]

    @pytest.fixture()
    def api_client(self) -> TestClient:
        return TestClient(create_app())

    def _seed_insight(self, dsn: str, cluster_key: str, approved_at: datetime | None = None) -> str:
        iid = str(uuid.uuid4())
        with psycopg.connect(dsn) as conn:
            conn.execute(
                """
                INSERT INTO cluster_insights
                  (id, cluster_key, pattern_name, description, discriminator_hint,
                   root_cause, confidence, is_coherent, auto_approve, approved_at,
                   approved_by, model_used)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    iid,
                    cluster_key,
                    "test_pattern",
                    "test description",
                    "check something",
                    "root cause",
                    0.9,
                    True,
                    True,
                    approved_at,
                    None,
                    "test-model",
                ),
            )
            conn.commit()
        return iid

    def test_get_insights_returns_seeded_row(self, api_client: TestClient, dsn: str) -> None:
        cluster_key = "TestBash::token_z_" + uuid.uuid4().hex[:8]
        iid = self._seed_insight(dsn, cluster_key)

        resp = api_client.get(f"/v1/clusters/{cluster_key}/insights")
        assert resp.status_code == 200
        ids = [r["id"] for r in resp.json()]
        assert iid in ids

    def test_approve_sets_approved_at(self, api_client: TestClient, dsn: str) -> None:
        cluster_key = "TestBash::approve_" + uuid.uuid4().hex[:8]

        # seed a discovery_queue row so generate_eval_set doesn't fail with 0 traces
        dq_id = uuid.uuid4().hex[:24]
        trace_id = uuid.uuid4().hex
        with psycopg.connect(dsn) as conn:
            conn.execute(
                "INSERT INTO discovery_queue (id, night_id, kind, trace_id, cluster_key, features) "
                "VALUES (%s, current_date, 'anomaly', %s, %s, %s::jsonb) ON CONFLICT DO NOTHING",
                (dq_id, trace_id, cluster_key, json.dumps({"dominant_feature": "token_z", "token_z": 5.0})),
            )
            conn.commit()

        iid = self._seed_insight(dsn, cluster_key)

        resp = api_client.post(f"/v1/clusters/{cluster_key}/insights/{iid}/approve")
        assert resp.status_code == 200
        assert resp.json()["status"] == "approved"

        # Verify approved_at was set.
        with psycopg.connect(dsn, row_factory=psycopg.rows.dict_row) as conn:
            row = conn.execute(
                "SELECT approved_at, approved_by FROM cluster_insights WHERE id = %s",
                (iid,),
            ).fetchone()
        assert row is not None
        assert row["approved_at"] is not None
        assert row["approved_by"] == "owner"

    def test_approve_idempotent(self, api_client: TestClient, dsn: str) -> None:
        cluster_key = "TestBash::idem_" + uuid.uuid4().hex[:8]
        iid = self._seed_insight(dsn, cluster_key, approved_at=_utcnow())

        resp = api_client.post(f"/v1/clusters/{cluster_key}/insights/{iid}/approve")
        assert resp.status_code == 200
        assert resp.json()["status"] == "already_approved"
