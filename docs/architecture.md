# Text-to-image generation service — system architecture

A design for a web app that turns text prompts into images with PyTorch (Stable Diffusion 1.5 through Hugging Face `diffusers`). Generation runs on **CPU only** and is **deliberately slow**. The project exists to demonstrate and test queueing, progress reporting, cancellation, load balancing across machines and autoscaling, not to make images quickly.

The code in this document is a reference sketch. Names such as `db`, `live` (Redis helpers) and `storage` stand for small helper modules. The logic, the SQL, the configuration values and the order of the timeouts are meant to be used as written.

---

## 1. Why async job processing

One generation takes minutes on CPU, by design, so it can't run inside an HTTP request. The system is split into processes that only talk through a queue, a database and Redis:

- **API servers** are light and stateless. They authenticate, validate, accept or refuse jobs, answer status questions and push live updates over WebSockets.
- **Workers** are the only processes that import `torch`. Each one loads the model once and then runs one job at a time.
- **The controller** is one small service that watches the whole system. It adds and removes workers, cleans up after crashes, and publishes live status and wait times (§4.7, §6).

This split is what makes load balancing and horizontal scaling meaningful: the API tier scales with open connections, the worker tier with CPU cores and RAM, and each scales separately.

### 1.1 CPU only

Workers use the CPU build of PyTorch (`pip install torch --index-url https://download.pytorch.org/whl/cpu`) and load the pipeline in float32 on `"cpu"`. There are no NVIDIA drivers, no VRAM and no GPU machines anywhere, so any computer can be a worker and CI needs no special hardware.

### 1.2 Slow on purpose

The goal is a queue that visibly forms, progress that visibly moves, a Cancel button that can be pressed mid-run, and autoscaling that actually triggers. Three settings control the speed:

1. **`WORKER_THREADS`**: `torch.set_num_threads`, plus `OMP_NUM_THREADS` and `MKL_NUM_THREADS` in the container. Give each worker 1–2 threads and add workers rather than threads.
2. **`NUM_STEPS`** (fixed on the server, default 25) and the user's **`size`**. Cost grows with `NUM_STEPS × size²`.
3. **`STEP_DELAY_SECONDS`**: an optional pause after every denoising step. It guarantees slowness on any hardware and makes timings repeatable. `0` turns it off.

### 1.3 Adjustable picture size

The user picks one value, `size`: the side length of the square output in pixels. Only these values are accepted, all multiples of 64 as Stable Diffusion requires:

| `size` | Cost relative to 512 px |
|---|---|
| 256 px | 0.25× |
| 384 px | 0.56× |
| 512 px (default) | 1× |
| 640 px | 1.56× |
| 768 px (maximum) | 2.25× |

A job's cost is `cost(size) = (size / 512)²`, in units where a default job costs 1. Attention makes large sizes slightly more expensive than the formula says; the calibration in §4.1 absorbs that. The 768 px maximum is the complexity limit for requirement 1.

### 1.4 Rough numbers

For Stable Diffusion 1.5 at 512 px, 25 steps, float32 (measure your own machines): about 1–5 minutes per image with several threads, and 5–15+ minutes with 1–2 threads. Each worker holds about 5–6 GB of RAM at 512 px and more at 768 px, so RAM, not CPU, decides how many workers fit on a machine (§7).

---

## 2. Components

| Component | Role | Technology |
|---|---|---|
| Client | A page for users and a page for admins; one WebSocket for live updates | React or Vue, static files served by Nginx |
| Load balancer | Terminates TLS, serves the frontend, balances API traffic across the app machines, rate-limits login | Nginx |
| API servers | Auth, validation, submit and cancel, history, images, WebSocket fan-out. One per app machine | FastAPI + Uvicorn |
| Broker | The job queue | Redis, with AOF persistence |
| Workers | Load the model once per process, run one job at a time | Celery (prefork, concurrency 1) + `torch` (CPU) + `diffusers` |
| Controller | Autoscaler, sweeper, status and wait-time publisher, history cleanup. One instance | Python + Docker SDK |
| Database | Users, refresh tokens, tasks | PostgreSQL |
| Object storage | Result images | MinIO (S3-compatible) |
| Live state | Progress, cancel flags, heartbeats, pub/sub channels | Redis (the same instance as the broker) |
| Orchestration | Runs every service across the machines, restarts crashed containers | Docker Swarm |
| Metrics history (optional) | Graphs over days; the admin panel doesn't need it | Prometheus + Grafana |

Placement on machines:

- **Machine A (infra):** Nginx, Redis, Postgres, MinIO and the controller. It is the Swarm manager.
- **Machines B and C (app):** one API server each, plus as many workers as their RAM allows.
- More app machines join the same way (§5).

Machine A is a single point of failure. That is acceptable for this project and should be stated in the report; §7.3 describes what happens if it fails.

---

## 3. Client–server contract

The browser never talks to a worker and never waits for a generation inside a request. It submits a job, gets a task ID straight away, and then follows the job over a WebSocket.

### 3.1 HTTP endpoints

| Method | Path | What it does |
|---|---|---|
| POST | `/api/auth/register` | Create an account and sign in (sets the cookies) |
| POST | `/api/auth/login` | Sign in: sets the access and refresh cookies |
| POST | `/api/auth/refresh` | Replace the refresh token and issue a new access cookie |
| POST | `/api/auth/logout` | Revoke the refresh token and clear both cookies |
| GET | `/api/status` | Running count, waiting count, time until a new image would be ready |
| POST | `/api/tasks` | Submit `{prompt, size}` with an `Idempotency-Key` header |
| GET | `/api/tasks/active` | The user's queued or running task, or `null`; restores the page after a reload |
| GET | `/api/tasks?before=<cursor>` | History, newest first (at most 50 kept per user) |
| GET | `/api/tasks/{id}` | One task |
| GET | `/api/tasks/{id}/image` | The result image, streamed from storage after an ownership check |
| DELETE | `/api/tasks/{id}` | Cancel a queued or running task |
| GET | `/api/admin/overview` | Admin snapshot (admin role only) |
| DELETE | `/api/admin/tasks/{id}` | Cancel any user's task (admin role only) |
| GET | `/ws` | WebSocket for live updates |

`POST /api/tasks` answers `202` with `{task_id, status, position, wait_s, ready_in_s}`, or refuses with:

- `422` for an empty or too-long prompt, or a size outside the list,
- `429` when the user already has a task queued or running,
- `503` with a `Retry-After` header when the global queue is full.

The `Idempotency-Key` is a UUID the browser creates for each click on Generate. If the request is retried (after a network error, or an API server dying mid-request), the server returns the task it already created instead of creating a second one.

Images are never exposed by URL: the API reads them from MinIO and streams them, so MinIO stays unreachable from outside and nobody can open another user's image.

### 3.2 WebSocket

`GET /ws` (over `wss://`) authenticates with the same access cookie as the HTTP API, because browsers can't attach an `Authorization` header to a WebSocket. The server also checks the `Origin` header, so another site can't open a socket on the user's behalf.

| Message (server → client) | When | Content |
|---|---|---|
| `snapshot` | Right after connecting | System status, and the user's active task if any |
| `system` | Every 5 s | `running`, `waiting`, `new_image_ready_in_s` |
| `task` | After each step, and every 5 s | `id`, `status`, `position`, `progress`, `stage`, `ready_in_s` |
| `task_final` | When the task ends | The final status |
| `admin` | Every 5 s, admin sockets only | The overview from §4.8 |

Rules that keep this reliable:

- The server sends a WebSocket ping every 25 s, well inside Nginx's read timeout (§4.5), so a socket stays open while its job waits in the queue for many minutes.
- Pub/sub messages sent while a client is disconnected are lost. So on every reconnect (backoff 1, 2, 5, 10 s) the client gets a fresh `snapshot` and continues from it.
- Each API process holds **one** Redis subscription (`system`, `admin` and the pattern `user:*`) and forwards messages to its own sockets. It never opens one Redis connection per browser.
- A socket stays authorised while it is open. When it reconnects after the access token has expired, the client calls `/api/auth/refresh` first.

### 3.3 Why any API server can answer any request

The access token is a signed JWT in a cookie, so every API server can check it without shared session memory. Everything else that is shared (tasks, progress, flags, images) lives in Postgres, Redis or MinIO. That is what lets Nginx send each request to whichever API server is least busy, without sticky sessions.

---

## 4. Requirement-by-requirement design

### 4.1 Limits on computational complexity (requirement 1)

All limits live in one config module. The server enforces them; the GUI's own checks (a size dropdown, a 500-character text box, a non-empty check) only save the user a round trip.

```python
# common/config.py — every limit in one place
ALLOWED_SIZES = (256, 384, 512, 640, 768)   # px, square output; the only cost the user controls
NUM_STEPS = 25                              # fixed on the server; the GUI doesn't expose it
MAX_PROMPT_LEN = 500
MAX_ACTIVE_TASKS_PER_USER = 1               # one queued-or-running task per user
MAX_QUEUED_TOTAL = 30                       # global queue cap; beyond it, new jobs are refused
MAX_HISTORY_PER_USER = 50                   # older finished tasks and their images are deleted
```

```python
class SubmitBody(BaseModel):
    prompt: str
    size: int = 512

def validate(body: SubmitBody) -> str:
    prompt = body.prompt.strip()
    if not prompt:
        raise HTTPException(422, "The prompt is empty")
    if len(prompt) > MAX_PROMPT_LEN:
        raise HTTPException(422, f"The prompt is longer than {MAX_PROMPT_LEN} characters")
    if any(ord(c) < 32 and c not in "\n\t" for c in prompt):
        raise HTTPException(422, "The prompt contains control characters")
    if body.size not in ALLOWED_SIZES:
        raise HTTPException(422, f"Size must be one of {ALLOWED_SIZES}")
    return prompt
```

#### The time limits form one chain

Computation time is limited too, and several timeouts hang off the same number: how long the largest allowed job (768 px) takes on the slowest app machine, including `STEP_DELAY_SECONDS`. Measure it with `scripts/calibrate.py`, which runs one 768 px job with the real model on each app machine and also records `CALIBRATED_SECONDS_PER_COST_UNIT` for the wait estimate (§4.6). Everything else is derived from it:

```python
# common/config.py — one measured number drives every timeout
MEASURED_MAX_RUNTIME_S = 18 * 60                          # example: default job 8 min × 2.25 for 768 px
TASK_SOFT_TIME_LIMIT = int(MEASURED_MAX_RUNTIME_S * 1.5)  # 27 min: the task marks itself failed
TASK_TIME_LIMIT = TASK_SOFT_TIME_LIMIT + 60               # 28 min: Celery kills the process
BROKER_VISIBILITY_TIMEOUT = TASK_TIME_LIMIT * 2           # 56 min: Redis redelivers an unacknowledged job only after this
WORKER_STOP_GRACE_S = TASK_TIME_LIMIT + 120               # 30 min: stop_grace_period in the stack file (§5.2)
CANCEL_FLAG_TTL_S = TASK_TIME_LIMIT                       # a cancel flag outlives any job
WORKER_PROC_ALIVE_TIMEOUT = 180                           # longer than loading the model (20–60 s), §4.2
```

The order must always hold: measured runtime < soft limit < hard limit < broker visibility timeout, and hard limit < stop grace period. A unit test checks it, including against the stack file (§9.2). Why each link matters:

- If the hard limit were below the real runtime, valid maximum-size jobs would be killed.
- If the visibility timeout were below the hard limit, Redis would hand a still-running job to a second worker.
- If the stop grace period were below the hard limit, scaling down could kill a job mid-run (§4.7).

If you use RabbitMQ instead of Redis, its `consumer_timeout` (default 30 minutes) plays the role of the visibility timeout and must be raised above `TASK_TIME_LIMIT`.

### 4.2 The worker task and progress reporting (requirement 2)

This is the only definition of the generation task; §4.7, §6 and the tests all refer to it.

#### Celery settings

```python
# worker/app.py
celery_app = Celery("imagegen", broker=config.BROKER_URL, include=["worker.tasks"])
celery_app.conf.update(
    task_default_queue="image_generation",
    task_acks_late=True,            # with prefetch 1, a busy worker never holds a second job
    worker_prefetch_multiplier=1,
    task_ignore_result=True,        # progress and results go to Redis keys and Postgres instead
    broker_transport_options={"visibility_timeout": config.BROKER_VISIBILITY_TIMEOUT},
    worker_proc_alive_timeout=config.WORKER_PROC_ALIVE_TIMEOUT,  # the child loads the model before it reports ready
)
```

Each worker container runs `celery -A worker.app:celery_app worker --pool=prefork --concurrency=1`. Prefork with a single child keeps Celery's time limits and forced termination working. The model is loaded inside the child process, through `worker_process_init`, never in the parent before the fork: PyTorch's thread pools don't survive a fork, and loading the model before one is a known cause of hangs.

#### Loading the model

```python
# worker/pipeline.py
def load_pipeline():
    if config.PIPELINE == "fake":                  # tests and CI: no weights, no real computation
        return FakePipeline(step_seconds=config.FAKE_STEP_SECONDS)
    from diffusers import StableDiffusionPipeline
    pipe = StableDiffusionPipeline.from_pretrained(config.MODEL_DIR, torch_dtype=torch.float32).to("cpu")
    pipe.enable_attention_slicing()                # caps peak RAM at 768 px
    pipe.set_progress_bar_config(disable=True)
    return pipe

class FakePipeline:
    """Same call as the real pipeline, but sleeps instead of computing."""
    def __init__(self, step_seconds):
        self.step_seconds = step_seconds

    def __call__(self, prompt, num_inference_steps, height, width, callback_on_step_end):
        for step in range(num_inference_steps):
            time.sleep(self.step_seconds)
            callback_on_step_end(self, step, 0, {})
        return SimpleNamespace(images=[Image.new("RGB", (width, height), "gray")])
```

The model weights are baked into the worker image at build time (`MODEL_DIR=/models/sd15`), so a new worker never downloads several GB before it can start.

#### The task

```python
# worker/tasks.py
HOST = socket.gethostname()        # e.g. "nodeB-worker-2", set by the stack file (§5.2)
pipe = None

@worker_process_init.connect
def init_worker_process(**_):
    global pipe
    live.start_heartbeat(HOST, state="loading")    # visible in the admin panel while it loads
    torch.set_num_threads(config.WORKER_THREADS)
    pipe = load_pipeline()                         # 20–60 s with the real model
    live.set_worker_state(HOST, "idle")

class TaskCancelled(Exception):
    pass

def pause_unless_cancelled(task_id, seconds):
    """STEP_DELAY_SECONDS, checking for a cancel every half second."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if live.cancel_requested(task_id):
            raise TaskCancelled()
        time.sleep(max(0.0, min(0.5, end - time.monotonic())))

@celery_app.task(soft_time_limit=config.TASK_SOFT_TIME_LIMIT, time_limit=config.TASK_TIME_LIMIT)
def generate_image(task_id: str, user_id: str, prompt: str, size: int) -> None:
    # Claim: only a task that is still 'queued' may start. This also skips tasks cancelled
    # while queued, even on a worker that started after the cancel was sent.
    if not db.claim_task(task_id, worker_host=HOST):
        return
    live.set_worker_state(HOST, "busy", task_id=task_id)
    live.publish_progress(user_id, task_id, progress=0, stage="starting")

    def on_step_end(pipeline, step, timestep, callback_kwargs):
        done = step + 1
        if live.cancel_requested(task_id):
            raise TaskCancelled()
        stage = "decoding image" if done == config.NUM_STEPS else f"step {done} of {config.NUM_STEPS}"
        live.publish_progress(user_id, task_id, progress=done * 90 // config.NUM_STEPS, stage=stage)
        if config.STEP_DELAY_SECONDS:
            pause_unless_cancelled(task_id, config.STEP_DELAY_SECONDS)
        return callback_kwargs

    key = f"results/{task_id}.png"
    image = None
    try:
        image = pipe(prompt=prompt, num_inference_steps=config.NUM_STEPS, height=size, width=size,
                     callback_on_step_end=on_step_end).images[0]
        live.publish_progress(user_id, task_id, progress=95, stage="saving")
        storage.put_png(key, image)
        # Finish guard: only a task that is still 'running' becomes 'done'.
        if not db.finish_task(task_id, "done", result_key=key):
            storage.delete(key)                    # cancelled during the last step: drop the image
    except TaskCancelled:
        pass                                       # the API has already set 'cancelled'
    except SoftTimeLimitExceeded:
        db.finish_task(task_id, "failed", error="time limit exceeded")
    except Exception as exc:
        db.finish_task(task_id, "failed", error=str(exc)[:500])
    finally:
        image = None
        gc.collect()                               # keeps the worker's RAM flat across jobs
        live.clear_task_keys(task_id)              # progress hash and cancel flag
        live.set_worker_state(HOST, "idle")
        live.publish_task_final(user_id, task_id)  # the client reads the final state
```

The two guards are plain SQL, so they hold no matter which worker, API server or controller touches the task:

```python
# common/db.py
def claim_task(task_id, worker_host) -> bool:
    return execute("""UPDATE tasks SET status='running', started_at=now(), worker_host=:h
                      WHERE id=:id AND status='queued' RETURNING id""",
                   id=task_id, h=worker_host) is not None

def finish_task(task_id, status, result_key=None, error=None) -> bool:
    return execute("""UPDATE tasks SET status=:s, finished_at=now(), result_key=:k, error_message=:e
                      WHERE id=:id AND status='running' RETURNING id""",
                   id=task_id, s=status, k=result_key, e=error) is not None
```

#### What the client sees as progress

| Stage | Progress |
|---|---|
| starting (encoding the prompt) | 0% |
| step *n* of 25 | 0–90%, in equal parts |
| decoding image | 90% |
| saving | 95% |
| done | 100% |

The denoising steps cover 0–90% because on CPU the decoding and saving after the last step take noticeable time. Each update is written to a Redis hash `progress:{task_id}`, so a reconnecting client and the admin panel can read the latest value, and is published on the owner's channel `user:{user_id}`.

### 4.3 History, status, cancellation and task limits (requirement 3)

#### Data model

```sql
CREATE TABLE users (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email         TEXT NOT NULL,
    password_hash TEXT NOT NULL,                     -- argon2
    role          TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('user', 'admin')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX users_email_unique ON users (lower(email));

CREATE TABLE refresh_tokens (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash  TEXT NOT NULL UNIQUE,                -- only a hash is stored
    expires_at  TIMESTAMPTZ NOT NULL,
    revoked_at  TIMESTAMPTZ
);

CREATE TABLE tasks (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status          TEXT NOT NULL CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled')),
    prompt          TEXT NOT NULL,
    size            SMALLINT NOT NULL CHECK (size IN (256, 384, 512, 640, 768)),
    idempotency_key UUID NOT NULL,
    worker_host     TEXT,                            -- which worker, on which machine, ran it
    result_key      TEXT,                            -- object-storage key of the image
    error_message   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    UNIQUE (user_id, idempotency_key)
);
CREATE INDEX tasks_user_history ON tasks (user_id, created_at DESC);
CREATE INDEX tasks_active ON tasks (status, created_at) WHERE status IN ('queued', 'running');
```

Live progress isn't stored in Postgres: it changes every few seconds and only matters while a job runs, so it lives in Redis (§4.2). Postgres holds only the states that must survive anything.

#### Three limits on the number of tasks

- **Per user:** at most one queued or running task (`MAX_ACTIVE_TASKS_PER_USER = 1`), which also matches the one-task GUI.
- **Whole system:** at most `MAX_QUEUED_TOTAL = 30` waiting tasks. Past that, new jobs are refused with `503` and a time to retry.
- **History:** at most `MAX_HISTORY_PER_USER = 50` finished tasks per user. The controller deletes older ones and their images (§6.5).

#### Submitting a task

```python
@app.post("/api/tasks", status_code=202)
def submit(body: SubmitBody, idempotency_key: UUID = Header(), user=Depends(current_user)):
    prompt = validate(body)
    with db.transaction() as tx:
        # Serialises every submit in the system for a few milliseconds, so two clicks that
        # land on different API servers can't both slip past the caps.
        tx.execute("SELECT pg_advisory_xact_lock(1)")
        existing = tx.fetch_one("SELECT * FROM tasks WHERE user_id=:u AND idempotency_key=:k",
                                u=user.id, k=idempotency_key)
        if existing:
            return task_response(existing)         # a retried request: same task, no duplicate
        active = tx.scalar("SELECT count(*) FROM tasks WHERE user_id=:u AND status IN ('queued','running')",
                           u=user.id)
        if active >= MAX_ACTIVE_TASKS_PER_USER:
            raise HTTPException(429, "You already have an image in progress")
        if tx.scalar("SELECT count(*) FROM tasks WHERE status='queued'") >= MAX_QUEUED_TOTAL:
            retry = int(estimate_wait_seconds())
            raise HTTPException(503, "The queue is full", headers={"Retry-After": str(retry)})
        task = tx.insert_task(user_id=user.id, prompt=prompt, size=body.size,
                              idempotency_key=idempotency_key, status="queued")
    # Enqueue only after the commit, so the worker's claim always finds the row.
    try:
        enqueue(task)
    except Exception:
        db.execute("""UPDATE tasks SET status='failed', finished_at=now(), error_message='queue unavailable'
                      WHERE id=:id AND status='queued'""", id=task.id)
        raise HTTPException(503, "The queue is unavailable, try again shortly")
    return task_response(task)                     # status, position, wait_s, ready_in_s (§4.6)

def enqueue(task):
    generate_image.apply_async(args=[str(task.id), str(task.user_id), task.prompt, task.size],
                               task_id=str(task.id))   # Celery's ID = the DB ID, so revoke() can find it
```

Reading results:

```python
@app.get("/api/tasks/{task_id}/image")
def task_image(task_id: UUID, user=Depends(current_user)):
    task = db.get_task(task_id, user_id=user.id)   # another user's task looks like a missing one
    if task is None or task.status != "done":
        raise HTTPException(404)
    return StreamingResponse(storage.open(task.result_key), media_type="image/png",
                             headers={"Cache-Control": "private, max-age=86400"})
```

The history endpoint pages through `tasks_user_history`, newest first. Cancelling is in §6.3.

### 4.4 Authorisation and HTTPS (requirement 4)

- **Passwords** are hashed with argon2 and never logged.
- **Access token:** a JWT valid for 15 minutes, in an `HttpOnly; Secure; SameSite=Strict` cookie. Any API server can verify it with the shared signing key (§3.3), and the WebSocket handshake carries it automatically.
- **Refresh token:** a random value valid for 7 days, in an `HttpOnly; Secure; SameSite=Strict` cookie limited to the path `/api/auth`, so both refresh and logout receive it (ADR 0003). Only its hash is stored in `refresh_tokens`. Each refresh replaces it with a new one and logout revokes it, so a session can really be ended.
- **Requests from other sites:** `SameSite=Strict` keeps the cookies off requests that another site starts, and the WebSocket handshake also checks `Origin`.
- **Admin role:** admin endpoints and the `admin` WebSocket channel check `users.role = 'admin'`, not just a valid token.
- **Login rate limit** is enforced in Nginx (§4.5), not in the API servers. A limit kept in each API server's memory would be multiplied by the number of servers.
- **HTTPS:** TLS ends at Nginx; port 80 redirects to 443 and HSTS is set. With a public domain, use Let's Encrypt (Certbot). For a local or lab demo without one, use `mkcert` (a locally trusted certificate) or a self-signed certificate.
- **Inside the cluster** traffic stays on the private network. Redis and Postgres still require passwords, and only Nginx's ports 80 and 443 are reachable from outside.

### 4.5 Load balancing across at least two servers (requirement 5)

```nginx
# nginx.conf (inside http {})
limit_req_zone $binary_remote_addr zone=auth:10m rate=10r/m;
limit_req_status 429;

upstream api_pool {
    least_conn;                                            # suits long-lived WebSockets
    server 10.0.0.11:8000 max_fails=3 fail_timeout=10s;    # machine B
    server 10.0.0.12:8000 max_fails=3 fail_timeout=10s;    # machine C
}

server {
    listen 80;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    ssl_certificate     /run/secrets/tls_cert;             # Swarm secrets (§5.2)
    ssl_certificate_key /run/secrets/tls_key;
    add_header Strict-Transport-Security "max-age=31536000" always;

    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_connect_timeout 2s;
    proxy_next_upstream error timeout http_502 http_503;

    location / {                                           # user page and admin page
        root /srv/frontend;
        try_files $uri /index.html;
    }

    location /api/auth/ {
        limit_req zone=auth burst=5 nodelay;
        proxy_pass http://api_pool;
    }

    location /api/ {
        proxy_pass http://api_pool;
    }

    location /ws {
        proxy_pass http://api_pool;
        proxy_http_version 1.1;
        # A location that sets any proxy_set_header inherits none from the server block,
        # so the forwarding headers are repeated here.
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade           $http_upgrade;
        proxy_set_header Connection        "upgrade";
        proxy_read_timeout 1h;                             # the default 60 s would close a queued user's quiet socket
        proxy_send_timeout 1h;
    }
}
```

How the load is spread:

- **Requests:** Nginx sends HTTP requests and WebSockets to the API servers on B and C, choosing the one with fewer open connections.
- **Generation jobs:** the queue spreads them over all workers on B and C; whichever worker is free takes the next job.
- So generation really runs on several application servers, as the requirement asks. Every task records `worker_host` and the admin panel shows it, which is how to demonstrate this to a grader.

When an API server dies:

- `max_fails` and `fail_timeout` take it out of rotation after 3 failed requests, for 10 s at a time. Open-source Nginx only notices failures on real requests; it has no active health checks.
- New requests, and GET requests that fail mid-flight, go to the other server.
- A POST that already reached the dead server fails, because Nginx never resends a POST once it has been sent. The browser retries it with the same `Idempotency-Key`, so no duplicate task is created.
- WebSockets on the dead server drop; the browser reconnects and gets a fresh snapshot (§3.2).

Adding a third app machine: add its line to `upstream api_pool`, run `nginx -s reload` (no downtime) and raise the API replica count by one (§5.2).

### 4.6 Queue notice and waiting time (requirement 6)

When every worker is busy, a new task waits. The submit response and the WebSocket say so (`status: "queued"`, the position, and when the image should be ready), and the numbers update every 5 seconds as the queue moves. "Resources exhausted" means exactly this: no free worker. It isn't measured by CPU %, because with `STEP_DELAY_SECONDS` a fully booked worker spends much of its time sleeping.

Jobs differ in cost (a 768 px job costs 9× a 256 px one), so the estimate simulates the queue instead of multiplying the position by an average:

```python
# common/estimate.py
def cost(size: int) -> float:
    return (size / 512) ** 2                        # 512 px = 1.0

def seconds_per_cost_unit() -> float:
    """Median of (run time / cost) over the last 50 finished tasks, or the calibrated value."""
    recent = db.recent_done_tasks(limit=50)
    if len(recent) < 5:
        return config.CALIBRATED_SECONDS_PER_COST_UNIT
    return statistics.median((t.finished_at - t.started_at).total_seconds() / cost(t.size)
                             for t in recent)

def estimate_wait_seconds(before=None) -> float:
    """Seconds until a task submitted at `before` (default: now) would start, found by
    simulating the queue, oldest first, on the workers that are ready now."""
    spu = seconds_per_cost_unit()
    n = max(live.count_workers(states=("idle", "busy")), 1)
    free_at = [cost(t.size) * (1 - live.progress(t.id) / 100) * spu for t in db.running_tasks()][:n]
    free_at += [0.0] * (n - len(free_at))
    heapq.heapify(free_at)
    for t in db.queued_tasks(created_before=before):
        heapq.heappush(free_at, heapq.heappop(free_at) + cost(t.size) * spu)
    return free_at[0]
```

What the user is shown:

- **Before submitting:** `new_image_ready_in_s = estimate_wait_seconds() + cost(512) × spu`.
- **While queued:** the position, and `ready_in_s = estimate_wait_seconds(before=task.created_at) + cost(task.size) × spu`.
- **While running:** `ready_in_s = cost(size) × (1 − progress) × spu`.

The estimate ignores workers that are still loading and scale-ups that haven't happened yet, so it errs towards longer waits. It is measured in real run times (`STEP_DELAY_SECONDS` included), so it corrects itself as tasks finish.

### 4.7 Horizontal autoscaling with a limit (requirement 7)

The controller (one instance, on machine A) changes how many worker containers Swarm runs. Swarm places each new container on an app machine with enough free RAM and CPU, so scaling spreads across machines.

```python
# controller/autoscaler.py
MIN_WORKERS = 1
MAX_WORKERS = 4                     # from the sizing rule in §7.1: measured, not guessed
SCALE_UP_WAITING_PER_WORKER = 2     # waiting jobs per ready worker before adding one
COOLDOWN_S = 120                    # longer than a worker needs to load the model
STARTUP_TIMEOUT_S = 600             # a worker still not ready after this can't be placed
SCALE_DOWN_IDLE_S = 300
SERVICE = "imagegen_worker"

def autoscale_tick(now):
    desired = swarm.replicas(SERVICE)                      # what the controller asked Swarm for
    ready = live.count_workers(states=("idle", "busy"))    # model loaded, taking jobs
    starting = max(desired - ready, 0)                     # launched but loading, or not placed yet
    waiting = db.count_tasks(status="queued")
    live.save_autoscaler_state(desired=desired, ready=ready, starting=starting, maximum=MAX_WORKERS)

    if starting > 0:
        # Never stack launches while one is still booting. If it can't be placed
        # (no machine has enough free RAM), give up on it instead of waiting forever.
        if now - state.last_scale_at > STARTUP_TIMEOUT_S:
            swarm.set_replicas(SERVICE, ready)
            state.last_scale_at = now
            live.alert("A new worker could not start: no app machine has enough free memory")
        return

    if now - state.last_scale_at < COOLDOWN_S:
        return

    if waiting > SCALE_UP_WAITING_PER_WORKER * ready and desired < MAX_WORKERS:
        swarm.set_replicas(SERVICE, desired + 1)
        state.last_scale_at = now
    elif waiting == 0 and desired > MIN_WORKERS and live.any_worker_idle_for(SCALE_DOWN_IDLE_S):
        swarm.set_replicas(SERVICE, desired - 1)           # one at a time
        state.last_scale_at = now
```

`swarm.replicas` and `swarm.set_replicas` are thin wrappers over the Docker SDK (`client.services.get(name)` and `service.scale(n)`).

Why it behaves well:

- **No overshoot.** A new worker needs 20–60 s to load the model, longer on a machine that must first pull the image. While any launched worker isn't ready, the controller launches nothing else, and after every change it waits out a cooldown.
- **No endless waiting.** If no machine has room for another worker, Swarm leaves it pending. After `STARTUP_TIMEOUT_S` the controller gives up on it, sets the count back and shows an alert in the admin panel.
- **The limit is real.** `MAX_WORKERS` comes from the sizing rule in §7.1, and Swarm's memory reservations (§5.2) stop it from putting more workers on a machine than fit.
- **Scaling down never kills a job.** It happens only when nothing is waiting and a worker has been idle for 5 minutes, one worker at a time. Swarm chooses which container to stop and may pick a busy one. That container receives SIGTERM, and Celery's warm shutdown stops taking new jobs but finishes the current one. Because `stop_grace_period` (30 min) is longer than `TASK_TIME_LIMIT` (28 min), Swarm waits for it before forcing anything.

What it doesn't do: start new virtual machines. New machines join the cluster by hand (§5.1), and from then on the controller uses them automatically. Starting VMs through a cloud provider's API would be the next step; the requirement ("virtual machines, containers") is met with containers.

`docker compose up --scale worker=N` only works on a single machine, so it is for local development only.

### 4.8 Admin panel (requirement 8)

A separate page of the same frontend at `/admin`, open only to users with `role = 'admin'`. It updates every 5 seconds over the WebSocket (`admin` messages) and has four blocks:

| Block | What it shows |
|---|---|
| Machines | Each machine's role, CPU % and RAM %; for app machines also the API server's open WebSockets and requests per minute |
| Workers | Name (machine and slot), state (loading, idle or busy), current task, progress, process RAM |
| Tasks | Every user's running and queued tasks: user, size, worker, progress or queue position, time so far, and a Cancel button |
| Autoscaler | Ready, starting, desired and maximum workers; last action; alerts |

#### Where the numbers come from: heartbeats

Every worker process, every API process and the controller write a heartbeat to Redis every 5 seconds, with a 15-second expiry, so a process that dies simply disappears. The same heartbeats feed the sweeper (§6.5) and the autoscaler's worker count (§4.7), so the three always agree.

```python
# common/live.py: the worker heartbeat, started from worker_process_init
def start_heartbeat(host, state):
    _state.update(state=state, task_id="", since=time.time())
    proc = psutil.Process()

    def loop():
        while True:
            redis.hset(f"hb:worker:{host}", mapping={
                **_state,
                "machine": host.split("-worker-")[0],
                "machine_cpu": psutil.cpu_percent(interval=None),   # the whole machine, even inside a container
                "machine_ram": psutil.virtual_memory().percent,
                "rss_mb": proc.memory_info().rss // 2**20,
            })
            redis.expire(f"hb:worker:{host}", 15)
            time.sleep(5)

    threading.Thread(target=loop, daemon=True).start()
```

`set_worker_state` updates `_state` (`state`, `task_id`, `since`), and the loop publishes it within 5 seconds. Inside a container, psutil's CPU and memory figures describe the whole machine, which is what the Machines block needs. API processes write `hb:api:{machine}:{pid}` the same way, with their WebSocket count and requests per minute, and the controller reports machine A.

The controller assembles the overview (machines, workers, tasks, autoscaler state) and publishes it on the `admin` channel every 5 seconds; `GET /api/admin/overview` returns the same object for the first paint.

Prometheus and Grafana are optional, for graphs over days. They would need Swarm service discovery (`dockerswarm_sd_configs`) to find the workers the autoscaler starts.

---

## 5. Running on several machines

Every API server and worker can run on its own physical or virtual machine. These conditions make that work:

- **No local state.** API servers and workers keep nothing another process needs. Tasks, progress, flags and images live in Postgres, Redis and MinIO on machine A.
- **One private network.** All machines are on the same LAN, VPC or VPN. Swarm needs ports 2377/tcp, 7946/tcp+udp and 4789/udp open between them.
- **Real addresses in Nginx.** The upstream block lists each app machine's IP with port 8000 (§4.5).
- **One broker.** Every worker uses the same `BROKER_URL`, so whichever worker is free takes the next job, whatever machine it is on.
- **Synchronised clocks.** Run NTP on every machine; token expiry and task timestamps compare times written by different machines.

### 5.1 Creating the cluster

```bash
# machine A
docker swarm init --advertise-addr <A-ip>
docker node update --label-add role=infra <A-node>

# machines B, C, ... (swarm init prints the exact join command)
docker swarm join --token <token> <A-ip>:2377

# back on A, for each new machine
docker node update --label-add role=app <node>

# on every app machine, before the demo: pull the worker image in advance,
# or the first worker started there spends minutes downloading several GB
docker pull <registry>/imagegen-worker:<tag>

# TLS certificate and key as Swarm secrets (Let's Encrypt, or mkcert for a lab demo)
docker secret create tls_cert fullchain.pem
docker secret create tls_key privkey.pem
```

### 5.2 Stack file

```yaml
# infra/stack.yml — deploy with: docker stack deploy -c infra/stack.yml imagegen
services:
  nginx:
    image: ${REGISTRY}/imagegen-nginx:${TAG}        # nginx + nginx.conf + the built frontend
    ports:
      - {target: 80, published: 80, mode: host}
      - {target: 443, published: 443, mode: host}
    secrets: [tls_cert, tls_key]
    deploy:
      placement: {constraints: [node.labels.role == infra]}

  redis:
    image: redis:7
    command: ["redis-server", "--appendonly", "yes", "--requirepass", "${REDIS_PASSWORD}"]
    volumes: [redis-data:/data]
    deploy:
      placement: {constraints: [node.labels.role == infra]}

  postgres:
    image: postgres:16
    environment:
      POSTGRES_USER: imagegen
      POSTGRES_PASSWORD: ${PG_PASSWORD}
      POSTGRES_DB: imagegen
    volumes: [pg-data:/var/lib/postgresql/data]
    deploy:
      placement: {constraints: [node.labels.role == infra]}

  minio:
    image: pgsty/minio                               # community build of MinIO (ADR 0002)
    command: server /data
    environment:
      MINIO_ROOT_USER: imagegen
      MINIO_ROOT_PASSWORD: ${MINIO_PASSWORD}
    volumes: [minio-data:/data]
    deploy:
      placement: {constraints: [node.labels.role == infra]}

  api:
    image: ${REGISTRY}/imagegen-api:${TAG}
    command: uvicorn api.main:app --host 0.0.0.0 --port 8000 --workers 4
    ports:
      - {target: 8000, published: 8000, mode: host}  # Nginx lists each app machine's IP:8000
    environment: &common_env
      BROKER_URL: redis://:${REDIS_PASSWORD}@redis:6379/0
      DATABASE_URL: postgresql://imagegen:${PG_PASSWORD}@postgres/imagegen
      DB_POOL_SIZE: "5"
      S3_ENDPOINT: http://minio:9000
    deploy:
      replicas: 2                                    # one per app machine
      placement:
        constraints: [node.labels.role == app]
        max_replicas_per_node: 1

  worker:
    image: ${REGISTRY}/imagegen-worker:${TAG}        # includes the model weights
    hostname: "{{.Node.Hostname}}-worker-{{.Task.Slot}}"
    command: celery -A worker.app:celery_app worker --pool=prefork --concurrency=1
    environment:
      BROKER_URL: redis://:${REDIS_PASSWORD}@redis:6379/0
      DATABASE_URL: postgresql://imagegen:${PG_PASSWORD}@postgres/imagegen
      DB_POOL_SIZE: "1"
      S3_ENDPOINT: http://minio:9000
      WORKER_THREADS: "2"
      OMP_NUM_THREADS: "2"
      MKL_NUM_THREADS: "2"
      STEP_DELAY_SECONDS: "2"
    stop_grace_period: 30m                           # = WORKER_STOP_GRACE_S (§4.1)
    deploy:
      replicas: 1                                    # the controller changes this (§4.7)
      placement: {constraints: [node.labels.role == app]}
      resources:
        reservations: {cpus: "2", memory: 7G}        # measured peak at 768 px + 1 GB margin
        limits: {cpus: "2", memory: 8G}
      restart_policy: {condition: any}

  controller:
    image: ${REGISTRY}/imagegen-controller:${TAG}
    environment: *common_env
    volumes: ["/var/run/docker.sock:/var/run/docker.sock"]   # to change the worker count
    deploy:
      replicas: 1
      placement: {constraints: [node.role == manager]}

volumes:
  redis-data: {}
  pg-data: {}
  minio-data: {}

secrets:
  tls_cert: {external: true}
  tls_key: {external: true}
```

What matters in it:

- **`api`** publishes port 8000 in `host` mode, with at most one replica per machine, so Nginx's upstream list maps exactly onto real servers. Swarm's default routing mesh would forward traffic between machines behind Nginx's back and hide a dead server.
- **`worker`** reserves its measured peak memory and two CPUs, so Swarm never places more workers on a machine than it can hold. The `hostname` template names each worker after its machine (`nodeB-worker-2`), which is what `worker_host` and the admin panel show.
- **`stop_grace_period`** equals `WORKER_STOP_GRACE_S` (§4.1); a test checks that the two match (§9.2).
- **Redis** runs with AOF persistence, so a restart doesn't lose the queue; the controller also re-sends queued jobs after a Redis restart (§6.5).

Adding a machine later: join it, label it `role=app`, add its IP to Nginx's upstream list, and raise `api` replicas by one. Workers start using it automatically.

---

## 6. Live status and safe cancellation

### 6.1 Task states and who changes them

```
queued ──► running ──► done
   │          ├──────► failed
   │          └──────► cancelled
   ├─────────────────► cancelled
   └─────────────────► failed      (only if the job couldn't be enqueued)
```

| Change | Made by | Guard |
|---|---|---|
| new → `queued` | API, on submit | Advisory lock; per-user and global caps (§4.3) |
| `queued` → `running` | Worker, when it claims the job | `WHERE status='queued'` |
| `queued` or `running` → `cancelled` | API, on Cancel | `WHERE status IN ('queued','running')` |
| `running` → `done` | Worker | `WHERE status='running'`; if refused, the image is deleted |
| `running` → `failed` | Worker (error, time limit) or controller (worker lost) | `WHERE status='running'` |

Every change is a single guarded `UPDATE`. So when two parties race (the user cancels while the last step finishes, or the sweeper gives up on a task just as its worker finishes), the first write wins cleanly, and nothing ever overwrites `done`, `failed` or `cancelled`.

### 6.2 Live status, before and during a job

The controller recomputes the status every 5 seconds and publishes it:

```python
# controller/status.py
def publish_status():
    spu = seconds_per_cost_unit()
    live.publish("system", {
        "running": db.count_tasks(status="running"),
        "waiting": db.count_tasks(status="queued"),
        "new_image_ready_in_s": estimate_wait_seconds() + cost(512) * spu,
    })
    for t in db.active_tasks():                       # each user receives only their own task
        live.publish(f"user:{t.user_id}", task_update(t, spu))   # status, position, ready_in_s (§4.6)
    live.publish("admin", admin_overview())          # §4.8
```

Counts always come from Postgres, never from a counter kept in Redis, so a crashed worker can't leave them wrong. With the partial index from §4.3 these queries are trivial.

What the user sees:

- **After signing in:** "4 generating · 3 waiting · about 14 min for a new image".
- **While waiting:** "Position 2 of 3 · ready in about 11 min".
- **While running:** the percentage and "about 4 min left".

### 6.3 Cancelling

```python
@app.delete("/api/tasks/{task_id}")
def cancel_own_task(task_id: UUID, user=Depends(current_user)):
    return cancel(task_id, owner_id=user.id)

@app.delete("/api/admin/tasks/{task_id}")
def cancel_any_task(task_id: UUID, admin=Depends(require_admin)):
    return cancel(task_id, owner_id=None)

def cancel(task_id, owner_id):
    row = db.execute("""UPDATE tasks SET status='cancelled', finished_at=now()
                        WHERE id=:id AND status IN ('queued','running')
                          AND (CAST(:owner AS uuid) IS NULL OR user_id=:owner)
                        RETURNING user_id""", id=task_id, owner=owner_id)
    if row is None:
        task = db.get_task(task_id, user_id=owner_id)
        if task is None:
            raise HTTPException(404)                  # another user's task looks like a missing one
        return {"status": task.status}               # already finished: nothing to do, safe to click twice
    live.request_cancel(str(task_id), ttl=config.CANCEL_FLAG_TTL_S)   # a running worker stops at its next check
    celery_app.control.revoke(str(task_id))          # a worker that receives the queued message drops it
    live.publish_task_final(row.user_id, task_id)
    return {"status": "cancelled"}
```

How quickly a running job stops: the worker checks the flag after every step, and every half second during `STEP_DELAY_SECONDS` (§4.2), so usually within a few seconds and at most one step's computation.

A cancelled job that is still queued never runs, in one of two ways. A worker that heard the revoke drops the message. A worker that started after the revoke and never heard it claims the job, sees the status is no longer `queued`, and returns.

### 6.4 What "cleaned up" means

After a cancel, the system is as if the task had never run:

- The task stays `cancelled`; the guards stop anything overwriting it.
- No image is kept. If the job finished during the cancel, the finish guard refuses and the worker deletes the image (§4.2).
- The worker's RAM is released (`gc.collect()`), so it doesn't grow from job to job.
- The worker reports `idle` and takes the next job straight away.
- The cancel flag and the progress hash are deleted.
- The user's own view updates at once; everyone's counters update within 5 seconds.
- If the worker hasn't stopped after 60 seconds (a step stuck in native code), the controller kills its process (§6.5).

### 6.5 The sweeper and the forced stop

Part of the controller, run every 5 seconds:

```python
# controller/sweeper.py
def sweep(now):
    hbs = live.worker_heartbeats()
    working_on = {hb.task_id for hb in hbs if hb.task_id}

    # 1. Running tasks that no worker is working on: the container died, or its process was
    #    killed by the hard time limit. The guard makes this safe even if a worker is just finishing.
    for t in db.running_tasks(started_before=now - 60):
        if t.id not in working_on:
            if db.finish_task(t.id, "failed", error="the worker stopped unexpectedly"):
                live.publish_task_final(t.user_id, t.id)

    # 2. A worker still busy with a task cancelled more than 60 s ago: kill that worker process.
    #    Celery's pool starts a fresh process, which reloads the model.
    for hb in hbs:
        if hb.task_id:
            t = db.get_task(hb.task_id)
            if t.status == "cancelled" and now - t.finished_at.timestamp() > 60:
                celery_app.control.revoke(hb.task_id, terminate=True)

    # 3. The controller just started, or Redis restarted (its run_id changed): queued messages
    #    may be gone, so send them again. Duplicates are harmless; only one claim can succeed.
    if live.redis_run_id() != state.redis_run_id:
        for t in db.queued_tasks():
            enqueue(t)
        state.redis_run_id = live.redis_run_id()

    # 4. Every 10 minutes: delete each user's finished tasks beyond MAX_HISTORY_PER_USER,
    #    with their images.
    if now - state.last_cleanup > 600:
        for t in db.tasks_beyond_history_limit(config.MAX_HISTORY_PER_USER):
            if t.result_key:
                storage.delete(t.result_key)
            db.delete_task(t.id)
        state.last_cleanup = now
```

The controller's main loop:

```python
# controller/main.py — one instance, on machine A
state = ControllerState(redis_run_id=None, last_scale_at=0.0, last_cleanup=0.0)

while True:
    now = time.time()
    sweep(now)              # §6.5
    autoscale_tick(now)     # §4.7
    publish_status()        # §6.2
    time.sleep(5)
```

The controller is a single process. If it crashes, Swarm restarts it within seconds. While it is down, jobs keep running and finishing; only the live counters, autoscaling and cleanup pause.

---

## 7. Capacity and bottlenecks

### 7.1 How many workers fit

Throughput is the number of workers divided by the time per job. Jobs take minutes, so the number of workers is the only real lever, and RAM decides it:

```
workers per machine = min( floor((machine RAM − RAM for OS and API) / RAM per worker),
                           floor((CPU cores − 1) / WORKER_THREADS) )
MAX_WORKERS         = sum over all app machines
```

- *RAM per worker* is the measured peak during a 768 px job with attention slicing, plus about 1 GB of margin. The same number goes into the stack file's memory reservation (§5.2).
- One core per app machine stays free for its API server, so pages and WebSockets stay responsive while workers compute.

An example with assumed hardware (two app machines with 16 GB RAM and 8 cores, 7 GB per worker):

- RAM allows (16 − 2) / 7 = 2 workers per machine, CPU allows (8 − 1) / 2 = 3, so 2 per machine and `MAX_WORKERS = 4`.
- At 8 minutes per default job, that is 4 × 60 / 8 = 30 images an hour.
- If 20 users submit default jobs at the same moment, the last starts after (20 − 4) / 4 × 8 = 32 minutes and is ready after about 40.

These numbers only illustrate the method; set yours after running `scripts/calibrate.py`.

### 7.2 Bottlenecks and how the design handles them

| Bottleneck | What would go wrong | How it's handled |
|---|---|---|
| RAM per worker | Too many workers on a machine: the kernel kills one, which looks like random crashes | Attention slicing; a measured memory reservation per worker; `MAX_WORKERS` from §7.1 |
| CPU oversubscription | Each torch process uses every core by default; workers and the API compete and everything slows | `WORKER_THREADS` plus `OMP_NUM_THREADS` and `MKL_NUM_THREADS`; a CPU limit per container; one core left for the API |
| Slow scale-up | A new worker needs 20–60 s, or minutes on a machine without the image | The image is pulled on every app machine in advance; the autoscaler waits instead of stacking launches |
| Long jobs against infrastructure timeouts | Jobs killed early, run twice, or cut off on scale-down | The timeout chain (§4.1) |
| Postgres connections | The default limit is 100 | 2 API servers × 4 processes × pool of 5 = 40, plus one per worker and the controller |
| Redis connections | One subscription per browser would grow with the number of users | One subscription per API process (§3.2) |
| Machine A | Nginx, Redis, Postgres, MinIO and the controller all run there | An accepted single point of failure (§7.3) |

### 7.3 If machine A fails

- Everything stops: users can't reach the site, and workers can't fetch jobs or save results.
- Jobs that were running fail; the sweeper marks them failed once machine A is back.
- When it comes back, Postgres and MinIO keep their volumes, Redis reloads its queue from the AOF file, and the controller re-sends queued jobs (§6.5). Users with queued jobs keep their place.

For a production system, the next steps would be a second Nginx with a floating IP, a Postgres replica and Redis Sentinel. The assignment doesn't need them.

### 7.4 Not bottlenecks

The API servers, Nginx, and the load on Postgres and Redis. Each job causes only a handful of requests and a few dozen small Redis writes spread over several minutes.

---

## 8. Security notes

- Prompts are validated on the server: length, emptiness, control characters (§4.1).
- Every task query filters by `user_id`; another user's task answers `404`, so its existence isn't revealed.
- Admin endpoints and the admin WebSocket channel check the role (§4.4).
- Images are served only through the API after an ownership check; MinIO isn't reachable from outside (§3.1).
- Login is rate-limited in Nginx; task creation is limited by the per-user and global caps.
- Secrets (the JWT signing key, database, Redis and MinIO passwords) come from environment variables or Swarm secrets, never from git; `gitleaks` in pre-commit blocks accidental commits (§11).
- Docker images and dependencies are scanned for known vulnerabilities in CI (§11).

---

## 9. Testing strategy

### 9.1 Layers

| Layer | What runs | Typical time |
|---|---|---|
| Unit | Pure functions: validation, cost and wait estimate, autoscaler decisions, timeout order | Seconds |
| API | FastAPI test client against real Postgres and Redis (testcontainers); no worker | Seconds |
| Worker integration | Real Redis, Postgres and MinIO plus real worker containers with `PIPELINE=fake` (1 s per step, no model) | A few minutes |
| End-to-end | Playwright in a browser against the full stack, fake pipeline | Minutes |
| Load, chaos and calibration | The real cluster; the real model only for calibration | Nightly, and before each release |

Celery's eager mode isn't used for anything involving claim, finish, cancel or revoke. In eager mode the task runs inside the POST request, so the job is already over before a cancel could arrive, and the broker, prefetch and revoke behaviour is skipped entirely. Those are exactly the parts that need testing, so they run against real worker containers.

### 9.2 Unit tests

```python
@pytest.mark.parametrize("size", ALLOWED_SIZES)
def test_every_allowed_size_accepted(size):
    validate(SubmitBody(prompt="a cat", size=size))

@pytest.mark.parametrize("size", [0, 100, 500, 1024, -1])
def test_other_sizes_rejected(size):
    with pytest.raises(HTTPException) as exc:
        validate(SubmitBody(prompt="a cat", size=size))
    assert exc.value.status_code == 422

@pytest.mark.parametrize("prompt", ["", "   ", "x" * (MAX_PROMPT_LEN + 1), "a\x00cat"])
def test_bad_prompts_rejected(prompt):
    with pytest.raises(HTTPException):
        validate(SubmitBody(prompt=prompt, size=512))

def test_prompt_at_length_limit_accepted():
    validate(SubmitBody(prompt="x" * MAX_PROMPT_LEN, size=512))

def test_timeouts_are_in_order():
    assert config.MEASURED_MAX_RUNTIME_S < config.TASK_SOFT_TIME_LIMIT < config.TASK_TIME_LIMIT
    assert config.TASK_TIME_LIMIT < config.BROKER_VISIBILITY_TIMEOUT
    assert config.TASK_TIME_LIMIT < config.WORKER_STOP_GRACE_S
    grace = stack_yaml()["services"]["worker"]["stop_grace_period"]
    assert parse_duration(grace) >= config.WORKER_STOP_GRACE_S      # stack file matches config

def test_wait_estimate_simulates_two_workers(fake_state):
    # 8 min per default job; one worker is halfway through a 512 px job, the other is idle;
    # waiting: one 512 px job, then one 768 px job.
    fake_state(workers=2, spu=480, running=[(512, 50)], queued=[512, 768])
    # Idle worker takes the 512 job (free at 480 s); the busy one frees at 240 s and takes
    # the 768 job (free at 240 + 1080 = 1320 s). A new task starts when the first is free.
    assert estimate_wait_seconds() == 480
```

Autoscaler decisions, with Swarm and the heartbeats mocked:

```python
def test_scales_up_when_queue_builds(swarm, hb):
    swarm.replicas = 1; hb.ready = 1; queued(3)
    autoscale_tick(now=1000)
    assert swarm.replicas == 2

def test_no_second_launch_while_one_is_loading(swarm, hb):
    swarm.replicas = 2; hb.ready = 1; queued(10)                 # one still loading
    autoscale_tick(now=1000)
    assert swarm.replicas == 2

def test_gives_up_on_a_worker_that_cannot_be_placed(swarm, hb):
    swarm.replicas = 3; hb.ready = 2; state.last_scale_at = 0
    autoscale_tick(now=STARTUP_TIMEOUT_S + 1)
    assert swarm.replicas == 2 and alert_raised()

def test_respects_cooldown(swarm, hb):
    swarm.replicas = 1; hb.ready = 1; queued(10); state.last_scale_at = 1000
    autoscale_tick(now=1000 + COOLDOWN_S - 1)
    assert swarm.replicas == 1

def test_respects_max(swarm, hb):
    swarm.replicas = MAX_WORKERS; hb.ready = MAX_WORKERS; queued(999)
    autoscale_tick(now=10_000)
    assert swarm.replicas == MAX_WORKERS

def test_scales_down_only_when_nothing_waits_and_a_worker_idles(swarm, hb):
    swarm.replicas = 3; hb.ready = 3; queued(0); hb.idle_for = SCALE_DOWN_IDLE_S + 1
    autoscale_tick(now=10_000)
    assert swarm.replicas == 2
```

### 9.3 API tests

- No cookie → `401`; another user's task → `404`; admin endpoints as a normal user → `403`.
- A second submit while one task is active → `429`.
- With `MAX_QUEUED_TOTAL` tasks already queued → `503` with a `Retry-After` header.
- The same `Idempotency-Key` twice → the same `task_id`, one row.
- Refresh: the old refresh token is rejected after it has been used once; after logout, refresh fails.
- Ten simultaneous submits for one user, split across two API instances → exactly one `202`:

```python
def test_concurrent_submits_across_instances_respect_the_cap(api_a, api_b, user):
    def submit(i):
        api = api_a if i % 2 else api_b
        return api.post("/api/tasks", json={"prompt": "a cat", "size": 256},
                        headers={"Idempotency-Key": str(uuid4())}, cookies=user.cookies)
    with ThreadPoolExecutor(10) as pool:
        codes = [r.status_code for r in pool.map(submit, range(10))]
    assert codes.count(202) == 1
```

### 9.4 Worker integration tests (fake pipeline)

The `stack` fixture starts Postgres, Redis, MinIO, the controller (autoscaler off) and two worker containers with `PIPELINE=fake` and `FAKE_STEP_SECONDS=1`.

```python
def test_full_lifecycle(stack, client):
    t = client.submit("a cat", size=256)
    stack.wait_status(t, "done")
    assert client.image(t).status_code == 200

def test_mid_run_cancel_cleans_up(stack, client):
    t = client.submit("a cat", size=256)
    stack.wait_progress(t, at_least=20)
    client.cancel(t)
    stack.wait_worker_idle()
    assert client.task(t)["status"] == "cancelled"
    assert not stack.storage.exists(f"results/{t}.png")
    assert not stack.redis.exists(f"cancel:{t}", f"progress:{t}")

def test_cancel_during_last_step_stays_cancelled(stack, client):
    t = client.submit("a cat", size=256)
    stack.wait_progress(t, at_least=85)
    client.cancel(t)
    stack.wait_worker_idle()
    assert client.task(t)["status"] == "cancelled"
    assert not stack.storage.exists(f"results/{t}.png")

def test_task_cancelled_while_queued_never_runs_on_a_new_worker(stack, client):
    stack.scale_workers(0)                       # nothing consumes yet
    t = client.submit("a cat", size=256)
    client.cancel(t)
    stack.scale_workers(1)                       # a worker that never heard the revoke
    stack.wait_worker_idle()
    assert stack.db.task(t).started_at is None

def test_soft_time_limit_marks_failed(stack, client):
    stack.restart_workers(env={"TASK_SOFT_TIME_LIMIT": "3", "TASK_TIME_LIMIT": "10"})
    t = client.submit("a cat", size=768)
    stack.wait_status(t, "failed")
    assert "time limit" in client.task(t)["error_message"]

def test_killed_worker_task_is_marked_failed(stack, client):
    t = client.submit("a cat", size=256)
    stack.wait_status(t, "running")
    stack.kill_worker_running(t)                 # SIGKILL: no finally block runs
    stack.wait_status(t, "failed", timeout=90)   # the sweeper notices

def test_stopping_a_busy_worker_lets_its_job_finish(stack, client):
    t = client.submit("a cat", size=256)
    stack.wait_status(t, "running")
    stack.stop_worker_running(t)                 # SIGTERM with the grace period, as on scale-down
    stack.wait_status(t, "done")

def test_tasks_spread_across_workers(stack, clients):
    ids = [c.submit("a cat", size=256) for c in clients[:6]]     # six users, one task each
    stack.wait_all(ids, "done")
    assert len({stack.db.task(t).worker_host for t in ids}) == 2

def test_redis_restart_does_not_lose_queued_tasks(stack, clients):
    ids = [c.submit("a cat", size=256) for c in clients[:4]]
    stack.restart_redis(keep_data=False)         # worst case: queue gone
    stack.wait_all(ids, "done", timeout=180)     # the controller re-sent them
```

### 9.5 Load-balancer tests

- **Login on one API server, use the cookie on the other** → `200`. This proves no state is kept per server.
- **Distribution:** over 100 requests through Nginx, both API servers' request counters rise.
- **An API server dies:** new requests keep succeeding; a WebSocket on the dead server reconnects and receives a snapshot; a POST retried with the same `Idempotency-Key` doesn't create a duplicate.
- **A queued user's WebSocket survives:** open it through Nginx with no workers running, wait 90 seconds (longer than Nginx's default 60 s), and check the socket is still open and has received pings and `system` messages.

### 9.6 End-to-end tests (Playwright)

- Sign up, sign in, see the status line with the generating and waiting counts.
- Submit a prompt: see the queue position and ready time, then the percentage and time left, then the image.
- Cancel while queued and while running: the task shows as cancelled in history.
- Submit an empty prompt: see the error line, and no request is sent.
- Fill the global queue: see the "queue is full" error with a retry time.
- Reload the page during a job: the job view comes back.
- Open a history entry: its image shows.
- Log out: the page returns to sign-in, and the old cookie no longer works.
- As an admin: the admin page lists machines, workers and tasks, and its Cancel button works.

### 9.7 Load, chaos and calibration (real cluster)

- **Calibration** (before each release): run a 768 px job with the real model on each app machine; fail if it takes longer than `TASK_SOFT_TIME_LIMIT`, and update `MEASURED_MAX_RUNTIME_S` and `CALIBRATED_SECONDS_PER_COST_UNIT`.
- **Load** (Locust): many users submitting; the shown ready times stay within about 25% of the actual ones; past the global cap, users get `503` with a retry time, never errors or timeouts.
- **Autoscaling:** a burst of jobs raises the worker count to `MAX_WORKERS` on several machines and no further; when the queue empties, it falls back to `MIN_WORKERS` without failing a job.
- **Chaos:** kill a worker container, an API container and the controller in turn; the system recovers each time without losing queued jobs.

### 9.8 Requirement → test coverage

| Requirement | Proven by |
|---|---|
| 1. Complexity limits | Size and prompt unit tests (§9.2), soft time limit test (§9.4), calibration (§9.7) |
| 2. Progress | Lifecycle and cancel tests watching progress (§9.4); E2E progress view (§9.6) |
| 3. History, status, cancel, new task, limits | Cancel tests (§9.4), cap and idempotency tests (§9.3), E2E history and cancel (§9.6) |
| 4. Auth + HTTPS | Auth and refresh tests (§9.3), cross-server cookie test (§9.5), HTTP→HTTPS redirect smoke test |
| 5. Load balancing on ≥2 servers | Distribution and failover tests (§9.5), tasks spread across workers (§9.4) |
| 6. Queue notice and wait time | Estimate unit test (§9.2), load test comparing shown and actual times (§9.7) |
| 7. Autoscaling with a limit | Autoscaler unit tests (§9.2), busy-worker stop test (§9.4), cluster autoscaling run (§9.7) |
| 8. Admin panel | E2E admin test (§9.6); overview numbers match direct DB and heartbeat queries |
| Safe cancellation | All cancel tests in §9.4 |
| Several machines | Spread-across-workers test (§9.4), cluster runs (§9.7) |

---

## 10. Git workflow and branching strategy

### 10.1 Repository layout

A monorepo suits this project well — the API, worker, controller and infra config tend to change together for most features:

```
/api         FastAPI app: auth, tasks, admin, WebSocket
/worker      Celery app, generation task, real and fake pipelines
/controller  autoscaler, sweeper, status publisher (§4.7, §6)
/common      config, db, live (Redis), storage, estimate
/frontend    user page and admin page
/infra       nginx.conf, stack.yml (§5.2), test stack, Dockerfiles
/scripts     calibrate.py (§4.1)
/tests       unit, api, worker integration, e2e, load (§9)
/docs        this architecture doc
```

### 10.2 Why a trimmed-down Git Flow

Full Git Flow (a release branch cut for every change) is built for long-running products with continuous releases. For a project with a small team working toward graded milestones, the useful parts are `main` + `develop` + short-lived `feature/*` branches for everyday work, with `release/*` and `hotfix/*` reserved for the two situations that actually need them: preparing for a milestone, and fixing something critical after one.

### 10.3 Branches

- **`main`** — always the state that's been demoed or graded. Protected: no direct pushes, only merges from `release/*` or `hotfix/*`.
- **`develop`** — the integration branch. Protected: requires a green CI run (§9) and at least one review before merge.
- **`feature/<area>-<short-desc>`** — branched from `develop` for a single piece of work, e.g. `feature/api-task-cancellation`, `feature/worker-progress-callback`, `feature/infra-nginx-lb`, `feature/tests-race-condition`. Merged back via PR, then deleted.
- **`release/<version>`** — cut from `develop` right before a milestone. Only bugfixes land here — no new features. Run the full test suite from §9, including load, chaos and calibration (§9.7), before merging.
- **`hotfix/<short-desc>`** — cut from `main` for a critical issue found after a milestone (e.g. a crash discovered right before a demo).

### 10.4 Commit convention

[Conventional Commits](https://www.conventionalcommits.org/): `feat:`, `fix:`, `test:`, `docs:`, `chore:`, `refactor:`, `perf:`, optionally scoped like `feat(api):` or `fix(worker):`. Makes it trivial to scan history for everything that touched, say, the cancellation logic or the autoscaler, and enables an auto-generated changelog if you want one.

### 10.5 CI tied to each branch stage

- Push to any `feature/*` branch → lint + unit tests (§9.2) — fast feedback.
- PR into `develop` → unit, API and worker-integration tests (§9.2–9.4) must pass before merge is allowed. They use the fake pipeline, so CI never downloads model weights.
- Merge into `develop` → auto-deploy to a shared staging stack (`docker compose up` on a shared VM) for manual click-through; optionally run the Playwright E2E suite (§9.6) against it.
- Merge into `main` (a release or hotfix) → tag the commit `vX.Y.Z`, build and push the `api`/`worker`/`controller`/`nginx` Docker images with that tag, and redeploy the multi-machine cluster from §5 with the new images.

### 10.6 Everyday feature work

```bash
git checkout develop && git pull
git checkout -b feature/api-task-cancellation

# ... work, committing as you go ...
git commit -m "feat(api): add cooperative cancellation endpoint"
git commit -m "test(api): cover cancel race condition"

# open a PR into develop, get it reviewed, wait for CI, then squash-merge
```

### 10.7 Cutting a release before a milestone

```bash
git checkout develop && git pull
git checkout -b release/1.2.0

# bugfixes only on this branch, then:
git checkout main
git merge --no-ff release/1.2.0
git tag -a v1.2.0 -m "Milestone 2 submission"

git checkout develop
git merge --no-ff release/1.2.0
git branch -d release/1.2.0
```

### 10.8 Emergency hotfix after a milestone

```bash
git checkout main
git checkout -b hotfix/fix-worker-crash-on-cancel

# fix, commit, then:
git checkout main
git merge --no-ff hotfix/fix-worker-crash-on-cancel
git tag -a v1.2.1 -m "Hotfix: worker crash on cancel"

git checkout develop
git merge --no-ff hotfix/fix-worker-crash-on-cancel
git branch -d hotfix/fix-worker-crash-on-cancel
```

## 11. Automation recommendations

Split by cost/value given a course project's time constraints.

### 11.1 Do these — cheap, high value

- **Pre-commit hooks** via the `pre-commit` framework: Black/Ruff (Python), ESLint/Prettier (frontend), and `gitleaks` or `detect-secrets` to block an accidental credential from ever reaching a commit. Runs locally before code even reaches CI.
- **Dependabot or Renovate** — one config file gets automatic PRs whenever a dependency (Python, npm, or a Docker base image) has a security fix or new version.
- **Stuck-task sweeper** — no longer optional: it is part of the controller and runs every 5 seconds (§6.5).
- **Auto-delete branches on merge** and **CODEOWNERS-based review assignment** — both a repo setting, not a script.

### 11.2 High value, moderate effort

- **Docker image + dependency vulnerability scanning in CI** (Trivy or Grype) — fail the build on a critical CVE rather than discovering it in production.
- **Automated staging deploy + smoke test on merge to `develop`** — after the automatic staging deploy from §10.5, hit `/health` and submit one trivial generation automatically; a failure here is the first signal a merge broke something, well before manual QA would notice.
- **Automated Postgres backups** — nightly `pg_dump` or a managed snapshot, plus a periodic automated restore-to-scratch so backups are proven to actually work.
- **Alerting rules** (Prometheus Alertmanager, or even just a Slack webhook) on: queue depth staying high for too long, the autoscaler pinned at `MAX_WORKERS` for a sustained period, a worker that could not be placed (§4.7), and worker crash-loops. The admin panel already shows these; alerting sends them somewhere you'll see them.

### 11.3 Nice polish if time remains

- **semantic-release / git-cliff** off the Conventional Commits from §10.4 — auto-bumps the version, writes `CHANGELOG.md`, and cuts the GitHub Release, removing the manual "what changed this milestone" writeup.
- **Automated rollback** — if the post-deploy smoke test fails, automatically redeploy the previous image tag instead of leaving staging broken.
- **Calibration before each release** — run `scripts/calibrate.py` on the cluster and stop the release if a 768 px job no longer fits under `TASK_SOFT_TIME_LIMIT` (§4.1, §9.7). Model weights are already baked into the worker image (§4.2), and images are pulled in advance on every app machine (§5.1).

---

## 12. Summary: what satisfies each requirement

| # | Requirement | Where it's handled |
|---|---|---|
| 1 | Limit on the complexity of one task | Fixed size list, fixed steps, prompt rules, and the measured time-limit chain (§4.1) |
| 1b | CPU only, deliberately slow | CPU build of torch, few threads per worker, `STEP_DELAY_SECONDS` (§1) |
| 1c | Adjustable picture size | `size` parameter, 256–768 px (§1.3, §4.1) |
| 2 | Progress reporting | The worker publishes % and stage after each step (§4.2); WebSocket delivery (§3.2) |
| 2b | Live "N generating, M waiting" and times | Controller status publisher (§6.2) and queue simulation (§4.6) |
| 3 | History, status, cancel, new task, task limits, database | Data model, three caps, submit and image endpoints (§4.3); cancellation (§6.3) |
| 3b | Safe cancellation with full cleanup | Claim and finish guards (§4.2), cancel flag and revoke (§6.3), cleanup list (§6.4), sweeper and forced stop (§6.5) |
| 4 | Authorisation and HTTPS | Cookie JWT with refresh rotation, admin role, TLS at Nginx (§4.4) |
| 5 | Load balancing across at least two servers | Nginx over the API servers on B and C; the queue over the workers on B and C (§4.5, §5) |
| 6 | Queue notice and waiting time | Queued status, position and simulated ready time; `503` with retry time when full (§4.6, §4.3) |
| 7 | Automatic horizontal scaling with a limit | Controller and Swarm, with cooldown, placement timeout and `MAX_WORKERS` (§4.7, §5.2, §7.1) |
| 8 | Admin panel | Machines, workers, tasks and autoscaler from heartbeats and the DB (§4.8) |
| + | Several machines | Swarm cluster and stack file (§5) |
| + | Capacity and bottlenecks | Sizing rule and bottleneck table (§7) |

---

## 13. Third-party tools

| Category | Tool | Used for | Section |
|---|---|---|---|
| Frontend | React or Vue | User page and admin page | §2 |
| API framework | FastAPI + Uvicorn | API servers and WebSockets | §2, §3 |
| Request validation | Pydantic | Request bodies | §4.1 |
| ML inference | PyTorch (CPU build) | Running the model | §1.1 |
| Diffusion pipeline | Hugging Face `diffusers` | Stable Diffusion 1.5 | §4.2 |
| Images | Pillow | PNG encoding, fake pipeline | §4.2 |
| Task runner | Celery | Workers, time limits, revoke | §4.2 |
| Broker and live state | Redis (AOF on) | Queue, progress, cancel flags, heartbeats, pub/sub | §2 |
| Database | PostgreSQL | Users, refresh tokens, tasks | §4.3 |
| Object storage | MinIO | Result images | §2 |
| Password hashing | argon2 (`argon2-cffi`) | Stored passwords | §4.4 |
| Tokens | JWT (`PyJWT`) | Access tokens | §4.4 |
| Load balancer and TLS | Nginx | Balancing, TLS, static files, login rate limit | §4.5 |
| Certificates | Let's Encrypt / Certbot, or `mkcert` for a local demo | HTTPS | §4.4 |
| Containers | Docker | Packaging every service | §5 |
| Orchestration | Docker Swarm | Multi-machine placement, restarts, scaling | §5 |
| Docker from Python | Docker SDK for Python | The controller changes the worker count | §4.7 |
| Host metrics | `psutil` | CPU and RAM in heartbeats | §4.8 |
| Metrics history (optional) | Prometheus + Grafana | Graphs over days | §4.8 |
| Tests | pytest, testcontainers, Playwright, Locust | All test layers | §9 |
| CI and repository | GitHub (Actions, Dependabot, CODEOWNERS, Releases) | CI pipeline, PR workflow | §10 |
| Code quality | `pre-commit`, Ruff, ESLint, Prettier, `gitleaks` | Formatting, linting, secret scanning | §11 |
| Vulnerability scanning | Trivy | Images and dependencies | §11 |
| Release notes | git-cliff | Changelog and version tags | §11 |
| Backups | `pg_dump` | Postgres backups | §11 |

Alternatives that fit the same design: RabbitMQ instead of Redis as the broker (raise its `consumer_timeout`, §4.1), HAProxy instead of Nginx, Kubernetes or K3s with KEDA instead of Swarm and the controller's autoscaler, Cypress instead of Playwright, k6 instead of Locust.
