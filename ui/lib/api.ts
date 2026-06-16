/**
 * Kairos API client — all reads go through here.
 *
 * Base URL from NEXT_PUBLIC_KAIROS_API (defaults to http://localhost:8000).
 * All functions throw on non-2xx so callers can handle errors uniformly.
 */
import type {
  FindingRow,
  LabelRow,
  TraceEnvelope,
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
  enrichHooks = false,
): Promise<TraceEnvelope> {
  const qs = enrichHooks ? "?enrich_hooks=true" : "";
  return apiFetch<TraceEnvelope>(`/v1/traces/${traceId}${qs}`);
}

export async function getFindings(traceId: string): Promise<FindingRow[]> {
  return apiFetch<FindingRow[]>(`/v1/findings?trace_id=${traceId}`);
}

export async function getLabels(traceId: string): Promise<LabelRow[]> {
  return apiFetch<LabelRow[]>(`/v1/labels?trace_id=${traceId}`);
}
