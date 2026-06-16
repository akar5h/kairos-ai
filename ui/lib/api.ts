/**
 * Kairos API client — all reads go through here.
 *
 * Base URL from NEXT_PUBLIC_KAIROS_API (defaults to http://localhost:8000).
 * All functions throw on non-2xx so callers can handle errors uniformly.
 */
import type {
  ClusterSummary,
  ClusterTraceMember,
  CreateLabelBody,
  FindingRow,
  LabelRow,
  RawSpan,
  SearchHits,
  SessionSummary,
  TraceEnvelope,
  TraceInSession,
  TraceSummary,
} from "@/types/api";

const BASE =
  process.env.NEXT_PUBLIC_KAIROS_API ?? "http://localhost:8000";

async function apiFetch<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    // disable Next.js caching — these are live reads from Postgres
    cache: "no-store",
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => res.statusText);
    throw new Error(`${res.status} ${detail}`);
  }
  return res.json() as Promise<T>;
}

// ── Sessions ──────────────────────────────────────────────────────────────────

export async function getSessions(opts?: {
  q?: string;
  since?: string;
  limit?: number;
}): Promise<SessionSummary[]> {
  const params = new URLSearchParams();
  if (opts?.q) params.set("q", opts.q);
  if (opts?.since) params.set("since", opts.since);
  if (opts?.limit != null) params.set("limit", String(opts.limit));
  const qs = params.size ? `?${params.toString()}` : "";
  return apiFetch<SessionSummary[]>(`/v1/sessions${qs}`);
}

export async function getSessionTraces(sessionId: string): Promise<TraceInSession[]> {
  return apiFetch<TraceInSession[]>(`/v1/sessions/${encodeURIComponent(sessionId)}`);
}

// ── Traces ────────────────────────────────────────────────────────────────────

export async function getTraces(opts?: {
  since?: string;
  limit?: number;
}): Promise<TraceSummary[]> {
  const params = new URLSearchParams();
  if (opts?.since) params.set("since", opts.since);
  if (opts?.limit != null) params.set("limit", String(opts.limit));
  const qs = params.size ? `?${params.toString()}` : "";
  return apiFetch<TraceSummary[]>(`/v1/traces${qs}`);
}

export async function getTrace(
  traceId: string,
  enrichHooks = true,
): Promise<TraceEnvelope> {
  // Hook-truth is the default; pass enrich_hooks explicitly so the raw toggle
  // (false) overrides the API's enriched default.
  const qs = enrichHooks ? "?enrich_hooks=true" : "?enrich_hooks=false";
  return apiFetch<TraceEnvelope>(`/v1/traces/${traceId}${qs}`);
}

export async function getTraceSpans(
  traceId: string,
  full = false,
): Promise<RawSpan[]> {
  const qs = full ? "?full=true" : "";
  return apiFetch<RawSpan[]>(`/v1/traces/${traceId}/spans${qs}`);
}

// ── Clusters ──────────────────────────────────────────────────────────────────

export async function getClusters(): Promise<ClusterSummary[]> {
  return apiFetch<ClusterSummary[]>("/v1/clusters");
}

export async function getClusterTraces(
  clusterKey: string,
): Promise<ClusterTraceMember[]> {
  return apiFetch<ClusterTraceMember[]>(
    `/v1/clusters/${encodeURIComponent(clusterKey)}/traces`,
  );
}

// ── Search ────────────────────────────────────────────────────────────────────

export async function search(opts: {
  q: string;
  types?: string;
  limit?: number;
}): Promise<SearchHits> {
  const params = new URLSearchParams({ q: opts.q });
  if (opts.types) params.set("types", opts.types);
  if (opts.limit != null) params.set("limit", String(opts.limit));
  return apiFetch<SearchHits>(`/v1/search?${params.toString()}`);
}

// ── Findings / Labels ─────────────────────────────────────────────────────────

export async function getFindings(traceId: string): Promise<FindingRow[]> {
  return apiFetch<FindingRow[]>(`/v1/findings?trace_id=${traceId}`);
}

export async function getLabels(traceId: string): Promise<LabelRow[]> {
  return apiFetch<LabelRow[]>(`/v1/labels?trace_id=${traceId}`);
}

/**
 * Create a label (POST /v1/labels). Append-only — runs client-side.
 * Returns the created LabelRow (201). Throws on non-2xx.
 */
export async function createLabel(body: CreateLabelBody): Promise<LabelRow> {
  const res = await fetch(`${BASE}/v1/labels`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    cache: "no-store",
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => res.statusText);
    throw new Error(`${res.status} ${detail}`);
  }
  return res.json() as Promise<LabelRow>;
}
