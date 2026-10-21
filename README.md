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

## Frontend

Requires Node 22. With `make run-api`, `make run-worker` and `make run-controller` running:

```bash
cd frontend
npm ci
npm run dev        # http://localhost:5173, proxies /api and /ws to the API on port 8000
npm run lint && npm run typecheck && npm test && npm run build
```

`ALLOWED_ORIGINS` in `.env` must include `http://localhost:5173`, or the server closes the WebSocket.

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

## Repository settings

Set these by hand on GitHub (Settings → Branches / General):

- Protect `main` and `develop`: no direct pushes, changes only through pull requests.
- Require the CI status checks `lint` and `unit` to pass before merging.
- Require at least one approving review.
- Optional: enable "Automatically delete head branches".
