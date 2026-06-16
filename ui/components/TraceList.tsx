/**
 * TraceList — renders a table of TraceSummary rows.
 * Pure presentation; data fetched by parent (Server Component page).
 */
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
            style={{ color: "var(--text-muted)", borderBottom: "1px solid var(--bg-border)" }}
          >
            <th className="px-4 py-3 font-medium w-48">Trace ID</th>
            <th className="px-4 py-3 font-medium">Started</th>
            <th className="px-4 py-3 font-medium text-right">Spans</th>
            <th className="px-4 py-3 font-medium text-right">Errors</th>
            <th className="px-4 py-3 font-medium w-8" aria-label="Actions" />
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
      className="group transition-colors"
      style={{
        borderBottom: "1px solid var(--bg-border)",
        background: hasErrors ? "rgba(229,83,75,0.03)" : "transparent",
      }}
      onMouseEnter={(e) => {
        (e.currentTarget as HTMLTableRowElement).style.background = "var(--bg-elevated)";
      }}
      onMouseLeave={(e) => {
        (e.currentTarget as HTMLTableRowElement).style.background =
          hasErrors ? "rgba(229,83,75,0.03)" : "transparent";
      }}
    >
      {/* Trace ID */}
      <td className="px-4 py-3">
        <div className="flex items-center gap-2">
          <Link
            href={`/traces/${trace.trace_id}`}
            className="font-mono text-xs hover:underline"
            style={{ color: "var(--text-link)" }}
            title={trace.trace_id}
          >
            {shortTraceId(trace.trace_id)}
          </Link>
          <span
            className="font-mono text-xs hidden sm:inline"
            style={{ color: "var(--text-muted)" }}
          >
            {trace.trace_id.slice(8, 16)}…
          </span>
          <CopyButton text={trace.trace_id} label="Copy trace ID" />
        </div>
      </td>

      {/* Started */}
      <td className="px-4 py-3" style={{ color: "var(--text-secondary)" }}>
        <time dateTime={trace.started_at ?? ""} title={trace.started_at ?? undefined}>
          {relativeTime(trace.started_at)}
        </time>
      </td>

      {/* Span count */}
      <td className="px-4 py-3 text-right font-mono tabular-nums" style={{ color: "var(--text-secondary)" }}>
        {trace.span_count}
      </td>

      {/* Error count */}
      <td className="px-4 py-3 text-right">
        <ErrorBadge count={trace.error_count} />
      </td>

      {/* Link arrow */}
      <td className="px-4 py-3 text-right">
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
