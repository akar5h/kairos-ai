"""Tests for src/kairos/readers/db.py — DB-backed span reader (F1.1).

Unit tests (no DB) verify _db_row_to_span shape and that fetch_envelope_from_db
produces a valid TraceEnvelope from fixture data.

Integration tests (require KAIROS_PG_DSN) verify the full round-trip:
persist_spans → fetch_spans_from_db → fetch_envelope_from_db.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from kairos.readers.db import _db_row_to_span, fetch_envelope_from_db, fetch_spans_from_db, list_trace_ids
from kairos.readers.phoenix import _PhoenixSpan, spans_to_envelope

_DSN = os.environ.get("KAIROS_PG_DSN", "").strip()
_DB_AVAILABLE = bool(_DSN)

_skip_no_db = pytest.mark.skipif(
    not _DB_AVAILABLE,
    reason="KAIROS_PG_DSN not set — kairos-pg container not reachable in this environment",
)

# ── Fixtures ─────────────────────────────────────────────────────────────────

_TRACE_ID = "aabbccdd" * 4  # 32 hex chars
_START = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
_END = datetime(2026, 6, 1, 12, 0, 1, tzinfo=UTC)


def _make_db_row(
    *,
    trace_id: str = _TRACE_ID,
    span_id: str = "1" * 16,
    parent_span_id: str | None = None,
    name: str = "kairos.task",
    status_code: str = "OK",
    attributes: dict[str, Any] | None = None,
    events: list[dict[str, Any]] | None = None,
    resource: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": parent_span_id,
        "name": name,
        "start_time": _START,
        "end_time": _END,
        "status_code": status_code,
        "attributes": attributes or {"kairos.agent.name": "test_agent"},
        "events": events or [],
        "resource": resource or {},
    }


def _make_phoenix_dict(
    *,
    trace_id: str = _TRACE_ID,
    span_id: str = "1" * 16,
    parent_id: str | None = None,
    name: str = "kairos.task",
    attributes: dict[str, Any] | None = None,
    events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": f"phoenix-{span_id}",
        "name": name,
        "context": {"trace_id": trace_id, "span_id": span_id},
        "parent_id": parent_id,
        "span_kind": "UNKNOWN",
        "start_time": "2026-06-01T12:00:00.000000+00:00",
        "end_time": "2026-06-01T12:00:01.000000+00:00",
        "status_code": "OK",
        "status_message": "",
        "attributes": attributes or {"kairos.agent.name": "test_agent"},
        "events": events or [],
    }


# ── Unit tests (no DB) ────────────────────────────────────────────────────────


class TestDbRowToSpan:
    """_db_row_to_span must produce the same duck-typed shape as _phoenix_dict_to_span."""

    def test_returns_phoenix_span(self) -> None:
        row = _make_db_row()
        span = _db_row_to_span(row)
        assert isinstance(span, _PhoenixSpan)

    def test_trace_id_roundtrip(self) -> None:
        row = _make_db_row(trace_id=_TRACE_ID)
        span = _db_row_to_span(row)
        assert f"{span.context.trace_id:032x}" == _TRACE_ID

    def test_span_id_roundtrip(self) -> None:
        row = _make_db_row(span_id="abcdef0123456789")
        span = _db_row_to_span(row)
        assert f"{span.context.span_id:016x}" == "abcdef0123456789"

    def test_parent_span_id_none(self) -> None:
        row = _make_db_row(parent_span_id=None)
        span = _db_row_to_span(row)
        assert span.parent is None

    def test_parent_span_id_set(self) -> None:
        row = _make_db_row(parent_span_id="fedcba9876543210")
        span = _db_row_to_span(row)
        assert span.parent is not None
        assert f"{span.parent.span_id:016x}" == "fedcba9876543210"

    def test_name_roundtrip(self) -> None:
        row = _make_db_row(name="claude_code.llm_request")
        span = _db_row_to_span(row)
        assert span.name == "claude_code.llm_request"

    def test_attributes_roundtrip(self) -> None:
        attrs = {"gen_ai.input_tokens": 100, "gen_ai.output_tokens": 50}
        row = _make_db_row(attributes=attrs)
        span = _db_row_to_span(row)
        assert span.attributes == attrs

    def test_events_roundtrip(self) -> None:
        evs = [{"name": "tool.output", "attributes": {"content": "result"}}]
        row = _make_db_row(events=evs)
        span = _db_row_to_span(row)
        assert len(span.events) == 1
        assert span.events[0].name == "tool.output"
        assert span.events[0].attributes == {"content": "result"}

    def test_status_ok(self) -> None:
        from opentelemetry.trace import StatusCode

        row = _make_db_row(status_code="OK")
        span = _db_row_to_span(row)
        assert span.status.status_code == StatusCode.OK

    def test_status_error(self) -> None:
        from opentelemetry.trace import StatusCode

        row = _make_db_row(status_code="ERROR")
        span = _db_row_to_span(row)
        assert span.status.status_code == StatusCode.ERROR

    def test_timestamps_converted_to_nanoseconds(self) -> None:
        row = _make_db_row()
        span = _db_row_to_span(row)
        # 2026-06-01 12:00:00 UTC in nanoseconds
        expected_ns = int(_START.timestamp() * 1_000_000_000)
        assert span.start_time == pytest.approx(expected_ns, abs=1_000_000)  # ±1ms

    def test_resource_roundtrip(self) -> None:
        row = _make_db_row(resource={"service.name": "kairos-test"})
        span = _db_row_to_span(row)
        assert span.resource.attributes == {"service.name": "kairos-test"}


class TestFetchEnvelopeFromDbUnit:
    """fetch_envelope_from_db shape matches the Phoenix path for the same fixture."""

    def test_db_and_phoenix_envelopes_equivalent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Both paths must produce envelopes with the same trace_id and is_valid."""
        task_row = _make_db_row(name="kairos.task", span_id="1" * 16)
        llm_row = _make_db_row(
            name="openai.chat",
            span_id="2" * 16,
            parent_span_id="1" * 16,
            attributes={
                "gen_ai.input_tokens": 100,
                "gen_ai.output_tokens": 50,
                "gen_ai.system": "openai",
                "gen_ai.request.model": "gpt-4",
            },
        )

        db_spans = [_db_row_to_span(task_row), _db_row_to_span(llm_row)]
        db_envelope = spans_to_envelope(db_spans)

        phoenix_spans = [
            _make_phoenix_dict(name="kairos.task", span_id="1" * 16),
            _make_phoenix_dict(
                name="openai.chat",
                span_id="2" * 16,
                parent_id="1" * 16,
                attributes={
                    "gen_ai.input_tokens": 100,
                    "gen_ai.output_tokens": 50,
                    "gen_ai.system": "openai",
                    "gen_ai.request.model": "gpt-4",
                },
            ),
        ]
        phoenix_envelope = spans_to_envelope(phoenix_spans)

        assert db_envelope.is_valid == phoenix_envelope.is_valid
        assert db_envelope.trace_id == phoenix_envelope.trace_id
        assert len(db_envelope.steps) == len(phoenix_envelope.steps)


# ── Integration tests (require live kairos-pg) ────────────────────────────────


@_skip_no_db
class TestSpansRoundTrip:
    """persist_spans → fetch_spans_from_db → fetch_envelope_from_db."""

    def _cleanup(self, trace_id: str) -> None:
        import psycopg

        with psycopg.connect(_DSN) as conn:
            conn.execute("DELETE FROM spans WHERE trace_id = %s", (trace_id,))
            conn.commit()

    def test_persist_and_fetch_spans(self) -> None:
        from kairos.ingest.spans import persist_spans
        from kairos.loop import db

        db.apply_migrations()

        tid = uuid.uuid4().hex
        phoenix_dicts = [
            _make_phoenix_dict(trace_id=tid, span_id="a" * 16, name="kairos.task"),
            _make_phoenix_dict(
                trace_id=tid,
                span_id="b" * 16,
                parent_id="a" * 16,
                name="openai.chat",
                attributes={"gen_ai.input_tokens": 10},
            ),
        ]

        n = persist_spans(phoenix_dicts, _DSN, source="test")
        assert n == 2

        try:
            spans = fetch_spans_from_db(tid, _DSN)
            assert len(spans) == 2
            names = {s.name for s in spans}
            assert names == {"kairos.task", "openai.chat"}

            # Verify round-trip attributes.
            chat_span = next(s for s in spans if s.name == "openai.chat")
            assert chat_span.attributes.get("gen_ai.input_tokens") == 10
        finally:
            self._cleanup(tid)

    def test_upsert_idempotent(self) -> None:
        from kairos.ingest.spans import persist_spans
        from kairos.loop import db

        db.apply_migrations()

        tid = uuid.uuid4().hex
        phoenix_dicts = [_make_phoenix_dict(trace_id=tid, span_id="c" * 16, name="kairos.task")]

        try:
            persist_spans(phoenix_dicts, _DSN)
            persist_spans(phoenix_dicts, _DSN)  # second call must not raise

            spans = fetch_spans_from_db(tid, _DSN)
            assert len(spans) == 1  # not doubled
        finally:
            self._cleanup(tid)

    def test_fetch_envelope_from_db_valid(self) -> None:
        from kairos.ingest.spans import persist_spans
        from kairos.loop import db

        db.apply_migrations()

        tid = uuid.uuid4().hex
        phoenix_dicts = [
            _make_phoenix_dict(trace_id=tid, span_id="d" * 16, name="kairos.task"),
            _make_phoenix_dict(
                trace_id=tid,
                span_id="e" * 16,
                parent_id="d" * 16,
                name="openai.chat",
                attributes={
                    "gen_ai.input_tokens": 100,
                    "gen_ai.output_tokens": 50,
                    "gen_ai.system": "openai",
                    "gen_ai.request.model": "gpt-4",
                },
            ),
        ]
        persist_spans(phoenix_dicts, _DSN)

        try:
            envelope = fetch_envelope_from_db(tid, _DSN)
            assert envelope.is_valid
            assert envelope.trace_id != ""
            assert len(envelope.steps) >= 1
        finally:
            self._cleanup(tid)

    def test_fetch_empty_trace_returns_invalid_envelope(self) -> None:
        from kairos.loop import db

        db.apply_migrations()

        nonexistent = uuid.uuid4().hex
        envelope = fetch_envelope_from_db(nonexistent, _DSN)
        assert not envelope.is_valid


# ── Unit tests for list_trace_ids (mocked psycopg) ───────────────────────────


class TestListTraceIdsUnit:
    """list_trace_ids builds correct SQL and returns trace_id strings."""

    def _mock_conn(self, rows: list[dict[str, str]]) -> Any:
        from unittest.mock import MagicMock

        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = rows
        return mock_conn

    def test_empty_result_returns_empty_list(self, monkeypatch: pytest.MonkeyPatch) -> None:

        import psycopg

        mock_conn = self._mock_conn([])
        monkeypatch.setattr(psycopg, "connect", lambda *a, **kw: mock_conn)
        result = list_trace_ids("postgresql://fake/kairos")
        assert result == []

    def test_returns_trace_ids_as_strings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import psycopg

        rows = [{"trace_id": "aabbccdd" * 4}, {"trace_id": "11223344" * 4}]
        mock_conn = self._mock_conn(rows)
        monkeypatch.setattr(psycopg, "connect", lambda *a, **kw: mock_conn)
        result = list_trace_ids("postgresql://fake/kairos")
        assert result == ["aabbccdd" * 4, "11223344" * 4]

    def test_since_filter_adds_having_clause(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import psycopg

        mock_conn = self._mock_conn([])
        monkeypatch.setattr(psycopg, "connect", lambda *a, **kw: mock_conn)
        list_trace_ids("postgresql://fake/kairos", since="2026-06-01T00:00:00Z")
        sql, params = (
            mock_conn.execute.call_args[0][0],
            mock_conn.execute.call_args[0][1],
        )
        assert "HAVING" in sql
        assert "min(start_time)" in sql
        assert "2026-06-01T00:00:00Z" in params

    def test_limit_adds_limit_clause(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import psycopg

        mock_conn = self._mock_conn([])
        monkeypatch.setattr(psycopg, "connect", lambda *a, **kw: mock_conn)
        list_trace_ids("postgresql://fake/kairos", limit=10)
        sql, params = (
            mock_conn.execute.call_args[0][0],
            mock_conn.execute.call_args[0][1],
        )
        assert "LIMIT" in sql
        assert 10 in params

    def test_no_filter_no_having_no_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import psycopg

        mock_conn = self._mock_conn([])
        monkeypatch.setattr(psycopg, "connect", lambda *a, **kw: mock_conn)
        list_trace_ids("postgresql://fake/kairos")
        sql = mock_conn.execute.call_args[0][0]
        assert "HAVING" not in sql
        assert "LIMIT" not in sql


# ── Integration test for list_trace_ids (requires live kairos-pg) ─────────────


@_skip_no_db
class TestListTraceIdsDb:
    """list_trace_ids queries the real spans table."""

    def _cleanup(self, trace_id: str) -> None:
        import psycopg

        with psycopg.connect(_DSN) as conn:
            conn.execute("DELETE FROM spans WHERE trace_id = %s", (trace_id,))
            conn.commit()

    def test_list_trace_ids_finds_persisted_trace(self) -> None:
        from kairos.ingest.spans import persist_spans
        from kairos.loop import db

        db.apply_migrations()

        tid = uuid.uuid4().hex
        phoenix_dicts = [
            _make_phoenix_dict(trace_id=tid, span_id="f" * 16, name="kairos.task"),
        ]
        persist_spans(phoenix_dicts, _DSN, source="test")

        try:
            ids = list_trace_ids(_DSN)
            assert tid in ids
        finally:
            self._cleanup(tid)

    def test_list_trace_ids_since_filter_excludes_old_traces(self) -> None:
        """Traces started before `since` are excluded."""
        from kairos.ingest.spans import persist_spans
        from kairos.loop import db

        db.apply_migrations()

        tid = uuid.uuid4().hex
        phoenix_dicts = [
            _make_phoenix_dict(trace_id=tid, span_id="e" * 16, name="kairos.task"),
        ]
        persist_spans(phoenix_dicts, _DSN, source="test")

        try:
            # since far in the future → no results for this trace
            future = "2099-01-01T00:00:00Z"
            ids = list_trace_ids(_DSN, since=future)
            assert tid not in ids
        finally:
            self._cleanup(tid)

    def test_list_trace_ids_limit_caps_results(self) -> None:
        from kairos.ingest.spans import persist_spans
        from kairos.loop import db

        db.apply_migrations()

        tids = []
        try:
            for _ in range(3):
                tid = uuid.uuid4().hex
                tids.append(tid)
                persist_spans(
                    [_make_phoenix_dict(trace_id=tid, span_id=uuid.uuid4().hex[:16], name="kairos.task")],
                    _DSN,
                    source="test",
                )
            result = list_trace_ids(_DSN, limit=1)
            assert len(result) == 1
        finally:
            for tid in tids:
                self._cleanup(tid)
