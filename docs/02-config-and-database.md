# Step 02 of 14 — Configuration and database

You are building **imagegen** (see `CLAUDE.md` and `docs/architecture.md`). This is step 02 of fourteen. Do only this step.

## Goal

All limits and timeouts in one validated configuration module, the Postgres schema as an Alembic migration, and a small data-access module whose functions are the only way task statuses ever change.

## Before you start

- Read `docs/architecture.md` §1.2–1.4, §4.1 (limits and the timeout chain), §4.3 (data model and caps), §6.1 (task states and guards).
- Check step 01 is merged into `develop` and `make test-unit` passes. If not, stop and tell me.
- Branch: `feature/common-config-db` from `develop`.

## Build

### 1. `common/config.py`

A `Settings` class (pydantic-settings, reads environment variables and `.env`) and a cached `get_settings()`.

Plain settings with defaults:

| Setting | Default |
|---|---|
| `DATABASE_URL`, `DB_POOL_SIZE` | dev values from `.env.example`, `5` |
| `BROKER_URL`, `REDIS_URL` | `redis://localhost:6379/0` |
| `S3_ENDPOINT`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_BUCKET` | MinIO dev values, bucket `imagegen` |
| `JWT_SECRET` | required, no default |
| `COOKIE_SECURE` | `true` |
| `ALLOWED_ORIGINS` | `https://localhost` (comma-separated list) |
| `MACHINE_NAME` | the hostname |
| `PIPELINE` | `real` (`real` or `fake`) |
| `FAKE_STEP_SECONDS` | `1.0` |
| `MODEL_DIR` | `/models/sd15` |
| `WORKER_THREADS` | `2` |
| `STEP_DELAY_SECONDS` | `0` |
| `NUM_STEPS` | `25` |
| `ALLOWED_SIZES` | `(256, 384, 512, 640, 768)` |
| `MAX_PROMPT_LEN` | `500` |
| `MAX_ACTIVE_TASKS_PER_USER` | `1` |
| `MAX_QUEUED_TOTAL` | `30` |
| `MAX_HISTORY_PER_USER` | `50` |
| `MEASURED_MAX_RUNTIME_S` | `1080` (example; replaced after calibration in step 12) |
| `CALIBRATED_SECONDS_PER_COST_UNIT` | `480` |
| `SCALER` | `none` (`none` or `swarm`) |
| `WORKER_SERVICE` | `imagegen_worker` |
| `MIN_WORKERS`, `MAX_WORKERS` | `1`, `4` |
| `SCALE_UP_WAITING_PER_WORKER`, `COOLDOWN_S`, `STARTUP_TIMEOUT_S`, `SCALE_DOWN_IDLE_S` | `2`, `120`, `600`, `300` |
| `WORKER_PROC_ALIVE_TIMEOUT` | `180` |
| `ALERT_WEBHOOK_URL` | empty (optional) |

Derived timeouts, exactly as §4.1. Each can be overridden by an environment variable of the same name (the integration tests in step 13 shorten them); when not set, derive it:

- `TASK_SOFT_TIME_LIMIT = int(MEASURED_MAX_RUNTIME_S * 1.5)`
- `TASK_TIME_LIMIT = TASK_SOFT_TIME_LIMIT + 60`
- `BROKER_VISIBILITY_TIMEOUT = TASK_TIME_LIMIT * 2`
- `WORKER_STOP_GRACE_S = TASK_TIME_LIMIT + 120`
- `CANCEL_FLAG_TTL_S = TASK_TIME_LIMIT`

A model validator refuses to start when the order is broken: `MEASURED_MAX_RUNTIME_S < TASK_SOFT_TIME_LIMIT < TASK_TIME_LIMIT < BROKER_VISIBILITY_TIMEOUT` and `TASK_TIME_LIMIT < WORKER_STOP_GRACE_S`. The error message names the two settings that conflict. Also validate `MIN_WORKERS <= MAX_WORKERS` and that every allowed size is a multiple of 64.

### 2. `common/validation.py`

`SubmitBody` (pydantic: `prompt: str`, `size: int = 512`) and `validate(body) -> str` exactly as §4.1, raising a small `ValidationProblem` exception with the user-facing message. The API turns it into `422` in step 05; keeping FastAPI out of `common` lets the worker and controller import this module.

### 3. Alembic migration

Alembic in `migrations/` (with `alembic.ini` at the root, URL from `DATABASE_URL`). The first revision creates `users`, `refresh_tokens` and `tasks` with every column, constraint and index from §4.3, byte for byte: the role check, `lower(email)` unique index, the size check, `UNIQUE (user_id, idempotency_key)`, `tasks_user_history`, and the partial index `tasks_active`. `make migrate` runs `alembic upgrade head`.

### 4. `common/db.py`

SQLAlchemy 2 Core (no ORM): `Table` objects mirroring the migration, an engine built from `DATABASE_URL` with `pool_size=DB_POOL_SIZE`, and a `transaction()` context manager. Return plain dataclasses or `Row` objects, never ORM instances.

Functions (names are used by later steps; keep them):

- Users: `create_user(email, password_hash, role='user')`, `get_user(user_id)`, `get_user_by_email(email)` (case-insensitive), `set_role(user_id, role)`.
- Refresh tokens: `insert_refresh_token(user_id, token_hash, expires_at)`, `get_refresh_token(token_hash)`, `revoke_refresh_token(token_hash)`, `revoke_all_refresh_tokens(user_id)`.
- Submit helpers (take a transaction): `advisory_lock(tx)` (`SELECT pg_advisory_xact_lock(1)`), `find_by_idempotency_key(tx, user_id, key)`, `count_active_for_user(tx, user_id)`, `count_queued(tx)`, `insert_task(tx, user_id, prompt, size, idempotency_key)`.
- Reads: `get_task(task_id, user_id=None)` (with a user id, another user's task returns `None`), `count_tasks(status)`, `queue_position(task)` (1-based, by `created_at` among `queued`), `active_tasks()`, `running_tasks(started_before=None)`, `queued_tasks(created_before=None)` (oldest first), `recent_done_tasks(limit)`, `history_page(user_id, before=None, limit=20)` (newest first, cursor = `created_at`), `tasks_beyond_history_limit(limit)` (finished tasks beyond each user's newest `limit`).
- **Status changes — the only ones allowed anywhere** (§6.1), each a single guarded `UPDATE … RETURNING`:
  - `claim_task(task_id, worker_host) -> bool` — `queued` → `running`, sets `started_at`, `worker_host`.
  - `finish_task(task_id, status, result_key=None, error=None) -> bool` — `running` → `done`/`failed`, sets `finished_at`.
  - `cancel_task(task_id, owner_id) -> user_id | None` — `queued`/`running` → `cancelled`, sets `finished_at`; `owner_id=None` means any owner (admin).
  - `fail_queued(task_id, error) -> bool` — `queued` → `failed` (used when enqueueing fails).
- Deletion: `delete_task(task_id)`.

## Tests

`unit` (no services):

- Derived timeouts follow the formulas; overriding one by environment variable works.
- The validator rejects a broken order and names the conflicting settings.
- `validate()` accepts every allowed size and a prompt of exactly `MAX_PROMPT_LEN`; rejects `""`, whitespace, `MAX_PROMPT_LEN + 1` characters, a control character, and sizes `0, 100, 500, 1024, -1` (§9.2).

`api` (Postgres via testcontainers, migrations applied in a session fixture, each test in a clean schema or truncated tables):

- `claim_task` succeeds once; a second claim of the same task returns `False`.
- `finish_task` works only from `running`; after `cancel_task`, `finish_task(..., "done")` returns `False` and the status stays `cancelled`.
- `cancel_task` with the wrong owner returns `None`; with `owner_id=None` it works for any owner; on a `done` task it returns `None`.
- The size check rejects `500`; the same idempotency key twice for one user fails; for two users it succeeds.
- `queued_tasks` and `queue_position` follow `created_at`; `created_before` filters correctly.
- `tasks_beyond_history_limit(50)` returns only finished tasks beyond each user's newest 50, never queued or running ones.

## Out of scope

HTTP endpoints, Redis, MinIO, password hashing.

## Done when

- `make migrate` applies cleanly to the dev database, and `alembic downgrade base` followed by `upgrade head` also works.
- `make lint`, `make test-unit` and `make test-api` pass. Add the `api` job to CI (it uses testcontainers, so the runner needs Docker).
- `grep -rn "UPDATE tasks" --include=*.py .` finds matches only in `common/db.py`.

## Finish

Merge `feature/common-config-db` into `develop`. Reply with what you built, how you verified it, and any deviation from §4.3 (there should be none).
