/**
 * Clusters list page (/clusters) — the moat surface (F2.3).
 *
 * GET /v1/clusters
 * Columns: CLUSTER_KEY | TRACES | KINDS | FIRST-SEEN
 * Ordered by trace_count desc (API enforces).
 */
import type { Metadata } from "next";
import { getClusters } from "@/lib/api";
import { ClusterTable } from "@/components/ClusterTable";
import { ClusterRefreshButton } from "@/components/ClusterRefreshButton";

export const metadata: Metadata = {
  title: "Clusters — Kairos",
};

export const dynamic = "force-dynamic";

export default async function ClustersPage() {
  let clusters = null;
  let error: string | null = null;

  try {
    clusters = await getClusters();
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
            Clusters
          </span>
          {clusters && (
            <span
              className="font-mono text-xs tabular-nums"
              style={{ color: "var(--text-muted)" }}
            >
              {clusters.length} rows
            </span>
          )}
        </div>
        <ClusterRefreshButton />
      </div>

      {/* Table */}
      <div className="flex-1 overflow-y-auto">
        {error ? (
          <ErrorState message={error} />
        ) : clusters ? (
          <ClusterTable clusters={clusters} />
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
