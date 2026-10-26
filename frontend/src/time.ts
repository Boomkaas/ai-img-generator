/** How long something will take, the way the page says it: "less than a minute" or "about N min". */
export function formatDuration(seconds: number): string {
  if (seconds < 60) return "less than a minute";
  return `about ${Math.ceil(seconds / 60)} min`;
}

/** "21:40" for today, "17 Oct, 21:40" for an earlier day, in the browser's time zone. */
export function formatClock(iso: string, now: Date = new Date()): string {
  const at = new Date(iso);
  const time = at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  if (at.toDateString() === now.toDateString()) return time;
  return `${at.toLocaleDateString([], { day: "numeric", month: "short" })}, ${time}`;
}

/** Time spent so far in whole minutes: "5 min", or "<1 min" during the first minute. */
export function formatMinutes(seconds: number): string {
  return seconds < 60 ? "<1 min" : `${Math.floor(seconds / 60)} min`;
}
