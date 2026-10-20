"""The FastAPI application.

Each API process is stateless: the engine, the Redis client and the request counter are per-process
resources, and everything shared lives in Postgres and Redis. Run with `make run-api`.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text

from api import deps
from api.metrics import RequestCounter
from api.routes import admin, auth, tasks
from common import celery_client, db, live
from common.config import get_settings

log = logging.getLogger(__name__)

HEALTH_PATH = "/api/health"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.get_engine()
    live.client()
    yield
    db.dispose_engine()
    live.reset_client()
    celery_client.reset_app()


def _check(name: str, probe) -> str:
    try:
        probe()
    except Exception as exc:
        log.warning("health check: %s is down (%s)", name, type(exc).__name__)
        return "down"
    return "ok"


def _ping_db() -> None:
    with db.transaction() as tx:
        tx.execute(text("SELECT 1"))


def health() -> JSONResponse:
    checks = {"db": _check("db", _ping_db), "redis": _check("redis", live.client().ping)}
    down = [name for name, state in checks.items() if state != "ok"]
    if not down:
        return JSONResponse(checks)
    detail = f"The server can't reach {' and '.join(down)}. Try again in a moment."
    return JSONResponse({"detail": detail, **checks}, status_code=503)


async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """FastAPI's 422 lists every error; answer with one sentence about the first, in the usual shape."""
    error = exc.errors()[0]
    # For malformed JSON the location holds a character offset, not a field name.
    field = "" if error["type"] == "json_invalid" else ".".join(str(p) for p in error["loc"] if p != "body")
    where = f"the field '{field}'" if field else "the request body"
    return JSONResponse({"detail": f"Check {where}: {error['msg']}"}, status_code=422)


async def server_error(request: Request, exc: Exception) -> JSONResponse:
    # Uvicorn logs the traceback; the user gets a sentence instead of a bare "Internal Server Error".
    return JSONResponse({"detail": "Something went wrong on our side. Please try again."}, status_code=500)


def create_app() -> FastAPI:
    app = FastAPI(title="imagegen", lifespan=lifespan)
    app.state.requests = RequestCounter(get_settings().REQUEST_RATE_WINDOW_S)

    app.add_api_route(HEALTH_PATH, health, methods=["GET"])
    app.include_router(auth.router)
    app.include_router(tasks.router)
    app.include_router(admin.router)
    app.add_exception_handler(RequestValidationError, validation_error)
    app.add_exception_handler(Exception, server_error)

    # The last middleware added runs first, so the counter also sees requests require_json refuses.
    app.middleware("http")(deps.require_json)

    @app.middleware("http")
    async def count_requests(request: Request, call_next):
        if request.url.path != HEALTH_PATH:  # Docker and Nginx probes would drown the real traffic
            request.app.state.requests.record()
        return await call_next(request)

    return app


app = create_app()
