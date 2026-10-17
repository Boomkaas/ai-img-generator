# Step 03 of 14 — Redis live state, image storage and wait estimates

You are building **imagegen** (see `CLAUDE.md` and `docs/architecture.md`). This is step 03 of fourteen. Do only this step.

## Goal

The shared modules every process uses for live state (progress, cancel flags, heartbeats, pub/sub), image storage in MinIO, the wait-time estimate, and one set of message schemas shared by the API, the controller and the frontend.

## Before you start

- Read `docs/architecture.md` §3.2 (WebSocket messages), §4.2 (progress stages), §4.6 (wait estimate), §4.8 (heartbeats), §6.5 (Redis run id).
- Check step 02 is merged into `develop` and `make test-unit` and `make test-api` pass. If not, stop and tell me.
- Branch: `feature/common-live-storage-estimate` from `develop`.

## Build

### 1. `common/live.py` — the only place Redis keys and channels are named

Use `redis-py` with `REDIS_URL`. Provide a sync client for workers, controller and HTTP handlers, and a factory for an async client (`redis.asyncio`) that step 07 uses.

Keys and channels (constants or small functions at the top of the module):

| Name | Type | Contents | Expiry |
|---|---|---|---|
| `progress:{task_id}` | hash | `progress` (0–100), `stage`, `updated_at` | `TASK_TIME_LIMIT` |
| `cancel:{task_id}` | string | `1` | `CANCEL_FLAG_TTL_S` |
| `hb:worker:{host}` | hash | `state` (loading/idle/busy), `task_id`, `since`, `machine`, `machine_cpu`, `machine_ram`, `rss_mb` | 15 s |
| `hb:api:{machine}:{pid}` | hash | `machine`, `sockets`, `requests_per_min`, `machine_cpu`, `machine_ram` | 15 s |
| `hb:machine:{name}` | hash | `machine`, `role`, `machine_cpu`, `machine_ram` | 15 s |
| `controller:autoscaler` | hash | `desired`, `ready`, `starting`, `maximum`, `last_action`, `updated_at` | none |
| `controller:alerts` | list | JSON `{at, message}`, newest first, capped at 20 | none |
| channel `system` | pub/sub | system status | — |
| channel `admin` | pub/sub | admin overview | — |
| channel `user:{user_id}` | pub/sub | that user's task messages | — |

Functions (names are used by later steps):

- `publish(channel, message)` — JSON-encodes a message model from `common/messages.py`.
- `publish_progress(user_id, task_id, progress, stage)` — writes the progress hash and publishes a `task` message with `status="running"`.
- `progress(task_id) -> int` and `progress_detail(task_id) -> (progress, stage) | None`.
- `publish_task_final(user_id, task_id, status)` — publishes `task_final`.
- `request_cancel(task_id, ttl)`, `cancel_requested(task_id) -> bool`, `clear_task_keys(task_id)`.
- Worker heartbeat: `start_heartbeat(host, state)` (starts a daemon thread writing every 5 s, as §4.8), `set_worker_state(host, state, task_id=None)` (updates the in-process state and writes immediately), `worker_heartbeats() -> list[WorkerHeartbeat]`, `count_workers(states) -> int`, `any_worker_idle_for(seconds) -> bool`. The machine name is the part of `host` before `-worker-`, or `MACHINE_NAME` if there is none.
- API and machine heartbeats: `write_api_heartbeat(machine, pid, sockets, requests_per_min)`, `api_heartbeats()`, `write_machine_heartbeat(machine, role)`, `machine_heartbeats()`. CPU and RAM come from `psutil` (whole-machine figures, which is what the admin panel needs even inside a container).
- Controller: `save_autoscaler_state(**fields)`, `autoscaler_state()`, `alert(message)`, `recent_alerts()`.
- `redis_run_id()` from `INFO server`.

Make the heartbeat interval and expiry parameters with the §4.8 values as defaults, so tests can shorten them.

### 2. `common/messages.py` — one schema for every message

Pydantic models, with `type` as a literal discriminator:

- `SystemStatus { type="system", running, waiting, new_image_ready_in_s }`
- `TaskUpdate { type="task", id, status, position?, queue_length?, progress?, stage?, ready_in_s? }` — workers send progress and stage; the controller sends position, queue length and ready time (step 08). Fields not sent are omitted, and the frontend merges what it receives.
- `TaskFinal { type="task_final", id, status }`
- `TaskOut` — what the HTTP API returns for a task: `task_id, status, prompt, size, created_at, started_at, finished_at, position, queue_length, progress, stage, ready_in_s, error_message`.
- `Snapshot { type="snapshot", system: SystemStatus, task: TaskOut | None }`
- `AdminOverview { type="admin", machines, workers, tasks, autoscaler }` with item models exactly as step 08 will fill them:
  - machine: `name, role, cpu, ram, api: {sockets, requests_per_min} | None`
  - worker: `name, machine, state, task_id, progress, rss_mb`
  - task: `id, user_email, size, status, worker_host, progress, position, seconds_so_far`
  - autoscaler: `desired, ready, starting, maximum, last_action, alerts: list[{at, message}]`

Also write `docs/messages.md`: one example JSON per message type. The frontend's TypeScript types (steps 10–11) are written from it.

### 3. `common/storage.py`

boto3 S3 client pointed at `S3_ENDPOINT` (path-style addressing for MinIO): `ensure_bucket()`, `put_png(key, image_or_bytes)` (accepts a PIL image or bytes), `open(key)` (an iterator of chunks for streaming), `exists(key)`, `delete(key)` (idempotent: deleting a missing key is not an error).

### 4. `common/estimate.py`

Exactly §4.6, split so the core is testable without services:

- `cost(size) -> float` = `(size / 512) ** 2`.
- `simulate_wait(running_remaining_costs, queued_costs, n_workers, seconds_per_unit) -> float` — the pure heap simulation.
- `seconds_per_cost_unit() -> float` — median over the last 50 `done` tasks, or `CALIBRATED_SECONDS_PER_COST_UNIT` when fewer than 5 exist.
- `estimate_wait_seconds(before=None) -> float` — gathers running tasks (remaining cost from `live.progress`), queued tasks and ready workers (`count_workers(("idle", "busy"))`, at least 1), then calls `simulate_wait`.
- `ready_in_seconds(task) -> float` — queued: wait for its position plus its own duration; running: remaining cost × seconds per unit; otherwise `0`.
- `new_image_ready_in_s() -> float` — wait for a task submitted now plus the duration of a 512 px job.

## Tests

`unit`:

- `simulate_wait([0.5], [1.0, 2.25], 2, 480) == 480` — the example from §9.2.
- No workers is treated as one; an empty queue returns the earliest free worker; more running tasks than workers is handled.
- `cost` for every allowed size matches the table in §1.3.
- Every message model serialises with its `type` and round-trips through JSON.

`api` (Redis and MinIO via testcontainers):

- The progress hash is written with the right expiry and the published message parses as `TaskUpdate`.
- `request_cancel` / `cancel_requested` / `clear_task_keys` behave and expire.
- A worker heartbeat with a 1-second expiry disappears after it; `count_workers(("idle","busy"))` ignores `loading`; `any_worker_idle_for` uses `since`.
- `alert()` keeps only the newest 20.
- Storage round trip: put, exists, stream, delete, delete again without error.

## Out of scope

HTTP endpoints, the worker, the controller loop.

## Done when

- `make lint`, `make test-unit` and `make test-api` pass.
- `grep -rn "\"progress:\|'progress:\|hb:worker\|cancel:" --include=*.py . | grep -v common/live.py` finds nothing outside `common/live.py` (and tests).
- `docs/messages.md` exists with one example per message type.

## Finish

Merge `feature/common-live-storage-estimate` into `develop`. Reply with what you built and how you verified it.
