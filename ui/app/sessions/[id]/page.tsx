/**
 * Session detail page — GET /v1/sessions/{session_id}
 *
 * Dense table: TRACE | SPANS | ERR | STARTED | ENDED | TOOLS
 * Breadcrumb: sessions › <id>
 */
import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import { getSessionTraces } from "@/lib/api";
import { SessionTraceTable } from "@/components/SessionTraceTable";
import { shortId } from "@/lib/format";

export const dynamic = "force-dynamic";

interface PageProps {
  params: Promise<{ id: string }>;
}

export async function generateMetadata({ params }: PageProps): Promise<Metadata> {
  const { id } = await params;
  return { title: `Session ${shortId(id)} — Kairos` };
}

export default async function SessionDetailPage({ params }: PageProps) {
  const { id } = await params;

  let traces = null;
  let fetchError: string | null = null;

  try {
    traces = await getSessionTraces(id);
  } catch (e) {
    const msg = e instanceof Error ? e.message : "Unknown error";
    if (msg.startsWith("404")) notFound();
    fetchError = msg;
  }

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* Sub-header */}
      <div
        className="flex items-center gap-2 px-4 border-b shrink-0"
        style={{ borderColor: "var(--bg-border)", height: 36 }}
      >
        {/* Breadcrumb */}
        <Link
          href="/"
          className="text-xs transition-colors"
          style={{ color: "var(--text-muted)" }}
        >
          sessions
        </Link>
        <span style={{ color: "var(--bg-border)", fontSize: 12 }}>›</span>
        <span
          className="font-mono text-xs"
          style={{ color: "var(--text-primary)" }}
          title={id}
        >
          {shortId(id)}
        </span>
        <span
          className="font-mono hidden sm:inline"
          style={{ color: "var(--text-muted)", fontSize: 11 }}
        >
          {id.slice(8)}
        </span>

        {traces && (
          <span
            className="font-mono text-xs tabular-nums ml-2"
            style={{ color: "var(--text-muted)" }}
          >
            {traces.length} trace{traces.length !== 1 ? "s" : ""}
          </span>
        )}
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto">
        {fetchError ? (
          <ErrorPanel message={fetchError} />
        ) : traces ? (
          <SessionTraceTable traces={traces} sessionId={id} />
        ) : null}
      </div>
    </div>
  );
}

function ErrorPanel({ message }: { message: string }) {
  return (
    <div className="flex items-center justify-center py-20" role="alert">
      <div
        className="rounded px-3 py-2 text-xs font-mono max-w-lg text-center"
        style={{
          background: "var(--accent-red-dim)",
          color: "var(--accent-red)",
          border: "1px solid rgba(220,38,38,0.3)",
        }}
      >
        <p className="font-semibold mb-1">Failed to load session</p>
        <p>{message}</p>
      </div>
    </div>
  );
}
