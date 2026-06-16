/**
 * SessionTable component tests.
 * Uses synthetic FIXTURE_SESSIONS — no real data.
 */
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { SessionTable } from "@/components/SessionTable";
import { FIXTURE_SESSIONS, FIXTURE_SESSION_ID } from "@/__tests__/fixtures/trace-envelope";

// Mock next/link
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

// Mock fetch for InlineTracePeek (not triggered in these tests since rows not expanded)
const fetchMock = vi.fn().mockResolvedValue({
  ok: true,
  json: () => Promise.resolve([]),
});
vi.stubGlobal("fetch", fetchMock);

describe("SessionTable", () => {
  it("renders a row for each session", () => {
    render(<SessionTable sessions={FIXTURE_SESSIONS} />);
    // 1 header + 3 data rows
    const rows = screen.getAllByRole("row");
    expect(rows).toHaveLength(4);
  });

  it("renders session id prefix and link", () => {
    render(<SessionTable sessions={FIXTURE_SESSIONS} />);
    // First 8 chars of FIXTURE_SESSION_ID = "sess-alp"
    expect(screen.getByText("sess-alp")).toBeTruthy();
    const links = screen.getAllByRole("link");
    const sessionLink = links.find(
      (l) =>
        l.getAttribute("href") === `/sessions/${encodeURIComponent(FIXTURE_SESSION_ID)}`,
    );
    expect(sessionLink).toBeTruthy();
  });

  it("shows error chip for sessions with errors", () => {
    render(<SessionTable sessions={FIXTURE_SESSIONS} />);
    // sess-beta-002 has error_count=1 — should show aria-label "1 errors"
    const errChip = screen.getByLabelText("1 errors");
    expect(errChip).toBeTruthy();
  });

  it("shows muted dot for zero-error sessions", () => {
    render(<SessionTable sessions={FIXTURE_SESSIONS} />);
    // sessions with 0 errors show "·" with aria-label "0 errors"
    const zeroDots = screen.getAllByLabelText("0 errors");
    expect(zeroDots.length).toBeGreaterThanOrEqual(2);
  });

  it("renders tool chips for sessions with tools", () => {
    render(<SessionTable sessions={FIXTURE_SESSIONS} />);
    // Tools may appear more than once across sessions (multiple sessions can share tools)
    expect(screen.getAllByText("Read").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("Bash").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("Edit").length).toBeGreaterThanOrEqual(1);
  });

  it("renders empty state when no sessions", () => {
    render(<SessionTable sessions={[]} />);
    expect(screen.getByRole("status")).toBeTruthy();
    expect(screen.getByText(/No sessions found/i)).toBeTruthy();
  });

  it("shows dash for null started_at", () => {
    render(<SessionTable sessions={FIXTURE_SESSIONS} />);
    // sess-gamma-003 has null started_at → relativeTime returns "—"
    const timEls = screen.getAllByRole("time");
    const dashTimes = timEls.filter((el) => el.textContent === "—");
    expect(dashTimes.length).toBeGreaterThanOrEqual(1);
  });
});
