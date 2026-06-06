# Telemetry Span Audit — OTel→Phoenix Data Fidelity

**Issue:** XER-106 | **Date:** 2026-06-06 | **Author:** CTO (audit only, no code changes)
**Emit path:** Claude Code native OTel (`CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1`) → adapter env injection → OTLP collector (port 4318) → Phoenix exporter (`otlphttp/phoenix`) → Phoenix UI

---

## As-Is View (from board screenshots on XER-61)

| Phoenix metric | Observed value | Expected |
|---|---|---|
| Total Traces | 134 | ✓ traces flowing |
| Total Tokens | **0** | should show actual token counts |
| Span kind | **"unknown"** for all | should be LLM / TOOL / RETRIEVER |
| Input / Output columns | **"–"** (empty) | should show prompt/response snippets |
| Span Status | **UNSET** | OK / ERROR |
| Trace structure (detail view) | **nested parent→child tree** ✓ | ✓ correct |

---

## Part 1 — Per-Span-Type Data-Fidelity Audit

### Span type 1: `claude_code.interaction` (Trace root / task boundary)

**What it is:** One per agent run. The interaction root that bookends the trace. Set by CC tracer with `span.type = "interaction"`.

| Attribute | Present? | Value seen | Gap |
|---|---|---|---|
| `span.type` | ✓ | `"interaction"` | — |
| `user_prompt` | ✓ when `OTEL_LOG_USER_PROMPTS=1` | prompt text | — |
| `openinference.span.kind` | ✗ | absent | Phoenix cannot classify → shown as "unknown" or absent from Traces list |
| `gen_ai.system` | ✗ | absent | — |
| Span status | ✗ | UNSET | No OK set on success |

**Finding:** This span IS emitted and IS the trace root. Kairos `genai_mapping.classify_span()` correctly identifies it via `span.type == "interaction"` (line 238). But Phoenix doesn't receive `openinference.span.kind = "CHAIN"` so it can't surface this as a structured trace root in its UI — the trace list may show child spans as top-level entries.

---

### Span type 2: `claude_code.llm_request` (LLM API call)

**What it is:** One per API call to Anthropic. The core span. Spans in the list view labeled `claude_code.llm_request`.

| Attribute | Present? | Value seen | Gap |
|---|---|---|---|
| `gen_ai.system` | ✓ | `"anthropic"` | — |
| `gen_ai.request.model` | ✓ | e.g. `"claude-sonnet-4-6"` | — |
| `gen_ai.usage.input_tokens` | ✓ emitted by CC | integer | **Phoenix ignores** — no `openinference.span.kind = "LLM"` set, so Phoenix's LLM aggregation skips this span → Token column = 0 |
| `gen_ai.usage.output_tokens` | ✓ emitted by CC | integer | same issue |
| `llm.token_count.prompt` | ✗ | absent | OpenInference format not used |
| `llm.input_messages.*` | ✗ | absent | CC puts content in `input.value`, not OI message arrays → Phoenix "Input" column = "–" |
| `llm.output_messages.*` | ✗ | absent | CC puts content in `output.value` → Phoenix "Output" column = "–" |
| `openinference.span.kind` | ✗ | absent | **Root cause of 0 tokens in Phoenix** — Phoenix requires this = "LLM" to classify and aggregate |
| Span status | ✗ | UNSET | No OK on success; ERROR set on failure |

**Finding:** Token counts ARE emitted by Claude Code in the correct OTel genai convention keys (`gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`). Kairos `genai_mapping.span_to_llm_call()` reads these correctly (lines 397–407). The bug is that Phoenix requires `openinference.span.kind = "LLM"` to recognize a span as an LLM span and populate its token aggregation UI. Without this attribute, Phoenix treats the span as "unknown" and the Total Tokens header stays 0. Message content doesn't surface because CC uses `input.value` / `output.value` flat keys, not the `llm.input_messages.{i}.message.{role,content}` OpenInference array format.

---

### Span type 3: `claude_code.tool` (Tool execution group)

**What it is:** One per tool invocation. Parent span wrapping `tool.blocked_on_user` + `tool.execution` sub-spans.

| Attribute | Present? | Value seen | Gap |
|---|---|---|---|
| `span.type` | ✓ | `"tool"` | — |
| `tool_name` | ✓ | e.g. `"Bash"`, `"Read"` | — |
| `input.value` | ✓ | tool args (JSON string) | Kairos maps this; Phoenix shows "–" (requires OI format) |
| `output.value` | ✓ | tool result | Same — Phoenix shows "–" |
| `openinference.span.kind` | ✗ | absent | Phoenix shows "unknown"; Kairos still classifies via `span.type == "tool"` |
| Span status | ✗ | UNSET | |

**Finding:** Kairos reader maps tool name via `attrs.get("tool_name")` (line 452) — correct. Phoenix shows "unknown" kind and empty input/output because it doesn't receive `openinference.span.kind = "TOOL"` and doesn't parse CC's flat `input.value` format.

---

### Span type 4: `claude_code.tool.execution` (Actual tool run sub-span)

**What it is:** Child of `claude_code.tool`. Represents the actual subprocess/function execution time.

| Attribute | Present? | Value seen | Gap |
|---|---|---|---|
| `span.type` | ✓ | `"tool.execution"` | — |
| `openinference.span.kind` | ✗ | absent | "unknown" in Phoenix |
| Span status | ✗ | UNSET | — |
| Token data | N/A | — | Tool sub-span; tokens not applicable |

**Finding:** Kairos `classify_span()` returns "other" for `span.type == "tool.execution"` — this is intentional (only the parent `claude_code.tool` is the event-level tool call). This sub-span is infrastructure detail. It passes through to Phoenix but Kairos doesn't promote it to a ToolCall event.

---

### Span type 5: `claude_code.tool.blocked_on_user` (User-wait sub-span)

**What it is:** Child of `claude_code.tool`. Represents time CC spent waiting for user/board input (Paperclip heartbeat pause).

| Attribute | Present? | Value seen | Gap |
|---|---|---|---|
| `span.type` | varies | `"tool.blocked_on_user"` | — |
| Duration | ✓ | e.g. 0.03s–0.06s | — |
| `openinference.span.kind` | ✗ | absent | "unknown" |
| Kairos classification | "other" | correctly skipped | — |

**Finding:** Correctly skipped by Kairos. Useful in Phoenix for understanding how long an agent was idle waiting for a human turn. No data gap here per se — gap is just the absent span kind.

---

### Span type 6: Resource attributes (trace-level metadata)

**What's emitted via `OTEL_RESOURCE_ATTRIBUTES`:**

| Attribute | Value | Phoenix shows |
|---|---|---|
| `service.name` | `paperclip-claude-<agent-urlKey>` | ✓ visible in attributes panel |
| `paperclip.company_id` | UUID | ✓ |
| `paperclip.agent_id` | UUID | ✓ |
| `paperclip.run_id` | UUID | ✓ |
| `paperclip.issue` | issue UUID | ✓ |
| `paperclip.project_id` | UUID (if set) | ✓ |

**Finding:** Resource attributes flow correctly. Screenshot 1 confirms the full `paperclip` JSON object is visible in the attributes panel. These enable Kairos to key traces to Paperclip run/issue provenance. **Gap:** `paperclip.run_id` appears twice in `OTEL_RESOURCE_ATTRIBUTES` due to the XER-75/76 A2 stopgap append (execute.js lines 186–191 duplicate what lines 124–131 already set). Low priority — Phoenix deduplates, no functional harm.

---

### Span type 7: Token counts (data-fidelity deep dive)

**Attribute keys Claude Code emits:**

| CC attribute key | Kairos genai_mapping reads? | Phoenix reads? |
|---|---|---|
| `gen_ai.usage.input_tokens` | ✓ line 399 | ✗ — needs span.kind=LLM |
| `gen_ai.usage.output_tokens` | ✓ line 403 | ✗ — same |
| `gen_ai.usage.total_tokens` | ✓ line 407 | ✗ — same |

**Root cause of Phoenix Total Tokens = 0:**

Phoenix aggregates tokens only from spans where `openinference.span.kind == "LLM"`. Because CC spans have no `openinference.span.kind` attribute, Phoenix's span classifier returns "unknown" → token columns and project-level Total Tokens header stay 0.

The token values ARE in the spans. They are NOT lost in the collector pipeline. They ARE read by Kairos correctly. The gap is Phoenix's display layer requirement.

**Cache tokens:** Kairos `ClaudeCodeNormalizer._input_tokens()` sums `input_tokens + cache_read_input_tokens + cache_creation_input_tokens` from the JSONL transcript (not from OTel spans). The live OTel path emits `gen_ai.usage.input_tokens` (which CC may or may not include cache in — unclear from CC beta docs). Verify: if Phoenix showed tokens, the count might not match Kairos's JSONL-derived count due to cache token handling.

---

### Span type 8: Span status (OK/ERROR)

**Finding:** Claude Code's OTel instrumentation follows the OTel spec strictly: status defaults to UNSET and only transitions to ERROR on exception. It never sets OK on success. This is technically correct per spec (OpenTelemetry SDK sets OK via `span.setStatus({code: SpanStatusCode.OK})` — CC doesn't call this). Phoenix displays the raw status, so all successful spans show "UNSET".

**Impact:** Kairos `_step_status()` at genai_mapping.py line 145 returns `StepStatus.ERROR` only when `code == StatusCode.ERROR`, defaulting to `StepStatus.OK` otherwise. So Kairos's internal model correctly treats UNSET as success. Phoenix's UI just doesn't show it visually.

---

### Span type 9: Trace context / parent-child propagation

**Finding: trace structure IS correct.** Screenshot 1 (Trace Details view) confirms parent→child nesting:

```
claude_code.interaction  [root — may not render in Phoenix list without OI kind]
  └── claude_code.tool  [1:23s]
        ├── claude_code.tool.blocked_on_user  [0.03s]
        ├── claude_code.tool.execution  [1.20s]
        └── claude_code.llm_request  [13.54s]
  └── claude_code.tool  [7.44s]
        └── ...
```

`parent_span_id` is correctly propagated by the CC tracer. All spans in a run share the same `trace_id`. The board's impression that spans are "individual disconnected" was from the **Spans list tab** (shows each span individually) vs the **Trace detail view** (shows the tree). The spans are NOT orphaned — they belong to a proper trace hierarchy.

**Gap:** The `claude_code.interaction` span may not surface as the visible root in Phoenix's trace list because Phoenix uses `openinference.span.kind` to identify roots. Without it, Phoenix may show `claude_code.tool` spans as apparent top-level entries when drilling into a trace.

---

### Span type 10: Missing / addable high-value attributes

What would make traces immediately more useful in Phoenix, cheaply:

| Attribute | Status | Value if added |
|---|---|---|
| `openinference.span.kind` | **Missing — highest priority** | Unlocks Phoenix's LLM/TOOL/RETRIEVER views, token aggregation, model analytics |
| `llm.model_name` | Missing (CC uses `gen_ai.request.model`) | Phoenix prefers OI key |
| `llm.token_count.prompt` | Missing | OI key for input tokens — Phoenix reads this |
| `llm.token_count.completion` | Missing | OI key for output tokens |
| `llm.input_messages.{i}.message.{role,content}` | Missing | Phoenix Input column |
| `llm.output_messages.0.message.content` | Missing | Phoenix Output column |
| `gen_ai.request.max_tokens` | Missing | Model config context |
| `gen_ai.response.finish_reason` | Missing | Understand stop reason |

---

## Part 2 — Why Only LLM-Call Spans Are Visible (No Paperclip Internal-Op Spans)

### Actual emit path

```
Paperclip server (Node.js)
  → spawns: claude_local adapter subprocess
    → spawns: claude CLI process
      → CC native OTel SDK emits spans
        → OTLP HTTP to localhost:4318
          → OTel Collector (kairos collector)
            → Jaeger + Phoenix
```

### Why Paperclip internal ops don't appear

**Paperclip is not instrumented with OTel.** The Paperclip Node.js server has no `@opentelemetry/sdk-node` or equivalent. Its internal operations — issue routing, comment fetching, tool orchestration, API handler dispatch, DB queries — emit zero OTel spans.

Only the `claude` CLI subprocess emits spans, via the CC native tracer (`com.anthropic.claude_code.tracing` instrumentation library). Those spans are the LLM calls, tool executions, and interaction roots visible in Phoenix.

**What Paperclip internal spans would look like if instrumented:**

```
paperclip.heartbeat_run [root]
  ├── paperclip.issue.checkout  [API call duration]
  ├── paperclip.issue.fetch_context  [API call]
  ├── paperclip.agent.execute  [wraps the claude subprocess]
  │     └── claude_code.interaction  [CC native — already flowing]
  │           ├── claude_code.tool
  │           └── claude_code.llm_request
  ├── paperclip.issue.comment  [API call — post result]
  └── paperclip.issue.update_status  [API call — done/blocked]
```

### The layer gap

| Layer | Instrumented? | Spans in Phoenix? |
|---|---|---|
| Paperclip HTTP server | ✗ | ✗ |
| Paperclip issue/agent orchestration | ✗ | ✗ |
| `adapter-claude-local` (execute.js) | ✗ (injects env, doesn't self-instrument) | ✗ |
| Claude CLI subprocess | ✓ (CC native OTel) | ✓ |
| Anthropic API call | ✓ (wrapped in `claude_code.llm_request`) | ✓ |

### What would it take to surface Paperclip spans?

Two options, non-exclusive:

**Option A — Instrument Paperclip server (full visibility)**
Add `@opentelemetry/sdk-node` to the Paperclip server. Instrument HTTP handlers, DB calls, agent dispatch. Set `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318` in the server process. This gives the complete "Paperclip heartbeat + Claude execution" trace. Requires code changes to the Paperclip server codebase (out of scope for kairos-ai).

**Option B — Synthetic Paperclip spans from the adapter (stopgap)**
The `adapter-claude-local` execute.js already injects env vars. It could also create a parent span using the OTel JS SDK before spawning the claude subprocess, then pass the span context via `OTEL_PROPAGATORS` env so CC's spans become children. This requires a small code change in execute.js and adding `@opentelemetry/sdk-node` as a dependency of the adapter package.

**Option C — Derive Paperclip ops from CC's existing spans (Kairos analysis layer)**
Kairos already synthesizes `TraceStart` / `TraceEnd` from the `claude_code.interaction` span. Paperclip run metadata (run_id, issue_id, etc.) flows via resource attributes. The analysis layer can infer Paperclip-level operations from the existing trace shape. No new spans needed — this is a Kairos analysis capability, not an observability gap.

---

## Summary Table — All 10 Span Findings

| # | Span / dimension | Flows to Phoenix? | Gap | Severity |
|---|---|---|---|---|
| 1 | `claude_code.interaction` | ✓ | No `openinference.span.kind=CHAIN` → invisible as trace root in Phoenix | Medium |
| 2 | `claude_code.llm_request` — tokens | ✓ emitted | `openinference.span.kind` absent → Phoenix Total Tokens = 0 | **High** |
| 3 | `claude_code.tool` — input/output | ✓ emitted | CC uses `input.value`/`output.value`, not OI message format → Phoenix shows "–" | Medium |
| 4 | `claude_code.tool.execution` | ✓ | Kairos skips (infrastructure sub-span); Phoenix shows "unknown" kind | Low |
| 5 | `claude_code.tool.blocked_on_user` | ✓ | Kairos skips; Phoenix "unknown" | Low |
| 6 | Resource attributes (paperclip.*) | ✓ | `run_id` duplicated in OTel_RESOURCE_ATTRIBUTES (A2 stopgap) | Low |
| 7 | Token counts | ✓ in spans | Phoenix ignores without span kind; Kairos reads correctly | **High** |
| 8 | Span status | UNSET | CC never sets OK; Phoenix shows "UNSET" for all success spans | Medium |
| 9 | Trace context / parent-child | ✓ | None — hierarchy IS correct; board was viewing Spans tab | None |
| 10 | Paperclip internal ops | ✗ | Paperclip server not instrumented — zero Paperclip spans emitted | **High (visibility gap)** |

---

## Recommendations (no code changes in this issue — file as follow-ups)

### R1 — Add `openinference.span.kind` via OTel Collector transform processor [HIGH]

**Where:** OTel collector config (`deploy/otel-collector-config.yaml`) — add a `transform` processor before the Phoenix exporter.

**What:** Map CC span names/attributes to OpenInference span kinds:
- span name contains `llm_request` OR `gen_ai.system` present → add `openinference.span.kind = "LLM"`
- `span.type == "tool"` → add `openinference.span.kind = "TOOL"`
- `span.type == "interaction"` → add `openinference.span.kind = "CHAIN"`

**Effect:** Phoenix immediately shows correct token totals, model analytics, LLM/TOOL classification. Kairos unaffected (doesn't use `openinference.span.kind` as primary classifier). No changes to CC or adapter.

**Reference:** `otelcol-contrib` `transform` processor, `ottl` (OpenTelemetry Transformation Language).

---

### R2 — Add `openinference.span.kind` + OI token keys via collector transform [HIGH]

Extend R1: also add `llm.token_count.prompt` / `llm.token_count.completion` / `llm.model_name` from the `gen_ai.*` keys that CC already emits. This makes Phoenix's Input/token columns work without touching the CC or adapter code.

---

### R3 — Set span status OK on success in CC spans [MEDIUM]

**Where:** This requires a change inside Claude Code itself (Anthropic upstream), or a collector transform that sets status=OK when status=UNSET and no error event exists.

Collector transform approach is feasible:
```yaml
# transform processor rule
- set(status.code, STATUS_CODE_OK) where status.code == STATUS_CODE_UNSET and status.message == ""
```

---

### R4 — Add `llm.input_messages.*` / `llm.output_messages.*` via collector [MEDIUM]

CC emits prompts in `input.value` (flat string). A collector transform could copy `input.value` → `llm.input_messages.0.message.content` (role=user) and `output.value` → `llm.output_messages.0.message.content`. This surfaces prompts in Phoenix's Input/Output columns.

---

### R5 — Instrument Paperclip server with OTel Node.js SDK [HIGH — separate initiative]

**Where:** Paperclip server codebase (not kairos-ai). Requires:
1. `npm install @opentelemetry/sdk-node @opentelemetry/auto-instrumentations-node`
2. Bootstrap OTel at server startup
3. Set `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318`
4. The adapter's claude subprocess already injects matching `OTEL_EXPORTER_OTLP_ENDPOINT` — with W3C trace context propagation, CC spans will automatically become children of Paperclip spans

**Effect:** Full "Paperclip heartbeat" traces visible in Phoenix — issue checkout, API calls, agent dispatch, result post — with CC's LLM/tool spans as children. The complete operational picture.

---

### R6 — Deduplicate `paperclip.run_id` in OTEL_RESOURCE_ATTRIBUTES [LOW]

The execute.js A2 stopgap (lines 186–191) appends `paperclip.run_id` again even though lines 124–131 already include it. Remove the duplicate in the XER-75/76 follow-up cleanup.

---

## Collector pipeline (current state)

```yaml
# deploy/otel-collector-config.yaml
service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch]       # ← only batch; no transform/attribute enrichment
      exporters: [otlp/jaeger, otlphttp/phoenix, debug]
```

The `batch` processor does not add or modify attributes. R1–R4 all require adding a `transform` processor here, or a separate pipeline for Phoenix with `transform` applied only to that export path.

---

*Audit complete. No code changes made. Follow-up issues for R1–R6 recommended for CEO routing.*
