/**
 * lib/format utility tests.
 */
import { describe, expect, it } from "vitest";
import {
  formatArgs,
  formatLatency,
  formatTokens,
  relativeTime,
  shortTraceId,
  truncate,
  tsOffset,
} from "@/lib/format";

describe("shortTraceId", () => {
  it("returns first 8 chars", () => {
    expect(shortTraceId("abcdef1234567890")).toBe("abcdef12");
  });
});

describe("formatTokens", () => {
  it("returns — for null", () => expect(formatTokens(null)).toBe("—"));
  it("formats sub-1000 as plain number", () => expect(formatTokens(500)).toBe("500"));
  it("formats 1500 as 1.5k", () => expect(formatTokens(1500)).toBe("1.5k"));
});

describe("formatLatency", () => {
  it("returns — for null", () => expect(formatLatency(null)).toBe("—"));
  it("formats sub-1000 as Nms", () => expect(formatLatency(420)).toBe("420ms"));
  it("formats 1500 as 1.5s", () => expect(formatLatency(1500)).toBe("1.5s"));
});

describe("truncate", () => {
  it("returns original if short enough", () =>
    expect(truncate("hello", 10)).toBe("hello"));
  it("truncates with ellipsis", () =>
    expect(truncate("hello world", 7)).toBe("hello w…"));
});

describe("relativeTime", () => {
  it("returns — for null", () => expect(relativeTime(null)).toBe("—"));
  it("returns Xs ago for recent timestamps", () => {
    const now = new Date(Date.now() - 5000).toISOString();
    expect(relativeTime(now)).toMatch(/\ds ago/);
  });
});

describe("formatArgs", () => {
  it("returns empty string for null", () => expect(formatArgs(null)).toBe(""));
  it("formats single key=value inline", () =>
    expect(formatArgs({ file_path: "/foo" })).toBe("file_path=/foo"));
  it("formats multi-key as JSON", () =>
    expect(formatArgs({ a: 1, b: 2 })).toMatch(/"a":1/));
});

describe("tsOffset", () => {
  it("returns null when base is null", () => expect(tsOffset(null, "x")).toBeNull());
  it("returns +Ns string", () => {
    const base = "2024-01-01T00:00:00Z";
    const ts = "2024-01-01T00:00:05Z";
    expect(tsOffset(base, ts)).toBe("+5s");
  });
});
