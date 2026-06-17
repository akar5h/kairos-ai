"use client";

/**
 * RawSpansTree — indent-by-parent span tree for the Trace view "Raw Spans" tab.
 *
 * Renders GET /v1/traces/{id}/spans response as a tree, ordered by start_time.
 * Each row: indent | STATUS | name/tool_name | duration | attributes (expandable).
 * Click a row to expand a structured detail panel inline below it.
 */
import { useState } from "react";
import type { RawSpan } from "@/types/api";
import { durationMs, formatLatency, shortId } from "@/lib/format";

const INPUT_COST = 3 / 1_000_000;
const OUTPUT_COST = 15 / 1_000_000;
const CACHE_READ_COST = 0.30 / 1_000_000;
const CACHE_CREATE_COST = 3.75 / 1_000_000;

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
  const [expandedSpanId, setExpandedSpanId] = useState<string | null>(null);

  if (spans.length === 0) {
    return (
      <div className="py-12 text-center text-xs" style={{ color: "var(--text-muted)" }}>
        No spans for this trace.
      </div>
    );
  }

  const roots = buildTree(spans);
  const flat = flatten(roots);

  function toggleExpand(spanId: string) {
    setExpandedSpanId((prev) => (prev === spanId ? null : spanId));
  }

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
          <SpanRow
            key={span.span_id}
            span={span}
            showFull={showFull}
            detailExpanded={expandedSpanId === span.span_id}
            onToggleDetail={() => toggleExpand(span.span_id)}
          />
        ))}
      </div>
    </div>
  );
}

function SpanRow({
  span,
  showFull,
  detailExpanded,
  onToggleDetail,
}: {
  span: SpanNode;
  showFull: boolean;
  detailExpanded: boolean;
  onToggleDetail: () => void;
}) {
  const [showAllAttrs, setShowAllAttrs] = useState(false);
  const isError = span.status_code === "ERROR";
  const isOk = span.status_code === "OK";
  const dur = durationMs(span.start_time, span.end_time);

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
    <div role="treeitem" aria-selected={false} aria-expanded={detailExpanded}>
      <div
        className="flex items-center gap-2 px-4 cursor-pointer"
        style={{
          height: 34,
          borderBottom: "1px solid var(--bg-border)",
          background: isError ? "rgba(220,38,38,0.04)" : undefined,
          paddingLeft: `${16 + span.depth * 20}px`,
        }}
        onClick={onToggleDetail}
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
            transform: detailExpanded ? "rotate(90deg)" : "none",
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

      {/* Detail panel */}
      {detailExpanded && (
        <SpanDetailPanel
          span={span}
          dur={dur}
          attrEntries={attrEntries}
          showAllAttrs={showAllAttrs}
          onToggleAllAttrs={() => setShowAllAttrs((v) => !v)}
        />
      )}
    </div>
  );
}

function SpanDetailPanel({
  span,
  dur,
  attrEntries,
  showAllAttrs,
  onToggleAllAttrs,
}: {
  span: SpanNode;
  dur: number | null;
  attrEntries: [string, unknown][];
  showAllAttrs: boolean;
  onToggleAllAttrs: () => void;
}) {
  const attrs = span.attributes;
  const isLlmRequest = span.name === "claude_code.llm_request";
  const isToolSpan = span.name === "claude_code.tool";

  const inputTokens = Number(attrs["llm.usage.prompt_tokens"] ?? attrs["gen_ai.usage.input_tokens"] ?? 0);
  const outputTokens = Number(attrs["llm.usage.completion_tokens"] ?? attrs["gen_ai.usage.output_tokens"] ?? 0);
  const cacheReadTokens = Number(attrs["gen_ai.usage.cache_read_input_tokens"] ?? 0);
  const cacheCreateTokens = Number(attrs["gen_ai.usage.cache_creation_input_tokens"] ?? 0);
  const ttftMs = attrs["gen_ai.usage.ttft_ms"] != null ? Number(attrs["gen_ai.usage.ttft_ms"]) : null;
  const requestId = attrs["gen_ai.request.id"] ?? attrs["llm.request_id"] ?? null;
  const stopReason = attrs["gen_ai.usage.stop_reason"] ?? attrs["llm.stop_reason"] ?? null;
  const model =
    attrs["gen_ai.request.model"] ??
    attrs["llm.model"] ??
    "claude-opus-4-8";

  const estimatedCost =
    inputTokens * INPUT_COST +
    outputTokens * OUTPUT_COST +
    cacheReadTokens * CACHE_READ_COST +
    cacheCreateTokens * CACHE_CREATE_COST;

  const panelPl = `${16 + span.depth * 20 + 20}px`;

  return (
    <div
      style={{
        paddingLeft: panelPl,
        paddingRight: 16,
        paddingTop: 12,
        paddingBottom: 12,
        background: "var(--bg-elevated)",
        borderTop: "1px solid var(--bg-border)",
        borderBottom: "1px solid var(--bg-border)",
        fontFamily: "var(--font-geist-mono), monospace",
        fontSize: 11,
      }}
    >
      {isLlmRequest && (
        <div className="flex flex-col gap-1 mb-3">
          <DetailRow label="MODEL" value={String(model)} />
          <div className="flex gap-8">
            <div className="flex flex-col gap-1">
              <DetailRow label="INPUT TOKENS" value={inputTokens.toLocaleString()} />
              <DetailRow label="OUTPUT TOKENS" value={outputTokens.toLocaleString()} />
              {ttftMs != null && (
                <DetailRow label="TTFT" value={`${ttftMs.toLocaleString()}ms`} />
              )}
              {stopReason && (
                <DetailRow label="STOP REASON" value={String(stopReason)} />
              )}
            </div>
            <div className="flex flex-col gap-1">
              <DetailRow label="CACHE READ" value={cacheReadTokens.toLocaleString()} />
              <DetailRow label="CACHE CREATE" value={cacheCreateTokens.toLocaleString()} />
              {dur != null && (
                <DetailRow label="DURATION" value={`${dur.toLocaleString()}ms`} />
              )}
              <DetailRow
                label="EST. COST"
                value={`$${estimatedCost.toFixed(4)}`}
              />
            </div>
          </div>
          {requestId && (
            <DetailRow label="REQUEST ID" value={String(requestId)} />
          )}
        </div>
      )}

      {isToolSpan && (
        <div className="flex flex-col gap-1 mb-3">
          <DetailRow label="TOOL" value={span.tool_name ?? span.name} />
          <DetailRow label="STATUS" value={span.status_code ?? "UNSET"} />
          {dur != null && (
            <DetailRow label="DURATION" value={formatLatency(dur)} />
          )}
          <p
            className="mt-1"
            style={{ color: "var(--text-muted)", fontSize: 10 }}
          >
            Input/output captured via hooks only
          </p>
        </div>
      )}

      {!isLlmRequest && !isToolSpan && dur != null && (
        <div className="flex flex-col gap-1 mb-3">
          <DetailRow label="DURATION" value={formatLatency(dur)} />
          <DetailRow label="STATUS" value={span.status_code ?? "UNSET"} />
        </div>
      )}

      {/* Raw attrs toggle */}
      {attrEntries.length > 0 && (
        <div>
          <button
            onClick={onToggleAllAttrs}
            className="text-xs mb-1"
            style={{
              color: "var(--text-link)",
              background: "none",
              border: "none",
              cursor: "pointer",
              padding: 0,
              fontFamily: "inherit",
              fontSize: 10,
            }}
          >
            {showAllAttrs ? "▾ hide attrs" : "▸ full attrs"}
          </button>
          {showAllAttrs && (
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
          )}
        </div>
      )}
    </div>
  );
}

function DetailRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline gap-3">
      <span
        style={{
          color: "var(--text-muted)",
          fontSize: 10,
          fontWeight: 600,
          letterSpacing: "0.06em",
          minWidth: 100,
          flexShrink: 0,
        }}
      >
        {label}
      </span>
      <span style={{ color: "var(--text-primary)" }}>{value}</span>
    </div>
  );
}
