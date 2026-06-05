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

## Delivery mechanism — adapter-side env injection (NOT `adapterConfig.env`)

> **Verified defect (XER-69):** API-set `adapterConfig.env` does **not** reach a
> `claude_local` agent's process. The API stores env values as `{type:"plain",
> value}` binding objects; the adapter's env merge
> (`adapter-utils … rewriteWorkspaceCwdEnvVarsForExecution`) filters to
> `typeof === "string"` only, and the server's `toPlainEnvValue` resolver runs
> only in secret tooling — never the spawn path. So binding-object env is
> silently dropped. Confirmed empirically: a configured agent's process carries
> no `OTEL_*` vars and emits zero spans.

Therefore the telemetry env block below must be injected **by the `claude_local`
adapter** (`adapter-claude-local/dist/server/execute.js`, where the spawn `env`
is built and `env.PAPERCLIP_RUN_ID` is set), not via `adapterConfig.env`. The
adapter owns the full block — static constants + per-run provenance — in one
place. Tracked for the platform/board in XER-76.

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

All are set by the adapter on the spawn `env` (see below); static keys are
constants/agent fields, per-run keys come from the run context.

| run_context key | live resource attribute | source |
| --- | --- | --- |
| `company_id` | `paperclip.company_id` | `agent.companyId` (static) |
| `agent_id` | `paperclip.agent_id` | `agent.id` (static) |
| `run_id` | `paperclip.run_id` | `runId` (per run) |
| `issue` | `paperclip.issue` | `wakeTaskId` (per run) |
| `project_id` | `paperclip.project_id` | `agent.projectId` (per run) |

### The adapter sets the whole block (static + dynamic)

Because `adapterConfig.env` is inert for `claude_local` (above), the adapter must
set every key directly on the spawn `env`. Static keys are constants / agent
fields; per-run ids come from the run context already in scope. Insert after
`env.PAPERCLIP_RUN_ID = runId;` and `wakeTaskId` resolution in
`adapter-claude-local/dist/server/execute.js`:

```js
// XER-69 live OTel emit — claude_local owns the telemetry env (adapterConfig.env
// is dropped by the string-only env filter, so it cannot deliver this).
env.CLAUDE_CODE_ENABLE_TELEMETRY = "1";
env.OTEL_TRACES_EXPORTER = "otlp";
env.OTEL_METRICS_EXPORTER = "none";
env.OTEL_LOGS_EXPORTER = "none";
env.OTEL_EXPORTER_OTLP_PROTOCOL = "http/protobuf";
env.OTEL_EXPORTER_OTLP_ENDPOINT = env.OTEL_EXPORTER_OTLP_ENDPOINT || "http://localhost:4318";
env.OTEL_RESOURCE_ATTRIBUTES = [
  `service.name=paperclip-claude-${agent.urlKey ?? agent.id}`,
  `paperclip.company_id=${agent.companyId}`,
  `paperclip.agent_id=${agent.id}`,
  `paperclip.run_id=${runId}`,
  wakeTaskId ? `paperclip.issue=${wakeTaskId}` : null,
  agent.projectId ? `paperclip.project_id=${agent.projectId}` : null,
].filter(Boolean).join(",");
```

This is a Paperclip-repo change (cross-repo from kairos-ai). Alternative durable
fix: make the server resolve `adapterConfig.env` bindings to strings before spawn
(apply `toPlainEnvValue` in the run path) — then `adapterConfig.env` works
generally and the env example below applies as written. Tracked in XER-76
(platform/board). Claude Code parses `OTEL_RESOURCE_ATTRIBUTES` with the standard
OTel `EnvDetector` (literal, no `${VAR}` expansion), which is why the per-run ids
must be interpolated in JS as above rather than via a config template.

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
