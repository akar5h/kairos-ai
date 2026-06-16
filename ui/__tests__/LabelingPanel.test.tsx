/**
 * LabelingPanel tests — renders existing labels + verifies POST body.
 * Mocks fetch; synthetic fixtures only.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { LabelingPanel } from "@/components/LabelingPanel";
import { FIXTURE_LABELS, FIXTURE_TRACE_ID } from "@/__tests__/fixtures/trace-envelope";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("LabelingPanel", () => {
  it("renders existing labels from the fixture", () => {
    vi.stubGlobal("fetch", vi.fn());
    render(<LabelingPanel traceId={FIXTURE_TRACE_ID} initialLabels={FIXTURE_LABELS} />);
    expect(screen.getByText(/Yes — ran cat on a nonexistent file\./)).toBeTruthy();
    expect(screen.getByText("tool_misuse")).toBeTruthy();
    expect(screen.getByText("(1)")).toBeTruthy();
  });

  it("shows empty hint when no labels", () => {
    vi.stubGlobal("fetch", vi.fn());
    render(<LabelingPanel traceId={FIXTURE_TRACE_ID} initialLabels={[]} />);
    expect(screen.getByText(/No labels yet/i)).toBeTruthy();
  });

  it("submits a POST with the correct body shape", async () => {
    const created = {
      id: "lbl-new",
      trace_id: FIXTURE_TRACE_ID,
      question: "",
      answer: "looks correct",
      verdict: "fp",
      label_class: "false_alarm",
      ts: "2024-03-16T00:00:00Z",
    };
    const fetchMock = vi.fn((_url: string, init?: { method?: string }) => {
      // POST → created row; GET refetch → array
      const isPost = init?.method === "POST";
      return Promise.resolve({
        ok: true,
        status: isPost ? 201 : 200,
        json: () => Promise.resolve(isPost ? created : [created]),
        text: () => Promise.resolve(""),
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<LabelingPanel traceId={FIXTURE_TRACE_ID} initialLabels={[]} />);

    fireEvent.change(screen.getByLabelText("verdict"), {
      target: { value: "fp" },
    });
    fireEvent.change(screen.getByLabelText("label class"), {
      target: { value: "false_alarm" },
    });
    fireEvent.change(screen.getByLabelText("answer"), {
      target: { value: "looks correct" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Add label/i }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    // First call is the POST /v1/labels
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toMatch(/\/v1\/labels$/);
    expect(init.method).toBe("POST");
    expect(init.headers["Content-Type"]).toBe("application/json");
    const body = JSON.parse(init.body);
    expect(body).toEqual({
      trace_id: FIXTURE_TRACE_ID,
      answer: "looks correct",
      label_class: "false_alarm",
      verdict: "fp",
    });
  });

  it("sends verdict null when 'none' selected and omits empty optionals", async () => {
    const row = {
      id: "x",
      trace_id: FIXTURE_TRACE_ID,
      question: "",
      answer: "a",
      verdict: "",
      label_class: "",
      ts: "2024-03-16T00:00:00Z",
    };
    const fetchMock = vi.fn((_url: string, init?: { method?: string }) => {
      const isPost = init?.method === "POST";
      return Promise.resolve({
        ok: true,
        status: isPost ? 201 : 200,
        json: () => Promise.resolve(isPost ? row : [row]),
        text: () => Promise.resolve(""),
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<LabelingPanel traceId={FIXTURE_TRACE_ID} initialLabels={[]} />);
    fireEvent.change(screen.getByLabelText("answer"), {
      target: { value: "a" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Add label/i }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const body = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(body).toEqual({
      trace_id: FIXTURE_TRACE_ID,
      answer: "a",
      verdict: null,
    });
  });
});
