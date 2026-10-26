# Messages

Every message the server sends, one example each. The models live in `common/messages.py`; the frontend's
TypeScript types are written from this page. `tests/unit/test_messages.py` parses every example
below, so this page can't drift from the models.

WebSocket and pub/sub messages carry a `type` that says which one they are. Ids are UUID strings, times are
ISO 8601 in UTC, durations are seconds, progress is a whole percentage (0–100).

## SystemStatus — `system`

Every 5 s, to every socket.

```json
{"type": "system", "running": 4, "waiting": 3, "new_image_ready_in_s": 840.0}
```

## TaskUpdate — `task`

A partial update of the user's active task, on `user:{user_id}`. Fields that weren't sent are absent, and the
frontend merges what it receives into what it already has. Workers send progress after every step:

```json
{"type": "task", "id": "3f2b8c1e-7d4a-4e5b-9a6c-1d2e3f4a5b6c", "status": "running", "progress": 36, "stage": "step 10 of 25"}
```

The controller sends the queue position and the ready time every 5 s:

```json
{"type": "task", "id": "3f2b8c1e-7d4a-4e5b-9a6c-1d2e3f4a5b6c", "status": "queued", "position": 2, "queue_length": 3, "ready_in_s": 660.0}
```

## TaskFinal — `task_final`

When the task ends (done, failed or cancelled). The client then fetches the task over HTTP.

The controller reads the queue a moment before it publishes, so a `task` message can arrive after the task
has moved on. A task's status only moves forward (queued, running, then final): the client ignores a `task`
message for an id that already had `task_final`, and a `queued` one for a task it has seen running.

```json
{"type": "task_final", "id": "3f2b8c1e-7d4a-4e5b-9a6c-1d2e3f4a5b6c", "status": "done"}
```

## Snapshot — `snapshot`

Right after a socket connects, and so after every reconnect. `task` is the user's active task as the HTTP API
returns it (`TaskOut`, below), or `null` when there is none.

```json
{
  "type": "snapshot",
  "system": {"type": "system", "running": 1, "waiting": 0, "new_image_ready_in_s": 720.0},
  "task": {
    "task_id": "3f2b8c1e-7d4a-4e5b-9a6c-1d2e3f4a5b6c",
    "status": "running",
    "prompt": "a lighthouse at dusk, oil painting",
    "size": 512,
    "created_at": "2026-10-17T09:12:03Z",
    "started_at": "2026-10-17T09:12:05Z",
    "finished_at": null,
    "position": null,
    "queue_length": null,
    "progress": 36,
    "stage": "step 10 of 25",
    "ready_in_s": 307.2,
    "error_message": null
  }
}
```

## Ping — `ping`

Every 25 s on every socket, so the client can tell a quiet socket from a dead one. It carries nothing else.

```json
{"type": "ping"}
```

## AdminOverview — `admin`

Every 5 s, to admin sockets only; `GET /api/admin/overview` returns the same object. `api` is `null` on
machines without an API server. A worker's `task_id` and `progress` are `null` while it isn't busy; a task has
a `progress` while running and a `position` while queued.

```json
{
  "type": "admin",
  "machines": [
    {"name": "nodeA", "role": "infra", "cpu": 23.5, "ram": 41.0, "api": {"sockets": 3, "requests_per_min": 42}},
    {"name": "nodeB", "role": "app", "cpu": 97.1, "ram": 68.4, "api": {"sockets": 2, "requests_per_min": 17}},
    {"name": "nodeC", "role": "app", "cpu": 3.0, "ram": 12.2, "api": null}
  ],
  "workers": [
    {"name": "nodeB-worker-1", "machine": "nodeB", "state": "busy", "task_id": "3f2b8c1e-7d4a-4e5b-9a6c-1d2e3f4a5b6c", "progress": 36, "rss_mb": 5320},
    {"name": "nodeC-worker-2", "machine": "nodeC", "state": "loading", "task_id": null, "progress": null, "rss_mb": 910}
  ],
  "tasks": [
    {"id": "3f2b8c1e-7d4a-4e5b-9a6c-1d2e3f4a5b6c", "user_email": "ana@example.com", "size": 512, "status": "running", "worker_host": "nodeB-worker-1", "progress": 36, "position": null, "seconds_so_far": 171.4},
    {"id": "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d", "user_email": "ben@example.com", "size": 768, "status": "queued", "worker_host": null, "progress": null, "position": 1, "seconds_so_far": 64.0}
  ],
  "autoscaler": {
    "desired": 2,
    "ready": 1,
    "starting": 1,
    "maximum": 4,
    "last_action": "scaled up to 2",
    "alerts": [{"at": "2026-10-17T08:55:40Z", "message": "A new worker could not start: no app machine has enough free memory"}]
  }
}
```

## TaskOut (HTTP)

Not a WebSocket message: what `GET /api/tasks/{id}`, the history and the submit response return for a task.
Fields that don't apply to the task's status are `null`.

```json
{
  "task_id": "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d",
  "status": "queued",
  "prompt": "a red fox in fresh snow",
  "size": 768,
  "created_at": "2026-10-17T09:13:10Z",
  "started_at": null,
  "finished_at": null,
  "position": 1,
  "queue_length": 1,
  "progress": null,
  "stage": null,
  "ready_in_s": 1387.2,
  "error_message": null
}
```

## TaskPage (HTTP)

What `GET /api/tasks?before=<time>&limit=<n>` returns: the user's tasks, newest first, each a `TaskOut`.
`next_before` goes back as `before` to fetch the next page; it is `null` on the last page.

```json
{
  "items": [
    {
      "task_id": "3f2b8c1e-7d4a-4e5b-9a6c-1d2e3f4a5b6c",
      "status": "done",
      "prompt": "a lighthouse at dusk, oil painting",
      "size": 512,
      "created_at": "2026-10-17T09:12:03Z",
      "started_at": "2026-10-17T09:12:05Z",
      "finished_at": "2026-10-17T09:20:11Z",
      "position": null,
      "queue_length": null,
      "progress": null,
      "stage": null,
      "ready_in_s": null,
      "error_message": null
    }
  ],
  "next_before": "2026-10-17T09:12:03Z"
}
```
