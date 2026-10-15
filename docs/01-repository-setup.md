# Step 01 of 14 — Repository setup

You are building **imagegen**: a web app that turns text prompts into images with PyTorch, on CPU only and deliberately slowly, so that queueing, progress reporting, cancellation, load balancing across machines and autoscaling can be demonstrated and tested. The complete design is in `docs/architecture.md` and is the source of truth for every step. This is the first of fourteen steps. Do only this step.

## Goal

An empty but fully tooled monorepo: directory layout, Python packaging, linting, local development services, a CI pipeline that runs, the Git branches, and a `CLAUDE.md` that later sessions load automatically. No application logic yet.

## Before you start

- Read `docs/architecture.md` in full once. For this step, §2 (components), §10 (Git workflow) and §11.1 (pre-commit) matter most.
- If `docs/architecture.md` is missing, stop and ask me for it.
- If the repository has no Git history, initialise it, make an initial commit on `main` (README stub), create `develop` from `main`, then create `feature/repo-setup` from `develop`.

## Build

### 1. Layout

Create this layout. Python packages get an `__init__.py`; empty directories get a `.gitkeep`.

```
/api          FastAPI app (step 04)
/worker       Celery app, generation task, pipelines (step 06)
/controller   controller loop, status, sweeper, autoscaler (steps 08–09)
/common       config, db, live (Redis), storage, estimate, celery client (steps 02–03)
/frontend     React + Vite + TypeScript, user page and admin page (steps 10–11)
/infra        compose files, Dockerfiles, nginx, Swarm stack (step 12)
/migrations   Alembic migrations (step 02)
/scripts      helper scripts (calibrate, create_admin, deploy …)
/tests        unit/, api/, integration/, lb/, e2e/, load/
/docs         architecture.md, decisions/, runbook.md (later)
```

### 2. Python packaging

One `pyproject.toml` at the root (build backend: hatchling), Python 3.12, packages `api`, `worker`, `controller`, `common`.

- Core dependencies (used everywhere): `pydantic>=2`, `pydantic-settings`, `sqlalchemy>=2`, `psycopg[binary]>=3`, `alembic`, `redis>=5`, `boto3`, `celery[redis]>=5.4`, `psutil`.
- Extra `api`: `fastapi`, `uvicorn[standard]`, `argon2-cffi`, `pyjwt`.
- Extra `worker`: `pillow`. Torch and diffusers are **not** listed here: the worker image installs them from the PyTorch CPU index (step 12), and tests use a fake pipeline that needs neither.
- Extra `controller`: `docker` (Docker SDK for Python).
- Extra `dev`: `pytest`, `pytest-asyncio`, `httpx`, `testcontainers[postgres,redis,minio]`, `ruff`, `pyyaml`, `freezegun`, `playwright`, `locust`.

Ruff: line length 110, rules `E,F,I,B,UP,SIM`, also used as the formatter.

Pytest: `testpaths = ["tests"]` and these markers, so each layer can be run alone:

| Marker | Needs | Added in |
|---|---|---|
| `unit` | nothing external | every step |
| `api` | Postgres, Redis, MinIO started by testcontainers | steps 02–09 |
| `integration` | the compose test stack | step 13 |
| `lb` | the compose test stack with Nginx | step 13 |
| `e2e` | the compose test stack and a browser | step 13 |

### 3. Local development services

`infra/compose.dev.yml` with:

- `postgres:16` on port 5432 (user/password/db `imagegen`, named volume),
- `redis:7` on port 6379 with `--appendonly yes`,
- `minio/minio` on ports 9000/9001 (named volume), plus a one-shot `minio/mc` container that creates the bucket `imagegen`.

Give each service a healthcheck.

### 4. Environment

`.env.example` listing every variable the project will use, with safe development defaults and a one-line comment each:

`DATABASE_URL`, `DB_POOL_SIZE`, `BROKER_URL`, `REDIS_URL`, `S3_ENDPOINT`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_BUCKET`, `JWT_SECRET`, `COOKIE_SECURE`, `ALLOWED_ORIGINS`, `MACHINE_NAME`, `WORKER_NAME`, `PIPELINE`, `FAKE_STEP_SECONDS`, `MODEL_DIR`, `WORKER_THREADS`, `STEP_DELAY_SECONDS`, `NUM_STEPS`, `MEASURED_MAX_RUNTIME_S`, `CALIBRATED_SECONDS_PER_COST_UNIT`, `SCALER`, `WORKER_SERVICE`, `MIN_WORKERS`, `MAX_WORKERS`, `ALERT_WEBHOOK_URL`.

`.gitignore` covers `.env`, virtualenvs, caches, `node_modules`, `dist`, test output.

### 5. Makefile

Targets: `dev-up`, `dev-down`, `install` (editable install with all extras except torch), `lint`, `fmt`, `migrate`, `test-unit`, `test-api`, `test-integration`, `test-lb`, `test-e2e`, `run-api`, `run-worker`, `run-controller`. Targets whose code arrives in a later step print `added in step NN` and exit 0.

### 6. Pre-commit

`.pre-commit-config.yaml` with ruff (lint and format), `gitleaks`, `check-yaml`, `end-of-file-fixer`, `trailing-whitespace`. Frontend hooks are added in step 10.

### 7. CI

`.github/workflows/ci.yml`, on every push and pull request:

- `lint`: ruff check, ruff format --check, gitleaks.
- `unit`: install, `pytest -m unit`.

Later steps add jobs; keep the file easy to extend.

### 8. CLAUDE.md

Write `CLAUDE.md` at the root with exactly this content (fill in nothing else yet):

````markdown
# imagegen — conventions for Claude

## What this is
A web app that generates images from text prompts with Stable Diffusion 1.5 on CPU only, deliberately slowly, to demonstrate queueing, progress, cancellation, load balancing across machines and autoscaling. `docs/architecture.md` is the design and the source of truth.

## Stack (decided; don't swap without an ADR)
- API: FastAPI + Uvicorn. DB access: SQLAlchemy 2 Core (not ORM) + psycopg 3. Migrations: Alembic.
- Queue: Celery 5 with Redis as broker. Live state (progress, cancel flags, heartbeats, pub/sub): the same Redis.
- Storage: MinIO through boto3. Orchestration: Docker Swarm. Load balancer: Nginx.
- Frontend: React + Vite + TypeScript, plain CSS with design tokens, no UI kit.

## Layout
api/ worker/ controller/ common/ frontend/ infra/ migrations/ scripts/ tests/ docs/

## Rules
- Every limit and timeout lives in `common/config.py`. No magic numbers elsewhere.
- Every Redis key and channel name lives in `common/live.py`.
- Every task status change is a single guarded `UPDATE` in `common/db.py` (architecture §6.1). Never update `tasks.status` anywhere else.
- `torch` and `diffusers` are imported only inside the real pipeline in `worker/pipeline.py`. The API and controller never import the `worker` package; they enqueue through `common/celery_client.py`.
- The GUI stays minimal: show only what the assignment and the architecture require.
- No secrets in code or git. Configuration comes from environment variables.
- If you must deviate from `docs/architecture.md`, write an ADR in `docs/decisions/` and update the doc in the same branch.

## Git
- Branches: `main` (graded/demoed), `develop` (integration), `feature/<area>-<desc>`, `release/<x.y.z>`, `hotfix/<desc>`.
- Conventional Commits: `feat(api): …`, `fix(worker): …`, `test(controller): …`, `docs: …`, `chore: …`.
- Merge features into `develop` only with all tests green.

## Commands
make dev-up · make install · make migrate · make lint · make fmt
make test-unit · make test-api · make test-integration · make test-lb · make test-e2e
make run-api · make run-worker · make run-controller

## Tests
- CI never downloads model weights: tests use `PIPELINE=fake`.
- Markers: unit, api, integration, lb, e2e.
````

### 9. Docs and repo hygiene

- `README.md`: one paragraph on what the project is, quick start (`cp .env.example .env`, `make dev-up`, `make install`), where the design lives, and a "Repository settings" section listing what to set by hand on GitHub: protect `main` and `develop`, require the CI checks, require one review.
- `docs/decisions/0001-record-architecture-decisions.md`: a short ADR template and the decision to keep ADRs.
- `tests/unit/test_smoke.py`: one test marked `unit` that imports the four packages, so CI has something green to run.

## Out of scope

Any application code, database schema, Dockerfiles for the services, frontend code.

## Done when

- `make dev-up` starts Postgres, Redis and MinIO, all healthy, and the `imagegen` bucket exists.
- `make install`, `make lint` and `make test-unit` pass.
- `pre-commit run --all-files` passes.
- The CI workflow file is valid YAML and its two jobs would run the same commands.
- `main`, `develop` and `feature/repo-setup` exist; the work is committed with Conventional Commits.

## Finish

Merge `feature/repo-setup` into `develop` (open a pull request if a remote is configured; otherwise `git merge --no-ff`). Then reply with what you built, how you verified it, and anything I must do by hand (for example the GitHub branch protection settings).
