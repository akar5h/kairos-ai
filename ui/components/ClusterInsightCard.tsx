"use client";

/**
 * ClusterInsightCard — dense console card for one LLM-generated cluster insight.
 *
 * Shows pattern_name, confidence, is_coherent, description, discriminator_hint,
 * root_cause, model_used, and an Approve button.
 */
import { useState } from "react";
import type { ClusterInsight } from "@/types/api";
import { approveInsight } from "@/lib/api";

interface Props {
  insight: ClusterInsight;
  clusterKey: string;
}

function ConfidenceBadge({ value }: { value: number | null }) {
  if (value == null) return null;
  const pct = Math.round(value * 100);
  const color =
    value >= 0.8 ? "#16a34a" : value >= 0.5 ? "#d97706" : "#dc2626";
  const bg =
    value >= 0.8
      ? "rgba(22,163,74,0.12)"
      : value >= 0.5
        ? "rgba(217,119,6,0.12)"
        : "rgba(220,38,38,0.12)";
  const border =
    value >= 0.8
      ? "rgba(22,163,74,0.35)"
      : value >= 0.5
        ? "rgba(217,119,6,0.35)"
        : "rgba(220,38,38,0.35)";
  return (
    <span
      className="font-mono rounded px-1.5 py-0.5"
      style={{ background: bg, color, border: `1px solid ${border}`, fontSize: 10 }}
    >
      conf {pct}%
    </span>
  );
}

function CoherenceBadge({ value }: { value: boolean | null }) {
  if (value == null) return null;
  return (
    <span
      className="font-mono rounded px-1.5 py-0.5"
      style={{
        background: value ? "rgba(22,163,74,0.12)" : "rgba(220,38,38,0.12)",
        color: value ? "#16a34a" : "#dc2626",
        border: `1px solid ${value ? "rgba(22,163,74,0.35)" : "rgba(220,38,38,0.35)"}`,
        fontSize: 10,
      }}
    >
      {value ? "coherent" : "incoherent"}
    </span>
  );
}

export function ClusterInsightCard({ insight, clusterKey }: Props) {
  const [approved, setApproved] = useState(insight.approved_at != null);
  const [loading, setLoading] = useState(false);
  const [evalSetId, setEvalSetId] = useState<string | null>(null);

  async function handleApprove() {
    setLoading(true);
    try {
      const res = await approveInsight(clusterKey, insight.id);
      setApproved(true);
      if (res.eval_set_id) setEvalSetId(res.eval_set_id);
    } catch {
      // silently ignore — user can retry
    } finally {
      setLoading(false);
    }
  }

  return (
    <div
      className="rounded mb-2 p-3"
      style={{
        background: "var(--bg-elevated)",
        border: "1px solid var(--bg-border)",
        fontSize: 12,
      }}
    >
      {/* Header row: pattern name + badges */}
      <div className="flex items-center gap-2 flex-wrap mb-2">
        <span
          className="font-mono font-semibold"
          style={{ color: "var(--text-primary)", fontSize: 13 }}
        >
          {insight.pattern_name ?? "unnamed_pattern"}
        </span>
        <ConfidenceBadge value={insight.confidence} />
        <CoherenceBadge value={insight.is_coherent} />
        {insight.auto_approve && (
          <span
            className="font-mono rounded px-1.5 py-0.5"
            style={{
              background: "rgba(99,102,241,0.12)",
              color: "#6366f1",
              border: "1px solid rgba(99,102,241,0.35)",
              fontSize: 10,
            }}
          >
            auto-approved
          </span>
        )}
      </div>

      {/* Description */}
      {insight.description && (
        <p className="mb-1" style={{ color: "var(--text-secondary)" }}>
          {insight.description}
        </p>
      )}

      {/* Discriminator hint */}
      {insight.discriminator_hint && (
        <p className="mb-1" style={{ color: "var(--text-muted)", fontStyle: "italic" }}>
          detector hint: {insight.discriminator_hint}
        </p>
      )}

      {/* Root cause */}
      {insight.root_cause && (
        <p className="mb-2" style={{ color: "var(--text-muted)" }}>
          root cause: {insight.root_cause}
        </p>
      )}

      {/* Footer: model + timestamp + approve button */}
      <div className="flex items-center gap-3 flex-wrap">
        {insight.model_used && (
          <span style={{ color: "var(--text-muted)", fontSize: 10, fontFamily: "monospace" }}>
            {insight.model_used}
          </span>
        )}
        <span style={{ color: "var(--text-muted)", fontSize: 10, fontFamily: "monospace" }}>
          {new Date(insight.created_at).toLocaleDateString()}
        </span>

        {evalSetId && (
          <span
            className="font-mono rounded px-1.5 py-0.5"
            style={{
              background: "rgba(22,163,74,0.12)",
              color: "#16a34a",
              border: "1px solid rgba(22,163,74,0.35)",
              fontSize: 10,
            }}
          >
            eval set generated
          </span>
        )}

        <button
          type="button"
          disabled={approved || loading}
          onClick={handleApprove}
          className="font-mono rounded px-2 py-0.5"
          style={{
            background: approved ? "rgba(22,163,74,0.12)" : "var(--bg-surface)",
            color: approved ? "#16a34a" : "var(--text-secondary)",
            border: `1px solid ${approved ? "rgba(22,163,74,0.35)" : "var(--bg-border)"}`,
            fontSize: 10,
            cursor: approved || loading ? "default" : "pointer",
            opacity: loading ? 0.6 : 1,
          }}
        >
          {approved ? "Approved ✓" : loading ? "approving…" : "Approve"}
        </button>
      </div>
    </div>
  );
}
