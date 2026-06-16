"use client";

/**
 * ClusterTable — dense light console table for the Clusters view (F2.3).
 *
 * Columns: CLUSTER_KEY | TRACES | KINDS | FIRST-SEEN
 * cluster_key is the moat primitive — rendered prominently in monospace,
 * truncated with full value on hover/title + copy, and URL-encoded in routes.
 * Row → /clusters/[key].
 */
import Link from "next/link";
import type { ClusterSummary } from "@/types/api";
import { CopyButton } from "@/components/CopyButton";

interface ClusterTableProps {
  clusters: ClusterSummary[];
}

export function ClusterTable({ clusters }: ClusterTableProps) {
  if (clusters.length === 0) {
    return (
      <div
        role="status"
        className="flex flex-col items-center justify-center py-20 gap-2"
        style={{ color: "var(--text-muted)" }}
      >
        <span className="text-2xl" aria-hidden="true">
          ◌
        </span>
        <p className="text-xs">No clusters found</p>
        <p className="text-xs" style={{ color: "var(--text-muted)" }}>
          Clusters appear once the discovery queue has grouped traces.
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
        aria-label="Cluster list"
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
            <Th>CLUSTER_KEY</Th>
            <Th right w={80}>
              TRACES
            </Th>
            <Th>KINDS</Th>
            <Th w={120}>FIRST-SEEN</Th>
            <Th w={28} />
          </tr>
        </thead>
        <tbody>
          {clusters.map((c) => (
            <ClusterRow key={c.cluster_key} cluster={c} />
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
      className="px-3 py-1.5 font-semibold"
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

function ClusterRow({ cluster }: { cluster: ClusterSummary }) {
  const href = `/clusters/${encodeURIComponent(cluster.cluster_key)}`;
  const topKinds = cluster.kinds.slice(0, 4);
  const moreKinds = cluster.kinds.length - topKinds.length;

  return (
    <tr
      className="console-row"
      onMouseEnter={(e) => {
        (e.currentTarget as HTMLElement).style.background = "var(--bg-hover)";
      }}
      onMouseLeave={(e) => {
        (e.currentTarget as HTMLElement).style.background = "";
      }}
    >
      {/* cluster_key — the moat primitive */}
      <td className="px-3" style={{ maxWidth: 0 }}>
        <div className="flex items-center gap-1.5">
          <Link
            href={href}
            className="font-mono hover:underline truncate"
            style={{ color: "var(--accent-blue)", fontSize: 12, maxWidth: 520 }}
            title={cluster.cluster_key}
          >
            {cluster.cluster_key}
          </Link>
          <CopyButton text={cluster.cluster_key} label="Copy cluster key" />
        </div>
      </td>

      {/* Traces */}
      <td
        className="px-3 text-right font-mono tabular-nums"
        style={{ color: "var(--text-secondary)" }}
      >
        {cluster.trace_count}
      </td>

      {/* Kinds */}
      <td className="px-3">
        <div className="flex flex-wrap gap-1">
          {topKinds.length === 0 ? (
            <span style={{ color: "var(--text-muted)", fontSize: 11 }}>·</span>
          ) : (
            topKinds.map((k) => <KindChip key={k} name={k} />)
          )}
          {moreKinds > 0 && (
            <span style={{ color: "var(--text-muted)", fontSize: 11 }}>
              +{moreKinds}
            </span>
          )}
        </div>
      </td>

      {/* First-seen (min_night_id) */}
      <td
        className="px-3 font-mono"
        style={{ color: "var(--text-secondary)", whiteSpace: "nowrap" }}
      >
        {cluster.min_night_id ?? "—"}
      </td>

      {/* Arrow link */}
      <td className="px-3 text-right" style={{ width: 28 }}>
        <Link
          href={href}
          aria-label={`View cluster ${cluster.cluster_key}`}
          style={{ color: "var(--text-muted)", fontSize: 12 }}
        >
          →
        </Link>
      </td>
    </tr>
  );
}

function KindChip({ name }: { name: string }) {
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
