/**
 * SessionTraceTable — dense table of traces within a session.
 *
 * Columns: TRACE | SPANS | ERR | STARTED | ENDED | TOOLS
 * Row click → /traces/[id]
 */
import Link from "next/link";
import type { TraceInSession } from "@/types/api";
import { CopyButton } from "@/components/CopyButton";
import { relativeTime, shortId, formatLatency, durationMs } from "@/lib/format";

interface SessionTraceTableProps {
  traces: TraceInSession[];
  sessionId: string;
}

export function SessionTraceTable({ traces }: SessionTraceTableProps) {
  if (traces.length === 0) {
    return (
      <div
        role="status"
        className="flex flex-col items-center justify-center py-20 gap-2"
        style={{ color: "var(--text-muted)" }}
      >
        <p className="text-xs">No traces in this session.</p>
      </div>
    );
  }

  return (
    <div className="overflow-x-auto">
      <table
        className="w-full border-collapse"
        style={{ fontSize: 12 }}
        role="grid"
        aria-label="Traces in session"
      >
        <thead>
          <tr
            style={{
              background: "var(--bg-surface)",
              borderBottom: "1px solid var(--bg-border)",
              position: "sticky",
              top: 0,
              zIndex: 10,
            }}
          >
            <Th>TRACE</Th>
            <Th right>SPANS</Th>
            <Th right>ERR</Th>
            <Th>STARTED</Th>
            <Th>DURATION</Th>
            <Th>TOOLS</Th>
            <Th w={8} />
          </tr>
        </thead>
        <tbody>
          {traces.map((t) => (
            <TraceRow key={t.trace_id} trace={t} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Th({
  children,
  right = false,
  w,
}: {
  children?: React.ReactNode;
  right?: boolean;
  w?: number;
}) {
  return (
    <th
      className="px-3 py-1.5 font-semibold text-left"
      style={{
        color: "var(--text-muted)",
        fontSize: 10,
        letterSpacing: "0.06em",
        textAlign: right ? "right" : "left",
        width: w,
        whiteSpace: "nowrap",
      }}
    >
      {children}
    </th>
  );
}

function TraceRow({ trace }: { trace: TraceInSession }) {
  const hasErrors = trace.error_count > 0;
  const topTools = trace.tools.slice(0, 4);
  const moreTools = trace.tools.length - topTools.length;
  const dur = durationMs(trace.started_at, trace.ended_at);

  return (
    <tr
      className="console-row"
      style={{
        background: hasErrors ? "rgba(220,38,38,0.03)" : undefined,
        cursor: "pointer",
      }}
      onMouseEnter={(e) => {
        (e.currentTarget as HTMLElement).style.background = "var(--bg-hover)";
      }}
      onMouseLeave={(e) => {
        (e.currentTarget as HTMLElement).style.background =
          hasErrors ? "rgba(220,38,38,0.03)" : "";
      }}
      onClick={() => {
        window.location.href = `/traces/${trace.trace_id}`;
      }}
    >
      {/* Trace ID */}
      <td className="px-3">
        <div className="flex items-center gap-1.5">
          <Link
            href={`/traces/${trace.trace_id}`}
            className="font-mono hover:underline"
            style={{ color: "var(--accent-blue)" }}
            title={trace.trace_id}
            onClick={(e) => e.stopPropagation()}
          >
            {shortId(trace.trace_id)}
          </Link>
          <span
            className="font-mono hidden sm:inline"
            style={{ color: "var(--text-muted)", fontSize: 11 }}
          >
            {trace.trace_id.slice(8, 20)}…
          </span>
          <span onClick={(e) => e.stopPropagation()}>
            <CopyButton text={trace.trace_id} label="Copy trace ID" />
          </span>
        </div>
      </td>

      {/* Spans */}
      <td
        className="px-3 text-right font-mono tabular-nums"
        style={{ color: "var(--text-secondary)" }}
      >
        {trace.span_count}
      </td>

      {/* Errors */}
      <td className="px-3 text-right">
        {hasErrors ? (
          <span
            className="font-mono tabular-nums rounded px-1.5"
            style={{
              background: "var(--accent-red-dim)",
              color: "var(--accent-red)",
              border: "1px solid rgba(220,38,38,0.25)",
              fontSize: 11,
            }}
            aria-label={`${trace.error_count} errors`}
          >
            {trace.error_count}
          </span>
        ) : (
          <span style={{ color: "var(--text-muted)", fontSize: 11 }}>·</span>
        )}
      </td>

      {/* Started */}
      <td className="px-3" style={{ color: "var(--text-secondary)", whiteSpace: "nowrap" }}>
        <time dateTime={trace.started_at ?? ""} title={trace.started_at ?? undefined}>
          {relativeTime(trace.started_at)}
        </time>
      </td>

      {/* Duration */}
      <td
        className="px-3 font-mono tabular-nums"
        style={{ color: "var(--text-muted)", whiteSpace: "nowrap" }}
      >
        {dur != null ? formatLatency(dur) : "—"}
      </td>

      {/* Tools */}
      <td className="px-3">
        <div className="flex flex-wrap gap-1">
          {topTools.map((t) => (
            <ToolChip key={t} name={t} />
          ))}
          {moreTools > 0 && (
            <span style={{ color: "var(--text-muted)", fontSize: 11 }}>+{moreTools}</span>
          )}
        </div>
      </td>

      {/* Arrow */}
      <td className="px-3 text-right" style={{ width: 28 }}>
        <Link
          href={`/traces/${trace.trace_id}`}
          aria-label={`View trace ${shortId(trace.trace_id)}`}
          style={{ color: "var(--text-muted)", fontSize: 12 }}
          onClick={(e) => e.stopPropagation()}
        >
          →
        </Link>
      </td>
    </tr>
  );
}

function ToolChip({ name }: { name: string }) {
  return (
    <span
      className="font-mono rounded px-1"
      style={{
        background: "var(--bg-elevated)",
        color: "var(--text-secondary)",
        border: "1px solid var(--bg-border)",
        fontSize: 10,
        whiteSpace: "nowrap",
      }}
    >
      {name}
    </span>
  );
}
