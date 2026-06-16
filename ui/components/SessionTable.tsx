"use client";

/**
 * SessionTable — dense light console table for the Sessions home view.
 *
 * Columns: SESSION | TRACES | SPANS | ERR | STARTED | TOOLS
 * Row click → /sessions/[id].
 * Expandable row via ▸ to peek traces inline (bonus feature).
 */
import { useState } from "react";
import Link from "next/link";
import type { SessionSummary } from "@/types/api";
import { CopyButton } from "@/components/CopyButton";
import { relativeTime, shortId } from "@/lib/format";

interface SessionTableProps {
  sessions: SessionSummary[];
}

export function SessionTable({ sessions }: SessionTableProps) {
  if (sessions.length === 0) {
    return (
      <div
        role="status"
        className="flex flex-col items-center justify-center py-20 gap-2"
        style={{ color: "var(--text-muted)" }}
      >
        <span className="text-2xl" aria-hidden="true">◌</span>
        <p className="text-xs">No sessions found</p>
        <p className="text-xs" style={{ color: "var(--text-muted)" }}>
          Run an agent instrumented with Kairos and sessions will appear here.
        </p>
      </div>
    );
  }

  return (
    <div className="overflow-x-auto">
      <table
        className="w-full border-collapse"
        style={{ fontSize: 12 }}
        role="grid"
        aria-label="Session list"
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
            <Th w={24} />
            <Th>SESSION</Th>
            <Th right>TRACES</Th>
            <Th right>SPANS</Th>
            <Th right>ERR</Th>
            <Th>STARTED</Th>
            <Th>TOOLS</Th>
            <Th w={8} />
          </tr>
        </thead>
        <tbody>
          {sessions.map((s) => (
            <SessionRow key={s.session_id} session={s} />
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

function SessionRow({ session }: { session: SessionSummary }) {
  const [expanded, setExpanded] = useState(false);
  const hasErrors = session.error_count > 0;
  const topTools = session.tools.slice(0, 4);
  const moreTools = session.tools.length - topTools.length;

  return (
    <>
      <tr
        className="console-row cursor-pointer select-none"
        style={{
          background: hasErrors ? "rgba(220,38,38,0.03)" : undefined,
        }}
        onClick={() => setExpanded((v) => !v)}
        onMouseEnter={(e) => {
          (e.currentTarget as HTMLElement).style.background = "var(--bg-hover)";
        }}
        onMouseLeave={(e) => {
          (e.currentTarget as HTMLElement).style.background =
            hasErrors ? "rgba(220,38,38,0.03)" : "";
        }}
        aria-expanded={expanded}
      >
        {/* Expand toggle */}
        <td
          className="px-3 text-center"
          style={{ color: "var(--text-muted)", width: 24, fontSize: 11 }}
        >
          <span
            style={{
              display: "inline-block",
              transition: "transform 0.1s",
              transform: expanded ? "rotate(90deg)" : "none",
            }}
          >
            ▸
          </span>
        </td>

        {/* Session ID */}
        <td className="px-3">
          <div className="flex items-center gap-1.5">
            <Link
              href={`/sessions/${encodeURIComponent(session.session_id)}`}
              className="font-mono hover:underline"
              style={{ color: "var(--accent-blue)", fontSize: 12 }}
              title={session.session_id}
              onClick={(e) => e.stopPropagation()}
            >
              {shortId(session.session_id)}
            </Link>
            <span
              className="font-mono hidden sm:inline"
              style={{ color: "var(--text-muted)", fontSize: 11 }}
            >
              {session.session_id.slice(8, 20)}…
            </span>
            <span onClick={(e) => e.stopPropagation()}>
              <CopyButton text={session.session_id} label="Copy session ID" />
            </span>
          </div>
        </td>

        {/* Traces */}
        <td
          className="px-3 text-right font-mono tabular-nums"
          style={{ color: "var(--text-secondary)" }}
        >
          {session.trace_count}
        </td>

        {/* Spans */}
        <td
          className="px-3 text-right font-mono tabular-nums"
          style={{ color: "var(--text-secondary)" }}
        >
          {session.span_count}
        </td>

        {/* Errors */}
        <td className="px-3 text-right">
          {hasErrors ? (
            <span
              className="inline-block font-mono tabular-nums rounded px-1.5"
              style={{
                background: "var(--accent-red-dim)",
                color: "var(--accent-red)",
                border: "1px solid rgba(220,38,38,0.25)",
                fontSize: 11,
              }}
              aria-label={`${session.error_count} errors`}
            >
              {session.error_count}
            </span>
          ) : (
            <span
              style={{ color: "var(--text-muted)", fontSize: 11 }}
              aria-label="0 errors"
            >
              ·
            </span>
          )}
        </td>

        {/* Started */}
        <td className="px-3" style={{ color: "var(--text-secondary)", whiteSpace: "nowrap" }}>
          <time dateTime={session.started_at ?? ""} title={session.started_at ?? undefined}>
            {relativeTime(session.started_at)}
          </time>
        </td>

        {/* Tools */}
        <td className="px-3">
          <div className="flex flex-wrap gap-1">
            {topTools.map((t) => (
              <ToolChip key={t} name={t} />
            ))}
            {moreTools > 0 && (
              <span style={{ color: "var(--text-muted)", fontSize: 11 }}>
                +{moreTools}
              </span>
            )}
          </div>
        </td>

        {/* Arrow link */}
        <td className="px-3 text-right" style={{ width: 28 }}>
          <Link
            href={`/sessions/${encodeURIComponent(session.session_id)}`}
            aria-label={`View session ${shortId(session.session_id)}`}
            style={{ color: "var(--text-muted)", fontSize: 12 }}
            onClick={(e) => e.stopPropagation()}
          >
            →
          </Link>
        </td>
      </tr>

      {/* Inline trace peek */}
      {expanded && (
        <tr
          style={{
            background: "var(--bg-surface)",
            borderBottom: "1px solid var(--bg-border)",
          }}
        >
          <td />
          <td colSpan={7} className="px-3 py-2">
            <InlineTracePeek sessionId={session.session_id} />
          </td>
        </tr>
      )}
    </>
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

// Inline trace peek — fetches on expand via client-side fetch
import { useEffect } from "react";
import type { TraceInSession } from "@/types/api";

function InlineTracePeek({ sessionId }: { sessionId: string }) {
  const [traces, setTraces] = useState<TraceInSession[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    const base = process.env.NEXT_PUBLIC_KAIROS_API ?? "http://localhost:8000";
    fetch(`${base}/v1/sessions/${encodeURIComponent(sessionId)}`, {
      cache: "no-store",
    })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`${r.status}`))))
      .then((data: TraceInSession[]) => setTraces(data))
      .catch((e: unknown) =>
        setErr(e instanceof Error ? e.message : "fetch error"),
      );
  }, [sessionId]);

  if (err) {
    return (
      <p className="text-xs" style={{ color: "var(--accent-red)" }}>
        {err}
      </p>
    );
  }
  if (!traces) {
    return (
      <p className="text-xs" style={{ color: "var(--text-muted)" }}>
        loading traces…
      </p>
    );
  }
  if (traces.length === 0) {
    return (
      <p className="text-xs" style={{ color: "var(--text-muted)" }}>
        no traces
      </p>
    );
  }

  return (
    <div className="flex flex-col gap-0.5">
      {traces.slice(0, 8).map((t) => (
        <div key={t.trace_id} className="flex items-center gap-3">
          <span style={{ color: "var(--text-muted)", fontSize: 11 }}>└─</span>
          <Link
            href={`/traces/${t.trace_id}`}
            className="font-mono hover:underline"
            style={{ color: "var(--text-link)", fontSize: 11 }}
          >
            {shortId(t.trace_id)}
          </Link>
          <span
            className="font-mono tabular-nums"
            style={{ color: "var(--text-muted)", fontSize: 11 }}
          >
            {t.span_count}s
          </span>
          {t.error_count > 0 && (
            <span style={{ color: "var(--accent-red)", fontSize: 11 }}>
              {t.error_count} err
            </span>
          )}
          <div className="flex gap-1">
            {t.tools.slice(0, 3).map((tool) => (
              <ToolChip key={tool} name={tool} />
            ))}
          </div>
          <span style={{ color: "var(--text-muted)", fontSize: 11 }}>
            {relativeTime(t.started_at)}
          </span>
        </div>
      ))}
      {traces.length > 8 && (
        <p className="text-xs ml-8" style={{ color: "var(--text-muted)" }}>
          +{traces.length - 8} more —{" "}
          <Link
            href={`/sessions/${encodeURIComponent(sessionId)}`}
            style={{ color: "var(--text-link)" }}
          >
            view all
          </Link>
        </p>
      )}
    </div>
  );
}
