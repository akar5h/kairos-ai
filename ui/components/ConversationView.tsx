/**
 * ConversationView — chronological interleaved conversation rendering.
 *
 * Ports the semantics of eval/review/app.py _render_conversation() to React,
 * consuming the TraceEnvelope's steps[] directly (no intermediate shape).
 *
 * Layout:
 *   - LLM steps → user/assistant turns (user_input as "user" turn, llm_output as assistant)
 *   - TOOL_CALL steps → tool call cards with args + output
 *   - RETRIEVAL steps → retrieval cards
 *   - Error steps are visually distinct (red border + badge)
 */
import type { TraceEnvelope, Step } from "@/types/api";
import { StepStatusDot } from "@/components/StatusBadge";
import {
  formatArgs,
  formatLatency,
  formatTimestamp,
  formatTokens,
  tsOffset,
  truncate,
} from "@/lib/format";

interface ConversationViewProps {
  envelope: TraceEnvelope;
}

export function ConversationView({ envelope }: ConversationViewProps) {
  const { steps, user_input, started_at } = envelope;

  if (steps.length === 0) {
    return (
      <div className="py-12 text-center text-sm" style={{ color: "var(--text-muted)" }}>
        No steps recorded for this trace.
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-0">
      {/* User intent banner — shown once at the top */}
      {user_input && (
        <UserTurnBlock text={user_input} ts={null} offset={null} isFirst />
      )}

      {steps.map((step) => (
        <StepBlock
          key={step.step_index}
          step={step}
          traceStart={started_at}
        />
      ))}
    </div>
  );
}

// ── Individual block renderers ────────────────────────────────────────────────

function OffsetChip({ offset }: { offset: string | null }) {
  if (!offset) return null;
  return (
    <span
      className="font-mono text-xs shrink-0"
      style={{ color: "var(--text-muted)" }}
    >
      {offset}
    </span>
  );
}

function UserTurnBlock({
  text,
  offset,
  isFirst = false,
}: {
  text: string;
  ts: string | null;
  offset: string | null;
  isFirst?: boolean;
}) {
  return (
    <div
      className="flex gap-3 px-4 py-3 border-b"
      style={{
        borderColor: "var(--bg-border)",
        background: isFirst ? "var(--accent-blue-dim)" : "transparent",
      }}
    >
      <span className="text-xs font-mono shrink-0 mt-0.5" style={{ color: "var(--accent-blue)" }}>
        USER
      </span>
      <div className="flex-1 min-w-0">
        <OffsetChip offset={offset} />
        <p className="text-sm mt-0.5 break-words" style={{ color: "var(--text-primary)" }}>
          {text}
        </p>
      </div>
    </div>
  );
}

function LLMBlock({ step, traceStart }: { step: Step; traceStart: string | null }) {
  const offset = tsOffset(traceStart, step.started_at);
  const hasOutput = Boolean(step.llm_output);

  return (
    <div
      className="flex gap-3 px-4 py-3 border-b"
      style={{ borderColor: "var(--bg-border)" }}
    >
      <span className="text-xs font-mono shrink-0 mt-0.5" style={{ color: "var(--text-muted)" }}>
        ASST
      </span>
      <div className="flex-1 min-w-0 flex flex-col gap-1">
        <div className="flex items-center gap-2">
          <OffsetChip offset={offset} />
          {step.llm_model && (
            <span className="text-xs font-mono" style={{ color: "var(--text-muted)" }}>
              {step.llm_model}
            </span>
          )}
          {step.total_tokens != null && (
            <span className="text-xs font-mono tabular-nums" style={{ color: "var(--text-muted)" }}>
              {formatTokens(step.total_tokens)} tok
            </span>
          )}
          {step.latency_ms != null && (
            <span className="text-xs font-mono tabular-nums" style={{ color: "var(--text-muted)" }}>
              {formatLatency(step.latency_ms)}
            </span>
          )}
        </div>
        {hasOutput && (
          <p
            className="text-sm leading-relaxed break-words"
            style={{ color: "var(--text-secondary)" }}
          >
            {truncate(step.llm_output!, 600)}
          </p>
        )}
      </div>
    </div>
  );
}

function ToolCallBlock({ step, traceStart }: { step: Step; traceStart: string | null }) {
  const isError = step.status === "error";
  const offset = tsOffset(traceStart, step.started_at);
  const argsStr = formatArgs(step.tool_args_normalized ?? step.tool_args, 400);
  const outputStr = step.tool_output ? truncate(step.tool_output, 500) : null;

  return (
    <div
      className="flex gap-3 px-4 py-3 border-b border-l-2"
      style={{
        borderColor: "var(--bg-border)",
        borderLeftColor: isError ? "var(--accent-red)" : "var(--bg-border)",
        background: isError ? "var(--accent-red-dim)" : "transparent",
      }}
    >
      {/* Status dot */}
      <StepStatusDot status={step.status} />

      <div className="flex-1 min-w-0 flex flex-col gap-1.5">
        {/* Header row: tool name + metadata */}
        <div className="flex flex-wrap items-center gap-2">
          <span
            className="text-xs font-mono font-semibold"
            style={{ color: isError ? "var(--accent-red)" : "var(--text-primary)" }}
          >
            {step.tool_name ?? "unknown_tool"}
          </span>
          <OffsetChip offset={offset} />
          {step.latency_ms != null && (
            <span className="text-xs font-mono tabular-nums" style={{ color: "var(--text-muted)" }}>
              {formatLatency(step.latency_ms)}
            </span>
          )}
          {isError && step.status_source !== "none" && (
            <span
              className="text-xs font-mono rounded px-1 py-0.5"
              style={{
                background: "var(--accent-red-dim)",
                color: "var(--accent-red)",
                border: "1px solid rgba(220,38,38,0.3)",
              }}
            >
              ERR via {step.status_source}
            </span>
          )}
        </div>

        {/* Args */}
        {argsStr && (
          <pre
            className="text-xs leading-relaxed break-all whitespace-pre-wrap rounded px-2 py-1.5"
            style={{
              background: "var(--bg-elevated)",
              color: "var(--text-secondary)",
              fontFamily: "var(--font-mono)",
              border: "1px solid var(--bg-border)",
            }}
          >
            {argsStr}
          </pre>
        )}

        {/* Error message */}
        {isError && step.error_message && (
          <p className="text-xs" style={{ color: "var(--accent-red)" }}>
            {step.error_message}
          </p>
        )}

        {/* Tool output */}
        {outputStr && (
          <ExpandableOutput
            content={outputStr}
            isError={isError}
            fullContent={step.tool_output ?? ""}
          />
        )}
      </div>
    </div>
  );
}

function RetrievalBlock({ step, traceStart }: { step: Step; traceStart: string | null }) {
  const offset = tsOffset(traceStart, step.started_at);

  return (
    <div
      className="flex gap-3 px-4 py-3 border-b"
      style={{ borderColor: "var(--bg-border)" }}
    >
      <span
        className="text-xs font-mono shrink-0 mt-0.5"
        style={{ color: "var(--accent-purple)" }}
      >
        RETR
      </span>
      <div className="flex-1 min-w-0 flex flex-col gap-1">
        <div className="flex items-center gap-2">
          <OffsetChip offset={offset} />
        </div>
        {step.retrieval_query && (
          <p className="text-xs" style={{ color: "var(--text-secondary)" }}>
            query: {truncate(step.retrieval_query, 200)}
          </p>
        )}
        {step.retrieval_chunks && step.retrieval_chunks.length > 0 && (
          <p className="text-xs" style={{ color: "var(--text-muted)" }}>
            {step.retrieval_chunks.length} chunk{step.retrieval_chunks.length !== 1 ? "s" : ""} returned
          </p>
        )}
      </div>
    </div>
  );
}

function StepBlock({ step, traceStart }: { step: Step; traceStart: string | null }) {
  switch (step.step_type) {
    case "llm":
      return <LLMBlock step={step} traceStart={traceStart} />;
    case "tool_call":
      return <ToolCallBlock step={step} traceStart={traceStart} />;
    case "retrieval":
      return <RetrievalBlock step={step} traceStart={traceStart} />;
    case "agent":
    case "other":
      return <OtherBlock step={step} traceStart={traceStart} />;
  }
}

function OtherBlock({ step, traceStart }: { step: Step; traceStart: string | null }) {
  const offset = tsOffset(traceStart, step.started_at);
  return (
    <div
      className="flex gap-3 px-4 py-2 border-b"
      style={{ borderColor: "var(--bg-border)" }}
    >
      <span className="text-xs font-mono shrink-0 mt-0.5" style={{ color: "var(--text-muted)" }}>
        {step.step_type.toUpperCase()}
      </span>
      <div className="flex items-center gap-2">
        <OffsetChip offset={offset} />
        {step.node_name && (
          <span className="text-xs font-mono" style={{ color: "var(--text-muted)" }}>
            {step.node_name}
          </span>
        )}
      </div>
    </div>
  );
}

// ── Collapsible output block ──────────────────────────────────────────────────

function ExpandableOutput({
  content,
  isError,
  fullContent,
}: {
  content: string;
  isError: boolean;
  fullContent: string;
}) {
  const isTruncated = fullContent.length > content.length;

  return (
    <details open={isError} className="group">
      <summary
        className="cursor-pointer select-none text-xs flex items-center gap-1 list-none"
        style={{ color: "var(--text-muted)" }}
      >
        <span className="group-open:rotate-90 transition-transform inline-block text-[10px]">▶</span>
        output{isTruncated ? " (truncated)" : ""}
      </summary>
      <pre
        className="mt-1 text-xs leading-relaxed break-all whitespace-pre-wrap rounded px-2 py-1.5 max-h-48 overflow-y-auto"
        style={{
          background: "var(--bg-elevated)",
          color: isError ? "var(--accent-red)" : "var(--text-secondary)",
          fontFamily: "var(--font-mono)",
          border: "1px solid var(--bg-border)",
        }}
      >
        {content}
      </pre>
    </details>
  );
}

// Export formatTimestamp used in trace detail header
export { formatTimestamp };
