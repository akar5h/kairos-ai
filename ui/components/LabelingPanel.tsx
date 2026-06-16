"use client";

/**
 * LabelingPanel — relabel-as-a-feature (F2.4).
 *
 * Lists existing labels for a trace (initialLabels from server, refetched
 * after a successful POST), and an append-only form to add a label.
 *
 * Form → POST /v1/labels body:
 *   { trace_id, answer (req), question?, verdict?: "tp"|"fp"|"fn"|null, label_class? }
 *
 * Keyboard-submittable: Cmd/Ctrl+Enter from the answer textarea submits.
 */
import { useState, useTransition } from "react";
import type { CreateLabelBody, LabelRow, Verdict } from "@/types/api";
import { createLabel, getLabels } from "@/lib/api";
import { formatTimestamp } from "@/lib/format";

interface LabelingPanelProps {
  traceId: string;
  initialLabels: LabelRow[];
}

const VERDICTS: { value: Verdict | "none"; label: string }[] = [
  { value: "none", label: "none" },
  { value: "tp", label: "tp" },
  { value: "fp", label: "fp" },
  { value: "fn", label: "fn" },
];

export function LabelingPanel({ traceId, initialLabels }: LabelingPanelProps) {
  const [labels, setLabels] = useState<LabelRow[]>(initialLabels);
  const [verdict, setVerdict] = useState<Verdict | "none">("none");
  const [labelClass, setLabelClass] = useState("");
  const [answer, setAnswer] = useState("");
  const [question, setQuestion] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [isPending, startTransition] = useTransition();

  const canSubmit = answer.trim().length > 0 && !isPending;

  const submit = () => {
    if (!canSubmit) return;
    setError(null);

    const body: CreateLabelBody = {
      trace_id: traceId,
      answer: answer.trim(),
    };
    if (question.trim()) body.question = question.trim();
    if (labelClass.trim()) body.label_class = labelClass.trim();
    body.verdict = verdict === "none" ? null : verdict;

    startTransition(async () => {
      try {
        const created = await createLabel(body);
        // optimistic-ish append, then refetch to stay authoritative
        setLabels((prev) => [created, ...prev]);
        setAnswer("");
        setQuestion("");
        setLabelClass("");
        setVerdict("none");
        try {
          const fresh = await getLabels(traceId);
          setLabels(fresh);
        } catch {
          // keep optimistic append if refetch fails
        }
      } catch (e) {
        setError(e instanceof Error ? e.message : "Failed to create label");
      }
    });
  };

  return (
    <div className="px-4 py-4 flex flex-col gap-4 max-w-3xl">
      {/* Existing labels */}
      <section>
        <h2
          className="text-xs font-semibold mb-2"
          style={{ color: "var(--text-primary)" }}
        >
          Labels{" "}
          <span
            className="font-mono tabular-nums"
            style={{ color: "var(--text-muted)" }}
          >
            ({labels.length})
          </span>
        </h2>
        {labels.length === 0 ? (
          <p className="text-xs" style={{ color: "var(--text-muted)" }}>
            No labels yet. Add the first one below.
          </p>
        ) : (
          <ul role="list" className="flex flex-col">
            {labels.map((l) => (
              <li
                key={l.id}
                className="flex flex-wrap items-baseline gap-2 py-1.5"
                style={{ borderBottom: "1px solid var(--bg-border)" }}
              >
                <VerdictBadge verdict={l.verdict} />
                {l.label_class && (
                  <span
                    className="font-mono rounded px-1"
                    style={{
                      background: "var(--bg-elevated)",
                      color: "var(--text-secondary)",
                      border: "1px solid var(--bg-border)",
                      fontSize: 10,
                    }}
                  >
                    {l.label_class}
                  </span>
                )}
                <span
                  className="text-xs flex-1 min-w-[12rem]"
                  style={{ color: "var(--text-primary)" }}
                >
                  {l.answer}
                </span>
                <time
                  dateTime={l.ts}
                  className="text-xs font-mono"
                  style={{ color: "var(--text-muted)" }}
                  title={l.ts}
                >
                  {formatTimestamp(l.ts)}
                </time>
              </li>
            ))}
          </ul>
        )}
      </section>

      {/* Add label form */}
      <section>
        <h3
          className="text-xs font-semibold mb-2"
          style={{ color: "var(--text-primary)" }}
        >
          Add label
        </h3>
        <form
          className="flex flex-col gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            submit();
          }}
          aria-label="Add label"
        >
          <div className="flex flex-wrap items-center gap-3">
            {/* Verdict selector */}
            <label className="flex items-center gap-1.5">
              <span className="text-xs" style={{ color: "var(--text-muted)" }}>
                verdict
              </span>
              <select
                value={verdict}
                onChange={(e) => setVerdict(e.target.value as Verdict | "none")}
                aria-label="verdict"
                className="font-mono rounded px-1.5 py-1"
                style={{
                  background: "var(--bg-base)",
                  color: "var(--text-primary)",
                  border: "1px solid var(--bg-border)",
                  fontSize: 12,
                }}
              >
                {VERDICTS.map((v) => (
                  <option key={v.value} value={v.value}>
                    {v.label}
                  </option>
                ))}
              </select>
            </label>

            {/* Label class */}
            <label className="flex items-center gap-1.5 flex-1 min-w-[12rem]">
              <span className="text-xs" style={{ color: "var(--text-muted)" }}>
                class
              </span>
              <input
                type="text"
                value={labelClass}
                onChange={(e) => setLabelClass(e.target.value)}
                placeholder="e.g. tool_misuse"
                aria-label="label class"
                className="font-mono rounded px-1.5 py-1 flex-1"
                style={{
                  background: "var(--bg-base)",
                  color: "var(--text-primary)",
                  border: "1px solid var(--bg-border)",
                  fontSize: 12,
                }}
              />
            </label>
          </div>

          {/* Question (optional) */}
          <label className="flex flex-col gap-1">
            <span className="text-xs" style={{ color: "var(--text-muted)" }}>
              question <span style={{ opacity: 0.6 }}>(optional)</span>
            </span>
            <input
              type="text"
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              placeholder="What is being judged?"
              aria-label="question"
              className="rounded px-2 py-1.5"
              style={{
                background: "var(--bg-base)",
                color: "var(--text-primary)",
                border: "1px solid var(--bg-border)",
                fontSize: 12,
              }}
            />
          </label>

          {/* Answer (required) */}
          <label className="flex flex-col gap-1">
            <span className="text-xs" style={{ color: "var(--text-muted)" }}>
              answer <span style={{ color: "var(--accent-red)" }}>*</span>
            </span>
            <textarea
              value={answer}
              onChange={(e) => setAnswer(e.target.value)}
              onKeyDown={(e) => {
                if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
                  e.preventDefault();
                  submit();
                }
              }}
              placeholder="The label / judgement for this trace…"
              aria-label="answer"
              required
              rows={3}
              className="rounded px-2 py-1.5 resize-y"
              style={{
                background: "var(--bg-base)",
                color: "var(--text-primary)",
                border: "1px solid var(--bg-border)",
                fontSize: 12,
                lineHeight: 1.5,
              }}
            />
          </label>

          {error && (
            <p
              className="text-xs font-mono rounded px-2 py-1"
              role="alert"
              style={{
                background: "var(--accent-red-dim)",
                color: "var(--accent-red)",
                border: "1px solid rgba(220,38,38,0.3)",
              }}
            >
              {error}
            </p>
          )}

          <div className="flex items-center gap-3">
            <button
              type="submit"
              disabled={!canSubmit}
              className="rounded px-3 py-1 font-semibold"
              style={{
                background: canSubmit ? "var(--accent-blue)" : "var(--bg-elevated)",
                color: canSubmit ? "#ffffff" : "var(--text-muted)",
                border: "1px solid var(--bg-border)",
                fontSize: 12,
                cursor: canSubmit ? "pointer" : "not-allowed",
              }}
            >
              {isPending ? "saving…" : "Add label"}
            </button>
            <span className="text-xs" style={{ color: "var(--text-muted)" }}>
              <kbd>⌘/Ctrl</kbd> + <kbd>Enter</kbd> to submit
            </span>
          </div>
        </form>
      </section>
    </div>
  );
}

function VerdictBadge({ verdict }: { verdict: string }) {
  const color =
    verdict === "tp"
      ? "var(--accent-green)"
      : verdict === "fp" || verdict === "fn"
        ? "var(--accent-red)"
        : "var(--text-muted)";
  const label = verdict && verdict !== "null" ? verdict : "—";
  return (
    <span
      className="font-mono rounded px-1.5"
      style={{
        color,
        background: "var(--bg-elevated)",
        border: "1px solid var(--bg-border)",
        fontSize: 10,
        lineHeight: "16px",
      }}
    >
      {label}
    </span>
  );
}
