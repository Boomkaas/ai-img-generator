import { describe, expect, it } from "vitest";
import { formatDuration } from "./time";

describe("formatDuration", () => {
  it.each([
    [59, "less than a minute"],
    [60, "about 1 min"],
    [61, "about 2 min"],
    [14.2 * 60, "about 15 min"],
  ])("%s s reads %s", (seconds, text) => {
    expect(formatDuration(seconds)).toBe(text);
  });
});
