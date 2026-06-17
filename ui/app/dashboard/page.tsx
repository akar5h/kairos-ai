/**
 * Dashboard page (/dashboard) — aggregate stats across all sessions.
 * GET /v1/stats
 */
import type { Metadata } from "next";
import { getStats } from "@/lib/api";
import type { StatsResponse } from "@/types/api";

export const metadata: Metadata = {
  title: "Dashboard — Kairos",
};

export const dynamic = "force-dynamic";

export default async function DashboardPage() {
  let stats: StatsResponse | null = null;
  let error: string | null = null;

  try {
    stats = await getStats();
  } catch (e) {
    error = e instanceof Error ? e.message : "Unknown error";
  }

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* Sub-header */}
      <div
        className="flex items-center px-4 border-b shrink-0"
        style={{ borderColor: "var(--bg-border)", height: 36 }}
      >
        <span
          className="text-xs font-semibold"
          style={{ color: "var(--text-primary)" }}
        >
          Dashboard
        </span>
      </div>

      <div className="flex-1 overflow-y-auto p-4">
        {error ? (
          <ErrorState message={error} />
        ) : stats ? (
          <StatGrid stats={stats} />
        ) : null}
      </div>
    </div>
  );
}

function StatGrid({ stats }: { stats: StatsResponse }) {
  const hasErrors = stats.total_errors > 0;

  return (
    <div
      className="grid gap-px"
      style={{
        gridTemplateColumns: "repeat(4, 1fr)",
        background: "var(--bg-border)",
        border: "1px solid var(--bg-border)",
      }}
    >
      <StatCard
        label="SESSIONS"
        value={stats.total_sessions.toLocaleString()}
        sub={`${stats.sessions_today.toLocaleString()} today`}
      />
      <StatCard
        label="SPANS"
        value={stats.total_spans.toLocaleString()}
        sub={`${stats.spans_today.toLocaleString()} today`}
      />
      <StatCard
        label="INPUT TOKENS"
        value={stats.total_input_tokens.toLocaleString()}
        sub={`cached: ${stats.total_cache_read_tokens.toLocaleString()}`}
      />
      <StatCard
        label="OUTPUT TOKENS"
        value={stats.total_output_tokens.toLocaleString()}
      />
      <StatCard
        label="CACHE CREATION"
        value={stats.total_cache_creation_tokens.toLocaleString()}
        sub="tokens"
      />
      <StatCard
        label="EST. COST"
        value={`$${stats.estimated_cost_usd.toFixed(4)}`}
        sub="all-time"
        mono
      />
      <StatCard
        label="ERRORS"
        value={stats.total_errors.toLocaleString()}
        valueColor={hasErrors ? "var(--accent-red)" : undefined}
      />
      <StatCard
        label="MODEL"
        value="claude-opus-4-8"
        mono
      />
    </div>
  );
}

function StatCard({
  label,
  value,
  sub,
  mono = true,
  valueColor,
}: {
  label: string;
  value: string;
  sub?: string;
  mono?: boolean;
  valueColor?: string;
}) {
  return (
    <div
      className="flex flex-col gap-1 p-4"
      style={{ background: "var(--bg-surface)" }}
    >
      <span
        className="tracking-wider"
        style={{
          color: "var(--text-muted)",
          fontSize: 10,
          fontWeight: 600,
          letterSpacing: "0.08em",
          textTransform: "uppercase",
        }}
      >
        {label}
      </span>
      <span
        className={`text-xl font-semibold${mono ? " font-mono" : ""}`}
        style={{ color: valueColor ?? "var(--text-primary)" }}
      >
        {value}
      </span>
      {sub && (
        <span
          className="font-mono text-xs"
          style={{ color: "var(--text-muted)" }}
        >
          {sub}
        </span>
      )}
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
