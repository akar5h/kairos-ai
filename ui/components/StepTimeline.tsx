/**
 * StepTimeline — compact tool-call sequence strip.
 *
 * Ports the semantics of eval/review/app.py _render_step_timeline() to React.
 *
 * Layout:
 *   - Tool histogram summary row (counts busiest-first)
 *   - Compact strip of steps: status dot | tool name | latency | error badge
 *   - Consecutive same-tool runs are collapsed into a single row
 *   - Error steps expand with output on click
 */
import type { Step, TraceEnvelope } from "@/types/api";
import { StepStatusDot } from "@/components/StatusBadge";
import { formatArgs, formatLatency, tsOffset, truncate } from "@/lib/format";

interface StepTimelineProps {
  envelope: TraceEnvelope;
}

interface CollapsedRun {
  tool_name: string;
  first_index: number;
  last_index: number;
  count: number;
  hasErrors: boolean;
  steps: Step[];
}

/** Collapse consecutive same-tool runs. */
function collapseRuns(steps: Step[]): Array<Step | CollapsedRun> {
  const toolSteps = steps.filter((s) => s.step_type === "tool_call");
  const result: Array<Step | CollapsedRun> = [];
  let i = 0;

  while (i < toolSteps.length) {
    const cur = toolSteps[i];
    const tool = cur.tool_name ?? "unknown";
    let j = i + 1;
    while (
      j < toolSteps.length &&
      (toolSteps[j].tool_name ?? "unknown") === tool
    ) {
      j++;
    }
    const run = toolSteps.slice(i, j);
    if (run.length >= 3) {
      // Collapse runs of 3+ identical consecutive tool calls
      result.push({
        tool_name: tool,
        first_index: run[0].step_index,
        last_index: run[run.length - 1].step_index,
        count: run.length,
        hasErrors: run.some((s) => s.status === "error"),
        steps: run,
      } satisfies CollapsedRun);
    } else {
      result.push(...run);
    }
    i = j;
  }

  return result;
}

function isCollapsedRun(item: Step | CollapsedRun): item is CollapsedRun {
  return "count" in item;
}

export function StepTimeline({ envelope }: StepTimelineProps) {
  const { steps, tool_sequence, error_count, started_at } = envelope;
  const toolSteps = steps.filter((s) => s.step_type === "tool_call");

  if (toolSteps.length === 0) {
    return (
      <div className="py-12 text-center text-sm" style={{ color: "var(--text-muted)" }}>
        No tool calls recorded.
      </div>
    );
  }

  // Build tool histogram
  const hist: Record<string, number> = {};
  const errHist: Record<string, number> = {};
  for (const s of toolSteps) {
    const t = s.tool_name ?? "unknown";
    hist[t] = (hist[t] ?? 0) + 1;
    if (s.status === "error") errHist[t] = (errHist[t] ?? 0) + 1;
  }
  const histEntries = Object.entries(hist).sort((a, b) => b[1] - a[1]);

  const collapsed = collapseRuns(steps);

  return (
    <div className="flex flex-col gap-4">
      {/* Tool histogram summary */}
      <div
        className="rounded px-3 py-2 flex flex-wrap gap-x-4 gap-y-1 text-xs font-mono"
        style={{
          background: "var(--bg-elevated)",
          border: "1px solid var(--bg-border)",
        }}
      >
        <span style={{ color: "var(--text-muted)" }}>
          {tool_sequence.length} tool calls
        </span>
        {histEntries.map(([tool, count]) => (
          <span key={tool}>
            <span style={{ color: "var(--text-secondary)" }}>{tool}</span>
            <span style={{ color: "var(--text-muted)" }}>×{count}</span>
            {errHist[tool] ? (
              <span style={{ color: "var(--accent-red)" }}>
                {" "}
                ({errHist[tool]} err)
              </span>
            ) : null}
          </span>
        ))}
        {error_count > 0 && (
          <span style={{ color: "var(--accent-red)", fontWeight: 600 }}>
            ✕ {error_count} failed
          </span>
        )}
        {error_count === 0 && (
          <span style={{ color: "var(--accent-green)" }}>no failures</span>
        )}
      </div>

      {/* Step strip */}
      <div
        className="flex flex-col divide-y"
        style={{ borderTop: "1px solid var(--bg-border)", borderColor: "var(--bg-border)" }}
        role="list"
        aria-label="Step timeline"
      >
        {collapsed.map((item) =>
          isCollapsedRun(item) ? (
            <CollapsedRunRow key={`run-${item.first_index}`} run={item} />
          ) : (
            <TimelineStepRow key={item.step_index} step={item} traceStart={started_at} />
          ),
        )}
      </div>
    </div>
  );
}

// ── Row renderers ─────────────────────────────────────────────────────────────

function TimelineStepRow({
  step,
  traceStart,
}: {
  step: Step;
  traceStart: string | null;
}) {
  const isError = step.status === "error";
  const offset = tsOffset(traceStart, step.started_at);
  const argsStr = step.tool_args
    ? formatArgs(step.tool_args_normalized ?? step.tool_args, 120)
    : "";
  const outputStr = step.tool_output ? truncate(step.tool_output, 300) : null;

  return (
    <div
      role="listitem"
      className="flex flex-col gap-1 px-3 py-2"
      style={{
        background: isError ? "var(--accent-red-dim)" : "transparent",
        borderColor: "var(--bg-border)",
      }}
    >
      {/* Main row */}
      <div className="flex items-start gap-2.5 min-w-0">
        <StepStatusDot status={step.status} />

        {/* Step index */}
        <span
          className="text-xs font-mono tabular-nums shrink-0 w-5 text-right"
          style={{ color: "var(--text-muted)" }}
        >
          {step.step_index}
        </span>

        {/* Tool name */}
        <span
          className="text-xs font-mono font-medium shrink-0"
          style={{ color: isError ? "var(--accent-red)" : "var(--text-primary)" }}
        >
          {step.tool_name ?? "unknown"}
        </span>

        {/* Args digest */}
        {argsStr && (
          <span
            className="text-xs font-mono truncate flex-1 min-w-0"
            style={{ color: "var(--text-muted)" }}
            title={argsStr}
          >
            {argsStr}
          </span>
        )}

        {/* Metrics cluster — right-aligned */}
        <div className="flex items-center gap-2 shrink-0 ml-auto">
          {offset && (
            <span className="text-xs font-mono" style={{ color: "var(--text-muted)" }}>
              {offset}
            </span>
          )}
          {step.latency_ms != null && (
            <span className="text-xs font-mono tabular-nums" style={{ color: "var(--text-muted)" }}>
              {formatLatency(step.latency_ms)}
            </span>
          )}
          {step.status_source && step.status_source !== "none" && isError && (
            <span
              className="text-xs font-mono"
              style={{ color: "var(--accent-red)" }}
              title={`status set by: ${step.status_source}`}
            >
              [{step.status_source}]
            </span>
          )}
        </div>
      </div>

      {/* Error message inline */}
      {isError && step.error_message && (
        <p
          className="text-xs pl-10 leading-relaxed"
          style={{ color: "var(--accent-red)" }}
        >
          {truncate(step.error_message, 300)}
        </p>
      )}

      {/* Output — auto-expanded on error */}
      {outputStr && (
        <div className="pl-10">
          <details open={isError}>
            <summary
              className="cursor-pointer select-none text-xs list-none flex items-center gap-1"
              style={{ color: "var(--text-muted)" }}
            >
              <span className="text-[10px]">▶</span>
              output
            </summary>
            <pre
              className="mt-1 text-xs leading-relaxed break-all whitespace-pre-wrap rounded px-2 py-1.5 max-h-32 overflow-y-auto"
              style={{
                background: "var(--bg-elevated)",
                color: isError ? "var(--accent-red)" : "var(--text-secondary)",
                fontFamily: "var(--font-mono)",
                border: "1px solid var(--bg-border)",
              }}
            >
              {outputStr}
            </pre>
          </details>
        </div>
      )}
    </div>
  );
}

function CollapsedRunRow({ run }: { run: CollapsedRun }) {
  const hasErrors = run.hasErrors;
  const firstArgs = run.steps[0].tool_args
    ? formatArgs(run.steps[0].tool_args_normalized ?? run.steps[0].tool_args, 80)
    : "";
  const lastArgs = run.steps[run.steps.length - 1].tool_args
    ? formatArgs(
        run.steps[run.steps.length - 1].tool_args_normalized ??
          run.steps[run.steps.length - 1].tool_args,
        80,
      )
    : "";

  return (
    <details
      role="listitem"
      className="px-3 py-2"
      style={{ borderColor: "var(--bg-border)" }}
    >
      <summary
        className="cursor-pointer list-none flex items-center gap-2.5 text-xs font-mono select-none"
        style={{ color: "var(--text-muted)" }}
      >
        <span style={{ color: "var(--text-muted)" }}>⬜</span>
        <span
          className="font-medium"
          style={{ color: hasErrors ? "var(--accent-red)" : "var(--text-secondary)" }}
        >
          {run.tool_name}
        </span>
        <span>×{run.count}</span>
        <span>
          steps {run.first_index}–{run.last_index}
        </span>
        <span className="text-[10px]" style={{ color: "var(--text-muted)" }}>
          consecutive, collapsed
        </span>
        {hasErrors && (
          <span style={{ color: "var(--accent-red)" }}>
            — {run.steps.filter((s) => s.status === "error").length} err
          </span>
        )}
      </summary>
      <div
        className="mt-2 ml-8 text-xs space-y-1"
        style={{ color: "var(--text-muted)" }}
      >
        {firstArgs && (
          <div>
            first:{" "}
            <span className="font-mono" style={{ color: "var(--text-secondary)" }}>
              {firstArgs}
            </span>
          </div>
        )}
        {lastArgs && lastArgs !== firstArgs && (
          <div>
            last:{" "}
            <span className="font-mono" style={{ color: "var(--text-secondary)" }}>
              {lastArgs}
            </span>
          </div>
        )}
      </div>
    </details>
  );
}
