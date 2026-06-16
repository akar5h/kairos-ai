/**
 * Shared formatting utilities — dates, trace IDs, token counts.
 */

/**
 * Return a human-readable relative time string (e.g. "3m ago", "2h ago").
 * Falls back to ISO date string when input is null.
 */
export function relativeTime(isoString: string | null): string {
  if (!isoString) return "—";
  const then = new Date(isoString);
  const diffMs = Date.now() - then.getTime();
  const diffSec = Math.floor(diffMs / 1000);

  if (diffSec < 60) return `${diffSec}s ago`;
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m ago`;
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h ago`;
  return `${Math.floor(diffSec / 86400)}d ago`;
}

/**
 * Format a timestamp for display in detail views.
 */
export function formatTimestamp(isoString: string | null): string {
  if (!isoString) return "—";
  return new Date(isoString).toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

/**
 * Truncate a trace ID for display — show first 8 chars.
 */
export function shortTraceId(traceId: string): string {
  return traceId.slice(0, 8);
}

/**
 * Format token count as compact string (e.g. 1.2k).
 */
export function formatTokens(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n < 1000) return String(n);
  return `${(n / 1000).toFixed(1)}k`;
}

/**
 * Compact-format latency in ms or seconds.
 */
export function formatLatency(ms: number | null | undefined): string {
  if (ms == null) return "—";
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

/**
 * Truncate a long string to maxLen chars, appending "…".
 */
export function truncate(s: string, maxLen: number): string {
  if (s.length <= maxLen) return s;
  return s.slice(0, maxLen) + "…";
}

/**
 * JSON-serialize tool args for display, truncated.
 */
export function formatArgs(
  args: Record<string, unknown> | null | undefined,
  maxLen = 200,
): string {
  if (!args) return "";
  const keys = Object.keys(args);
  if (keys.length === 0) return "{}";

  // For single-key args, show key=value inline
  if (keys.length === 1) {
    const val = args[keys[0]];
    const strVal =
      typeof val === "string" ? val : JSON.stringify(val);
    return truncate(`${keys[0]}=${strVal}`, maxLen);
  }

  return truncate(JSON.stringify(args, null, 0), maxLen);
}

/**
 * Offset in seconds from a base time, formatted as "+Ns".
 */
export function tsOffset(
  base: string | null,
  ts: string | null,
): string | null {
  if (!base || !ts) return null;
  const diffMs = new Date(ts).getTime() - new Date(base).getTime();
  const diffSec = Math.round(diffMs / 1000);
  if (isNaN(diffSec)) return null;
  return `+${diffSec}s`;
}
