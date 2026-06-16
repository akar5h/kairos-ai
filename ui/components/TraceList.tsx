/**
 * TraceList — renders a table of TraceSummary rows.
 * Pure presentation; data fetched by parent (Server Component page).
 * Light theme — uses CSS custom properties from globals.css.
 */
"use client";
import Link from "next/link";
import type { TraceSummary } from "@/types/api";
import { ErrorBadge } from "@/components/StatusBadge";
import { CopyButton } from "@/components/CopyButton";
import { relativeTime, shortTraceId } from "@/lib/format";

interface TraceListProps {
  traces: TraceSummary[];
}

export function TraceList({ traces }: TraceListProps) {
  if (traces.length === 0) {
    return (
      <div
        role="status"
        className="flex flex-col items-center justify-center py-24 gap-3"
        style={{ color: "var(--text-muted)" }}
      >
        <span className="text-3xl" aria-hidden="true">◌</span>
        <p className="text-sm">No traces found</p>
        <p className="text-xs" style={{ color: "var(--text-muted)" }}>
          Run an agent instrumented with Kairos and spans will appear here.
        </p>
      </div>
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm border-collapse" role="grid" aria-label="Trace list">
        <thead>
          <tr
            className="text-xs uppercase tracking-wider text-left"
            style={{
              color: "var(--text-muted)",
              borderBottom: "1px solid var(--bg-border)",
              background: "var(--bg-surface)",
              position: "sticky",
              top: 0,
            }}
          >
            <th className="px-4 py-2 font-semibold w-48" style={{ fontSize: 10, letterSpacing: "0.06em" }}>Trace ID</th>
            <th className="px-4 py-2 font-semibold" style={{ fontSize: 10, letterSpacing: "0.06em" }}>Started</th>
            <th className="px-4 py-2 font-semibold text-right" style={{ fontSize: 10, letterSpacing: "0.06em" }}>Spans</th>
            <th className="px-4 py-2 font-semibold text-right" style={{ fontSize: 10, letterSpacing: "0.06em" }}>Errors</th>
            <th className="px-4 py-2 font-semibold w-8" aria-label="Actions" />
          </tr>
        </thead>
        <tbody>
          {traces.map((trace) => (
            <TraceRow key={trace.trace_id} trace={trace} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TraceRow({ trace }: { trace: TraceSummary }) {
  const hasErrors = trace.error_count > 0;

  return (
    <tr
      className="group console-row"
      style={{
        background: hasErrors ? "rgba(220,38,38,0.03)" : "transparent",
      }}
      onMouseEnter={(e) => {
        (e.currentTarget as HTMLTableRowElement).style.background = "var(--bg-hover)";
      }}
      onMouseLeave={(e) => {
        (e.currentTarget as HTMLTableRowElement).style.background =
          hasErrors ? "rgba(220,38,38,0.03)" : "transparent";
      }}
    >
      {/* Trace ID */}
      <td className="px-4 py-2">
        <div className="flex items-center gap-2">
          <Link
            href={`/traces/${trace.trace_id}`}
            className="font-mono text-xs hover:underline"
            style={{ color: "var(--accent-blue)", fontSize: 12 }}
            title={trace.trace_id}
          >
            {shortTraceId(trace.trace_id)}
          </Link>
          <span
            className="font-mono text-xs hidden sm:inline"
            style={{ color: "var(--text-muted)", fontSize: 11 }}
          >
            {trace.trace_id.slice(8, 16)}…
          </span>
          <CopyButton text={trace.trace_id} label="Copy trace ID" />
        </div>
      </td>

      {/* Started */}
      <td className="px-4 py-2" style={{ color: "var(--text-secondary)", fontSize: 12 }}>
        <time dateTime={trace.started_at ?? ""} title={trace.started_at ?? undefined}>
          {relativeTime(trace.started_at)}
        </time>
      </td>

      {/* Span count */}
      <td className="px-4 py-2 text-right font-mono tabular-nums" style={{ color: "var(--text-secondary)", fontSize: 12 }}>
        {trace.span_count}
      </td>

      {/* Error count */}
      <td className="px-4 py-2 text-right">
        <ErrorBadge count={trace.error_count} />
      </td>

      {/* Link arrow */}
      <td className="px-4 py-2 text-right">
        <Link
          href={`/traces/${trace.trace_id}`}
          aria-label={`View trace ${shortTraceId(trace.trace_id)}`}
          className="text-xs transition-colors opacity-40 group-hover:opacity-100"
          style={{ color: "var(--text-link)" }}
        >
          →
        </Link>
      </td>
    </tr>
  );
}
