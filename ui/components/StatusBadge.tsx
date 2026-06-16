/**
 * StatusBadge — compact colored badge for error counts and step statuses.
 */
interface StatusBadgeProps {
  count: number;
  label?: string;
}

/** Error count badge — red when > 0, muted when 0. */
export function ErrorBadge({ count, label }: StatusBadgeProps) {
  const hasErrors = count > 0;
  return (
    <span
      className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-xs font-mono font-medium tabular-nums"
      style={{
        background: hasErrors ? "var(--accent-red-dim)" : "transparent",
        color: hasErrors ? "var(--accent-red)" : "var(--text-muted)",
        border: `1px solid ${hasErrors ? "var(--accent-red)" : "var(--bg-border)"}`,
      }}
      aria-label={`${count} ${label ?? "error"}${count !== 1 ? "s" : ""}`}
    >
      {hasErrors && <span aria-hidden="true">✕</span>}
      {count}
    </span>
  );
}

/** Terminal status chip. */
export function TerminalBadge({ status }: { status: string }) {
  const color =
    status === "completed"
      ? "var(--accent-green)"
      : status === "error" || status === "timeout"
        ? "var(--accent-red)"
        : "var(--text-muted)";
  const bg =
    status === "completed"
      ? "var(--accent-green-dim)"
      : status === "error" || status === "timeout"
        ? "var(--accent-red-dim)"
        : "var(--bg-elevated)";

  return (
    <span
      className="inline-block rounded px-1.5 py-0.5 text-xs font-mono uppercase tracking-wider"
      style={{ background: bg, color }}
    >
      {status}
    </span>
  );
}

/** Step status dot. */
export function StepStatusDot({ status }: { status: "ok" | "error" }) {
  return (
    <span
      aria-label={status}
      className="inline-block w-1.5 h-1.5 rounded-full shrink-0 mt-1.5"
      style={{
        background:
          status === "error" ? "var(--accent-red)" : "var(--accent-green)",
      }}
    />
  );
}
