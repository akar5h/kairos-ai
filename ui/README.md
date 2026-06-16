# Kairos UI — Light Dense Console

A production-quality observability console for Kairos agent traces. Light theme, dense data-console aesthetic — modeled on Honeycomb/Datadog light, not an airy SaaS product.

## Quick Start

```bash
cd ui
npm install
NEXT_PUBLIC_KAIROS_API=http://localhost:8000 npm run dev
```

Open [http://localhost:3000](http://localhost:3000).

## Environment

| Variable | Default | Description |
|---|---|---|
| `NEXT_PUBLIC_KAIROS_API` | `http://localhost:8000` | Kairos API base URL (CORS must be enabled on API) |

## 3-Level Hierarchy

```
Sessions  /                       → GET /v1/sessions
  └─ Session  /sessions/[id]      → GET /v1/sessions/{id}
       └─ Trace  /traces/[id]     → GET /v1/traces/{id} + /v1/traces/{id}/spans
```

### Sessions view (`/`)
Dense table: `SESSION | TRACES | SPANS | ERR | STARTED | TOOLS`. Expandable rows (▸) peek the session's traces inline without navigation. Global search bar in the header queries `/v1/search`.

### Session view (`/sessions/[id]`)
Breadcrumb `sessions › <id>`. Dense trace table: `TRACE | SPANS | ERR | STARTED | DURATION | TOOLS`. Row click navigates to Trace view.

### Trace view (`/traces/[id]`)
Breadcrumb `sessions › <session_id> › <trace_id>`. Three tabs:
- **Raw Spans** (default): indent-by-parent span tree from `/v1/traces/{id}/spans`. Each row: expand ▸ | name | tool | status | duration | span_id. Expandable to see attributes.
- **Conversation**: LLM/tool/retrieval steps in chronological order (from `/v1/traces/{id}`).
- **Step Timeline**: tool histogram + collapsed run strip.

`enrich_hooks` toggle swaps the TraceEnvelope between raw OTel and hook-enriched data.

## Design System

**Palette**: White base (`#fff`), near-white surface (`#fafafa`), light gray elevated (`#f4f4f5`), hairline borders (`#e4e4e7`). Semantic accents only: blue (`#2563eb`) for links/ids, red (`#dc2626`) for errors, green (`#16a34a`) for ok, amber for warnings. No dark mode, no gradients.

**Typography**: Geist Sans for prose at 13px base. Geist Mono (`ui-monospace`) for all ids, metrics, tool names, status codes. Tabular numerics throughout.

**Density**: ~34px table rows (`console-row` CSS class), 12px table font, sticky column headers. Lots visible at once without scrolling.

## Development

```bash
npm run dev         # development server with hot reload
npm run build       # production build (must pass before commit)
npm run lint        # eslint (must be clean before commit)
npm run test        # vitest run (all tests)
npm run test:watch  # vitest watch mode
```

## API Contract

All types in `types/api.ts` are derived from `src/kairos/api/read.py` Pydantic models. Field names match exactly:

| Model | Key fields |
|---|---|
| `SessionSummary` | `session_id, trace_count, span_count, error_count, started_at, ended_at, tools` |
| `TraceInSession` | `trace_id, span_count, error_count, started_at, ended_at, tools` |
| `RawSpan` | `span_id, parent_span_id, name, tool_name, status_code, start_time, end_time, attributes` |
| `SearchHits` | `sessions[], traces[], spans[]` |

## File Structure

```
ui/
  app/
    layout.tsx              # Root layout — sticky header + SearchBar
    page.tsx                # Sessions home view
    sessions/[id]/page.tsx  # Session detail (traces in session)
    traces/[id]/page.tsx    # Trace detail (raw spans + conversation + timeline)
    globals.css             # Light palette CSS custom properties
  components/
    SearchBar.tsx           # Global search — queries /v1/search, grouped dropdown
    SessionTable.tsx        # Dense sessions table with inline expand
    SessionsFilterBar.tsx   # q/since/limit filter controls
    SessionTraceTable.tsx   # Dense traces-in-session table
    RawSpansTree.tsx        # Span tree indent renderer + attribute expander
    TraceViewTabs.tsx       # Raw Spans | Conversation | Step Timeline tab switcher
    ConversationView.tsx    # Step-by-step conversation renderer
    StepTimeline.tsx        # Tool histogram + collapsed timeline strip
    StatusBadge.tsx         # ErrorBadge, TerminalBadge, StepStatusDot
    CopyButton.tsx          # Click-to-copy button
    TraceList.tsx           # Trace table (used in legacy traces view)
    TracesFilterBar.tsx     # Legacy since/limit controls
  lib/
    api.ts                  # Typed API client (getSessions, getSessionTraces, getTraceSpans, search…)
    format.ts               # relativeTime, formatLatency, durationMs, shortId, shortTraceId…
  types/
    api.ts                  # TypeScript interfaces matching Python Pydantic models
  __tests__/
    fixtures/trace-envelope.ts      # Synthetic fixtures (sessions, traces, spans — no real data)
    SessionTable.test.tsx
    SessionTraceTable.test.tsx
    RawSpansTree.test.tsx
    ConversationView.test.tsx
    StepTimeline.test.tsx
    TraceList.test.tsx
    format.test.ts
```
