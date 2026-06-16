/**
 * Trace detail page — three tabs: Raw Spans | Conversation | Step Timeline
 *
 * URL: /traces/[id]?view=spans|conversation|timeline&enrich_hooks=true|false
 *
 * Breadcrumb: sessions › <session_id> › <trace_id>
 * Server component — fetches TraceEnvelope + RawSpans from the API.
 */
import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import { getFindings, getLabels, getTrace, getTraceSpans } from "@/lib/api";
import { ConversationView } from "@/components/ConversationView";
import { StepTimeline } from "@/components/StepTimeline";
import { RawSpansTree } from "@/components/RawSpansTree";
import { ErrorBadge, TerminalBadge } from "@/components/StatusBadge";
import { CopyButton } from "@/components/CopyButton";
import {
  formatLatency,
  formatTimestamp,
  formatTokens,
  shortId,
} from "@/lib/format";
import type { FindingRow, LabelRow } from "@/types/api";
import { TraceViewTabs } from "@/components/TraceViewTabs";

export const dynamic = "force-dynamic";

type TabView = "spans" | "conversation" | "timeline";

interface PageProps {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ view?: string; enrich_hooks?: string }>;
}

export async function generateMetadata({ params }: PageProps): Promise<Metadata> {
  const { id } = await params;
  return { title: `Trace ${shortId(id)} — Kairos` };
}

function parseView(v: string | undefined): TabView {
  if (v === "conversation" || v === "timeline") return v;
  return "spans"; // default to raw spans
}

export default async function TraceDetailPage({ params, searchParams }: PageProps) {
  const { id } = await params;
  const sp = await searchParams;
  const view = parseView(sp.view);
  const enrichHooks = sp.enrich_hooks === "true";

  let envelope = null;
  let rawSpans = null;
  let findings: FindingRow[] = [];
  let labels: LabelRow[] = [];
  let fetchError: string | null = null;

  try {
    // Fetch envelope for metadata; raw spans always fetched
    [envelope, rawSpans] = await Promise.all([
      getTrace(id, enrichHooks),
      getTraceSpans(id).catch(() => []),
    ]);
  } catch (e) {
    const msg = e instanceof Error ? e.message : "Unknown error";
    if (msg.startsWith("404")) notFound();
    fetchError = msg;
  }

  if (envelope) {
    [findings, labels] = await Promise.all([
      getFindings(id).catch(() => []),
      getLabels(id).catch(() => []),
    ]);
  }

  const sessionId = envelope?.session_id ?? null;

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
            href="/"
            className="text-xs transition-colors"
            style={{ color: "var(--text-muted)" }}
          >
            sessions
          </Link>
          <span style={{ color: "var(--bg-border)", fontSize: 12 }}>›</span>
          {sessionId ? (
            <Link
              href={`/sessions/${encodeURIComponent(sessionId)}`}
              className="text-xs font-mono transition-colors hover:underline"
              style={{ color: "var(--text-muted)" }}
            >
              {shortId(sessionId)}
            </Link>
          ) : (
            <span className="text-xs" style={{ color: "var(--text-muted)" }}>
              —
            </span>
          )}
          <span style={{ color: "var(--bg-border)", fontSize: 12 }}>›</span>
          <span
            className="font-mono text-xs"
            style={{ color: "var(--text-primary)" }}
          >
            {shortId(id)}
          </span>
          <span
            className="font-mono text-xs hidden sm:inline"
            style={{ color: "var(--text-muted)" }}
          >
            {id.slice(8)}
          </span>
          <CopyButton text={id} label="Copy full trace ID" />
        </div>

        {/* Metrics strip */}
        {envelope && (
          <div className="flex flex-wrap items-center gap-3">
            <TerminalBadge status={envelope.terminal_status} />
            <ErrorBadge count={envelope.error_count} />
            <MetricChip label="steps" value={String(envelope.step_count)} />
            <MetricChip label="spans" value={String(rawSpans?.length ?? "—")} />
            <MetricChip label="tokens" value={formatTokens(envelope.total_tokens)} />
            <MetricChip label="latency" value={formatLatency(envelope.total_latency_ms)} />
            <MetricChip label="started" value={formatTimestamp(envelope.started_at)} />
            {envelope.agent_type && (
              <MetricChip label="agent" value={envelope.agent_type} mono />
            )}
            {envelope.integrity === "partial" && (
              <span
                className="text-xs rounded px-1.5 py-0.5 font-mono"
                style={{
                  background: "var(--accent-amber-dim)",
                  color: "var(--accent-amber)",
                  border: "1px solid rgba(217,119,6,0.3)",
                }}
              >
                ⚠ partial trace
              </span>
            )}
          </div>
        )}

        {/* Findings / Labels */}
        {findings.length > 0 && <FindingsSummary findings={findings} />}
        {labels.length > 0 && <LabelsSummary labels={labels} />}

        {/* User intent */}
        {envelope?.user_input && (
          <p
            className="text-xs leading-relaxed max-w-2xl"
            style={{ color: "var(--text-secondary)" }}
          >
            <span style={{ color: "var(--text-muted)" }}>task: </span>
            {envelope.user_input.slice(0, 300)}
            {envelope.user_input.length > 300 && "…"}
          </p>
        )}

        {/* View tabs — client component */}
        {(envelope || rawSpans) && (
          <TraceViewTabs currentView={view} enrichHooks={enrichHooks} />
        )}
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto">
        {fetchError ? (
          <ErrorPanel message={fetchError} />
        ) : view === "spans" ? (
          rawSpans != null ? (
            <RawSpansTree spans={rawSpans} />
          ) : null
        ) : view === "timeline" ? (
          envelope ? (
            <div className="px-4 py-4">
              <StepTimeline envelope={envelope} />
            </div>
          ) : null
        ) : envelope ? (
          <ConversationView envelope={envelope} />
        ) : null}
      </div>
    </div>
  );
}

// ── Sub-components ─────────────────────────────────────────────────────────────

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

function FindingsSummary({ findings }: { findings: FindingRow[] }) {
  const bySeverity: Record<string, number> = {};
  for (const f of findings) {
    bySeverity[f.severity] = (bySeverity[f.severity] ?? 0) + 1;
  }
  const sevOrder = ["critical", "high", "medium", "low", "info"];
  const entries = sevOrder
    .filter((s) => bySeverity[s])
    .map((s) => ({ sev: s, count: bySeverity[s] }));

  const sevColor: Record<string, string> = {
    critical: "var(--accent-red)",
    high: "var(--accent-red)",
    medium: "var(--accent-amber)",
    low: "var(--text-secondary)",
    info: "var(--text-muted)",
  };

  return (
    <div className="flex items-center gap-2 flex-wrap">
      <span className="text-xs" style={{ color: "var(--text-muted)" }}>
        findings:
      </span>
      {entries.map(({ sev, count }) => (
        <span
          key={sev}
          className="text-xs font-mono rounded px-1.5 py-0.5"
          style={{
            color: sevColor[sev] ?? "var(--text-muted)",
            background: "var(--bg-elevated)",
            border: `1px solid ${sevColor[sev] ?? "var(--bg-border)"}`,
          }}
        >
          {sev} ×{count}
        </span>
      ))}
    </div>
  );
}

function LabelsSummary({ labels }: { labels: LabelRow[] }) {
  const verdictCounts: Record<string, number> = {};
  for (const l of labels) {
    verdictCounts[l.verdict] = (verdictCounts[l.verdict] ?? 0) + 1;
  }

  return (
    <div className="flex items-center gap-2 flex-wrap">
      <span className="text-xs" style={{ color: "var(--text-muted)" }}>
        labels:
      </span>
      {Object.entries(verdictCounts).map(([verdict, count]) => (
        <span
          key={verdict}
          className="text-xs font-mono rounded px-1.5 py-0.5"
          style={{
            color: verdict === "pass" ? "var(--accent-green)" : "var(--accent-red)",
            background: "var(--bg-elevated)",
            border: "1px solid var(--bg-border)",
          }}
        >
          {verdict} ×{count}
        </span>
      ))}
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
        <p className="font-semibold mb-1">Failed to load trace</p>
        <p>{message}</p>
      </div>
    </div>
  );
}
