/**
 * ConversationView component tests.
 *
 * Uses FIXTURE_ENVELOPE — all synthetic data, no real traces.
 */
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { ConversationView } from "@/components/ConversationView";
import { FIXTURE_ENVELOPE } from "@/__tests__/fixtures/trace-envelope";

describe("ConversationView", () => {
  it("renders user input as a USER turn at the top", () => {
    render(<ConversationView envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByText("Summarise the quarterly report")).toBeTruthy();
    expect(screen.getAllByText("USER").length).toBeGreaterThanOrEqual(1);
  });

  it("renders LLM steps as ASST blocks", () => {
    render(<ConversationView envelope={FIXTURE_ENVELOPE} />);
    const asstLabels = screen.getAllByText("ASST");
    // Two LLM steps in the fixture
    expect(asstLabels.length).toBe(2);
  });

  it("renders tool calls with tool name", () => {
    render(<ConversationView envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByText("Read")).toBeTruthy();
    expect(screen.getByText("Bash")).toBeTruthy();
  });

  it("visually distinguishes error steps with ERR badge", () => {
    render(<ConversationView envelope={FIXTURE_ENVELOPE} />);
    // Step index 2 has status=error — should show ERR via attr_success
    const errBadge = screen.getByText(/ERR via/i);
    expect(errBadge).toBeTruthy();
  });

  it("shows error message for failed steps", () => {
    render(<ConversationView envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByText("No such file or directory")).toBeTruthy();
  });

  it("renders empty state when steps is empty", () => {
    const empty = { ...FIXTURE_ENVELOPE, steps: [], user_input: null };
    render(<ConversationView envelope={empty} />);
    expect(screen.getByText(/No steps recorded/i)).toBeTruthy();
  });

  it("shows llm model name when present", () => {
    render(<ConversationView envelope={FIXTURE_ENVELOPE} />);
    const modelLabels = screen.getAllByText("claude-opus-4-5");
    expect(modelLabels.length).toBeGreaterThanOrEqual(1);
  });
});
