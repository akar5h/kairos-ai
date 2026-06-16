"use client";

/**
 * RawSpansTree — indent-by-parent span tree for the Trace view "Raw Spans" tab.
 *
 * Renders GET /v1/traces/{id}/spans response as a tree, ordered by start_time.
 * Each row: indent | STATUS | name/tool_name | duration | attributes (expandable).
 */
import { useState } from "react";
import type { RawSpan } from "@/types/api";
import { durationMs, formatLatency, shortId } from "@/lib/format";

interface RawSpansTreeProps {
  spans: RawSpan[];
}

interface SpanNode extends RawSpan {
  children: SpanNode[];
  depth: number;
}

function buildTree(spans: RawSpan[]): SpanNode[] {
  const byId = new Map<string, SpanNode>();
  for (const s of spans) {
    byId.set(s.span_id, { ...s, children: [], depth: 0 });
  }

  const roots: SpanNode[] = [];
  for (const node of byId.values()) {
    if (node.parent_span_id && byId.has(node.parent_span_id)) {
      byId.get(node.parent_span_id)!.children.push(node);
    } else {
      roots.push(node);
    }
  }

  // Assign depths via BFS
  function assignDepth(nodes: SpanNode[], depth: number) {
    for (const n of nodes) {
      n.depth = depth;
      assignDepth(n.children, depth + 1);
    }
  }
  assignDepth(roots, 0);

  return roots;
}

function flatten(nodes: SpanNode[]): SpanNode[] {
  const result: SpanNode[] = [];
  function walk(ns: SpanNode[]) {
    for (const n of ns) {
      result.push(n);
      walk(n.children);
    }
  }
  walk(nodes);
  return result;
}

export function RawSpansTree({ spans }: RawSpansTreeProps) {
  const [showFull, setShowFull] = useState(false);

  if (spans.length === 0) {
    return (
      <div className="py-12 text-center text-xs" style={{ color: "var(--text-muted)" }}>
        No spans for this trace.
      </div>
    );
  }

  const roots = buildTree(spans);
  const flat = flatten(roots);

  return (
    <div>
      {/* Toolbar */}
      <div
        className="flex items-center gap-3 px-4 py-1.5 border-b"
        style={{ borderColor: "var(--bg-border)", background: "var(--bg-surface)" }}
      >
        <span className="text-xs font-mono tabular-nums" style={{ color: "var(--text-muted)" }}>
          {spans.length} span{spans.length !== 1 ? "s" : ""}
        </span>
        <label
          className="flex items-center gap-1.5 cursor-pointer select-none text-xs ml-auto"
          style={{ color: "var(--text-muted)" }}
        >
          <input
            type="checkbox"
            checked={showFull}
            onChange={(e) => setShowFull(e.target.checked)}
            className="w-3 h-3"
            aria-label="Show full attributes"
          />
          full attrs
        </label>
      </div>

      {/* Column headers */}
      <div
        className="grid px-4 py-1.5 text-left border-b"
        style={{
          gridTemplateColumns: "24px minmax(200px,1fr) 80px 70px 60px",
          borderColor: "var(--bg-border)",
          background: "var(--bg-surface)",
          position: "sticky",
          top: 0,
          zIndex: 10,
        }}
      >
        {["", "SPAN", "TOOL", "STATUS", "DUR"].map((h) => (
          <span
            key={h}
            style={{
              color: "var(--text-muted)",
              fontSize: 10,
              fontWeight: 600,
              letterSpacing: "0.06em",
            }}
          >
            {h}
          </span>
        ))}
      </div>

      {/* Span rows */}
      <div role="tree" aria-label="Span tree">
        {flat.map((span) => (
          <SpanRow key={span.span_id} span={span} showFull={showFull} />
        ))}
      </div>
    </div>
  );
}

function SpanRow({ span, showFull }: { span: SpanNode; showFull: boolean }) {
  const [expanded, setExpanded] = useState(false);
  const isError = span.status_code === "ERROR";
  const isOk = span.status_code === "OK";
  const dur = durationMs(span.start_time, span.end_time);
  const hasAttrs = Object.keys(span.attributes).length > 0;

  const displayName = span.tool_name ?? span.name;
  const subName = span.tool_name ? span.name : null;

  const filteredAttrs = showFull
    ? span.attributes
    : Object.fromEntries(
        Object.entries(span.attributes).filter(
          ([, v]) => v != null && v !== "" && v !== false,
        ),
      );
  const attrEntries = Object.entries(filteredAttrs);

  return (
    <div role="treeitem" aria-selected={false} aria-expanded={hasAttrs ? expanded : undefined}>
      <div
        className="flex items-center gap-2 px-4 cursor-pointer"
        style={{
          height: 34,
          borderBottom: "1px solid var(--bg-border)",
          background: isError ? "rgba(220,38,38,0.04)" : undefined,
          paddingLeft: `${16 + span.depth * 20}px`,
        }}
        onClick={() => hasAttrs && setExpanded((v) => !v)}
        onMouseEnter={(e) => {
          (e.currentTarget as HTMLElement).style.background = isError
            ? "rgba(220,38,38,0.07)"
            : "var(--bg-hover)";
        }}
        onMouseLeave={(e) => {
          (e.currentTarget as HTMLElement).style.background = isError
            ? "rgba(220,38,38,0.04)"
            : "";
        }}
      >
        {/* Expand toggle */}
        <span
          style={{
            width: 14,
            fontSize: 10,
            color: "var(--text-muted)",
            flexShrink: 0,
            visibility: hasAttrs ? "visible" : "hidden",
            transform: expanded ? "rotate(90deg)" : "none",
            display: "inline-block",
            transition: "transform 0.1s",
          }}
        >
          ▸
        </span>

        {/* Span name */}
        <span
          className="font-mono truncate flex-1 min-w-0"
          style={{
            fontSize: 12,
            color: isError ? "var(--accent-red)" : "var(--text-primary)",
          }}
          title={`${displayName}${subName ? ` (${subName})` : ""} — ${span.span_id}`}
        >
          {displayName}
          {subName && (
            <span style={{ color: "var(--text-muted)", marginLeft: 4 }}>
              {subName}
            </span>
          )}
        </span>

        {/* Tool chip (if name differs from tool_name) */}
        {span.tool_name && span.tool_name !== span.name && (
          <span
            className="font-mono shrink-0 rounded px-1"
            style={{
              background: "var(--bg-elevated)",
              color: "var(--text-secondary)",
              border: "1px solid var(--bg-border)",
              fontSize: 10,
              whiteSpace: "nowrap",
            }}
          >
            {span.tool_name}
          </span>
        )}

        {/* Status */}
        <span
          className="font-mono shrink-0 tabular-nums"
          style={{
            width: 44,
            fontSize: 11,
            color: isError
              ? "var(--accent-red)"
              : isOk
                ? "var(--accent-green)"
                : "var(--text-muted)",
            textAlign: "right",
          }}
        >
          {span.status_code ?? "—"}
        </span>

        {/* Duration */}
        <span
          className="font-mono shrink-0 tabular-nums text-right"
          style={{ width: 54, fontSize: 11, color: "var(--text-muted)" }}
        >
          {dur != null ? formatLatency(dur) : "—"}
        </span>

        {/* Span ID (small) */}
        <span
          className="font-mono shrink-0 hidden lg:inline"
          style={{ color: "var(--text-muted)", fontSize: 10, width: 60, textAlign: "right" }}
          title={span.span_id}
        >
          {shortId(span.span_id)}
        </span>
      </div>

      {/* Attributes panel */}
      {expanded && attrEntries.length > 0 && (
        <div
          className="px-4 py-2"
          style={{
            paddingLeft: `${16 + span.depth * 20 + 20}px`,
            background: "var(--bg-surface)",
            borderBottom: "1px solid var(--bg-border)",
          }}
        >
          <table className="w-full border-collapse" style={{ fontSize: 11 }}>
            <tbody>
              {attrEntries.map(([k, v]) => (
                <tr key={k}>
                  <td
                    className="font-mono pr-4 py-0.5 align-top"
                    style={{ color: "var(--text-muted)", whiteSpace: "nowrap" }}
                  >
                    {k}
                  </td>
                  <td
                    className="font-mono py-0.5 break-all"
                    style={{ color: "var(--text-secondary)" }}
                  >
                    {typeof v === "object" ? JSON.stringify(v) : String(v)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
