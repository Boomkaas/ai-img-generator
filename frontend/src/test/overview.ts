import type { AdminOverview } from "../api/types";

/** Two running and two waiting tasks, out of order: the page sorts them itself. */
export function overview(fields: Partial<AdminOverview> = {}): AdminOverview {
  return {
    type: "admin",
    machines: [
      { name: "nodeA", role: "infra", cpu: 23.5, ram: 41.0, api: { sockets: 6, requests_per_min: 41 } },
      { name: "nodeB", role: "app", cpu: 97.1, ram: 84.4, api: { sockets: 1, requests_per_min: 17 } },
      { name: "nodeC", role: "app", cpu: 3.0, ram: 84.6, api: null },
    ],
    workers: [
      {
        name: "nodeB-worker-1",
        machine: "nodeB",
        state: "busy",
        task_id: "3f2b8c1e-7d4a-4e5b-9a6c-1d2e3f4a5b6c",
        progress: 63,
        rss_mb: 6042,
      },
      {
        name: "nodeC-worker-1",
        machine: "nodeC",
        state: "busy",
        task_id: "77aa0000-0000-4000-8000-000000000000",
        progress: 12,
        rss_mb: 5320,
      },
      {
        name: "nodeC-worker-2",
        machine: "nodeC",
        state: "loading",
        task_id: null,
        progress: null,
        rss_mb: 910,
      },
    ],
    tasks: [
      {
        id: "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d",
        user_email: "ben@example.com",
        size: 768,
        status: "queued",
        worker_host: null,
        progress: null,
        position: 2,
        seconds_so_far: 64.0,
      },
      {
        id: "3f2b8c1e-7d4a-4e5b-9a6c-1d2e3f4a5b6c",
        user_email: "ana@example.com",
        size: 512,
        status: "running",
        worker_host: "nodeB-worker-1",
        progress: 63,
        position: null,
        seconds_so_far: 5 * 60 + 12,
      },
      {
        id: "c0de0000-0000-4000-8000-000000000000",
        user_email: "cleo@example.com",
        size: 256,
        status: "queued",
        worker_host: null,
        progress: null,
        position: 1,
        seconds_so_far: 4 * 60 + 30,
      },
      {
        id: "77aa0000-0000-4000-8000-000000000000",
        user_email: "dan@example.com",
        size: 384,
        status: "running",
        worker_host: "nodeC-worker-1",
        progress: 12,
        position: null,
        seconds_so_far: 40,
      },
    ],
    autoscaler: {
      desired: 4,
      ready: 3,
      starting: 1,
      maximum: 4,
      last_action: "Added a worker (3 → 4) at 21:41",
      alerts: [
        {
          at: "2026-10-17T08:55:40Z",
          message: "A new worker could not start: no app machine has enough free memory",
        },
        { at: "2026-10-17T09:20:00Z", message: "A newer alert" },
      ],
    },
    ...fields,
  };
}
