/**
 * Cluster detail page (/clusters/[key]) — F2.3.
 *
 * Breadcrumb: clusters › <key>
 * Shows sample_features (key/value) from the cluster summary + the cluster's
 * traces table (trace_id, labeled badge) → click navigates to the trace view.
 *
 * cluster_key is URL-encoded in the route segment ([key]); it can contain
 * "::", "|", and other special chars.
 */
import type { Metadata } from "next";
import Link from "next/link";
import { getClusterInsights, getClusterTraces, getClusters } from "@/lib/api";
import { ClusterInsightCard } from "@/components/ClusterInsightCard";
import { ClusterTracesTable } from "@/components/ClusterTracesTable";
import { CopyButton } from "@/components/CopyButton";
import type { ClusterInsight, ClusterSummary, ClusterTraceMember } from "@/types/api";

export const dynamic = "force-dynamic";

interface PageProps {
  params: Promise<{ key: string }>;
}

export async function generateMetadata({ params }: PageProps): Promise<Metadata> {
  const { key } = await params;
  return { title: `Cluster ${decodeURIComponent(key)} — Kairos` };
}

export default async function ClusterDetailPage({ params }: PageProps) {
  const { key } = await params;
  const clusterKey = decodeURIComponent(key);

  let traces: ClusterTraceMember[] = [];
  let summary: ClusterSummary | null = null;
  let insights: ClusterInsight[] = [];
  let error: string | null = null;

  try {
    const [allClusters, clusterTraces, clusterInsights] = await Promise.all([
      getClusters().catch(() => [] as ClusterSummary[]),
      getClusterTraces(clusterKey),
      getClusterInsights(clusterKey).catch(() => [] as ClusterInsight[]),
    ]);
    traces = clusterTraces;
    summary = allClusters.find((c) => c.cluster_key === clusterKey) ?? null;
    insights = clusterInsights;
  } catch (e) {
    error = e instanceof Error ? e.message : "Unknown error";
  }

  const features = summary?.sample_features ?? {};
  const featureEntries = Object.entries(features);

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* Sub-header */}
      <div
        className="px-4 py-2 border-b shrink-0 flex flex-col gap-2"
        style={{ borderColor: "var(--bg-border)", background: "var(--bg-surface)" }}
      >
        {/* Breadcrumb */}
        <div className="flex items-center gap-1.5 flex-wrap">
          <Link
            href="/clusters"
            className="text-xs transition-colors hover:underline"
            style={{ color: "var(--text-muted)" }}
          >
            clusters
          </Link>
          <span style={{ color: "var(--bg-border)", fontSize: 12 }}>›</span>
          <span
            className="font-mono text-xs"
            style={{ color: "var(--text-primary)", wordBreak: "break-all" }}
            title={clusterKey}
          >
            {clusterKey}
          </span>
          <CopyButton text={clusterKey} label="Copy cluster key" />
        </div>

        {/* Metrics strip */}
        <div className="flex flex-wrap items-center gap-3">
          <MetricChip label="traces" value={String(traces.length)} />
          {summary && (
            <>
              <MetricChip label="first-seen" value={summary.min_night_id ?? "—"} mono />
              {summary.kinds.length > 0 && (
                <div className="flex items-center gap-1 flex-wrap">
                  <span className="text-xs" style={{ color: "var(--text-muted)" }}>
                    kinds
                  </span>
                  {summary.kinds.map((k) => (
                    <span
                      key={k}
                      className="font-mono rounded px-1"
                      style={{
                        background: "var(--bg-elevated)",
                        color: "var(--text-secondary)",
                        border: "1px solid var(--bg-border)",
                        fontSize: 10,
                      }}
                    >
                      {k}
                    </span>
                  ))}
                </div>
              )}
            </>
          )}
        </div>

        {/* Sample features */}
        {featureEntries.length > 0 && (
          <div className="flex flex-col gap-1">
            <span className="text-xs" style={{ color: "var(--text-muted)" }}>
              sample features:
            </span>
            <div className="flex flex-wrap gap-x-4 gap-y-1">
              {featureEntries.map(([k, v]) => (
                <div key={k} className="flex items-baseline gap-1.5">
                  <span
                    className="font-mono text-xs"
                    style={{ color: "var(--text-muted)" }}
                  >
                    {k}
                  </span>
                  <span
                    className="font-mono text-xs"
                    style={{ color: "var(--text-secondary)" }}
                  >
                    {typeof v === "string" ? v : JSON.stringify(v)}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* Pattern insights */}
      {insights.length > 0 && (
        <div
          className="px-4 py-3 border-b shrink-0"
          style={{ borderColor: "var(--bg-border)" }}
        >
          <h3
            className="text-xs font-semibold mb-2"
            style={{ color: "var(--text-muted)", letterSpacing: "0.06em" }}
          >
            PATTERN INSIGHTS
          </h3>
          {insights.map((i) => (
            <ClusterInsightCard key={i.id} insight={i} clusterKey={clusterKey} />
          ))}
        </div>
      )}

      {/* Traces table */}
      <div className="flex-1 overflow-y-auto">
        {error ? (
          <ErrorPanel message={error} />
        ) : (
          <ClusterTracesTable traces={traces} />
        )}
      </div>
    </div>
  );
}

function MetricChip({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="flex items-center gap-1">
      <span className="text-xs" style={{ color: "var(--text-muted)" }}>
        {label}
      </span>
      <span
        className={`text-xs ${mono ? "font-mono" : ""}`}
        style={{ color: "var(--text-secondary)" }}
      >
        {value}
      </span>
    </div>
  );
}

function ErrorPanel({ message }: { message: string }) {
  return (
    <div className="flex items-center justify-center py-20" role="alert">
      <div
        className="rounded px-4 py-3 text-xs max-w-lg text-center font-mono"
        style={{
          background: "var(--accent-red-dim)",
          color: "var(--accent-red)",
          border: "1px solid rgba(220,38,38,0.3)",
        }}
      >
        <p className="font-semibold mb-1">Failed to load cluster</p>
        <p>{message}</p>
      </div>
    </div>
  );
}
