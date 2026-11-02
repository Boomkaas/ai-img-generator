export function formatDuration(seconds: number): string {
  if (seconds < 60) return "less than a minute";
  return `about ${Math.ceil(seconds / 60)} min`;
}

export function formatClock(iso: string, now: Date = new Date()): string {
  const at = new Date(iso);
  const time = at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  if (at.toDateString() === now.toDateString()) return time;
  return `${at.toLocaleDateString([], { day: "numeric", month: "short" })}, ${time}`;
}

export function formatMinutes(seconds: number): string {
  return seconds < 60 ? "<1 min" : `${Math.floor(seconds / 60)} min`;
}
