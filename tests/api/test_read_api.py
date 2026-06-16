"""Tests for F2.1 Read API — GET /v1/traces, /v1/clusters, /v1/findings, /v1/labels.

Coverage
--------
Unit tests (mock DB layer)
  - GET /v1/traces            happy path, empty, since filter, bad limit
  - GET /v1/traces/{id}       happy path, 404 on missing, DB error → 500
  - GET /v1/clusters          happy path, empty
  - GET /v1/clusters/{k}/traces happy path, empty cluster
  - GET /v1/findings          happy path, empty, no-filter → 400, both filters
  - GET /v1/labels            happy path, empty

Security tests
  - SQL-injection-attempt on trace_id, night_id, cluster_key: params are bound,
    no DB error should leak (assert parameterized via captured calls).

DB-gated integration tests (KAIROS_PG_DSN required; @pytest.mark.integration)
  - Seed spans → GET /traces returns them
  - GET /traces/{id} round-trip, 404 for unknown
  - Seed discovery_queue → GET /clusters, GET /clusters/{k}/traces
  - Seed findings → GET /findings
  - Seed labels → GET /labels
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from fastapi.testclient import TestClient

from kairos.api.app import create_app
from kairos.models.enums import TerminalStatus
from kairos.models.trace import TraceEnvelope

# ─── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app())


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _fake_trace_rows() -> list[dict[str, Any]]:
    return [
        {
            "trace_id": "aaaa" * 8,
            "started_at": _utcnow(),
            "span_count": 5,
            "error_count": 1,
        },
        {
            "trace_id": "bbbb" * 8,
            "started_at": _utcnow(),
            "span_count": 3,
            "error_count": 0,
        },
    ]


def _fake_envelope(trace_id: str = "aaaa" * 8) -> TraceEnvelope:
    return TraceEnvelope(
        trace_id=trace_id,
        source="otlp",
        terminal_status=TerminalStatus.COMPLETED,
    )


# ─── GET /v1/traces ───────────────────────────────────────────────────────────


class TestGetTraces:
    def test_happy_path_returns_list(self, client: TestClient) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: s
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = _fake_trace_rows()

        with patch("kairos.api.read._connect", return_value=mock_conn):
            resp = client.get("/v1/traces")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["trace_id"] == "aaaa" * 8
        assert data[0]["span_count"] == 5
        assert data[0]["error_count"] == 1

    def test_empty_returns_200_empty_list(self, client: TestClient) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: s
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = []

        with patch("kairos.api.read._connect", return_value=mock_conn):
            resp = client.get("/v1/traces")

        assert resp.status_code == 200
        assert resp.json() == []

    def test_since_param_passed_to_query(self, client: TestClient) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: s
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = []

        with patch("kairos.api.read._connect", return_value=mock_conn):
            resp = client.get("/v1/traces?since=2026-06-01T00:00:00%2B00:00")

        assert resp.status_code == 200
        # Verify the since param was passed to conn.execute as a bound param.
        call_args = mock_conn.execute.call_args
        sql, params = call_args[0]
        assert "2026-06-01T00:00:00+00:00" in params
        # Param is bound — not interpolated into the SQL string.
        assert "2026-06-01" not in sql

    def test_limit_param_respected(self, client: TestClient) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: s
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = []

        with patch("kairos.api.read._connect", return_value=mock_conn):
            resp = client.get("/v1/traces?limit=5")

        assert resp.status_code == 200
        call_args = mock_conn.execute.call_args
        _, params = call_args[0]
        assert 5 in params

    def test_limit_too_large_rejected(self, client: TestClient) -> None:
        resp = client.get("/v1/traces?limit=9999")
        assert resp.status_code == 422

    def test_limit_zero_rejected(self, client: TestClient) -> None:
        resp = client.get("/v1/traces?limit=0")
        assert resp.status_code == 422

    def test_db_error_returns_500(self, client: TestClient) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: s
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.side_effect = Exception("connection refused")

        with patch("kairos.api.read._connect", return_value=mock_conn):
            resp = client.get("/v1/traces")

        assert resp.status_code == 500
        # DSN / exception text must NOT leak.
        assert "connection refused" not in resp.text
        assert "postgresql" not in resp.text.lower()


# ─── GET /v1/traces/{trace_id} ────────────────────────────────────────────────


class TestGetTrace:
    def _mock_count_conn(self, n: int) -> MagicMock:
        """Return a mock connection that returns n for the span count query."""
        conn = MagicMock()
        conn.__enter__ = lambda s: s
        conn.__exit__ = MagicMock(return_value=False)
        conn.execute.return_value.fetchone.return_value = {"n": n}
        return conn

    def test_happy_path_returns_envelope(self, client: TestClient) -> None:
        trace_id = "aaaa" * 8
        envelope = _fake_envelope(trace_id)

        with (
            patch("kairos.api.read._dsn", return_value="postgresql://test/test"),
            patch("psycopg.connect", return_value=self._mock_count_conn(3)),
            patch(
                "kairos.api.read.fetch_envelope_from_db",
                return_value=envelope,
            ),
        ):
            resp = client.get(f"/v1/traces/{trace_id}")

        assert resp.status_code == 200
        data = resp.json()
        assert data["trace_id"] == trace_id

    def test_missing_trace_returns_404(self, client: TestClient) -> None:
        with (
            patch("kairos.api.read._dsn", return_value="postgresql://test/test"),
            patch("psycopg.connect", return_value=self._mock_count_conn(0)),
        ):
            resp = client.get("/v1/traces/nonexistent_trace_id")

        assert resp.status_code == 404

    def test_dsn_error_returns_500(self, client: TestClient) -> None:
        with patch("kairos.api.read._dsn", side_effect=RuntimeError("KAIROS_PG_DSN not set")):
            resp = client.get("/v1/traces/sometrace")

        assert resp.status_code == 500
        assert "KAIROS_PG_DSN" not in resp.text

    def test_db_error_on_count_returns_500(self, client: TestClient) -> None:
        bad_conn = MagicMock()
        bad_conn.__enter__ = lambda s: s
        bad_conn.__exit__ = MagicMock(return_value=False)
        bad_conn.execute.side_effect = Exception("timeout")

        with (
            patch("kairos.api.read._dsn", return_value="postgresql://test/test"),
            patch("psycopg.connect", return_value=bad_conn),
        ):
            resp = client.get("/v1/traces/sometrace")

        assert resp.status_code == 500
        assert "timeout" not in resp.text

    def test_enrich_hooks_param_forwarded(self, client: TestClient) -> None:
        trace_id = "cccc" * 8
        envelope = _fake_envelope(trace_id)
        captured: list[dict[str, Any]] = []

        def fake_fetch(tid: str, dsn: str, **kwargs: Any) -> TraceEnvelope:
            captured.append({"trace_id": tid, "kwargs": kwargs})
            return envelope

        with (
            patch("kairos.api.read._dsn", return_value="postgresql://test/test"),
            patch("psycopg.connect", return_value=self._mock_count_conn(2)),
            patch("kairos.api.read.fetch_envelope_from_db", side_effect=fake_fetch),
        ):
            resp = client.get(f"/v1/traces/{trace_id}?enrich_hooks=true")

        assert resp.status_code == 200
        assert captured[0]["kwargs"]["enrich_hooks"] is True

    def test_enrich_hooks_default_is_true(self, client: TestClient) -> None:
        """No query param → hook-truth by default (enrich_hooks=True forwarded)."""
        trace_id = "dddd" * 8
        envelope = _fake_envelope(trace_id)
        captured: list[dict[str, Any]] = []

        def fake_fetch(tid: str, dsn: str, **kwargs: Any) -> TraceEnvelope:
            captured.append({"trace_id": tid, "kwargs": kwargs})
            return envelope

        with (
            patch("kairos.api.read._dsn", return_value="postgresql://test/test"),
            patch("psycopg.connect", return_value=self._mock_count_conn(2)),
            patch("kairos.api.read.fetch_envelope_from_db", side_effect=fake_fetch),
        ):
            resp = client.get(f"/v1/traces/{trace_id}")

        assert resp.status_code == 200
        assert captured[0]["kwargs"]["enrich_hooks"] is True

    def test_enrich_hooks_false_forwarded_for_raw(self, client: TestClient) -> None:
        """UI raw toggle: ?enrich_hooks=false forwards False (raw OTel)."""
        trace_id = "eeee" * 8
        envelope = _fake_envelope(trace_id)
        captured: list[dict[str, Any]] = []

        def fake_fetch(tid: str, dsn: str, **kwargs: Any) -> TraceEnvelope:
            captured.append({"trace_id": tid, "kwargs": kwargs})
            return envelope

        with (
            patch("kairos.api.read._dsn", return_value="postgresql://test/test"),
            patch("psycopg.connect", return_value=self._mock_count_conn(2)),
            patch("kairos.api.read.fetch_envelope_from_db", side_effect=fake_fetch),
        ):
            resp = client.get(f"/v1/traces/{trace_id}?enrich_hooks=false")

        assert resp.status_code == 200
        assert captured[0]["kwargs"]["enrich_hooks"] is False


# ─── GET /v1/clusters ─────────────────────────────────────────────────────────


class TestGetClusters:
    def _fake_cluster_rows(self) -> list[dict[str, Any]]:
        return [
            {
                "cluster_key": "bash|read_file::token_z",
                "trace_count": 12,
                "min_night_id": "2026-06-10",
                "kinds": ["anomaly"],
                "sample_features": {"token_z": 4.2, "tool_signature": "bash|read_file"},
            },
            {
                "cluster_key": "expectation_miss::order::submit_order",
                "trace_count": 3,
                "min_night_id": "2026-06-11",
                "kinds": ["expectation_miss"],
                "sample_features": {"missing_tool": "submit_order"},
            },
        ]

    def test_happy_path(self, client: TestClient) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: s
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = self._fake_cluster_rows()

        with patch("kairos.api.read._connect", return_value=mock_conn):
            resp = client.get("/v1/clusters")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["cluster_key"] == "bash|read_file::token_z"
        assert data[0]["trace_count"] == 12
        assert data[0]["kinds"] == ["anomaly"]
        assert isinstance(data[0]["sample_features"], dict)

    def test_empty_returns_200(self, client: TestClient) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: s
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = []

        with patch("kairos.api.read._connect", return_value=mock_conn):
            resp = client.get("/v1/clusters")

        assert resp.status_code == 200
        assert resp.json() == []

    def test_db_error_returns_500(self, client: TestClient) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: s
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.side_effect = Exception("pg down")

        with patch("kairos.api.read._connect", return_value=mock_conn):
            resp = client.get("/v1/clusters")

        assert resp.status_code == 500
        assert "pg down" not in resp.text


# ─── GET /v1/clusters/{cluster_key}/traces ────────────────────────────────────


class TestGetClusterTraces:
    def test_happy_path(self, client: TestClient) -> None:
        rows = [
            {"trace_id": "aaaa" * 8, "labeled": False},
            {"trace_id": "bbbb" * 8, "labeled": True},
        ]
        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: s
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = rows

        with patch("kairos.api.read._connect", return_value=mock_conn):
            resp = client.get("/v1/clusters/bash%7Cread_file%3A%3Atoken_z/traces")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[1]["labeled"] is True

    def test_empty_cluster_returns_200(self, client: TestClient) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: s
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = []

        with patch("kairos.api.read._connect", return_value=mock_conn):
            resp = client.get("/v1/clusters/nonexistent/traces")

        assert resp.status_code == 200
        assert resp.json() == []

    def test_cluster_key_is_parameterized(self, client: TestClient) -> None:
        """SQL injection attempt: cluster_key is a bound parameter, not interpolated."""
        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: s
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = []

        injected_key = "'; DROP TABLE discovery_queue; --"

        with patch("kairos.api.read._connect", return_value=mock_conn):
            resp = client.get(f"/v1/clusters/{injected_key}/traces")

        # No 500 — the parameter was safely bound, not interpolated.
        assert resp.status_code == 200
        # Confirm the injected string was passed as a param, not in the SQL.
        call_args = mock_conn.execute.call_args
        sql, params = call_args[0]
        assert injected_key in params
        assert "DROP TABLE" not in sql


# ─── GET /v1/findings ─────────────────────────────────────────────────────────


class TestGetFindings:
    def _fake_finding_rows(self) -> list[dict[str, Any]]:
        return [
            {
                "night_id": "2026-06-10",
                "trace_id": "aaaa" * 8,
                "unit_id": "unit-1",
                "workflow": "order",
                "agent": "order_agent",
                "detector": "coordination_waste",
                "severity": "medium",
                "evidence_steps": [2, 4],
                "tokens": 3000,
                "struggle": 0.5,
                "outcome": "pass",
                "config_hash": "abc123",
                "ingested_at": _utcnow(),
            }
        ]

    def test_happy_path_trace_id_filter(self, client: TestClient) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: s
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = self._fake_finding_rows()

        with patch("kairos.api.read._connect", return_value=mock_conn):
            resp = client.get(f"/v1/findings?trace_id={'aaaa' * 8}")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["detector"] == "coordination_waste"
        assert data[0]["evidence_steps"] == [2, 4]

    def test_happy_path_night_id_filter(self, client: TestClient) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: s
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = self._fake_finding_rows()

        with patch("kairos.api.read._connect", return_value=mock_conn):
            resp = client.get("/v1/findings?night_id=2026-06-10")

        assert resp.status_code == 200

    def test_both_filters_applied(self, client: TestClient) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: s
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = self._fake_finding_rows()

        with patch("kairos.api.read._connect", return_value=mock_conn):
            resp = client.get(f"/v1/findings?trace_id={'aaaa' * 8}&night_id=2026-06-10")

        assert resp.status_code == 200
        # Both params must be bound.
        call_args = mock_conn.execute.call_args
        _, params = call_args[0]
        assert len(params) == 2

    def test_no_filter_returns_400(self, client: TestClient) -> None:
        resp = client.get("/v1/findings")
        assert resp.status_code == 400
        assert "trace_id" in resp.json()["detail"].lower() or "night_id" in resp.json()["detail"].lower()

    def test_empty_returns_200(self, client: TestClient) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: s
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = []

        with patch("kairos.api.read._connect", return_value=mock_conn):
            resp = client.get("/v1/findings?trace_id=doesnotexist")

        assert resp.status_code == 200
        assert resp.json() == []

    def test_trace_id_is_parameterized(self, client: TestClient) -> None:
        """SQL injection attempt on trace_id — must be a bound param."""
        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: s
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = []

        injected = "' OR '1'='1"

        with patch("kairos.api.read._connect", return_value=mock_conn):
            resp = client.get(f"/v1/findings?trace_id={injected}")

        assert resp.status_code == 200
        call_args = mock_conn.execute.call_args
        sql, params = call_args[0]
        assert injected in params
        assert "OR '1'='1" not in sql

    def test_db_error_returns_500(self, client: TestClient) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: s
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.side_effect = Exception("query failed")

        with patch("kairos.api.read._connect", return_value=mock_conn):
            resp = client.get("/v1/findings?trace_id=any")

        assert resp.status_code == 500
        assert "query failed" not in resp.text


# ─── GET /v1/labels ───────────────────────────────────────────────────────────


class TestGetLabels:
    def _fake_label_rows(self) -> list[dict[str, Any]]:
        return [
            {
                "id": "label-1",
                "trace_id": "aaaa" * 8,
                "question": "Was the tool call helpful?",
                "answer": "Yes",
                "verdict": "tp",
                "label_class": "coordination_waste",
                "ts": _utcnow(),
            }
        ]

    def test_happy_path(self, client: TestClient) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: s
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = self._fake_label_rows()

        with patch("kairos.api.read._connect", return_value=mock_conn):
            resp = client.get(f"/v1/labels?trace_id={'aaaa' * 8}")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["verdict"] == "tp"
        assert data[0]["label_class"] == "coordination_waste"

    def test_empty_returns_200(self, client: TestClient) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: s
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = []

        with patch("kairos.api.read._connect", return_value=mock_conn):
            resp = client.get("/v1/labels?trace_id=nonexistent")

        assert resp.status_code == 200
        assert resp.json() == []

    def test_missing_trace_id_param_rejected(self, client: TestClient) -> None:
        resp = client.get("/v1/labels")
        assert resp.status_code == 422

    def test_trace_id_parameterized(self, client: TestClient) -> None:
        """SQL injection attempt on labels trace_id."""
        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: s
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = []

        injected = "' UNION SELECT 1,2,3,4,5,6,7--"

        with patch("kairos.api.read._connect", return_value=mock_conn):
            resp = client.get(f"/v1/labels?trace_id={injected}")

        assert resp.status_code == 200
        call_args = mock_conn.execute.call_args
        sql, params = call_args[0]
        assert injected in params
        assert "UNION SELECT" not in sql

    def test_db_error_returns_500(self, client: TestClient) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: s
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.side_effect = Exception("labels table missing")

        with patch("kairos.api.read._connect", return_value=mock_conn):
            resp = client.get("/v1/labels?trace_id=any")

        assert resp.status_code == 500
        assert "labels table missing" not in resp.text


# ─── POST /v1/labels ──────────────────────────────────────────────────────────


class TestPostLabels:
    def _returning_conn(self, returned: dict[str, Any]) -> MagicMock:
        """Mock connection whose INSERT ... RETURNING yields ``returned``."""
        conn = MagicMock()
        conn.__enter__ = lambda s: s
        conn.__exit__ = MagicMock(return_value=False)
        conn.execute.return_value.fetchone.return_value = returned
        return conn

    def test_happy_path_returns_201_and_row(self, client: TestClient) -> None:
        returned = {
            "id": "deadbeef",
            "trace_id": "aaaa" * 8,
            "question": "Was it helpful?",
            "answer": "Yes",
            "verdict": "tp",
            "label_class": "coordination_waste",
            "ts": _utcnow(),
        }
        conn = self._returning_conn(returned)

        with patch("kairos.api.read._connect", return_value=conn):
            resp = client.post(
                "/v1/labels",
                json={
                    "trace_id": "aaaa" * 8,
                    "answer": "Yes",
                    "question": "Was it helpful?",
                    "verdict": "tp",
                    "label_class": "coordination_waste",
                },
            )

        assert resp.status_code == 201
        data = resp.json()
        assert data["id"] == "deadbeef"
        assert data["verdict"] == "tp"
        assert data["trace_id"] == "aaaa" * 8

    def test_minimal_body_only_required_fields(self, client: TestClient) -> None:
        """Only trace_id + answer required; optional fields default to null."""
        returned = {
            "id": "feed",
            "trace_id": "bbbb" * 8,
            "question": None,
            "answer": "looks fine",
            "verdict": None,
            "label_class": None,
            "ts": _utcnow(),
        }
        conn = self._returning_conn(returned)

        with patch("kairos.api.read._connect", return_value=conn):
            resp = client.post(
                "/v1/labels",
                json={"trace_id": "bbbb" * 8, "answer": "looks fine"},
            )

        assert resp.status_code == 201
        data = resp.json()
        assert data["question"] is None
        assert data["verdict"] is None
        assert data["label_class"] is None

    def test_missing_trace_id_rejected_422(self, client: TestClient) -> None:
        resp = client.post("/v1/labels", json={"answer": "x"})
        assert resp.status_code == 422

    def test_missing_answer_rejected_422(self, client: TestClient) -> None:
        resp = client.post("/v1/labels", json={"trace_id": "aaaa" * 8})
        assert resp.status_code == 422

    def test_bad_verdict_rejected_422(self, client: TestClient) -> None:
        resp = client.post(
            "/v1/labels",
            json={"trace_id": "aaaa" * 8, "answer": "y", "verdict": "maybe"},
        )
        assert resp.status_code == 422

    def test_null_verdict_allowed(self, client: TestClient) -> None:
        returned = {
            "id": "n1",
            "trace_id": "cccc" * 8,
            "question": None,
            "answer": "y",
            "verdict": None,
            "label_class": None,
            "ts": _utcnow(),
        }
        conn = self._returning_conn(returned)
        with patch("kairos.api.read._connect", return_value=conn):
            resp = client.post(
                "/v1/labels",
                json={"trace_id": "cccc" * 8, "answer": "y", "verdict": None},
            )
        assert resp.status_code == 201

    def test_insert_is_parameterized(self, client: TestClient) -> None:
        """SQL injection attempt on trace_id — must be bound, not interpolated."""
        injected = "'; DROP TABLE labels; --"
        returned = {
            "id": "p1",
            "trace_id": injected,
            "question": None,
            "answer": "y",
            "verdict": None,
            "label_class": None,
            "ts": _utcnow(),
        }
        conn = self._returning_conn(returned)

        with patch("kairos.api.read._connect", return_value=conn):
            resp = client.post(
                "/v1/labels",
                json={"trace_id": injected, "answer": "y"},
            )

        assert resp.status_code == 201
        sql, params = conn.execute.call_args[0]
        assert "INSERT INTO labels" in sql
        assert "DROP TABLE" not in sql
        assert injected in params

    def test_db_error_returns_500(self, client: TestClient) -> None:
        conn = MagicMock()
        conn.__enter__ = lambda s: s
        conn.__exit__ = MagicMock(return_value=False)
        conn.execute.side_effect = Exception("insert failed")

        with patch("kairos.api.read._connect", return_value=conn):
            resp = client.post(
                "/v1/labels",
                json={"trace_id": "aaaa" * 8, "answer": "y"},
            )

        assert resp.status_code == 500
        assert "insert failed" not in resp.text


# ─── DB-gated integration tests ───────────────────────────────────────────────


@pytest.mark.integration
class TestReadApiIntegration:
    """Requires KAIROS_PG_DSN. Seeds real rows and reads them back via the API."""

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

    def _unique_trace_id(self) -> str:
        return uuid.uuid4().hex + uuid.uuid4().hex[:0]  # 32 hex chars

    def test_traces_list_returns_seeded_span(
        self, api_client: TestClient, dsn: str
    ) -> None:
        trace_id = self._unique_trace_id()
        span_id = uuid.uuid4().hex[:16]

        with psycopg.connect(dsn) as conn:
            conn.execute(
                "INSERT INTO spans (trace_id, span_id, name, start_time, status_code) "
                "VALUES (%s, %s, %s, now(), 'OK') "
                "ON CONFLICT DO NOTHING",
                (trace_id, span_id, "test.span.f2.1"),
            )
            conn.commit()

        resp = api_client.get("/v1/traces?limit=500")
        assert resp.status_code == 200
        ids = [r["trace_id"] for r in resp.json()]
        assert trace_id in ids

        # Cleanup.
        with psycopg.connect(dsn) as conn:
            conn.execute("DELETE FROM spans WHERE trace_id = %s", (trace_id,))
            conn.commit()

    def test_get_trace_404_for_unknown(self, api_client: TestClient) -> None:
        resp = api_client.get("/v1/traces/00000000000000000000000000000000")
        assert resp.status_code == 404

    def test_findings_seeded_and_retrieved(
        self, api_client: TestClient, dsn: str
    ) -> None:
        import datetime

        trace_id = self._unique_trace_id()
        night = datetime.date(2026, 6, 15)

        with psycopg.connect(dsn) as conn:
            conn.execute(
                "INSERT INTO findings "
                "(night_id, trace_id, unit_id, workflow, agent, detector, "
                " severity, evidence_steps, tokens, struggle, outcome, config_hash) "
                "VALUES (%s, %s, 'u1', 'wf', 'ag', 'det', 'low', %s, 100, 0.1, 'pass', 'cfg') "
                "ON CONFLICT DO NOTHING",
                (night, trace_id, [1, 2]),
            )
            conn.commit()

        resp = api_client.get(f"/v1/findings?trace_id={trace_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["detector"] == "det"
        assert data[0]["evidence_steps"] == [1, 2]

        # Cleanup.
        with psycopg.connect(dsn) as conn:
            conn.execute(
                "DELETE FROM findings WHERE trace_id = %s", (trace_id,)
            )
            conn.commit()

    def test_labels_seeded_and_retrieved(
        self, api_client: TestClient, dsn: str
    ) -> None:
        trace_id = self._unique_trace_id()
        label_id = uuid.uuid4().hex

        with psycopg.connect(dsn) as conn:
            conn.execute(
                "INSERT INTO labels (id, trace_id, question, answer, verdict, label_class) "
                "VALUES (%s, %s, 'Q?', 'A', 'tp', 'cls') "
                "ON CONFLICT DO NOTHING",
                (label_id, trace_id),
            )
            conn.commit()

        resp = api_client.get(f"/v1/labels?trace_id={trace_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["verdict"] == "tp"
        assert data[0]["id"] == label_id

        # Cleanup.
        with psycopg.connect(dsn) as conn:
            conn.execute("DELETE FROM labels WHERE id = %s", (label_id,))
            conn.commit()

    def test_post_label_then_get_round_trip(
        self, api_client: TestClient, dsn: str
    ) -> None:
        """POST /v1/labels appends a row that GET /v1/labels reads back."""
        trace_id = self._unique_trace_id()

        resp = api_client.post(
            "/v1/labels",
            json={
                "trace_id": trace_id,
                "answer": "appended via POST",
                "question": "is it good?",
                "verdict": "fp",
                "label_class": "loop_waste",
            },
        )
        assert resp.status_code == 201
        created = resp.json()
        new_id = created["id"]
        assert created["verdict"] == "fp"
        assert created["trace_id"] == trace_id

        try:
            got = api_client.get(f"/v1/labels?trace_id={trace_id}")
            assert got.status_code == 200
            rows = got.json()
            assert any(r["id"] == new_id and r["verdict"] == "fp" for r in rows)
        finally:
            with psycopg.connect(dsn) as conn:
                conn.execute("DELETE FROM labels WHERE id = %s", (new_id,))
                conn.commit()

    def test_post_label_minimal_nullable_fields(
        self, api_client: TestClient, dsn: str
    ) -> None:
        """Migration 0014: optional columns accept NULL on insert."""
        trace_id = self._unique_trace_id()

        resp = api_client.post(
            "/v1/labels",
            json={"trace_id": trace_id, "answer": "minimal"},
        )
        assert resp.status_code == 201
        created = resp.json()
        new_id = created["id"]
        assert created["question"] is None
        assert created["verdict"] is None
        assert created["label_class"] is None

        with psycopg.connect(dsn) as conn:
            conn.execute("DELETE FROM labels WHERE id = %s", (new_id,))
            conn.commit()

    def test_clusters_seeded_and_retrieved(
        self, api_client: TestClient, dsn: str
    ) -> None:
        import datetime

        from psycopg.types.json import Jsonb

        night = datetime.date(2026, 6, 15)
        trace_id = self._unique_trace_id()
        cid = uuid.uuid4().hex[:24]
        cluster_key = f"test_cluster_{cid}"
        features: dict[str, object] = {"token_z": 4.5, "tool_signature": "bash"}

        with psycopg.connect(dsn) as conn:
            conn.execute(
                "INSERT INTO discovery_queue "
                "(id, night_id, kind, trace_id, cluster_key, features) "
                "VALUES (%s, %s, 'anomaly', %s, %s, %s) "
                "ON CONFLICT DO NOTHING",
                (cid, night, trace_id, cluster_key, Jsonb(features)),
            )
            conn.commit()

        # GET /clusters should include our cluster.
        resp = api_client.get("/v1/clusters")
        assert resp.status_code == 200
        cluster_keys = [c["cluster_key"] for c in resp.json()]
        assert cluster_key in cluster_keys

        # GET /clusters/{key}/traces should return our trace.
        resp2 = api_client.get(f"/v1/clusters/{cluster_key}/traces")
        assert resp2.status_code == 200
        members = resp2.json()
        assert any(m["trace_id"] == trace_id for m in members)

        # Cleanup.
        with psycopg.connect(dsn) as conn:
            conn.execute("DELETE FROM discovery_queue WHERE id = %s", (cid,))
            conn.commit()
