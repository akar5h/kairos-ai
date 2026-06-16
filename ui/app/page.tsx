/**
 * Traces list page — GET /v1/traces with since/limit controls.
 *
 * Server component: fetches data server-side, no client JS for the initial
 * render. Filter controls are URL searchParams (shareable state).
 */
import { Suspense } from "react";
import type { Metadata } from "next";
import Link from "next/link";
import { getTraces } from "@/lib/api";
import { TraceList } from "@/components/TraceList";
import { TracesFilterBar } from "@/components/TracesFilterBar";

export const metadata: Metadata = {
  title: "Traces — Kairos",
};

// Force dynamic rendering so `since` searchParam doesn't get cached
export const dynamic = "force-dynamic";

interface PageProps {
  searchParams: Promise<{ since?: string; limit?: string }>;
}

export default async function TracesPage({ searchParams }: PageProps) {
  const params = await searchParams;
  const limit = Math.min(Math.max(parseInt(params.limit ?? "50", 10) || 50, 1), 1000);
  const since = params.since ?? undefined;

  let traces = null;
  let error: string | null = null;

  try {
    traces = await getTraces({ since, limit });
  } catch (e) {
    error = e instanceof Error ? e.message : "Unknown error";
  }

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* Page header */}
      <div
        className="flex items-center justify-between px-6 py-4 border-b shrink-0"
        style={{ borderColor: "var(--bg-border)" }}
      >
        <div>
          <h1 className="text-base font-semibold" style={{ color: "var(--text-primary)" }}>
            Traces
          </h1>
          {traces && (
            <p className="text-xs mt-0.5" style={{ color: "var(--text-muted)" }}>
              {traces.length} trace{traces.length !== 1 ? "s" : ""}
              {since ? ` since ${since}` : ""}
            </p>
          )}
        </div>

        {/* Filter controls — client component */}
        <Suspense>
          <TracesFilterBar defaultLimit={limit} defaultSince={since ?? ""} />
        </Suspense>
      </div>

      {/* Content area */}
      <div className="flex-1 overflow-y-auto">
        {error ? (
          <ErrorState message={error} />
        ) : traces ? (
          <TraceList traces={traces} />
        ) : null}
      </div>
    </div>
  );
}

function ErrorState({ message }: { message: string }) {
  return (
    <div
      className="flex flex-col items-center justify-center py-24 gap-3"
      role="alert"
    >
      <div
        className="rounded px-3 py-2 text-sm font-mono max-w-lg text-center"
        style={{
          background: "var(--accent-red-dim)",
          color: "var(--accent-red)",
          border: "1px solid var(--accent-red)",
        }}
      >
        <p className="font-semibold mb-1">API error</p>
        <p className="text-xs">{message}</p>
        <p className="text-xs mt-2" style={{ color: "var(--text-muted)" }}>
          Is the Kairos API running?{" "}
          <code>uvicorn kairos.api.app:create_app --factory --port 8000</code>
        </p>
      </div>
      <Link
        href="/"
        className="text-xs"
        style={{ color: "var(--text-link)" }}
      >
        Retry
      </Link>
    </div>
  );
}
