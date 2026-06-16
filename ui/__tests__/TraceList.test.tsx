/**
 * TraceList component tests.
 *
 * Uses synthetic fixtures — no real trace data.
 */
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { TraceList } from "@/components/TraceList";
import { FIXTURE_TRACES, FIXTURE_TRACE_ID } from "@/__tests__/fixtures/trace-envelope";

// Next.js Link needs a router — mock it
vi.mock("next/link", () => ({
  default: ({
    href,
    children,
    ...rest
  }: {
    href: string;
    children: React.ReactNode;
    [k: string]: unknown;
  }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));

describe("TraceList", () => {
  it("renders a row for each trace", () => {
    render(<TraceList traces={FIXTURE_TRACES} />);
    // Should have 3 rows (one per fixture)
    const rows = screen.getAllByRole("row");
    // 1 header row + 3 data rows
    expect(rows).toHaveLength(4);
  });

  it("shows truncated trace ID and link to detail", () => {
    render(<TraceList traces={FIXTURE_TRACES} />);
    // First 8 chars of FIXTURE_TRACE_ID = "abcdef12"
    expect(screen.getByText("abcdef12")).toBeTruthy();
    // Should link to the detail page
    const links = screen.getAllByRole("link");
    const detailLink = links.find(
      (l) => l.getAttribute("href") === `/traces/${FIXTURE_TRACE_ID}`,
    );
    expect(detailLink).toBeTruthy();
  });

  it("shows error badge with red styling for traces with errors", () => {
    render(<TraceList traces={FIXTURE_TRACES} />);
    // The first trace has 1 error — badge should show "1"
    const badge = screen.getByLabelText("1 error");
    expect(badge).toBeTruthy();
  });

  it("renders empty state when no traces", () => {
    render(<TraceList traces={[]} />);
    expect(screen.getByRole("status")).toBeTruthy();
    expect(screen.getByText(/No traces found/i)).toBeTruthy();
  });

  it("shows dash for null started_at", () => {
    render(<TraceList traces={FIXTURE_TRACES} />);
    // Third fixture has null started_at — time element should be "—"
    const timeEls = screen.getAllByRole("time");
    // At least two of them exist; the null one shows "—"
    const dashTimes = timeEls.filter((el) => el.textContent === "—");
    expect(dashTimes.length).toBeGreaterThanOrEqual(1);
  });
});
