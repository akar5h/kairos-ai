# Kairos UI

Next.js front-end for the Kairos agent trace debugger.

## Prerequisites

- Node.js 18+
- The Kairos read API running (see below)

## Running the API

The UI talks exclusively to the Kairos read API. Start it with:

```sh
uvicorn kairos.api.app:create_app --factory --port 8000
```

(from the repo root with the correct Python environment activated)

## Running the UI

```sh
cd ui
npm install
npm run dev
```

Open [http://localhost:3000](http://localhost:3000).

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `NEXT_PUBLIC_KAIROS_API` | `http://localhost:8000` | Base URL for the Kairos read API |

Create a `.env.local` file in `ui/` to override:

```sh
NEXT_PUBLIC_KAIROS_API=http://my-kairos-api:8000
```

## Available scripts

```sh
npm run dev      # development server (hot reload)
npm run build    # production build
npm run start    # serve production build
npm run lint     # ESLint
npm run test     # vitest unit tests (one-shot)
npm run test:watch  # vitest in watch mode
```

## Architecture

```
ui/
  app/
    layout.tsx            # Root layout + nav bar
    page.tsx              # / — Traces list page (Server Component)
    not-found.tsx         # 404
    traces/[id]/page.tsx  # /traces/[id] — Trace detail (Server Component)
  components/
    TraceList.tsx         # Trace summary table
    TracesFilterBar.tsx   # since/limit URL-state controls (Client)
    ConversationView.tsx  # Chronological conversation + tool call renderer
    StepTimeline.tsx      # Compact tool-call strip with run collapsing
    TraceViewTabs.tsx     # Conversation/Timeline tab + enrich_hooks toggle (Client)
    StatusBadge.tsx       # ErrorBadge, TerminalBadge, StepStatusDot
    CopyButton.tsx        # Click-to-copy chip (Client)
  lib/
    api.ts                # Typed fetch wrappers for all /v1/* routes
    format.ts             # relativeTime, formatTokens, formatArgs, etc.
  types/
    api.ts                # TypeScript types derived from Python models
  __tests__/
    fixtures/             # Synthetic test fixtures (no real data)
    TraceList.test.tsx
    ConversationView.test.tsx
    StepTimeline.test.tsx
    format.test.ts
```

## Pages

### `/` — Traces list

Table of recent traces: trace_id (truncated, copyable), started_at (relative), span_count, error_count (red badge when > 0). Filter by `since` and `limit` via URL search params. Click a row to open the detail view.

### `/traces/[id]` — Trace detail

Two views of the same trace, switchable via tabs:

**Conversation view** — chronological interleave of all steps:
- `llm` steps: ASST blocks with model, token count, latency, truncated output
- `tool_call` steps: bordered card with tool name, args, output, error badge on failure
- `retrieval` steps: query + chunk count
- Error steps have red left-border and auto-expanded output

**Step timeline** — compact tool sequence:
- Histogram summary (tool x count, errors highlighted)
- Per-step rows: status dot, step index, tool name, args digest, latency, offset
- Consecutive runs of 3+ same-tool calls collapse into a single expandable row

**enrich_hooks toggle** — URL param `?enrich_hooks=true` passes through to the API, switching between raw OTel data and hook-enriched corrected outcomes. Demonstrates the emitter-lie fix in the UI.
