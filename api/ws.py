"""The WebSocket each page keeps open for live updates."""

import contextlib
import logging
import threading
import time

import anyio
from fastapi import APIRouter, WebSocket
from starlette.concurrency import run_in_threadpool

from api import deps, security
from api.hub import Connection, Hub
from api.services import tasks as service
from common import db
from common.config import get_settings
from common.messages import Ping, Snapshot, SystemStatus

log = logging.getLogger(__name__)

router = APIRouter()

UNAUTHORISED = 4401  # the browser refreshes its access token and reconnects
FORBIDDEN_ORIGIN = 4403
TOO_MANY_SOCKETS = 1008  # policy violation
INTERNAL_ERROR = 1011

PING = Ping().model_dump_json()


class SystemStatusCache:
    """The system status, recomputed at most once every `ttl` seconds however many sockets ask."""

    def __init__(self, ttl: float):
        self._ttl = ttl
        self._lock = threading.Lock()
        self._status: SystemStatus | None = None
        self._at = 0.0

    def get(self) -> SystemStatus:
        # Held while computing, so sockets arriving together wait for one estimate instead of each running it.
        with self._lock:
            if self._status is None or time.monotonic() - self._at >= self._ttl:
                self._status = service.system_status()
                self._at = time.monotonic()
            return self._status


def snapshot(status: SystemStatusCache, user_id) -> Snapshot:
    task = db.get_active_task(user_id)
    return Snapshot(system=status.get(), task=service.task_out(task) if task else None)


@router.websocket("/ws")
async def updates(websocket: WebSocket) -> None:
    # Refusing the handshake would reach the browser as a bare HTTP 403, which it can't tell from a network
    # error; a close code after accepting says what to do.
    await websocket.accept()
    if not deps.check_origin(websocket.headers.get("origin")):
        await websocket.close(FORBIDDEN_ORIGIN)
        return
    try:
        user = await run_in_threadpool(deps.user_from_token, websocket.cookies.get(security.ACCESS_COOKIE))
    except Exception:
        log.exception("could not check a websocket's access token")
        await _close(websocket, INTERNAL_ERROR)
        return
    if user is None:
        await websocket.close(UNAUTHORISED)
        return

    hub: Hub = websocket.app.state.hub
    connection = Connection(websocket, str(user.id), user.role == "admin")
    # Registered before the snapshot is read, so nothing published meanwhile is lost; it waits in the outbox.
    if not hub.add(connection):
        await websocket.close(TOO_MANY_SOCKETS)
        return
    try:
        try:
            current = await hub.snapshot(connection.user_id)
        except Exception:
            log.exception("could not build the snapshot for user %s", user.id)
            await _close(websocket, INTERNAL_ERROR)
            return
        connection.drop_superseded(current.task)
        try:
            await websocket.send_text(current.model_dump_json())
        except Exception:
            return  # the client left while the snapshot was being read
        await _serve(connection)
    finally:
        hub.remove(connection)


async def _serve(connection: Connection) -> None:
    """Run until the client leaves, a send fails, or the socket falls behind; then close it."""
    async with anyio.create_task_group() as group:

        async def until_done(job, *args) -> None:
            try:
                await job(*args)
            except Exception:
                log.info("dropping a websocket of user %s", connection.user_id, exc_info=True)
            group.cancel_scope.cancel()

        group.start_soon(until_done, connection.write)
        group.start_soon(until_done, _read_until_closed, connection.websocket)
        group.start_soon(until_done, _keep_alive, connection)
    await _close(connection.websocket)


async def _close(websocket: WebSocket, code: int = 1000) -> None:
    # Already closed if the client left; a socket that stopped reading may not answer at all.
    with contextlib.suppress(Exception), anyio.move_on_after(get_settings().WS_SEND_TIMEOUT_S):
        await websocket.close(code)


async def _read_until_closed(websocket: WebSocket) -> None:
    # The client has nothing to say; reading only notices when it leaves.
    while (await websocket.receive())["type"] != "websocket.disconnect":
        pass


async def _keep_alive(connection: Connection) -> None:
    interval = get_settings().WS_PING_INTERVAL_S
    while True:
        await anyio.sleep(interval)
        if not connection.offer(PING):
            return
