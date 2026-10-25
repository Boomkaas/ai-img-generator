PY_VERSION ?= python3.12
VENV       ?= .venv
BIN        := $(VENV)/bin
COMPOSE_DEV := docker compose -f infra/compose.dev.yml
COMPOSE_TEST := docker compose -p imagegen-test -f infra/compose.test.yml

.DEFAULT_GOAL := help
.PHONY: help dev-up dev-down install lint fmt migrate \
        test-unit test-api test-integration test-lb test-e2e test-images browsers \
        run-api run-worker run-controller images image-worker-real constraints

REGISTRY ?= local
TAG      ?= dev
BUILD    := docker build

help:  ## List targets
	@grep -E '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-18s %s\n", $$1, $$2}'

# --- Local services ---------------------------------------------------------

dev-up:  ## Start Postgres, Redis and MinIO; wait until healthy and the bucket exists
	$(COMPOSE_DEV) up -d --wait postgres redis minio
	$(COMPOSE_DEV) run --rm minio-init

dev-down:  ## Stop the local services (data volumes are kept)
	$(COMPOSE_DEV) down

# --- Images -----------------------------------------------------------------

images:  ## Build the api, controller, nginx and fake worker images as $(REGISTRY)/imagegen-*:$(TAG)
	$(BUILD) -f infra/docker/api.Dockerfile -t $(REGISTRY)/imagegen-api:$(TAG) .
	$(BUILD) -f infra/docker/controller.Dockerfile -t $(REGISTRY)/imagegen-controller:$(TAG) .
	$(BUILD) -f infra/docker/nginx.Dockerfile -t $(REGISTRY)/imagegen-nginx:$(TAG) .
	$(BUILD) -f infra/docker/worker.Dockerfile --target fake -t $(REGISTRY)/imagegen-worker:$(TAG)-fake .

constraints:  ## Pin the images' Python packages to the versions installed in .venv
	{ echo "# Exact versions for the images, the ones the test suite last ran against. Regenerate with make constraints."; \
	  $(BIN)/pip freeze --exclude-editable | grep -viE '^(imagegen|-e )'; } > infra/docker/constraints.txt

image-worker-real:  ## Build the worker with torch and the model weights (several GB to download)
	$(BUILD) -f infra/docker/worker.Dockerfile --target real -t $(REGISTRY)/imagegen-worker:$(TAG) .

# --- Python -----------------------------------------------------------------

$(BIN)/python:
	$(PY_VERSION) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip

install: $(BIN)/python  ## Editable install with every extra (torch is installed only in the worker image)
	$(BIN)/pip install -e '.[api,worker,controller,dev]'

lint:  ## ruff check + format check
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

fmt:  ## Apply ruff fixes and formatting
	$(BIN)/ruff check --fix .
	$(BIN)/ruff format .

migrate:  ## Apply Alembic migrations to DATABASE_URL
	$(BIN)/alembic upgrade head

# --- Tests ------------------------------------------------------------------

test-unit:  ## Unit tests (nothing external)
	$(BIN)/pytest -m unit

test-api:  ## API tests (Postgres, Redis, MinIO via testcontainers; needs Docker)
	$(BIN)/pytest -m api

# The stack tests start infra/compose.test.yml themselves and remove it afterwards (STACK_KEEP=1 leaves it
# running for the next run). NO_BUILD=1 skips rebuilding its images, for CI, which builds them with a cache.
STACK_IMAGES := $(if $(NO_BUILD),,test-images)

test-images:  ## Build the images of the compose test stack
	$(COMPOSE_TEST) build

test-integration: $(STACK_IMAGES)  ## Worker integration tests (compose test stack)
	$(BIN)/pytest -m integration

test-lb: $(STACK_IMAGES)  ## Load-balancer tests (compose test stack with Nginx)
	$(BIN)/pytest -m lb

browsers:  ## Download the Chromium the end-to-end tests drive
	$(BIN)/playwright install chromium

test-e2e: $(STACK_IMAGES)  ## Playwright end-to-end tests in Chromium (compose test stack)
	$(BIN)/pytest -m e2e

# --- Run --------------------------------------------------------------------

run-api:  ## Run the API server locally
	$(BIN)/uvicorn api.main:app --reload --port 8000 --ws-ping-interval 25

run-worker:  ## Run one worker locally with the fake pipeline
	PIPELINE=fake FAKE_STEP_SECONDS=1 $(BIN)/celery -A worker.app:celery_app worker \
		--pool=prefork --concurrency=1 -Q image_generation --loglevel=info

run-controller:  ## Run the controller locally
	$(BIN)/python -m controller.main
