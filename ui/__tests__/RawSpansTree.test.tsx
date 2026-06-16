/**
 * RawSpansTree component tests.
 * Uses synthetic FIXTURE_RAW_SPANS — no real data.
 */
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { RawSpansTree } from "@/components/RawSpansTree";
import { FIXTURE_RAW_SPANS } from "@/__tests__/fixtures/trace-envelope";

describe("RawSpansTree", () => {
  it("renders the span count in the toolbar", () => {
    render(<RawSpansTree spans={FIXTURE_RAW_SPANS} />);
    expect(screen.getByText(/3 spans/i)).toBeTruthy();
  });

  it("renders a row for each span", () => {
    render(<RawSpansTree spans={FIXTURE_RAW_SPANS} />);
    const rows = screen.getAllByRole("treeitem");
    expect(rows).toHaveLength(3);
  });

  it("shows tool name for tool spans", () => {
    render(<RawSpansTree spans={FIXTURE_RAW_SPANS} />);
    // Read and Bash tools should appear
    expect(screen.getAllByText("Read").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("Bash").length).toBeGreaterThanOrEqual(1);
  });

  it("shows ERROR status for error spans", () => {
    render(<RawSpansTree spans={FIXTURE_RAW_SPANS} />);
    expect(screen.getByText("ERROR")).toBeTruthy();
  });

  it("shows OK status for ok spans", () => {
    render(<RawSpansTree spans={FIXTURE_RAW_SPANS} />);
    const okEls = screen.getAllByText("OK");
    expect(okEls.length).toBeGreaterThanOrEqual(1);
  });

  it("renders empty state when no spans", () => {
    render(<RawSpansTree spans={[]} />);
    expect(screen.getByText(/No spans for this trace/i)).toBeTruthy();
  });

  it("shows root span name", () => {
    render(<RawSpansTree spans={FIXTURE_RAW_SPANS} />);
    // Root span is "agent.run"
    expect(screen.getByText("agent.run")).toBeTruthy();
  });
});
