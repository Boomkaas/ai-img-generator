// Mirrors the server's limits so the form can help early; the server still enforces them.
export const MAX_PROMPT_LEN = 500;
export const SIZES = [256, 384, 512, 640, 768] as const;
export const DEFAULT_SIZE = 512;

// The server pings every 25 s, so two missed pings mean the socket is dead.
export const PING_TIMEOUT_S = 60;
export const RECONNECT_DELAYS_S = [1, 2, 5, 10]; // the last value repeats
