/**
 * ClusterTracesTable component tests — synthetic fixtures only.
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { ClusterTracesTable } from "@/components/ClusterTracesTable";
import {
  FIXTURE_CLUSTER_TRACES,
  FIXTURE_TRACE_ID,
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

vi.stubGlobal(
  "fetch",
  vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve([]) }),
);

describe("ClusterTracesTable", () => {
  it("renders a row per trace linking to the trace view", () => {
    render(<ClusterTracesTable traces={FIXTURE_CLUSTER_TRACES} />);
    // header + 2 data rows
    expect(screen.getAllByRole("row")).toHaveLength(3);
    const links = screen.getAllByRole("link");
    const traceLink = links.find(
      (l) => l.getAttribute("href") === `/traces/${FIXTURE_TRACE_ID}`,
    );
    expect(traceLink).toBeTruthy();
  });

  it("shows labeled and unlabeled states", () => {
    render(<ClusterTracesTable traces={FIXTURE_CLUSTER_TRACES} />);
    expect(screen.getByLabelText("labeled")).toBeTruthy();
    expect(screen.getByLabelText("unlabeled")).toBeTruthy();
  });

  it("renders empty state with no traces", () => {
    render(<ClusterTracesTable traces={[]} />);
    expect(screen.getByRole("status")).toBeTruthy();
    expect(screen.getByText(/No traces in this cluster/i)).toBeTruthy();
  });
});
