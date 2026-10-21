import type { CancelOut, TaskOut, TaskPage, User } from "./types";

/** The server answered with an error status; `detail` is its sentence for the user, if it sent one. */
export class ApiError extends Error {
  readonly status: number;
  readonly body: Record<string, unknown> | null;

  constructor(status: number, body: Record<string, unknown> | null) {
    super(typeof body?.detail === "string" ? body.detail : `The server answered ${status}`);
    this.status = status;
    this.body = body;
  }
}

/** No answer at all: the network is down, or the server died mid-request. */
export class NetworkError extends Error {}

const signedOutListeners = new Set<() => void>();

export function onSignedOut(listener: () => void): () => void {
  signedOutListeners.add(listener);
  return () => signedOutListeners.delete(listener);
}

export function notifySignedOut(): void {
  signedOutListeners.forEach((listener) => listener());
}

// A 401 from these is the answer itself, not an expired access token.
const NO_REFRESH = new Set([
  "/api/auth/login",
  "/api/auth/register",
  "/api/auth/refresh",
  "/api/auth/logout",
]);

let refreshing: Promise<boolean> | null = null;

/**
 * Trade the refresh cookie for a new access cookie: true if that worked, false if the server refused it.
 * Rejects with NetworkError or ApiError when there was no clear answer, so a dropped connection doesn't sign
 * the user out. Callers that arrive together share one request: the server takes a refresh token used twice
 * for a stolen one and ends every session of the user.
 */
export function refreshSession(): Promise<boolean> {
  refreshing ??= send("POST", "/api/auth/refresh")
    .then(async (response) => {
      if (response.ok) return true;
      if (response.status === 401) return false;
      throw new ApiError(response.status, await readBody(response));
    })
    .finally(() => {
      refreshing = null;
    });
  return refreshing;
}

async function send(
  method: string,
  path: string,
  body?: unknown,
  headers: Record<string, string> = {},
): Promise<Response> {
  const init: RequestInit = { method, credentials: "same-origin", headers: { ...headers } };
  // The server refuses a POST that isn't JSON, even one without a body.
  if (method === "POST") {
    init.headers = { ...headers, "Content-Type": "application/json" };
    if (body !== undefined) init.body = JSON.stringify(body);
  }
  try {
    return await fetch(path, init);
  } catch {
    throw new NetworkError("Can't reach the server");
  }
}

async function readBody(response: Response): Promise<Record<string, unknown> | null> {
  try {
    const body: unknown = await response.json();
    return body !== null && typeof body === "object" ? (body as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  headers?: Record<string, string>,
): Promise<T> {
  let response = await send(method, path, body, headers);
  if (response.status === 401 && !NO_REFRESH.has(path)) {
    if (await refreshSession()) response = await send(method, path, body, headers);
    if (response.status === 401) notifySignedOut();
  }
  if (!response.ok) throw new ApiError(response.status, await readBody(response));
  return (response.status === 204 ? null : await response.json()) as T;
}

export const getMe = () => request<User>("GET", "/api/auth/me");

export const signIn = (email: string, password: string) =>
  request<User>("POST", "/api/auth/login", { email, password });

export const register = (email: string, password: string) =>
  request<User>("POST", "/api/auth/register", { email, password });

export const logOut = () => request<null>("POST", "/api/auth/logout");

export const getActiveTask = () => request<TaskOut | null>("GET", "/api/tasks/active");

export const getTask = (id: string) => request<TaskOut>("GET", `/api/tasks/${id}`);

export const cancelTask = (id: string) => request<CancelOut>("DELETE", `/api/tasks/${id}`);

export const getHistory = (before: string | null) =>
  request<TaskPage>("GET", before ? `/api/tasks?before=${encodeURIComponent(before)}` : "/api/tasks");

export const imageUrl = (id: string) => `/api/tasks/${id}/image`;

/**
 * Submit a prompt. The key is made once per click, so the one retry after a lost answer returns the task
 * the first attempt may already have created instead of making a second.
 */
export async function submitTask(prompt: string, size: number): Promise<TaskOut> {
  const headers = { "Idempotency-Key": crypto.randomUUID() };
  const attempt = () => request<TaskOut>("POST", "/api/tasks", { prompt, size }, headers);
  try {
    return await attempt();
  } catch (error) {
    if (error instanceof NetworkError) return await attempt();
    throw error;
  }
}
