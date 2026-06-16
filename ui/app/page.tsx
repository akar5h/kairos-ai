/**
 * Sessions list page (home /) — dense light console table.
 *
 * GET /v1/sessions?q=&since=&limit=
 * Columns: SESSION | TRACES | SPANS | ERR | STARTED | TOOLS
 */
import type { Metadata } from "next";
import { Suspense } from "react";
import { getSessions } from "@/lib/api";
import { SessionTable } from "@/components/SessionTable";
import { SessionsFilterBar } from "@/components/SessionsFilterBar";

export const metadata: Metadata = {
  title: "Sessions — Kairos",
};

export const dynamic = "force-dynamic";

interface PageProps {
  searchParams: Promise<{ q?: string; since?: string; limit?: string }>;
}

export default async function SessionsPage({ searchParams }: PageProps) {
  const params = await searchParams;
  const limit = Math.min(Math.max(parseInt(params.limit ?? "100", 10) || 100, 1), 1000);
  const since = params.since ?? undefined;
  const q = params.q ?? undefined;

  let sessions = null;
  let error: string | null = null;

  try {
    sessions = await getSessions({ q, since, limit });
  } catch (e) {
    error = e instanceof Error ? e.message : "Unknown error";
  }

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* Sub-header */}
      <div
        className="flex items-center justify-between px-4 border-b shrink-0"
        style={{ borderColor: "var(--bg-border)", height: 36 }}
      >
        <div className="flex items-center gap-3">
          <span
            className="text-xs font-semibold"
            style={{ color: "var(--text-primary)" }}
          >
            Sessions
          </span>
          {sessions && (
            <span
              className="font-mono text-xs tabular-nums"
              style={{ color: "var(--text-muted)" }}
            >
              {sessions.length}{limit > 0 && sessions.length === limit ? "+" : ""} rows
            </span>
          )}
        </div>
        <Suspense>
          <SessionsFilterBar defaultLimit={limit} defaultSince={since ?? ""} defaultQ={q ?? ""} />
        </Suspense>
      </div>

      {/* Table */}
      <div className="flex-1 overflow-y-auto">
        {error ? (
          <ErrorState message={error} />
        ) : sessions ? (
          <SessionTable sessions={sessions} />
        ) : null}
      </div>
    </div>
  );
}

function ErrorState({ message }: { message: string }) {
  return (
    <div
      className="flex flex-col items-center justify-center py-20 gap-2"
      role="alert"
    >
      <div
        className="rounded px-3 py-2 text-xs font-mono max-w-lg text-center"
        style={{
          background: "var(--accent-red-dim)",
          color: "var(--accent-red)",
          border: "1px solid rgba(220,38,38,0.3)",
        }}
      >
        <p className="font-semibold mb-1">API error</p>
        <p>{message}</p>
        <p className="mt-1" style={{ color: "var(--text-muted)" }}>
          Is the Kairos API running?{" "}
          <code>uvicorn kairos.api.app:create_app --factory --port 8000</code>
        </p>
      </div>
    </div>
  );
}
