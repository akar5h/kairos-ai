"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { refreshClusters } from "@/lib/api";

type ButtonState =
  | { phase: "idle" }
  | { phase: "running" }
  | { phase: "success"; message: string }
  | { phase: "error"; message: string };

export function ClusterRefreshButton() {
  const [state, setState] = useState<ButtonState>({ phase: "idle" });
  const router = useRouter();

  async function handleClick() {
    if (state.phase === "running") return;
    setState({ phase: "running" });

    try {
      const result = await refreshClusters();
      setState({
        phase: "success",
        message: `✓ ${result.clusters_found} clusters`,
      });
      router.refresh();
      setTimeout(() => setState({ phase: "idle" }), 3000);
    } catch (e) {
      setState({
        phase: "error",
        message: e instanceof Error ? e.message : "refresh failed",
      });
      setTimeout(() => setState({ phase: "idle" }), 3000);
    }
  }

  const isRunning = state.phase === "running";
  const isError = state.phase === "error";
  const isSuccess = state.phase === "success";

  return (
    <button
      onClick={handleClick}
      disabled={isRunning}
      className="text-xs px-2 py-1 border"
      style={{
        borderColor: isError
          ? "rgba(220,38,38,0.4)"
          : "var(--bg-border)",
        background: isError
          ? "var(--accent-red-dim)"
          : "var(--bg-surface)",
        color: isError
          ? "var(--accent-red)"
          : isSuccess
            ? "var(--accent-green)"
            : "var(--text-secondary)",
        cursor: isRunning ? "wait" : "pointer",
        borderRadius: 2,
        fontFamily: "inherit",
      }}
      onMouseEnter={(e) => {
        if (!isRunning && !isError && !isSuccess) {
          (e.currentTarget as HTMLElement).style.background =
            "var(--bg-hover)";
        }
      }}
      onMouseLeave={(e) => {
        if (!isRunning && !isError && !isSuccess) {
          (e.currentTarget as HTMLElement).style.background =
            "var(--bg-surface)";
        }
      }}
    >
      {isRunning
        ? "Running..."
        : isSuccess
          ? (state as { phase: "success"; message: string }).message
          : isError
            ? (state as { phase: "error"; message: string }).message
            : "Run Analysis"}
    </button>
  );
}
