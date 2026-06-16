"use client";

import { useState, useRef, useEffect, useCallback, useTransition } from "react";
import { useRouter } from "next/navigation";
import type { SearchHits } from "@/types/api";
import { shortId, relativeTime } from "@/lib/format";

// Debounce helper — only state is debounced value; no setState in effect
function useDebouncedCallback(callback: (val: string) => void, ms: number) {
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  return useCallback(
    (val: string) => {
      if (timerRef.current) clearTimeout(timerRef.current);
      timerRef.current = setTimeout(() => callback(val), ms);
    },
    [callback, ms],
  );
}

export function SearchBar() {
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<SearchHits | null>(null);
  const [open, setOpen] = useState(false);
  const [isPending, startTransition] = useTransition();
  const router = useRouter();
  const wrapperRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const activeQueryRef = useRef<string>("");

  const doSearch = useCallback((q: string) => {
    if (!q.trim()) {
      setHits(null);
      setOpen(false);
      return;
    }
    activeQueryRef.current = q;
    const base = process.env.NEXT_PUBLIC_KAIROS_API ?? "http://localhost:8000";
    const url = `${base}/v1/search?q=${encodeURIComponent(q)}&limit=8`;
    startTransition(() => {
      fetch(url, { cache: "no-store" })
        .then((r) => (r.ok ? r.json() : null))
        .then((data: SearchHits | null) => {
          if (activeQueryRef.current === q && data) {
            setHits(data);
            setOpen(true);
          }
        })
        .catch(() => {
          /* ignore search errors */
        });
    });
  }, []);

  const debouncedSearch = useDebouncedCallback(doSearch, 280);

  const handleQueryChange = (val: string) => {
    setQuery(val);
    debouncedSearch(val);
  };

  // Close on outside click
  useEffect(() => {
    function onOutside(e: MouseEvent) {
      if (wrapperRef.current && !wrapperRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", onOutside);
    return () => document.removeEventListener("mousedown", onOutside);
  }, []);

  const navigate = useCallback(
    (path: string) => {
      setOpen(false);
      setQuery("");
      router.push(path);
    },
    [router],
  );

  const showDropdown = open && query.trim().length > 0;
  const totalHits =
    (hits?.sessions.length ?? 0) +
    (hits?.traces.length ?? 0) +
    (hits?.spans.length ?? 0);

  return (
    <div ref={wrapperRef} className="relative w-full">
      <div
        className="flex items-center gap-1.5 rounded px-2 h-7"
        style={{
          background: "var(--bg-elevated)",
          border: "1px solid var(--bg-border)",
        }}
      >
        <span style={{ color: "var(--text-muted)", fontSize: 11 }}>⌕</span>
        <input
          ref={inputRef}
          type="search"
          value={query}
          onChange={(e) => handleQueryChange(e.target.value)}
          onFocus={() => hits && setOpen(true)}
          onKeyDown={(e) => {
            if (e.key === "Escape") {
              setOpen(false);
              inputRef.current?.blur();
            }
          }}
          placeholder="search sessions, traces, spans…"
          aria-label="Search"
          className="flex-1 bg-transparent border-none outline-none text-xs"
          style={{ color: "var(--text-primary)" }}
          autoComplete="off"
        />
        {isPending && (
          <span className="text-[10px]" style={{ color: "var(--text-muted)" }}>
            …
          </span>
        )}
      </div>

      {showDropdown && hits && totalHits > 0 && (
        <div
          className="absolute top-full left-0 right-0 mt-1 rounded border overflow-hidden z-50"
          style={{
            background: "var(--bg-base)",
            border: "1px solid var(--bg-border)",
            boxShadow: "0 4px 12px rgba(0,0,0,0.08)",
          }}
          role="listbox"
          aria-label="Search results"
        >
          {/* Sessions */}
          {hits.sessions.length > 0 && (
            <ResultGroup label="sessions">
              {hits.sessions.map((s) => (
                <ResultRow
                  key={s.session_id}
                  onClick={() => navigate(`/sessions/${encodeURIComponent(s.session_id)}`)}
                >
                  <span
                    className="font-mono text-xs shrink-0"
                    style={{ color: "var(--accent-blue)" }}
                  >
                    {shortId(s.session_id)}
                  </span>
                  <span
                    className="text-xs truncate flex-1"
                    style={{ color: "var(--text-secondary)" }}
                    title={s.session_id}
                  >
                    {s.session_id}
                  </span>
                  <span
                    className="font-mono text-xs shrink-0 tabular-nums"
                    style={{ color: "var(--text-muted)" }}
                  >
                    {s.trace_count}t · {s.span_count}s
                  </span>
                  {s.started_at && (
                    <span className="text-xs shrink-0" style={{ color: "var(--text-muted)" }}>
                      {relativeTime(s.started_at)}
                    </span>
                  )}
                </ResultRow>
              ))}
            </ResultGroup>
          )}

          {/* Traces */}
          {hits.traces.length > 0 && (
            <ResultGroup label="traces">
              {hits.traces.map((t) => (
                <ResultRow
                  key={t.trace_id}
                  onClick={() => navigate(`/traces/${t.trace_id}`)}
                >
                  <span
                    className="font-mono text-xs shrink-0"
                    style={{ color: "var(--text-secondary)" }}
                  >
                    {shortId(t.trace_id)}
                  </span>
                  <span
                    className="text-xs truncate flex-1"
                    style={{ color: "var(--text-muted)" }}
                  >
                    {t.trace_id.slice(8, 24)}…
                  </span>
                  {t.error_count != null && t.error_count > 0 && (
                    <span
                      className="font-mono text-xs shrink-0"
                      style={{ color: "var(--accent-red)" }}
                    >
                      {t.error_count} err
                    </span>
                  )}
                  {t.span_count != null && (
                    <span
                      className="font-mono text-xs shrink-0 tabular-nums"
                      style={{ color: "var(--text-muted)" }}
                    >
                      {t.span_count}s
                    </span>
                  )}
                </ResultRow>
              ))}
            </ResultGroup>
          )}

          {/* Spans */}
          {hits.spans.length > 0 && (
            <ResultGroup label="spans">
              {hits.spans.map((sp) => (
                <ResultRow
                  key={sp.span_id}
                  onClick={() => navigate(`/traces/${sp.trace_id}?view=spans`)}
                >
                  <span
                    className="font-mono text-xs shrink-0"
                    style={{ color: "var(--text-muted)" }}
                  >
                    {shortId(sp.span_id)}
                  </span>
                  <span
                    className="font-mono text-xs shrink-0"
                    style={{
                      color:
                        sp.status_code === "ERROR"
                          ? "var(--accent-red)"
                          : sp.tool_name
                            ? "var(--text-primary)"
                            : "var(--text-secondary)",
                    }}
                  >
                    {sp.tool_name ?? sp.name}
                  </span>
                  {sp.status_code === "ERROR" && (
                    <span
                      className="text-xs rounded px-1"
                      style={{
                        background: "var(--accent-red-dim)",
                        color: "var(--accent-red)",
                      }}
                    >
                      ERR
                    </span>
                  )}
                  <span
                    className="text-xs truncate flex-1"
                    style={{ color: "var(--text-muted)" }}
                  >
                    → {shortId(sp.trace_id)}
                  </span>
                </ResultRow>
              ))}
            </ResultGroup>
          )}
        </div>
      )}

      {showDropdown && hits && totalHits === 0 && !isPending && (
        <div
          className="absolute top-full left-0 right-0 mt-1 rounded border px-3 py-2 text-xs z-50"
          style={{
            background: "var(--bg-base)",
            border: "1px solid var(--bg-border)",
            color: "var(--text-muted)",
          }}
        >
          No results for &ldquo;{query}&rdquo;
        </div>
      )}
    </div>
  );
}

function ResultGroup({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <div
        className="px-3 py-1 text-[10px] font-semibold uppercase tracking-widest"
        style={{
          color: "var(--text-muted)",
          background: "var(--bg-surface)",
          borderBottom: "1px solid var(--bg-border)",
        }}
      >
        {label}
      </div>
      {children}
    </div>
  );
}

function ResultRow({
  onClick,
  children,
}: {
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      aria-selected={false}
      role="option"
      onClick={onClick}
      className="w-full flex items-center gap-2 px-3 py-1.5 text-left cursor-pointer"
      style={{ borderBottom: "1px solid var(--bg-border)" }}
      onMouseEnter={(e) => {
        (e.currentTarget as HTMLElement).style.background = "var(--bg-hover)";
      }}
      onMouseLeave={(e) => {
        (e.currentTarget as HTMLElement).style.background = "transparent";
      }}
    >
      {children}
    </button>
  );
}
