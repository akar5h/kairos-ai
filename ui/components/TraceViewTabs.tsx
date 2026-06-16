"use client";

import { useRouter, useSearchParams } from "next/navigation";

type TabView = "conversation" | "timeline" | "spans";

interface TraceViewTabsProps {
  currentView: TabView;
  enrichHooks: boolean;
}

const TABS: { id: TabView; label: string }[] = [
  { id: "spans", label: "Raw Spans" },
  { id: "conversation", label: "Conversation" },
  { id: "timeline", label: "Step Timeline" },
];

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

  return (
    <div className="flex items-center gap-1 pt-1">
      {/* View tabs */}
      <div
        className="flex items-center rounded"
        style={{
          background: "var(--bg-elevated)",
          border: "1px solid var(--bg-border)",
          padding: "2px",
          gap: 2,
        }}
      >
        {TABS.map(({ id, label }) => {
          const active = currentView === id;
          return (
            <button
              key={id}
              onClick={() => navigate({ view: id })}
              aria-pressed={active}
              style={{
                fontSize: 11,
                fontWeight: active ? 600 : 400,
                color: active ? "var(--text-primary)" : "var(--text-muted)",
                background: active ? "var(--bg-base)" : "transparent",
                border: active ? "1px solid var(--bg-border)" : "1px solid transparent",
                borderRadius: 3,
                padding: "2px 8px",
                cursor: "pointer",
                lineHeight: "20px",
                transition: "color 0.1s, background 0.1s",
              }}
            >
              {label}
            </button>
          );
        })}
      </div>

      {/* Divider */}
      <span style={{ color: "var(--bg-border)", margin: "0 6px" }} aria-hidden="true">|</span>

      {/* enrich_hooks toggle */}
      <label
        className="flex items-center gap-1.5 cursor-pointer select-none"
        style={{ color: "var(--text-muted)", fontSize: 11 }}
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
          className="rounded px-1"
          style={{
            background: enrichHooks ? "var(--accent-blue-dim)" : "var(--bg-elevated)",
            color: enrichHooks ? "var(--accent-blue)" : "var(--text-muted)",
            border: `1px solid ${enrichHooks ? "rgba(37,99,235,0.2)" : "var(--bg-border)"}`,
            fontSize: 10,
            padding: "0px 4px",
          }}
        >
          {enrichHooks ? "on" : "off"}
        </span>
      </label>
    </div>
  );
}
