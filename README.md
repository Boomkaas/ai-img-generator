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

Optional: install [pre-commit](https://pre-commit.com) (`pipx install pre-commit` or `uv tool install pre-commit`)
and run `pre-commit install` so ruff and gitleaks run before every commit.

If a host port is already taken (for example by a local Postgres), override it:
`POSTGRES_PORT=5433 make dev-up`, and use the same port in `DATABASE_URL` in `.env`. `REDIS_PORT`,
`MINIO_PORT` and `MINIO_CONSOLE_PORT` work the same way.

`make help` lists every target.

## Where the design lives

- `docs/architecture.md`: the full design, and the source of truth.
- `docs/decisions/`: Architecture Decision Records for every deviation from it.
- `CLAUDE.md`: project conventions in short form.

## Repository settings

Set these by hand on GitHub (Settings → Branches / General):

- Protect `main` and `develop`: no direct pushes, changes only through pull requests.
- Require the CI status checks `lint` and `unit` to pass before merging (more jobs are added in later steps).
- Require at least one approving review.
- Optional, per the architecture (§11.1): enable "Automatically delete head branches".
