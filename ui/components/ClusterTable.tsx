"use client";

/**
 * ClusterTable — dense light console table for the Clusters view (F2.3).
 *
 * Columns: CLUSTER_KEY | STATUS | TRACES | KINDS | FIRST-SEEN | ACTIONS
 * cluster_key is the moat primitive — rendered prominently in monospace,
 * truncated with full value on hover/title + copy, and URL-encoded in routes.
 * Row → /clusters/[key].
 *
 * P3.4 additions: status badge per cluster + Resolve/Regress action buttons.
 */
import { useState } from "react";
import Link from "next/link";
import type { ClusterStatus, ClusterSummary } from "@/types/api";
import { CopyButton } from "@/components/CopyButton";
import { resolveCluster, regressCluster } from "@/lib/api";

interface ClusterTableProps {
  clusters: ClusterSummary[];
}

export function ClusterTable({ clusters: initial }: ClusterTableProps) {
  // Track per-cluster status client-side after actions.
  const [statusMap, setStatusMap] = useState<Record<string, ClusterStatus>>({});

  function effectiveStatus(c: ClusterSummary): ClusterStatus {
    return statusMap[c.cluster_key] ?? c.status;
  }

  async function handleResolve(clusterKey: string) {
    try {
      const res = await resolveCluster(clusterKey);
      setStatusMap((prev) => ({ ...prev, [clusterKey]: res.status }));
    } catch {
      // silently ignore — status stays unchanged; user can retry
    }
  }

  async function handleRegress(clusterKey: string) {
    try {
      const res = await regressCluster(clusterKey);
      setStatusMap((prev) => ({ ...prev, [clusterKey]: res.status }));
    } catch {
      // silently ignore
    }
  }

  if (initial.length === 0) {
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
            <Th w={90}>STATUS</Th>
            <Th right w={80}>
              TRACES
            </Th>
            <Th>KINDS</Th>
            <Th w={120}>FIRST-SEEN</Th>
            <Th w={120}>ACTIONS</Th>
            <Th w={28} />
          </tr>
        </thead>
        <tbody>
          {initial.map((c) => (
            <ClusterRow
              key={c.cluster_key}
              cluster={c}
              status={effectiveStatus(c)}
              onResolve={handleResolve}
              onRegress={handleRegress}
            />
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

function StatusBadge({ status }: { status: ClusterStatus }) {
  const styles: Record<
    ClusterStatus,
    { bg: string; color: string; border: string; label: string }
  > = {
    open: {
      bg: "rgba(217,119,6,0.12)",
      color: "#d97706",
      border: "rgba(217,119,6,0.35)",
      label: "open",
    },
    resolved: {
      bg: "rgba(22,163,74,0.12)",
      color: "#16a34a",
      border: "rgba(22,163,74,0.35)",
      label: "resolved",
    },
    regressed: {
      bg: "rgba(220,38,38,0.12)",
      color: "#dc2626",
      border: "rgba(220,38,38,0.35)",
      label: "regressed",
    },
  };

  const s = styles[status] ?? styles.open;

  return (
    <span
      className="font-mono rounded px-1.5 py-0.5"
      style={{
        background: s.bg,
        color: s.color,
        border: `1px solid ${s.border}`,
        fontSize: 10,
        whiteSpace: "nowrap",
      }}
    >
      {s.label}
    </span>
  );
}

function ActionButton({
  label,
  onClick,
}: {
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={(e) => {
        e.stopPropagation();
        onClick();
      }}
      className="font-mono rounded px-1.5 py-0.5"
      style={{
        background: "var(--bg-elevated)",
        color: "var(--text-secondary)",
        border: "1px solid var(--bg-border)",
        fontSize: 10,
        cursor: "pointer",
        whiteSpace: "nowrap",
      }}
    >
      {label}
    </button>
  );
}

function ClusterRow({
  cluster,
  status,
  onResolve,
  onRegress,
}: {
  cluster: ClusterSummary;
  status: ClusterStatus;
  onResolve: (key: string) => void;
  onRegress: (key: string) => void;
}) {
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

      {/* Status badge */}
      <td className="px-3">
        <StatusBadge status={status} />
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

      {/* Lifecycle actions */}
      <td className="px-3">
        <div className="flex gap-1">
          {(status === "open" || status === "regressed") && (
            <ActionButton
              label="Resolve"
              onClick={() => onResolve(cluster.cluster_key)}
            />
          )}
          {status === "resolved" && (
            <ActionButton
              label="Regress"
              onClick={() => onRegress(cluster.cluster_key)}
            />
          )}
        </div>
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
