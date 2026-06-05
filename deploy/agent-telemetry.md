# Live agent telemetry — Paperclip Claude Code agents (XER-69, Phase B)

Wires live OTel emit for Paperclip's `claude_local` agents using the Phase A
emit point (XER-68 decision: **Option B — per-engine
OTel hooks, native to Claude Code**). The agent emits standard OTel GenAI spans;
**Kairos is not imported on the agent hot path** — emit is vendor-neutral OTel,
Kairos enters only at read time (`PhoenixReader` / backend reader → IR).

```
claude_local agent (live)            always-on backend          pull (XER-70)
  claude_code.llm_request  ─OTLP─►   OTel Collector ─► Jaeger    KairosEngine
  claude_code.tool          async    (deploy/docker-compose.yml)  → reader → IR → analyze
  (BatchSpanProcessor, bg thread)
```

## Emit point: native Claude Code OTel (Option B)

Claude Code (>= 2.1.x) ships a built-in tracer (`com.anthropic.claude_code.tracing`)
that emits trace spans (`claude_code.llm_request` with `gen_ai.*` semconv attrs,
`claude_code.tool`, `claude_code.interaction`, `claude_code.hook`) when gated on
by env. No central model-gateway exists for these agents (the SDK talks directly
to `api.anthropic.com`), so emit is per-engine, enabled purely by env.

## Wiring (per agent)

Set the keys from [`paperclip-agent-otel.env.example`](paperclip-agent-otel.env.example)
into each agent's `adapterConfig.env` (Paperclip server-side). The `claude_local`
adapter merges `adapterConfig.env` into the spawned `claude` process env.

| Key | Value | Notes |
| --- | --- | --- |
| `CLAUDE_CODE_ENABLE_TELEMETRY` | `1` | turns the native tracer on |
| `OTEL_TRACES_EXPORTER` | `otlp` | traces over OTLP |
| `OTEL_METRICS_EXPORTER` | `none` | traces-only; the default deploy has no metrics pipeline |
| `OTEL_LOGS_EXPORTER` | `none` | traces-only |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `http/protobuf` | OTLP/HTTP; SDK posts to `…/v1/traces` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4318` | the collector (deploy stack) |
| `OTEL_RESOURCE_ATTRIBUTES` | `service.name=paperclip-claude-<key>,paperclip.company_id=<id>,paperclip.agent_id=<id>` | static provenance |

The 3 Xero Claude Code agents (`claude_local`): **cto**, **claudecoder**,
**qaengineer**.

## Provenance — mirrors `PaperclipNormalizer` run_context

`PaperclipNormalizer` (offline transcript path) folds these keys into trace
metadata: `run_id`, `issue`, `company_id`, `agent_id`, `project_id`. The live
path mirrors them as `paperclip.*` OTel **resource** attributes so a backend span
carries the same provenance as the offline envelope:

| run_context key | live resource attribute | when set |
| --- | --- | --- |
| `company_id` | `paperclip.company_id` | static (adapterConfig.env) |
| `agent_id` | `paperclip.agent_id` | static (adapterConfig.env) |
| `run_id` | `paperclip.run_id` | **dynamic — adapter, per run** |
| `issue` | `paperclip.issue` | **dynamic — adapter, per run** |
| `project_id` | `paperclip.project_id` | **dynamic — adapter, per run** |

### Static vs dynamic — why dynamic needs an adapter change

`adapterConfig.env` values are assigned **literally** (`env[key] = value`, no
interpolation), and Claude Code parses `OTEL_RESOURCE_ATTRIBUTES` with the
standard OTel `EnvDetector` (no `${VAR}` expansion). So per-run ids cannot be
injected by config alone.

The `claude_local` adapter already computes `runId` and the wake `taskId` at
launch (`adapter-claude-local/dist/server/execute.js`, where `env.PAPERCLIP_RUN_ID`
is set). The one-line enhancement is to **append** the dynamic ids to
`OTEL_RESOURCE_ATTRIBUTES` there, e.g.:

```js
// after env.PAPERCLIP_RUN_ID = runId; and wakeTaskId resolution:
const prov = [
  `paperclip.run_id=${runId}`,
  wakeTaskId ? `paperclip.issue=${wakeTaskId}` : null,
  agent.projectId ? `paperclip.project_id=${agent.projectId}` : null,
].filter(Boolean).join(",");
env.OTEL_RESOURCE_ATTRIBUTES = [env.OTEL_RESOURCE_ATTRIBUTES, prov]
  .filter(Boolean).join(",");
```

This is a Paperclip-repo change (cross-repo from kairos-ai) — tracked for CTO
decision on XER-69. Static provenance + telemetry
wiring above is independent and ships now.

## Non-blocking (by construction)

Claude Code exports via OTel `BatchSpanProcessor` on a background thread with
disk-queued retry. The agent thread never blocks on export; a down backend
retries then drops. **Zero added latency on the agent's critical path, zero
backpressure.** No `kairos` import on the agent hot path (grep-gated — see
`tests/deploy/test_agent_telemetry_wiring.py`).

## Verify (live run)

1. Backend up: `docker compose -f deploy/docker-compose.yml up -d`.
2. Run any one agent; it makes a model + tool call.
3. Jaeger (`http://localhost:16686`) shows service `paperclip-claude-<key>` with
   spans `claude_code.llm_request` / `claude_code.tool` carrying `paperclip.*`
   resource attributes.

Backend-equivalent check (no agent run, proves ingest + provenance round-trip):

```bash
curl -sS -X POST http://localhost:4318/v1/traces -H 'Content-Type: application/json' \
  -d '{"resourceSpans":[{"resource":{"attributes":[
        {"key":"service.name","value":{"stringValue":"paperclip-claude-claudecoder"}},
        {"key":"paperclip.company_id","value":{"stringValue":"<id>"}},
        {"key":"paperclip.agent_id","value":{"stringValue":"<id>"}}]},
      "scopeSpans":[{"spans":[{"traceId":"5b8efff798038103d269b633813fc60c",
        "spanId":"eee19b7ec3c1b174","name":"claude_code.llm_request","kind":1,
        "startTimeUnixNano":"1544712660000000000","endTimeUnixNano":"1544712661000000000"}]}]}]}'
# -> 200; the span shows in Jaeger under "paperclip-claude-claudecoder" with the paperclip.* tags.
```
