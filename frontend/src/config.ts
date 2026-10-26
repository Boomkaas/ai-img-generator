// Mirrors the server's limits so the form can help early; the server still enforces them.
export const MAX_PROMPT_LEN = 500;
export const SIZES = [256, 384, 512, 640, 768] as const;
export const DEFAULT_SIZE = 512;

// The server pings every 25 s, so two missed pings mean the socket is dead.
export const PING_TIMEOUT_S = 60;
// Between socket reconnects and between refetches of a finished task; the last value repeats.
export const RETRY_DELAYS_S = [1, 2, 5, 10];

export function retryDelayMs(attempt: number): number {
  return (RETRY_DELAYS_S[Math.min(attempt, RETRY_DELAYS_S.length - 1)] ?? 10) * 1000;
}

// The overview arrives every 5 s; three missed in a row mean the live feed has stopped.
export const ADMIN_PAUSED_S = 15;
// Machine CPU or RAM at or above this is shown in the warn colour.
export const LOAD_WARN_PERCENT = 85;
