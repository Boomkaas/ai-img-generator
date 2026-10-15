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
