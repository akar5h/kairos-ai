"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";

interface SessionsFilterBarProps {
  defaultLimit: number;
  defaultSince: string;
  defaultQ: string;
}

export function SessionsFilterBar({ defaultLimit, defaultSince, defaultQ }: SessionsFilterBarProps) {
  const router = useRouter();
  const searchParams = useSearchParams();

  // Derive local controlled values from searchParams (source of truth)
  const currentQ = searchParams.get("q") ?? defaultQ;
  const currentSince = searchParams.get("since") ?? defaultSince;
  const currentLimit = searchParams.get("limit") ?? String(defaultLimit);

  const [localQ, setLocalQ] = useState(currentQ);
  const [localSince, setLocalSince] = useState(currentSince);
  const [localLimit, setLocalLimit] = useState(currentLimit);

  const apply = () => {
    const params = new URLSearchParams();
    if (localQ.trim()) params.set("q", localQ.trim());
    if (localSince.trim()) params.set("since", localSince.trim());
    const l = parseInt(localLimit, 10);
    if (!isNaN(l) && l !== 100) params.set("limit", String(l));
    router.push(`?${params.toString()}`);
  };

  return (
    <div className="flex items-center gap-2">
      <input
        type="text"
        value={localQ}
        onChange={(e) => setLocalQ(e.target.value)}
        onKeyDown={(e) => e.key === "Enter" && apply()}
        placeholder="filter by id…"
        aria-label="Filter sessions by ID"
        className="rounded px-2 h-6 text-xs"
        style={{
          background: "var(--bg-elevated)",
          border: "1px solid var(--bg-border)",
          color: "var(--text-primary)",
          width: 120,
          outline: "none",
        }}
      />
      <input
        type="text"
        value={localSince}
        onChange={(e) => setLocalSince(e.target.value)}
        onKeyDown={(e) => e.key === "Enter" && apply()}
        placeholder="since (ISO)"
        aria-label="Filter sessions since date"
        className="rounded px-2 h-6 text-xs hidden sm:block"
        style={{
          background: "var(--bg-elevated)",
          border: "1px solid var(--bg-border)",
          color: "var(--text-primary)",
          width: 140,
          outline: "none",
        }}
      />
      <select
        value={localLimit}
        onChange={(e) => setLocalLimit(e.target.value)}
        aria-label="Row limit"
        className="rounded px-1 h-6 text-xs"
        style={{
          background: "var(--bg-elevated)",
          border: "1px solid var(--bg-border)",
          color: "var(--text-secondary)",
          outline: "none",
        }}
      >
        {["25", "50", "100", "250", "500"].map((v) => (
          <option key={v} value={v}>
            {v} rows
          </option>
        ))}
      </select>
      <button
        onClick={apply}
        className="rounded px-2 h-6 text-xs"
        style={{
          background: "var(--bg-elevated)",
          border: "1px solid var(--bg-border)",
          color: "var(--text-secondary)",
          cursor: "pointer",
        }}
      >
        apply
      </button>
    </div>
  );
}
