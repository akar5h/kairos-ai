# scripts/ — OTel preload for Paperclip server

## Purpose

`paperclip-otel-preload.mjs` bootstraps `@opentelemetry/sdk-node` **before** any
Paperclip server code runs, using Node's `--import` flag. This gives HTTP, Express,
and Postgres spans in Phoenix without modifying the closed-source server package.

## Install Node deps

```bash
cd scripts/
npm install
```

## Start Paperclip with OTel instrumentation

```bash
NODE_OPTIONS="--import file:///$(pwd)/scripts/paperclip-otel-preload.mjs" npx paperclipai server start
```

Run from the repo root so `$(pwd)` resolves correctly.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4318` | OTLP HTTP collector endpoint |

The collector should forward traces to Phoenix. See the OTel Collector config in
`deploy/` for the Phoenix exporter pipeline.

## Failure behaviour

If OTel SDK init fails (e.g. collector unreachable at startup), the error is logged
to `stderr` and the Paperclip server starts normally — instrumentation is best-effort.
