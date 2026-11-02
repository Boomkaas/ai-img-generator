export const MAX_PROMPT_LEN = 500;
export const SIZES = [256, 384, 512, 640, 768] as const;
export const DEFAULT_SIZE = 512;

export const PING_TIMEOUT_S = 60;
export const RETRY_DELAYS_S = [1, 2, 5, 10];

export function retryDelayMs(attempt: number): number {
  return (RETRY_DELAYS_S[Math.min(attempt, RETRY_DELAYS_S.length - 1)] ?? 10) * 1000;
}

export const ADMIN_PAUSED_S = 15;
export const LOAD_WARN_PERCENT = 85;
