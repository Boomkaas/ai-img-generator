# imagegen

A web app that turns text prompts into images with Stable Diffusion 1.5 (PyTorch + `diffusers`), on CPU only
and deliberately slowly. Speed isn't the goal. A generation takes minutes, so the project can demonstrate and
test queueing, live progress, safe cancellation, load balancing across several machines, and horizontal
autoscaling of workers with a limit.

## Quick start

Requires Python 3.12, Docker with Compose v2, and `make`.

```bash
cp .env.example .env
make dev-up        # Postgres, Redis and MinIO (bucket "imagegen"), all healthy
make install       # .venv with every extra except torch
make lint
make test-unit
```

If a host port is already taken (for example by a local Postgres), override it:
`POSTGRES_PORT=5433 make dev-up`, and use the same port in `DATABASE_URL` in `.env`. `REDIS_PORT`,
`MINIO_PORT` and `MINIO_CONSOLE_PORT` work the same way.

`make help` lists every target.

## Tests

| Command | What runs | Needs |
|---|---|---|
| `make test-unit` | pure functions | nothing |
| `make test-api` | the API in-process against Postgres, Redis and MinIO from testcontainers | Docker |
| `make test-integration` | real worker containers with the fake pipeline: progress, cancel, crashes, Redis restart | Docker |
| `make test-lb` | two API servers behind Nginx on `https://localhost:8443`: cookies, spreading, failover, WebSockets | Docker |
| `make test-e2e` | the user and admin pages in Chromium, at desktop width and at 360 px | Docker, `make browsers` |

The last three build the images and start `infra/compose.test.yml` (project `imagegen-test`) themselves, and
remove it afterwards. It publishes ports 8001, 8002, 8443, 8080, 5442, 6389 and 9010, which must be free.
`STACK_KEEP=1` leaves the stack running, so the next run starts in seconds; `docker compose -p imagegen-test
down -v` removes it. After a failure, `test-results/compose.log` holds every container's log and
`test-results/e2e/` the Playwright trace of each failed browser test (`.venv/bin/playwright show-trace <file>`).
Where Playwright's own Chromium can't run, point `CHROMIUM_PATH` at an installed one.

The tests that need a real cluster run by hand: `tests/load/locustfile.py` (shown against actual waiting
times), `scripts/chaos.sh` (killing a worker, an API server and the controller) and `scripts/autoscale_check.sh`
(a burst up to `MAX_WORKERS` and back). The top of each file says how to run it.

## Frontend

Requires Node 22. With `make run-api`, `make run-worker` and `make run-controller` running:

```bash
cd frontend
npm ci
npm run dev        # http://localhost:5173, proxies /api and /ws to the API on port 8000
npm run lint && npm run typecheck && npm test && npm run build
```

`ALLOWED_ORIGINS` in `.env` must include `http://localhost:5173`, or the server closes the WebSocket.

Open the page as `localhost` or over HTTPS. Submitting creates its idempotency key with
`crypto.randomUUID()`, which browsers only provide in a secure context, so `http://<LAN address>:5173` can sign
in but can't generate.

## Running the real model locally

`make run-worker` uses the fake pipeline, which sleeps instead of computing. To generate real images, install
the CPU build of PyTorch and the Stable Diffusion libraries into the same environment:

```bash
.venv/bin/pip install torch --index-url https://download.pytorch.org/whl/cpu
.venv/bin/pip install diffusers transformers accelerate safetensors
```

Download a Stable Diffusion 1.5 checkpoint in `diffusers` format (for example
`stable-diffusion-v1-5/stable-diffusion-v1-5` from Hugging Face) into a directory, point `MODEL_DIR` at it,
and start the worker with `PIPELINE=real`:

```bash
.venv/bin/huggingface-cli download stable-diffusion-v1-5/stable-diffusion-v1-5 --local-dir ./models/sd15
MODEL_DIR=./models/sd15 PIPELINE=real .venv/bin/celery -A worker.app:celery_app worker \
    --pool=prefork --concurrency=1 -Q image_generation --loglevel=info
```

Loading takes 20–60 s, and one 512 px image takes several minutes with `WORKER_THREADS=2`.

## Running on a cluster

The system runs on Docker Swarm: machine A (label `imagegen.infra=true`) holds Nginx, Redis, Postgres, MinIO
and the controller; the app machines (label `imagegen.app=true`) run the API servers and workers.

```bash
make images TAG=1.0.0 REGISTRY=<registry>             # api, controller, nginx, fake worker
make image-worker-real TAG=1.0.0 REGISTRY=<registry>  # torch and the model weights, several GB
```

- `infra/docker/` holds the Dockerfiles, `infra/nginx/` the load balancer config, `infra/stack.yml` the stack.
- `scripts/swarm/` creates the cluster (`init-manager.sh`, `label-app-node.sh`, `prepull.sh`, and
  `create-secrets.sh`, which stores the certificate and every password as Swarm secrets) and deploys it (`deploy.sh <tag>`, which reads `infra/.env.prod`; start from
  `infra/.env.prod.example`). `scripts/swarm/add-machine.md` covers growing the cluster.
- `infra/stack.dev-swarm.yml` is an override for trying the whole stack on one machine with the fake worker:
  `scripts/swarm/deploy.sh <tag> infra/stack.dev-swarm.yml`.

Every timeout and the worker sizing come from `scripts/calibrate.py`, run on each app machine inside the real
worker image:

```bash
docker run --rm --cpus 2 --memory 10g --hostname "$(hostname)" -e STEP_DELAY_SECONDS=2 \
    <registry>/imagegen-worker:<tag> python -m scripts.calibrate
```

It prints `MEASURED_MAX_RUNTIME_S` and `CALIBRATED_SECONDS_PER_COST_UNIT` for `common/config.py`, the worker's
`stop_grace_period` and memory reservation for `infra/stack.yml`, and how many workers fit on the machine
(their sum is `MAX_WORKERS`). Keep the slowest machine's times.

## Repository settings

Set these by hand on GitHub (Settings → Branches / General):

- Protect `main` and `develop`: no direct pushes, changes only through pull requests.
- Require the CI status checks `lint`, `unit`, `api` and `frontend` to pass before merging into `develop` and
  `main`, and also `integration`, `lb` and `e2e`, which run on pull requests into `develop` and on pushes to
  `develop` and `main`.
- Require at least one approving review.
- Optional: enable "Automatically delete head branches".
