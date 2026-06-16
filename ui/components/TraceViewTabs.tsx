"use client";

import { useRouter, useSearchParams } from "next/navigation";

interface TraceViewTabsProps {
  currentView: "conversation" | "timeline";
  enrichHooks: boolean;
}

export function TraceViewTabs({ currentView, enrichHooks }: TraceViewTabsProps) {
  const router = useRouter();
  const searchParams = useSearchParams();

  const navigate = (updates: Record<string, string | null>) => {
    const params = new URLSearchParams(searchParams.toString());
    for (const [key, val] of Object.entries(updates)) {
      if (val === null) {
        params.delete(key);
      } else {
        params.set(key, val);
      }
    }
    router.push(`?${params.toString()}`);
  };

  const tabStyle = (active: boolean): React.CSSProperties => ({
    color: active ? "var(--text-primary)" : "var(--text-muted)",
    borderBottom: active ? "2px solid var(--accent-blue)" : "2px solid transparent",
    paddingBottom: "6px",
    background: "transparent",
    cursor: "pointer",
    fontSize: "12px",
    fontWeight: active ? 600 : 400,
    transition: "color 0.1s",
  });

  return (
    <div className="flex items-center gap-6 pt-1">
      {/* View tabs */}
      <div className="flex items-center gap-4">
        <button
          style={tabStyle(currentView === "conversation")}
          onClick={() => navigate({ view: "conversation" })}
          aria-pressed={currentView === "conversation"}
        >
          Conversation
        </button>
        <button
          style={tabStyle(currentView === "timeline")}
          onClick={() => navigate({ view: "timeline" })}
          aria-pressed={currentView === "timeline"}
        >
          Step Timeline
        </button>
      </div>

      {/* Divider */}
      <span style={{ color: "var(--bg-border)" }} aria-hidden="true">|</span>

      {/* enrich_hooks toggle */}
      <label
        className="flex items-center gap-1.5 cursor-pointer select-none text-xs"
        style={{ color: "var(--text-muted)" }}
        title="When on: hook-enriched step data (corrected tool outcomes). When off: raw OTel spans."
      >
        <input
          type="checkbox"
          checked={enrichHooks}
          onChange={(e) =>
            navigate({ enrich_hooks: e.target.checked ? "true" : null })
          }
          className="w-3 h-3"
          aria-label="Enrich with hooks"
        />
        enrich hooks
        <span
          className="text-xs rounded px-1 py-0.5"
          style={{
            background: enrichHooks ? "var(--accent-blue-dim)" : "var(--bg-elevated)",
            color: enrichHooks ? "var(--accent-blue)" : "var(--text-muted)",
          }}
        >
          {enrichHooks ? "on" : "off"}
        </span>
      </label>
    </div>
  );
}
