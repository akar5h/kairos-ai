/**
 * StepTimeline component tests.
 *
 * Synthetic fixture only.
 */
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { StepTimeline } from "@/components/StepTimeline";
import { FIXTURE_ENVELOPE } from "@/__tests__/fixtures/trace-envelope";

describe("StepTimeline", () => {
  it("renders tool histogram summary", () => {
    render(<StepTimeline envelope={FIXTURE_ENVELOPE} />);
    // "2 tool calls" summary
    expect(screen.getByText(/2 tool calls/i)).toBeTruthy();
  });

  it("shows tool names in the histogram", () => {
    render(<StepTimeline envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getAllByText("Read").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("Bash").length).toBeGreaterThanOrEqual(1);
  });

  it("marks errors in the histogram", () => {
    render(<StepTimeline envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByText(/1 failed/i)).toBeTruthy();
  });

  it("renders step rows for each tool call step", () => {
    render(<StepTimeline envelope={FIXTURE_ENVELOPE} />);
    const listItems = screen.getAllByRole("listitem");
    // 2 tool_call steps → 2 list items
    expect(listItems.length).toBe(2);
  });

  it("renders empty state when no tool calls", () => {
    const noTools = {
      ...FIXTURE_ENVELOPE,
      steps: FIXTURE_ENVELOPE.steps.filter((s) => s.step_type !== "tool_call"),
      tool_sequence: [],
    };
    render(<StepTimeline envelope={noTools} />);
    expect(screen.getByText(/No tool calls recorded/i)).toBeTruthy();
  });

  it("collapses consecutive same-tool runs when 3+ identical", () => {
    // Build envelope with 5 consecutive Read calls
    const readStep = FIXTURE_ENVELOPE.steps[1]; // Read step
    const manyReads = Array.from({ length: 5 }, (_, i) => ({
      ...readStep,
      step_index: i,
    }));
    const collapsed = {
      ...FIXTURE_ENVELOPE,
      steps: manyReads,
      tool_sequence: manyReads.map((s) => s.tool_name ?? ""),
      error_count: 0,
    };
    render(<StepTimeline envelope={collapsed} />);
    // Should show "×5" for the collapsed run (may appear in histogram + row)
    const fiveMatches = screen.getAllByText(/×5/);
    expect(fiveMatches.length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText(/collapsed/i)).toBeTruthy();
  });
});
