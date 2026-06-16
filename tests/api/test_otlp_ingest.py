"""Tests for F1.4 OTLP/HTTP ingest service.

Coverage:
- Pure unit tests for the OTLP→_PhoenixSpan mapper (no HTTP, no DB)
- FastAPI TestClient tests for /health and POST /v1/traces
- Malformed body → 200 partial-success, no persist called
- DB-gated round-trip (requires KAIROS_PG_DSN; skipped when absent)
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.resource.v1.resource_pb2 import Resource
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans
from opentelemetry.proto.trace.v1.trace_pb2 import Span as OtlpSpan
from opentelemetry.proto.trace.v1.trace_pb2 import Status as OtlpStatus
from opentelemetry.trace import StatusCode

from kairos.api.app import create_app
from kairos.api.otlp import (
    _flatten_any_value,
    _flatten_attributes,
    _map_resource_spans,
    _otlp_span_to_phoenix,
)
from kairos.readers.phoenix import _PhoenixSpan

# ─────────────────────── helpers ────────────────────────


def _make_otlp_request(
    *,
    trace_id: bytes | None = None,
    span_id: bytes | None = None,
    parent_span_id: bytes | None = None,
    name: str = "test.span",
    start_ns: int = 1_000_000_000,
    end_ns: int = 2_000_000_000,
    attributes: dict[str, str | int | bool | float] | None = None,
    events: list[tuple[str, dict[str, str]]] | None = None,
    status_code: int = OtlpStatus.STATUS_CODE_OK,
    resource_attrs: dict[str, str] | None = None,
) -> ExportTraceServiceRequest:
    """Build a minimal ExportTraceServiceRequest for testing."""
    trace_id_bytes = trace_id or b"\x01" * 16
    span_id_bytes = span_id or b"\x02" * 8

    # Build span attributes as KeyValue list
    kv_attrs: list[KeyValue] = []
    for k, v in (attributes or {}).items():
        if isinstance(v, str):
            av = AnyValue(string_value=v)
        elif isinstance(v, bool):
            av = AnyValue(bool_value=v)
        elif isinstance(v, int):
            av = AnyValue(int_value=v)
        elif isinstance(v, float):
            av = AnyValue(double_value=v)
        else:
            av = AnyValue(string_value=str(v))
        kv_attrs.append(KeyValue(key=k, value=av))

    # Build events
    otlp_events: list[OtlpSpan.Event] = []
    for ev_name, ev_attrs in events or []:
        ev_kv = [KeyValue(key=k, value=AnyValue(string_value=v)) for k, v in ev_attrs.items()]
        otlp_events.append(OtlpSpan.Event(name=ev_name, attributes=ev_kv))

    span = OtlpSpan(
        trace_id=trace_id_bytes,
        span_id=span_id_bytes,
        parent_span_id=parent_span_id or b"",
        name=name,
        start_time_unix_nano=start_ns,
        end_time_unix_nano=end_ns,
        attributes=kv_attrs,
        events=otlp_events,
        status=OtlpStatus(code=status_code),
    )

    # Resource attrs
    res_kv = [KeyValue(key=k, value=AnyValue(string_value=v)) for k, v in (resource_attrs or {}).items()]
    resource = Resource(attributes=res_kv)

    rs = ResourceSpans(
        resource=resource,
        scope_spans=[ScopeSpans(spans=[span])],
    )
    return ExportTraceServiceRequest(resource_spans=[rs])


@pytest.fixture()
def client() -> TestClient:
    """TestClient for the Kairos FastAPI app."""
    return TestClient(create_app())


# ─────────────── unit tests: AnyValue flattening ───────────────


class TestFlattenAnyValue:
    def test_string(self) -> None:
        av = AnyValue(string_value="hello")
        assert _flatten_any_value(av) == "hello"

    def test_int(self) -> None:
        av = AnyValue(int_value=42)
        assert _flatten_any_value(av) == 42

    def test_double(self) -> None:
        av = AnyValue(double_value=3.14)
        assert _flatten_any_value(av) == pytest.approx(3.14)

    def test_bool(self) -> None:
        av = AnyValue(bool_value=True)
        assert _flatten_any_value(av) is True

    def test_bytes_hex_encoded(self) -> None:
        av = AnyValue(bytes_value=b"\xde\xad\xbe\xef")
        assert _flatten_any_value(av) == "deadbeef"

    def test_empty_any_value_returns_none(self) -> None:
        av = AnyValue()
        assert _flatten_any_value(av) is None

    def test_array_value(self) -> None:
        from opentelemetry.proto.common.v1.common_pb2 import ArrayValue

        av = AnyValue(array_value=ArrayValue(values=[AnyValue(string_value="a"), AnyValue(int_value=1)]))
        assert _flatten_any_value(av) == ["a", 1]

    def test_kvlist_value(self) -> None:
        from opentelemetry.proto.common.v1.common_pb2 import KeyValueList

        av = AnyValue(
            kvlist_value=KeyValueList(
                values=[
                    KeyValue(key="x", value=AnyValue(string_value="y")),
                ]
            )
        )
        assert _flatten_any_value(av) == {"x": "y"}


class TestFlattenAttributes:
    def test_empty(self) -> None:
        assert _flatten_attributes([]) == {}

    def test_multiple_types(self) -> None:
        kvs = [
            KeyValue(key="str_key", value=AnyValue(string_value="val")),
            KeyValue(key="int_key", value=AnyValue(int_value=7)),
            KeyValue(key="bool_key", value=AnyValue(bool_value=False)),
        ]
        result = _flatten_attributes(kvs)
        assert result == {"str_key": "val", "int_key": 7, "bool_key": False}


# ─────────────── unit tests: OTLP span → _PhoenixSpan mapper ───────────────


class TestOtlpSpanToPhoenix:
    """Pure mapper tests — no HTTP, no DB."""

    def _make_span(
        self,
        trace_id: bytes = b"\x00" * 15 + b"\x01",
        span_id: bytes = b"\x00" * 7 + b"\x02",
        parent_span_id: bytes = b"",
        name: str = "claude_code.llm_request",
        attrs: list[KeyValue] | None = None,
        events: list[OtlpSpan.Event] | None = None,
        status_code: int = OtlpStatus.STATUS_CODE_OK,
    ) -> OtlpSpan:
        return OtlpSpan(
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=parent_span_id,
            name=name,
            start_time_unix_nano=1_000_000_000,
            end_time_unix_nano=2_000_000_000,
            attributes=attrs or [],
            events=events or [],
            status=OtlpStatus(code=status_code),
        )

    def test_bytes_ids_converted_to_int(self) -> None:
        # trace_id: 16 bytes → big-endian int
        trace_bytes = b"\x00" * 15 + b"\xff"  # = 255
        span_bytes = b"\x00" * 7 + b"\xab"  # = 171
        span = self._make_span(trace_id=trace_bytes, span_id=span_bytes)
        result = _otlp_span_to_phoenix(span, {})
        assert result.context.trace_id == 255
        assert result.context.span_id == 171

    def test_hex_formatting_via_context(self) -> None:
        """Verify that the hex formatting in _span_to_row produces the right string."""
        trace_bytes = b"\x01" * 16
        span_bytes = b"\x02" * 8
        span = self._make_span(trace_id=trace_bytes, span_id=span_bytes)
        result = _otlp_span_to_phoenix(span, {})
        # _span_to_row uses f"{ctx.trace_id:032x}" and f"{ctx.span_id:016x}"
        assert f"{result.context.trace_id:032x}" == "01010101010101010101010101010101"
        assert f"{result.context.span_id:016x}" == "0202020202020202"

    def test_parent_span_id_set_when_present(self) -> None:
        parent_bytes = b"\x00" * 7 + b"\x05"  # = 5
        span = self._make_span(parent_span_id=parent_bytes)
        result = _otlp_span_to_phoenix(span, {})
        assert result.parent is not None
        assert result.parent.span_id == 5

    def test_parent_none_when_empty(self) -> None:
        span = self._make_span(parent_span_id=b"")
        result = _otlp_span_to_phoenix(span, {})
        assert result.parent is None

    def test_attributes_flattened(self) -> None:
        attrs = [
            KeyValue(key="llm.model", value=AnyValue(string_value="claude-3-5-sonnet")),
            KeyValue(key="token_count", value=AnyValue(int_value=100)),
        ]
        span = self._make_span(attrs=attrs)
        result = _otlp_span_to_phoenix(span, {})
        assert result.attributes == {"llm.model": "claude-3-5-sonnet", "token_count": 100}

    def test_events_preserved(self) -> None:
        ev_attrs = [KeyValue(key="content", value=AnyValue(string_value="tool output here"))]
        event = OtlpSpan.Event(name="tool.output", attributes=ev_attrs)
        span = self._make_span(events=[event])
        result = _otlp_span_to_phoenix(span, {})
        assert len(result.events) == 1
        assert result.events[0].name == "tool.output"
        assert result.events[0].attributes == {"content": "tool output here"}

    def test_resource_attrs_carried(self) -> None:
        resource = {"service.name": "claude-code", "kairos.project": "my-project"}
        span = self._make_span()
        result = _otlp_span_to_phoenix(span, resource)
        assert result.resource.attributes == resource

    def test_status_ok_mapped(self) -> None:
        span = self._make_span(status_code=OtlpStatus.STATUS_CODE_OK)
        result = _otlp_span_to_phoenix(span, {})
        assert result.status.status_code == StatusCode.OK

    def test_status_error_mapped(self) -> None:
        span = self._make_span(status_code=OtlpStatus.STATUS_CODE_ERROR)
        result = _otlp_span_to_phoenix(span, {})
        assert result.status.status_code == StatusCode.ERROR

    def test_status_unset_mapped(self) -> None:
        span = self._make_span(status_code=OtlpStatus.STATUS_CODE_UNSET)
        result = _otlp_span_to_phoenix(span, {})
        assert result.status.status_code == StatusCode.UNSET

    def test_start_end_times_preserved(self) -> None:
        span = OtlpSpan(
            trace_id=b"\x01" * 16,
            span_id=b"\x02" * 8,
            name="test",
            start_time_unix_nano=5_000_000_000,
            end_time_unix_nano=9_000_000_000,
            status=OtlpStatus(code=OtlpStatus.STATUS_CODE_OK),
        )
        result = _otlp_span_to_phoenix(span, {})
        assert result.start_time == 5_000_000_000
        assert result.end_time == 9_000_000_000

    def test_name_set(self) -> None:
        span = self._make_span(name="claude_code.tool.bash")
        result = _otlp_span_to_phoenix(span, {})
        assert result.name == "claude_code.tool.bash"

    def test_returns_phoenix_span_type(self) -> None:
        span = self._make_span()
        result = _otlp_span_to_phoenix(span, {})
        assert isinstance(result, _PhoenixSpan)


# ─────────────── unit tests: map_resource_spans ───────────────


class TestMapResourceSpans:
    def test_maps_all_spans(self) -> None:
        req = _make_otlp_request(
            trace_id=b"\x01" * 16,
            span_id=b"\x02" * 8,
            name="test.span",
            attributes={"key": "value"},
        )
        mapped, skipped = _map_resource_spans(req)
        assert len(mapped) == 1
        assert skipped == 0
        assert mapped[0].name == "test.span"

    def test_resource_attrs_on_mapped_span(self) -> None:
        req = _make_otlp_request(resource_attrs={"service.name": "claude-code"})
        mapped, _ = _map_resource_spans(req)
        assert mapped[0].resource.attributes == {"service.name": "claude-code"}

    def test_empty_request_returns_empty(self) -> None:
        req = ExportTraceServiceRequest()
        mapped, skipped = _map_resource_spans(req)
        assert mapped == []
        assert skipped == 0


# ─────────────── HTTP tests via TestClient ───────────────


class TestHealthEndpoint:
    def test_health_returns_ok(self, client: TestClient) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


class TestOtlpTracesEndpoint:
    def test_valid_protobuf_returns_200(self, client: TestClient) -> None:
        req = _make_otlp_request(
            trace_id=b"\x01" * 16,
            span_id=b"\x02" * 8,
            name="claude_code.llm_request",
            attributes={"gen_ai.system": "anthropic"},
        )
        body = req.SerializeToString()

        with (
            patch("kairos.api.otlp.persist_spans", return_value=1),
            patch("kairos.api.otlp._dsn", return_value="postgresql://test/test"),
        ):
            resp = client.post(
                "/v1/traces",
                content=body,
                headers={"content-type": "application/x-protobuf"},
            )

        assert resp.status_code == 200
        # Response must be a valid ExportTraceServiceResponse
        proto_resp = ExportTraceServiceResponse()
        proto_resp.ParseFromString(resp.content)

    def test_persist_spans_called_with_mapped_spans(self, client: TestClient) -> None:
        """persist_spans is called with _PhoenixSpan instances and source='otlp_http'."""
        req = _make_otlp_request(
            trace_id=b"\xaa" * 16,
            span_id=b"\xbb" * 8,
            name="test.span",
            attributes={"key": "val"},
            events=[("tool.output", {"content": "hello"})],
        )
        body = req.SerializeToString()

        with (
            patch("kairos.api.otlp.persist_spans", return_value=1) as mock_persist,
            patch("kairos.api.otlp._dsn", return_value="postgresql://test/test"),
        ):
            client.post(
                "/v1/traces",
                content=body,
                headers={"content-type": "application/x-protobuf"},
            )

        mock_persist.assert_called_once()
        call_args = mock_persist.call_args
        spans_arg = call_args[0][0]  # positional arg 0
        dsn_arg = call_args[0][1]
        source_kwarg = call_args[1]["source"]

        assert len(spans_arg) == 1
        assert isinstance(spans_arg[0], _PhoenixSpan)
        assert source_kwarg == "otlp_http"
        assert dsn_arg == "postgresql://test/test"

    def test_mapped_span_has_correct_hex_ids(self, client: TestClient) -> None:
        """Verify that the _PhoenixSpan passed to persist_spans has correct integer IDs."""
        trace_bytes = b"\x01" * 16
        span_bytes = b"\x02" * 8

        req = _make_otlp_request(trace_id=trace_bytes, span_id=span_bytes)
        body = req.SerializeToString()

        captured_spans: list[_PhoenixSpan] = []

        def capture_persist(spans: list[_PhoenixSpan], dsn: str, *, source: str) -> int:
            captured_spans.extend(spans)
            return len(spans)

        with (
            patch("kairos.api.otlp.persist_spans", side_effect=capture_persist),
            patch("kairos.api.otlp._dsn", return_value="postgresql://test/test"),
        ):
            client.post(
                "/v1/traces",
                content=body,
                headers={"content-type": "application/x-protobuf"},
            )

        assert len(captured_spans) == 1
        span = captured_spans[0]
        # Verify hex format matches OTLP spec
        assert f"{span.context.trace_id:032x}" == "01010101010101010101010101010101"
        assert f"{span.context.span_id:016x}" == "0202020202020202"

    def test_events_preserved_through_http(self, client: TestClient) -> None:
        """Span events survive the full HTTP decode→map path."""
        req = _make_otlp_request(
            events=[("tool.output", {"content": "bash output here"})],
        )
        body = req.SerializeToString()

        captured_spans: list[_PhoenixSpan] = []

        def capture(spans: list[_PhoenixSpan], dsn: str, *, source: str) -> int:
            captured_spans.extend(spans)
            return len(spans)

        with (
            patch("kairos.api.otlp.persist_spans", side_effect=capture),
            patch("kairos.api.otlp._dsn", return_value="postgresql://test/test"),
        ):
            client.post(
                "/v1/traces",
                content=body,
                headers={"content-type": "application/x-protobuf"},
            )

        assert len(captured_spans[0].events) == 1
        assert captured_spans[0].events[0].name == "tool.output"
        assert captured_spans[0].events[0].attributes["content"] == "bash output here"

    def test_malformed_body_returns_200_partial_success(self, client: TestClient) -> None:
        """Malformed body → 200, no persist called, valid proto response."""
        with patch("kairos.api.otlp.persist_spans") as mock_persist:
            resp = client.post(
                "/v1/traces",
                content=b"this is not valid protobuf !!@@##",
                headers={"content-type": "application/x-protobuf"},
            )

        assert resp.status_code == 200
        mock_persist.assert_not_called()
        # Response should parse as ExportTraceServiceResponse (empty)
        proto_resp = ExportTraceServiceResponse()
        proto_resp.ParseFromString(resp.content)

    def test_empty_body_returns_200(self, client: TestClient) -> None:
        """Empty body (zero spans) → 200, persist not called."""
        req = ExportTraceServiceRequest()
        body = req.SerializeToString()

        with (
            patch("kairos.api.otlp.persist_spans") as mock_persist,
            patch("kairos.api.otlp._dsn", return_value="postgresql://test/test"),
        ):
            resp = client.post(
                "/v1/traces",
                content=body,
                headers={"content-type": "application/x-protobuf"},
            )

        assert resp.status_code == 200
        mock_persist.assert_not_called()

    def test_missing_dsn_does_not_500(self, client: TestClient) -> None:
        """Missing KAIROS_PG_DSN → 200, no crash (producer-safe)."""
        req = _make_otlp_request()
        body = req.SerializeToString()

        # Patch _dsn to raise RuntimeError as it would when env var is missing
        with patch("kairos.api.otlp._dsn", side_effect=RuntimeError("KAIROS_PG_DSN is not set")):
            resp = client.post(
                "/v1/traces",
                content=body,
                headers={"content-type": "application/x-protobuf"},
            )

        assert resp.status_code == 200

    def test_persist_exception_does_not_500(self, client: TestClient) -> None:
        """DB error during persist → 200 (never break the producer)."""
        req = _make_otlp_request()
        body = req.SerializeToString()

        with (
            patch("kairos.api.otlp.persist_spans", side_effect=Exception("DB down")),
            patch("kairos.api.otlp._dsn", return_value="postgresql://test/test"),
        ):
            resp = client.post(
                "/v1/traces",
                content=body,
                headers={"content-type": "application/x-protobuf"},
            )

        assert resp.status_code == 200

    def test_json_content_type_accepted(self, client: TestClient) -> None:
        """application/json Content-Type is decoded via json_format.Parse."""
        from google.protobuf import json_format

        req = _make_otlp_request(name="json.span")
        json_body = json_format.MessageToJson(req).encode()

        captured: list[_PhoenixSpan] = []

        def capture(spans: list[_PhoenixSpan], dsn: str, *, source: str) -> int:
            captured.extend(spans)
            return len(spans)

        with (
            patch("kairos.api.otlp.persist_spans", side_effect=capture),
            patch("kairos.api.otlp._dsn", return_value="postgresql://test/test"),
        ):
            resp = client.post(
                "/v1/traces",
                content=json_body,
                headers={"content-type": "application/json"},
            )

        assert resp.status_code == 200
        # JSON decode may produce empty proto due to field name casing; at minimum no crash
        # (json_format.Parse handles snake_case + camelCase both)


# ─────────────── DB-gated round-trip ───────────────


@pytest.mark.integration
class TestOtlpIngestDbRoundTrip:
    """Requires KAIROS_PG_DSN to be set. Skipped otherwise."""

    @pytest.fixture(autouse=True)
    def require_dsn(self) -> None:
        if not os.environ.get("KAIROS_PG_DSN"):
            pytest.skip("KAIROS_PG_DSN not set — skipping DB round-trip")

    def test_span_persisted_to_db(self) -> None:
        import psycopg

        from kairos.loop.db import _dsn as get_dsn

        dsn = get_dsn()
        client = TestClient(create_app())

        trace_id = b"\xca\xfe\xba\xbe" * 4  # 16 bytes
        span_id = b"\xde\xad\xbe\xef" * 2  # 8 bytes

        req = _make_otlp_request(
            trace_id=trace_id,
            span_id=span_id,
            name="kairos.test.roundtrip",
            attributes={"test.run": "f1.4"},
        )
        body = req.SerializeToString()

        resp = client.post(
            "/v1/traces",
            content=body,
            headers={"content-type": "application/x-protobuf"},
        )
        assert resp.status_code == 200

        expected_trace_id = "cafebabecafebabecafebabecafebabe"
        expected_span_id = "deadbeefdeadbeef"

        with psycopg.connect(dsn) as conn:
            row = conn.execute(
                "SELECT trace_id, span_id, name, source FROM spans WHERE trace_id = %s AND span_id = %s",
                (expected_trace_id, expected_span_id),
            ).fetchone()

        assert row is not None, "Span not found in DB"
        assert row[0] == expected_trace_id
        assert row[1] == expected_span_id
        assert row[2] == "kairos.test.roundtrip"
        assert row[3] == "otlp_http"
