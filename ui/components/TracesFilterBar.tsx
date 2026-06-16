"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useCallback } from "react";

interface TracesFilterBarProps {
  defaultLimit: number;
  defaultSince: string;
}

export function TracesFilterBar({ defaultLimit, defaultSince }: TracesFilterBarProps) {
  const router = useRouter();
  const searchParams = useSearchParams();

  const update = useCallback(
    (key: string, value: string) => {
      const params = new URLSearchParams(searchParams.toString());
      if (value) {
        params.set(key, value);
      } else {
        params.delete(key);
      }
      router.push(`/?${params.toString()}`);
    },
    [router, searchParams],
  );

  return (
    <div className="flex items-center gap-3">
      {/* Since filter */}
      <div className="flex items-center gap-1.5">
        <label
          htmlFor="since-filter"
          className="text-xs"
          style={{ color: "var(--text-muted)" }}
        >
          since
        </label>
        <input
          id="since-filter"
          type="datetime-local"
          defaultValue={defaultSince}
          onChange={(e) => update("since", e.target.value ? new Date(e.target.value).toISOString() : "")}
          className="text-xs rounded px-2 py-1 border outline-none"
          style={{
            background: "var(--bg-elevated)",
            color: "var(--text-primary)",
            borderColor: "var(--bg-border)",
          }}
        />
      </div>

      {/* Limit */}
      <div className="flex items-center gap-1.5">
        <label
          htmlFor="limit-filter"
          className="text-xs"
          style={{ color: "var(--text-muted)" }}
        >
          limit
        </label>
        <select
          id="limit-filter"
          defaultValue={String(defaultLimit)}
          onChange={(e) => update("limit", e.target.value)}
          className="text-xs rounded px-2 py-1 border outline-none"
          style={{
            background: "var(--bg-elevated)",
            color: "var(--text-primary)",
            borderColor: "var(--bg-border)",
          }}
        >
          <option value="25">25</option>
          <option value="50">50</option>
          <option value="100">100</option>
          <option value="500">500</option>
        </select>
      </div>
    </div>
  );
}
