import { describe, expect, it } from "vitest";
import { formatDuration, formatMinutes } from "./time";

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

describe("formatMinutes", () => {
  it.each([
    [0, "<1 min"],
    [59.9, "<1 min"],
    [60, "1 min"],
    [171.4, "2 min"],
    [5 * 60 + 59, "5 min"],
  ])("%s s reads %s", (seconds, text) => {
    expect(formatMinutes(seconds)).toBe(text);
  });
});
