/**
 * SessionTraceTable component tests.
 * Uses synthetic FIXTURE_TRACES_IN_SESSION — no real data.
 */
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { SessionTraceTable } from "@/components/SessionTraceTable";
import {
  FIXTURE_TRACES_IN_SESSION,
  FIXTURE_TRACE_ID,
  FIXTURE_SESSION_ID,
} from "@/__tests__/fixtures/trace-envelope";

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

describe("SessionTraceTable", () => {
  it("renders a row for each trace", () => {
    render(
      <SessionTraceTable
        traces={FIXTURE_TRACES_IN_SESSION}
        sessionId={FIXTURE_SESSION_ID}
      />,
    );
    // 1 header + 2 data rows
    const rows = screen.getAllByRole("row");
    expect(rows).toHaveLength(3);
  });

  it("shows truncated trace id and link to trace", () => {
    render(
      <SessionTraceTable
        traces={FIXTURE_TRACES_IN_SESSION}
        sessionId={FIXTURE_SESSION_ID}
      />,
    );
    expect(screen.getByText("abcdef12")).toBeTruthy();
    const links = screen.getAllByRole("link");
    const traceLink = links.find(
      (l) => l.getAttribute("href") === `/traces/${FIXTURE_TRACE_ID}`,
    );
    expect(traceLink).toBeTruthy();
  });

  it("shows error chip for traces with errors", () => {
    render(
      <SessionTraceTable
        traces={FIXTURE_TRACES_IN_SESSION}
        sessionId={FIXTURE_SESSION_ID}
      />,
    );
    // Second trace has 1 error
    const errChip = screen.getByLabelText("1 errors");
    expect(errChip).toBeTruthy();
  });

  it("renders empty state when no traces", () => {
    render(<SessionTraceTable traces={[]} sessionId={FIXTURE_SESSION_ID} />);
    expect(screen.getByText(/No traces in this session/i)).toBeTruthy();
  });
});
