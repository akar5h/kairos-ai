"use client";

import { useState } from "react";

export function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // clipboard not available (e.g. in tests)
    }
  };

  return (
    <button
      onClick={handleCopy}
      aria-label={copied ? "Copied" : label}
      className="text-xs rounded px-1.5 py-0.5 transition-colors cursor-pointer"
      style={{
        background: copied ? "var(--accent-blue-dim)" : "transparent",
        color: copied ? "var(--accent-blue)" : "var(--text-muted)",
        border: "1px solid var(--bg-border)",
      }}
    >
      {copied ? "copied" : "copy"}
    </button>
  );
}
