# Kairos OTLP backend (Phase 0)

Always-on collection layer for Kairos. Agents emit OpenTelemetry spans over
OTLP; this stack stores them and exposes a raw-trace UI. Analysis stays
pull-based (`kairos analyze` / `KairosEngine.analyze`) — nothing here runs an
LLM or blocks an agent run.

```
agent process ──OTLP (async batch)──► OTLP backend ──► store + raw-trace UI
                                       (this stack)      (pull) → PhoenixReader → IR → analyze
```

## Default stack — Apache-2.0, zero ELv2

```bash
docker compose -f deploy/docker-compose.yml up -d
```

| Service          | Image                                    | License    | Purpose                       |
| ---------------- | ---------------------------------------- | ---------- | ----------------------------- |
| `otel-collector` | `otel/opentelemetry-collector-contrib`   | Apache-2.0 | OTLP ingest endpoint          |
| `jaeger`         | `jaegertracing/all-in-one`               | Apache-2.0 | Trace store + raw-trace UI     |

- **Emit endpoint** (point agents here): `http://localhost:4318` (OTLP HTTP) or
  `localhost:4317` (OTLP gRPC). Standard OTel env: `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318`.
- **Raw-trace UI:** http://localhost:16686 (Jaeger).

The backend sits behind the collector and is swappable without touching the
emit side. This default deploy contains **no source-available (ELv2) code** —
it keeps the MIT exit clean.

## Opt-in — Phoenix (Elastic License 2.0)

Phoenix is the backend Kairos's `PhoenixReader` currently reads from. It is
**ELv2 (source-available, not MIT)**, runs as a separate process, and is **not**
part of the default deploy. Enable it explicitly:

```bash
docker compose -f deploy/docker-compose.yml --profile phoenix up -d
```

| Service   | Image                  | License | Purpose                         |
| --------- | ---------------------- | ------- | ------------------------------- |
| `phoenix` | `arizephoenix/phoenix` | ELv2    | OTLP backend + Phoenix UI        |

- **Phoenix UI:** http://localhost:6006
- **Phoenix OTLP gRPC:** `localhost:4319` (host port; container `:4317`).
- Point `PhoenixReader` at the Phoenix endpoint to pull traces back to IR.

> ELv2 only restricts offering Phoenix *as a managed service*. Self-host /
> single-tenant use is clear; embedding Phoenix UI in a multi-tenant SaaS needs
> a legal check first (tracked on [XER-61](/XER/issues/XER-61)).

## Smoke test

```bash
# Default stack up, then send one OTLP/HTTP test span:
curl -sS -o /dev/null -w "%{http_code}\n" \
  -X POST http://localhost:4318/v1/traces \
  -H 'Content-Type: application/json' \
  -d '{"resourceSpans":[{"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"kairos-smoke"}}]},"scopeSpans":[{"spans":[{"traceId":"5b8efff798038103d269b633813fc60c","spanId":"eee19b7ec3c1b174","name":"smoke","kind":1,"startTimeUnixNano":"1544712660000000000","endTimeUnixNano":"1544712661000000000"}]}]}]}'
# Expect 200. The span appears in Jaeger UI under service "kairos-smoke".
```

## Teardown

```bash
docker compose -f deploy/docker-compose.yml --profile phoenix down
```
