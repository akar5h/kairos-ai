import Link from "next/link";

export default function NotFound() {
  return (
    <div className="flex flex-col items-center justify-center flex-1 gap-4 py-24">
      <span
        className="font-mono text-4xl font-semibold"
        style={{ color: "var(--text-muted)" }}
      >
        404
      </span>
      <p className="text-sm" style={{ color: "var(--text-secondary)" }}>
        Trace not found.
      </p>
      <Link
        href="/"
        className="text-xs"
        style={{ color: "var(--text-link)" }}
      >
        ← Back to sessions
      </Link>
    </div>
  );
}
