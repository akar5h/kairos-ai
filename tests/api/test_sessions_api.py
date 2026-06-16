"""Tests for R1 Session→Trace→Span hierarchy + Search API.

Coverage
--------
Unit tests (mock DB layer)
  - GET /v1/sessions          happy path, empty, q filter, since filter,
                              null session_id excluded, DB error → 500
  - GET /v1/sessions/{id}     happy path, 404 on missing, DB error → 500
  - GET /v1/traces/{id}/spans happy path (compact + full), 404, DB error → 500
  - GET /v1/search            id / tool / content / status dimensions each hit,
                              types param restricts groups, DB error → 500

Security tests
  - SQL injection on q, session_id path, trace_id path: params bound, not
    interpolated into the SQL string.

DB-gated integration tests (KAIROS_PG_DSN required; @pytest.mark.integration)
  - Migration 0013: session_id backfilled from attributes; indexes present.
  - Ingest: span with session.id attr → session_id column populated.
  - GET /v1/sessions groups seeded spans correctly.
  - GET /v1/sessions/{id} returns its traces, 404 for unknown.
  - GET /v1/traces/{id}/spans returns raw spans, 404 for unknown trace.
  - GET /v1/search hits each of the 4 dimensions and resolves back.
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
from psycopg.types.json import Jsonb

from kairos.api.app import create_app

# ─── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app())


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _mock_conn(fetchall: list[dict[str, Any]] | None = None) -> MagicMock:
    """Build a mock psycopg connection that returns ``fetchall`` rows."""
    conn = MagicMock()
    conn.__enter__ = lambda s: s
    conn.__exit__ = MagicMock(return_value=False)
    if fetchall is not None:
        conn.execute.return_value.fetchall.return_value = fetchall
    return conn


def _fake_session_rows() -> list[dict[str, Any]]:
    return [
        {
            "session_id": "demo-sess-1",
            "trace_count": 3,
            "span_count": 12,
            "error_count": 1,
            "started_at": _utcnow(),
            "ended_at": _utcnow(),
            "tools": ["bash", "read_file"],
        },
        {
            "session_id": "demo-sess-2",
            "trace_count": 1,
            "span_count": 5,
            "error_count": 0,
            "started_at": _utcnow(),
            "ended_at": _utcnow(),
            "tools": None,
        },
    ]


def _fake_trace_in_session_rows() -> list[dict[str, Any]]:
    return [
        {
            "trace_id": "a" * 32,
            "span_count": 7,
            "error_count": 0,
            "started_at": _utcnow(),
            "ended_at": _utcnow(),
            "tools": ["bash"],
        }
    ]


def _fake_raw_span_rows() -> list[dict[str, Any]]:
    return [
        {
            "span_id": "span001",
            "parent_span_id": None,
            "name": "tool_call",
            "tool_name": "bash",
            "status_code": "OK",
            "start_time": _utcnow(),
            "end_time": _utcnow(),
            "attributes": {
                "tool_name": "bash",
                "session.id": "demo-sess-1",
                "span.type": "interaction",
                "kairos.span.kind": "task",
                "extra_attr": "hidden_by_default",
            },
        }
    ]


# ─── GET /v1/sessions ─────────────────────────────────────────────────────────


class TestGetSessions:
    def test_happy_path_returns_list(self, client: TestClient) -> None:
        with patch("kairos.api.read._connect", return_value=_mock_conn(_fake_session_rows())):
            resp = client.get("/v1/sessions")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["session_id"] == "demo-sess-1"
        assert data[0]["trace_count"] == 3
        assert data[0]["span_count"] == 12
        assert data[0]["error_count"] == 1
        assert "bash" in data[0]["tools"]
        # NULL tools row should return empty list, not None.
        assert data[1]["tools"] == []

    def test_empty_returns_200_empty_list(self, client: TestClient) -> None:
        with patch("kairos.api.read._connect", return_value=_mock_conn([])):
            resp = client.get("/v1/sessions")

        assert resp.status_code == 200
        assert resp.json() == []

    def test_q_param_passed_as_bound_param(self, client: TestClient) -> None:
        mock_conn = _mock_conn([])
        with patch("kairos.api.read._connect", return_value=mock_conn):
            resp = client.get("/v1/sessions?q=demo")

        assert resp.status_code == 200
        call_args = mock_conn.execute.call_args
        sql, params = call_args[0]
        # q must appear as bound param (%demo%), not interpolated in SQL.
        assert "%demo%" in params
        assert "demo" not in sql

    def test_since_param_passed_as_bound_param(self, client: TestClient) -> None:
        mock_conn = _mock_conn([])
        with patch("kairos.api.read._connect", return_value=mock_conn):
            resp = client.get("/v1/sessions?since=2026-06-01T00:00:00%2B00:00")

        assert resp.status_code == 200
        call_args = mock_conn.execute.call_args
        sql, params = call_args[0]
        assert "2026-06-01T00:00:00+00:00" in params
        assert "2026-06-01" not in sql

    def test_limit_respected(self, client: TestClient) -> None:
        mock_conn = _mock_conn([])
        with patch("kairos.api.read._connect", return_value=mock_conn):
            resp = client.get("/v1/sessions?limit=5")

        assert resp.status_code == 200
        _, params = mock_conn.execute.call_args[0]
        assert 5 in params

    def test_limit_too_large_rejected(self, client: TestClient) -> None:
        resp = client.get("/v1/sessions?limit=9999")
        assert resp.status_code == 422

    def test_db_error_returns_500(self, client: TestClient) -> None:
        mock_conn = _mock_conn()
        mock_conn.execute.side_effect = Exception("connection refused")
        with patch("kairos.api.read._connect", return_value=mock_conn):
            resp = client.get("/v1/sessions")

        assert resp.status_code == 500
        assert "connection refused" not in resp.text

    def test_q_sql_injection_is_parameterized(self, client: TestClient) -> None:
        injected = "'; DROP TABLE spans; --"
        mock_conn = _mock_conn([])
        with patch("kairos.api.read._connect", return_value=mock_conn):
            resp = client.get(f"/v1/sessions?q={injected}")

        assert resp.status_code == 200
        _, params = mock_conn.execute.call_args[0]
        assert f"%{injected}%" in params
        assert "DROP TABLE" not in mock_conn.execute.call_args[0][0]


# ─── GET /v1/sessions/{session_id} ───────────────────────────────────────────


class TestGetSessionTraces:
    def test_happy_path_returns_traces(self, client: TestClient) -> None:
        with patch(
            "kairos.api.read._connect",
            return_value=_mock_conn(_fake_trace_in_session_rows()),
        ):
            resp = client.get("/v1/sessions/demo-sess-1")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["trace_id"] == "a" * 32
        assert data[0]["span_count"] == 7
        assert data[0]["tools"] == ["bash"]

    def test_missing_session_returns_404(self, client: TestClient) -> None:
        with patch("kairos.api.read._connect", return_value=_mock_conn([])):
            resp = client.get("/v1/sessions/nonexistent-session")

        assert resp.status_code == 404

    def test_db_error_returns_500(self, client: TestClient) -> None:
        mock_conn = _mock_conn()
        mock_conn.execute.side_effect = Exception("db down")
        with patch("kairos.api.read._connect", return_value=mock_conn):
            resp = client.get("/v1/sessions/any-session")

        assert resp.status_code == 500
        assert "db down" not in resp.text

    def test_session_id_is_parameterized(self, client: TestClient) -> None:
        injected = "'; DROP TABLE spans; --"
        mock_conn = _mock_conn([])
        with patch("kairos.api.read._connect", return_value=mock_conn):
            resp = client.get(f"/v1/sessions/{injected}")

        # 404 because empty rows — but param was bound, not injected.
        assert resp.status_code == 404
        _, params = mock_conn.execute.call_args[0]
        assert injected in params
        assert "DROP TABLE" not in mock_conn.execute.call_args[0][0]


# ─── GET /v1/traces/{trace_id}/spans ─────────────────────────────────────────


class TestGetTraceSpans:
    def test_happy_path_compact_attrs(self, client: TestClient) -> None:
        with patch(
            "kairos.api.read._connect",
            return_value=_mock_conn(_fake_raw_span_rows()),
        ):
            resp = client.get(f"/v1/traces/{'a' * 32}/spans")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        span = data[0]
        assert span["span_id"] == "span001"
        assert span["tool_name"] == "bash"
        assert span["status_code"] == "OK"
        # Compact: only defined keys included; extra_attr should be absent.
        assert "extra_attr" not in span["attributes"]
        assert "tool_name" in span["attributes"]
        assert "session.id" in span["attributes"]

    def test_full_flag_returns_all_attrs(self, client: TestClient) -> None:
        with patch(
            "kairos.api.read._connect",
            return_value=_mock_conn(_fake_raw_span_rows()),
        ):
            resp = client.get(f"/v1/traces/{'a' * 32}/spans?full=true")

        assert resp.status_code == 200
        data = resp.json()
        assert "extra_attr" in data[0]["attributes"]

    def test_missing_trace_returns_404(self, client: TestClient) -> None:
        with patch("kairos.api.read._connect", return_value=_mock_conn([])):
            resp = client.get(f"/v1/traces/{'0' * 32}/spans")

        assert resp.status_code == 404

    def test_db_error_returns_500(self, client: TestClient) -> None:
        mock_conn = _mock_conn()
        mock_conn.execute.side_effect = Exception("timeout")
        with patch("kairos.api.read._connect", return_value=mock_conn):
            resp = client.get(f"/v1/traces/{'a' * 32}/spans")

        assert resp.status_code == 500
        assert "timeout" not in resp.text

    def test_trace_id_parameterized(self, client: TestClient) -> None:
        injected = "'; DROP TABLE spans; --"
        mock_conn = _mock_conn([])
        with patch("kairos.api.read._connect", return_value=mock_conn):
            resp = client.get(f"/v1/traces/{injected}/spans")

        assert resp.status_code == 404
        _, params = mock_conn.execute.call_args[0]
        assert injected in params
        assert "DROP TABLE" not in mock_conn.execute.call_args[0][0]


# ─── GET /v1/search ───────────────────────────────────────────────────────────


class TestSearch:
    """Unit tests for the search endpoint.

    Because /search makes multiple conn.execute calls (sessions, traces, hook
    join, spans), we use a single mock that returns an empty list for all
    fetchall calls.  Dimension-specific tests verify bound params.
    """

    def _multi_execute_conn(self, return_sequences: list[list[dict[str, Any]]] | None = None) -> MagicMock:
        """Mock conn with execute().fetchall() returning from a sequence."""
        conn = MagicMock()
        conn.__enter__ = lambda s: s
        conn.__exit__ = MagicMock(return_value=False)
        if return_sequences is None:
            conn.execute.return_value.fetchall.return_value = []
        else:
            conn.execute.return_value.fetchall.side_effect = return_sequences
        return conn

    def test_missing_q_rejected(self, client: TestClient) -> None:
        resp = client.get("/v1/search")
        assert resp.status_code == 422

    def test_empty_q_rejected(self, client: TestClient) -> None:
        resp = client.get("/v1/search?q=")
        assert resp.status_code == 422

    def test_happy_path_returns_groups(self, client: TestClient) -> None:
        with patch(
            "kairos.api.read._connect",
            return_value=self._multi_execute_conn(),
        ):
            resp = client.get("/v1/search?q=demo")

        assert resp.status_code == 200
        data = resp.json()
        assert "sessions" in data
        assert "traces" in data
        assert "spans" in data
        # All empty — but structure is correct.
        assert isinstance(data["sessions"], list)

    def test_types_param_restricts_sessions_only(self, client: TestClient) -> None:
        """types=sessions means only the sessions group should be queried."""
        mock_conn = self._multi_execute_conn()
        with patch("kairos.api.read._connect", return_value=mock_conn):
            resp = client.get("/v1/search?q=demo&types=sessions")

        assert resp.status_code == 200
        data = resp.json()
        # sessions queried; traces/spans not in requested types → empty.
        assert data["traces"] == []
        assert data["spans"] == []
        # Only one execute call (sessions query).
        assert mock_conn.execute.call_count == 1

    def test_types_param_restricts_spans_only(self, client: TestClient) -> None:
        mock_conn = self._multi_execute_conn()
        with patch("kairos.api.read._connect", return_value=mock_conn):
            resp = client.get("/v1/search?q=demo&types=spans")

        assert resp.status_code == 200
        data = resp.json()
        assert data["sessions"] == []
        assert data["traces"] == []
        # One execute call (spans query).
        assert mock_conn.execute.call_count == 1

    def test_dimension_id_param_is_bound(self, client: TestClient) -> None:
        """session_id ILIKE param must be bound, not interpolated."""
        mock_conn = self._multi_execute_conn()
        with patch("kairos.api.read._connect", return_value=mock_conn):
            resp = client.get("/v1/search?q=sess-xyz&types=sessions")

        assert resp.status_code == 200
        _, params = mock_conn.execute.call_args[0]
        assert "%sess-xyz%" in params

    def test_dimension_tool_param_is_bound(self, client: TestClient) -> None:
        """tool_name ILIKE param must be bound."""
        mock_conn = self._multi_execute_conn()
        with patch("kairos.api.read._connect", return_value=mock_conn):
            resp = client.get("/v1/search?q=bash&types=spans")

        assert resp.status_code == 200
        _, params = mock_conn.execute.call_args[0]
        assert "%bash%" in params

    def test_dimension_status_match_error(self, client: TestClient) -> None:
        """Searching 'error' should include a status_code = 'ERROR' filter."""
        mock_conn = self._multi_execute_conn()
        with patch("kairos.api.read._connect", return_value=mock_conn):
            resp = client.get("/v1/search?q=error&types=spans")

        assert resp.status_code == 200
        _, params = mock_conn.execute.call_args[0]
        assert "ERROR" in params

    def test_sql_injection_on_q_is_parameterized(self, client: TestClient) -> None:
        injected = "'; DROP TABLE spans; --"
        mock_conn = self._multi_execute_conn()
        with patch("kairos.api.read._connect", return_value=mock_conn):
            resp = client.get(f"/v1/search?q={injected}&types=spans")

        assert resp.status_code == 200
        for c in mock_conn.execute.call_args_list:
            sql, _params = c[0]
            assert "DROP TABLE" not in sql

    def test_db_error_returns_500(self, client: TestClient) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: s
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.side_effect = Exception("pg crashed")
        with patch("kairos.api.read._connect", return_value=mock_conn):
            resp = client.get("/v1/search?q=demo")

        assert resp.status_code == 500
        assert "pg crashed" not in resp.text

    def test_limit_param_respected(self, client: TestClient) -> None:
        mock_conn = self._multi_execute_conn()
        with patch("kairos.api.read._connect", return_value=mock_conn):
            resp = client.get("/v1/search?q=demo&limit=5")

        assert resp.status_code == 200
        # Limit=5 must appear as a bound param in at least one call.
        all_params = [c[0][1] for c in mock_conn.execute.call_args_list]
        assert any(5 in p for p in all_params)

    def test_limit_too_large_rejected(self, client: TestClient) -> None:
        resp = client.get("/v1/search?q=demo&limit=9999")
        assert resp.status_code == 422


# ─── DB-gated integration tests ───────────────────────────────────────────────


@pytest.mark.integration
class TestSessionsApiIntegration:
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

    def _unique_id(self, n: int = 32) -> str:
        return uuid.uuid4().hex[:n]

    def _insert_span(
        self,
        conn: psycopg.Connection[Any],
        trace_id: str,
        span_id: str,
        session_id: str | None = None,
        tool_name: str | None = None,
        status_code: str = "OK",
    ) -> None:
        attrs: dict[str, Any] = {}
        if session_id:
            attrs["session.id"] = session_id
        if tool_name:
            attrs["tool_name"] = tool_name
        conn.execute(
            """
            INSERT INTO spans
                (trace_id, span_id, name, start_time, end_time,
                 status_code, attributes, session_id)
            VALUES (%s, %s, 'test.span', now(), now(),
                    %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (trace_id, span_id, status_code, Jsonb(attrs), session_id),
        )

    # ── Migration / column test ────────────────────────────────────────────────

    def test_session_id_column_exists(self, dsn: str) -> None:
        with psycopg.connect(dsn) as conn:
            row = conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='spans' AND column_name='session_id'"
            ).fetchone()
        assert row is not None, "session_id column missing from spans table"

    def test_session_id_backfill_from_attributes(self, dsn: str) -> None:
        """A span with session.id in attributes should have session_id set."""
        with psycopg.connect(dsn) as conn:
            row = conn.execute(
                "SELECT session_id FROM spans WHERE attributes->>'session.id' IS NOT NULL LIMIT 1"
            ).fetchone()
        assert row is not None, "No spans with session.id attribute found"
        assert row[0] is not None, "session_id not backfilled from attributes"

    def test_trgm_index_created(self, dsn: str) -> None:
        with psycopg.connect(dsn) as conn:
            row = conn.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename='spans' AND indexname='spans_attrs_trgm_gin_idx'"
            ).fetchone()
        assert row is not None, "spans_attrs_trgm_gin_idx not found"

    def test_tool_name_expr_index_created(self, dsn: str) -> None:
        with psycopg.connect(dsn) as conn:
            row = conn.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename='spans' AND indexname='spans_tool_name_expr_idx'"
            ).fetchone()
        assert row is not None, "spans_tool_name_expr_idx not found"

    # ── Ingest test ───────────────────────────────────────────────────────────

    def test_ingest_populates_session_id(self, dsn: str) -> None:
        """persist_spans with session.id in attributes → session_id column set."""
        from kairos.ingest.spans import persist_spans

        trace_id = "ff" + self._unique_id(30)
        span_id = self._unique_id(16)
        # Build a minimal raw span dict with session.id attribute.
        raw_span: dict[str, Any] = {
            "context": {"trace_id": trace_id, "span_id": span_id},
            "name": "test.ingest.session_id",
            "parent_id": None,
            "start_time": "2026-06-16T10:00:00+00:00",
            "end_time": "2026-06-16T10:00:01+00:00",
            "status_code": "OK",
            "attributes": {"session.id": "ingest-test-session", "tool_name": "bash"},
            "events": [],
        }
        n = persist_spans([raw_span], dsn, source="test")
        assert n == 1

        with psycopg.connect(dsn) as conn:
            row = conn.execute(
                "SELECT session_id FROM spans WHERE trace_id = %s AND span_id = %s",
                (trace_id, span_id),
            ).fetchone()
        assert row is not None
        assert row[0] == "ingest-test-session"

        # Cleanup.
        with psycopg.connect(dsn) as conn:
            conn.execute("DELETE FROM spans WHERE trace_id = %s", (trace_id,))
            conn.commit()

    # ── GET /v1/sessions integration ─────────────────────────────────────────

    def test_sessions_list_groups_correctly(self, api_client: TestClient, dsn: str) -> None:
        sess = f"integ-sess-{self._unique_id(8)}"
        trace1 = self._unique_id(32)
        trace2 = self._unique_id(32)

        with psycopg.connect(dsn) as conn:
            # Two traces under the same session.
            self._insert_span(conn, trace1, self._unique_id(16), session_id=sess, tool_name="bash")
            self._insert_span(conn, trace1, self._unique_id(16), session_id=sess, status_code="ERROR")
            self._insert_span(conn, trace2, self._unique_id(16), session_id=sess, tool_name="read_file")
            conn.commit()

        resp = api_client.get(f"/v1/sessions?q={sess}&limit=10")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        s = data[0]
        assert s["session_id"] == sess
        assert s["trace_count"] == 2
        assert s["span_count"] == 3
        assert s["error_count"] == 1

        # Cleanup.
        with psycopg.connect(dsn) as conn:
            for tid in (trace1, trace2):
                conn.execute("DELETE FROM spans WHERE trace_id = %s", (tid,))
            conn.commit()

    def test_sessions_excludes_null_session_id(self, api_client: TestClient, dsn: str) -> None:
        """Spans with no session.id must not appear in /sessions."""
        trace_id = self._unique_id(32)
        span_id = self._unique_id(16)
        with psycopg.connect(dsn) as conn:
            conn.execute(
                "INSERT INTO spans (trace_id, span_id, name, start_time, status_code, attributes) "
                "VALUES (%s, %s, 'no_session', now(), 'OK', %s) ON CONFLICT DO NOTHING",
                (trace_id, span_id, Jsonb({})),
            )
            conn.commit()

        resp = api_client.get(f"/v1/sessions?q={trace_id[:8]}")
        assert resp.status_code == 200
        session_ids = [s["session_id"] for s in resp.json()]
        assert None not in session_ids

        # Cleanup.
        with psycopg.connect(dsn) as conn:
            conn.execute("DELETE FROM spans WHERE trace_id = %s", (trace_id,))
            conn.commit()

    # ── GET /v1/sessions/{id} integration ────────────────────────────────────

    def test_session_detail_returns_traces(self, api_client: TestClient, dsn: str) -> None:
        sess = f"integ-det-{self._unique_id(8)}"
        trace1 = self._unique_id(32)
        trace2 = self._unique_id(32)

        with psycopg.connect(dsn) as conn:
            self._insert_span(conn, trace1, self._unique_id(16), session_id=sess)
            self._insert_span(conn, trace2, self._unique_id(16), session_id=sess)
            conn.commit()

        resp = api_client.get(f"/v1/sessions/{sess}")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        trace_ids = {t["trace_id"] for t in data}
        assert trace1 in trace_ids
        assert trace2 in trace_ids

        # Cleanup.
        with psycopg.connect(dsn) as conn:
            for tid in (trace1, trace2):
                conn.execute("DELETE FROM spans WHERE trace_id = %s", (tid,))
            conn.commit()

    def test_session_detail_404_for_unknown(self, api_client: TestClient) -> None:
        resp = api_client.get("/v1/sessions/totally-unknown-session-xyz")
        assert resp.status_code == 404

    # ── GET /v1/traces/{id}/spans integration ────────────────────────────────

    def test_trace_spans_returns_raw_spans(self, api_client: TestClient, dsn: str) -> None:
        trace_id = self._unique_id(32)
        span_id1 = self._unique_id(16)
        span_id2 = self._unique_id(16)

        with psycopg.connect(dsn) as conn:
            self._insert_span(conn, trace_id, span_id1, tool_name="bash")
            self._insert_span(conn, trace_id, span_id2, tool_name="read_file")
            conn.commit()

        resp = api_client.get(f"/v1/traces/{trace_id}/spans")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        span_ids = {s["span_id"] for s in data}
        assert span_id1 in span_ids
        assert span_id2 in span_ids

        # Cleanup.
        with psycopg.connect(dsn) as conn:
            conn.execute("DELETE FROM spans WHERE trace_id = %s", (trace_id,))
            conn.commit()

    def test_trace_spans_404_for_unknown(self, api_client: TestClient) -> None:
        resp = api_client.get(f"/v1/traces/{'0' * 32}/spans")
        assert resp.status_code == 404

    def test_trace_spans_compact_vs_full(self, api_client: TestClient, dsn: str) -> None:
        trace_id = self._unique_id(32)
        span_id = self._unique_id(16)

        with psycopg.connect(dsn) as conn:
            conn.execute(
                """
                INSERT INTO spans (trace_id, span_id, name, start_time, status_code, attributes)
                VALUES (%s, %s, 'test', now(), 'OK', %s) ON CONFLICT DO NOTHING
                """,
                (
                    trace_id,
                    span_id,
                    Jsonb({"tool_name": "bash", "session.id": "s1", "hidden": "secret_val"}),
                ),
            )
            conn.commit()

        # Default compact: hidden key absent.
        resp = api_client.get(f"/v1/traces/{trace_id}/spans")
        assert resp.status_code == 200
        span = resp.json()[0]
        assert "hidden" not in span["attributes"]
        assert "tool_name" in span["attributes"]

        # Full: hidden key present.
        resp_full = api_client.get(f"/v1/traces/{trace_id}/spans?full=true")
        assert resp_full.status_code == 200
        span_full = resp_full.json()[0]
        assert span_full["attributes"]["hidden"] == "secret_val"

        # Cleanup.
        with psycopg.connect(dsn) as conn:
            conn.execute("DELETE FROM spans WHERE trace_id = %s", (trace_id,))
            conn.commit()

    # ── GET /v1/search integration ────────────────────────────────────────────

    def test_search_dimension_id_match(self, api_client: TestClient, dsn: str) -> None:
        """Dimension 1: session_id prefix match."""
        sess = f"search-id-{self._unique_id(8)}"
        trace_id = self._unique_id(32)

        with psycopg.connect(dsn) as conn:
            self._insert_span(conn, trace_id, self._unique_id(16), session_id=sess)
            conn.commit()

        resp = api_client.get(f"/v1/search?q={sess}&types=sessions")
        assert resp.status_code == 200
        sessions = resp.json()["sessions"]
        assert any(s["session_id"] == sess for s in sessions)

        # Cleanup.
        with psycopg.connect(dsn) as conn:
            conn.execute("DELETE FROM spans WHERE trace_id = %s", (trace_id,))
            conn.commit()

    def test_search_dimension_tool_match(self, api_client: TestClient, dsn: str) -> None:
        """Dimension 2: tool_name ILIKE match."""
        unique_tool = f"unique_tool_{self._unique_id(8)}"
        trace_id = self._unique_id(32)

        with psycopg.connect(dsn) as conn:
            self._insert_span(conn, trace_id, self._unique_id(16), tool_name=unique_tool)
            conn.commit()

        resp = api_client.get(f"/v1/search?q={unique_tool}&types=spans")
        assert resp.status_code == 200
        spans = resp.json()["spans"]
        assert any(s["tool_name"] == unique_tool for s in spans)

        # Cleanup.
        with psycopg.connect(dsn) as conn:
            conn.execute("DELETE FROM spans WHERE trace_id = %s", (trace_id,))
            conn.commit()

    def test_search_dimension_content_match(self, api_client: TestClient, dsn: str) -> None:
        """Dimension 3: attributes::text ILIKE match."""
        unique_content = f"unique_content_val_{self._unique_id(8)}"
        trace_id = self._unique_id(32)
        span_id = self._unique_id(16)

        with psycopg.connect(dsn) as conn:
            conn.execute(
                """
                INSERT INTO spans (trace_id, span_id, name, start_time, status_code, attributes)
                VALUES (%s, %s, 'test', now(), 'OK', %s) ON CONFLICT DO NOTHING
                """,
                (trace_id, span_id, Jsonb({"custom_key": unique_content})),
            )
            conn.commit()

        resp = api_client.get(f"/v1/search?q={unique_content}&types=spans")
        assert resp.status_code == 200
        spans = resp.json()["spans"]
        assert any(s["span_id"] == span_id for s in spans)

        # Cleanup.
        with psycopg.connect(dsn) as conn:
            conn.execute("DELETE FROM spans WHERE trace_id = %s", (trace_id,))
            conn.commit()

    def test_search_dimension_status_match(self, api_client: TestClient, dsn: str) -> None:
        """Dimension 4: q='error' → status_code='ERROR' filter."""
        trace_id = self._unique_id(32)
        span_id = self._unique_id(16)

        with psycopg.connect(dsn) as conn:
            self._insert_span(conn, trace_id, span_id, status_code="ERROR")
            conn.commit()

        resp = api_client.get("/v1/search?q=error&types=spans")
        assert resp.status_code == 200
        spans = resp.json()["spans"]
        assert any(s["status_code"] == "ERROR" for s in spans)

        # Cleanup.
        with psycopg.connect(dsn) as conn:
            conn.execute("DELETE FROM spans WHERE trace_id = %s", (trace_id,))
            conn.commit()

    def test_search_content_hit_resolves_to_trace(self, api_client: TestClient, dsn: str) -> None:
        """Dimension 3 via hook_events: content match resolves to trace_id."""
        sess = f"hook-sess-{self._unique_id(8)}"
        trace_id = self._unique_id(32)
        unique_output = f"unique_hook_output_{self._unique_id(8)}"

        with psycopg.connect(dsn) as conn:
            # Seed a span with the session.
            self._insert_span(conn, trace_id, self._unique_id(16), session_id=sess)
            # Seed a hook_event with unique content under the same session.
            conn.execute(
                """
                INSERT INTO hook_events
                    (session_id, seq, event_name, tool_output, occurred_at, payload_redacted)
                VALUES (%s, %s, 'PostToolUse', %s, now(), %s)
                ON CONFLICT DO NOTHING
                """,
                (sess, 9999901, unique_output, Jsonb({})),
            )
            conn.commit()

        resp = api_client.get(f"/v1/search?q={unique_output}&types=traces")
        assert resp.status_code == 200
        traces = resp.json()["traces"]
        assert any(t["trace_id"] == trace_id for t in traces)

        # Cleanup.
        with psycopg.connect(dsn) as conn:
            conn.execute("DELETE FROM spans WHERE trace_id = %s", (trace_id,))
            conn.execute(
                "DELETE FROM hook_events WHERE session_id = %s AND seq = 9999901",
                (sess,),
            )
            conn.commit()
