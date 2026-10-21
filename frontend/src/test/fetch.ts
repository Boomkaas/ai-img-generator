import { vi } from "vitest";
import type { TaskOut } from "../api/types";

type Handler = (init: RequestInit) => Response | Promise<Response>;

export const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

/** Stub fetch with handlers keyed by "METHOD path"; anything else answers 404. */
export function stubFetch(routes: Record<string, Handler>) {
  const fetch = vi.fn(async (path: string, init: RequestInit = {}) => {
    const handler = routes[`${init.method ?? "GET"} ${path}`];
    return handler ? handler(init) : json({ detail: "Not found" }, 404);
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

export function task(fields: Partial<TaskOut> = {}): TaskOut {
  return {
    task_id: "11111111-1111-4111-8111-111111111111",
    status: "queued",
    prompt: "a red fox in fresh snow",
    size: 512,
    created_at: "2026-10-17T09:12:03Z",
    started_at: null,
    finished_at: null,
    position: 1,
    queue_length: 1,
    progress: null,
    stage: null,
    ready_in_s: 600,
    error_message: null,
    ...fields,
  };
}
