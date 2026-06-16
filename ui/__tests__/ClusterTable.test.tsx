/**
 * ClusterTable component tests — synthetic fixtures only.
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { ClusterTable } from "@/components/ClusterTable";
import { FIXTURE_CLUSTERS, FIXTURE_CLUSTER_KEY } from "@/__tests__/fixtures/trace-envelope";

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

vi.stubGlobal(
  "fetch",
  vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve([]) }),
);

describe("ClusterTable", () => {
  it("renders a row per cluster", () => {
    render(<ClusterTable clusters={FIXTURE_CLUSTERS} />);
    // 1 header row + 3 data rows
    expect(screen.getAllByRole("row")).toHaveLength(4);
  });

  it("renders cluster_key prominently and links URL-encoded", () => {
    render(<ClusterTable clusters={FIXTURE_CLUSTERS} />);
    expect(screen.getByText(FIXTURE_CLUSTER_KEY)).toBeTruthy();
    const links = screen.getAllByRole("link");
    const keyLink = links.find(
      (l) =>
        l.getAttribute("href") ===
        `/clusters/${encodeURIComponent(FIXTURE_CLUSTER_KEY)}`,
    );
    expect(keyLink).toBeTruthy();
  });

  it("renders trace_count and kinds", () => {
    render(<ClusterTable clusters={FIXTURE_CLUSTERS} />);
    expect(screen.getByText("12")).toBeTruthy();
    expect(screen.getAllByText("tool_call").length).toBeGreaterThanOrEqual(1);
  });

  it("renders empty state with no clusters", () => {
    render(<ClusterTable clusters={[]} />);
    expect(screen.getByRole("status")).toBeTruthy();
    expect(screen.getByText(/No clusters found/i)).toBeTruthy();
  });
});
