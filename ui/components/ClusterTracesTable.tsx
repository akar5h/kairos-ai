"use client";

/**
 * ClusterTracesTable — traces belonging to a cluster (F2.3 detail).
 *
 * Columns: TRACE_ID | LABELED
 * Row → existing trace view /traces/[id].
 */
import Link from "next/link";
import type { ClusterTraceMember } from "@/types/api";
import { CopyButton } from "@/components/CopyButton";
import { shortId } from "@/lib/format";

interface ClusterTracesTableProps {
  traces: ClusterTraceMember[];
}

export function ClusterTracesTable({ traces }: ClusterTracesTableProps) {
  if (traces.length === 0) {
    return (
      <div
        role="status"
        className="flex flex-col items-center justify-center py-16 gap-2"
        style={{ color: "var(--text-muted)" }}
      >
        <span className="text-2xl" aria-hidden="true">
          ◌
        </span>
        <p className="text-xs">No traces in this cluster</p>
      </div>
    );
  }

  return (
    <div className="overflow-x-auto">
      <table
        className="w-full border-collapse"
        style={{ fontSize: 12 }}
        role="grid"
        aria-label="Cluster traces"
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
            <th
              className="px-3 py-1.5 font-semibold text-left"
              style={{
                color: "var(--text-muted)",
                fontSize: 10,
                letterSpacing: "0.06em",
                whiteSpace: "nowrap",
              }}
            >
              TRACE_ID
            </th>
            <th
              className="px-3 py-1.5 font-semibold text-left"
              style={{
                color: "var(--text-muted)",
                fontSize: 10,
                letterSpacing: "0.06em",
                width: 100,
                whiteSpace: "nowrap",
              }}
            >
              LABELED
            </th>
            <th style={{ width: 28 }} />
          </tr>
        </thead>
        <tbody>
          {traces.map((t, i) => (
            <tr
              key={`${t.trace_id}-${i}`}
              className="console-row"
              onMouseEnter={(e) => {
                (e.currentTarget as HTMLElement).style.background =
                  "var(--bg-hover)";
              }}
              onMouseLeave={(e) => {
                (e.currentTarget as HTMLElement).style.background = "";
              }}
            >
              <td className="px-3">
                <div className="flex items-center gap-1.5">
                  <Link
                    href={`/traces/${t.trace_id}`}
                    className="font-mono hover:underline"
                    style={{ color: "var(--accent-blue)", fontSize: 12 }}
                    title={t.trace_id}
                  >
                    {shortId(t.trace_id)}
                  </Link>
                  <span
                    className="font-mono hidden sm:inline"
                    style={{ color: "var(--text-muted)", fontSize: 11 }}
                  >
                    {t.trace_id.slice(8)}
                  </span>
                  <CopyButton text={t.trace_id} label="Copy trace ID" />
                </div>
              </td>
              <td className="px-3">
                <LabeledBadge labeled={t.labeled} />
              </td>
              <td className="px-3 text-right" style={{ width: 28 }}>
                <Link
                  href={`/traces/${t.trace_id}`}
                  aria-label={`View trace ${shortId(t.trace_id)}`}
                  style={{ color: "var(--text-muted)", fontSize: 12 }}
                >
                  →
                </Link>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function LabeledBadge({ labeled }: { labeled: boolean }) {
  if (labeled) {
    return (
      <span
        className="inline-block font-mono rounded px-1.5"
        style={{
          background: "var(--accent-green-dim)",
          color: "var(--accent-green)",
          border: "1px solid rgba(22,163,74,0.25)",
          fontSize: 10,
          lineHeight: "16px",
        }}
        aria-label="labeled"
      >
        ✓ labeled
      </span>
    );
  }
  return (
    <span
      className="font-mono"
      style={{ color: "var(--text-muted)", fontSize: 11 }}
      aria-label="unlabeled"
    >
      ·
    </span>
  );
}
