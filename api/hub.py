"""Fans pub/sub messages out to this process's WebSockets.

The process holds one Redis subscription however many browsers are connected. Each socket has its own outbox
and writer, so a slow or dead browser only ever delays itself.
"""

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable, Iterable

from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocket

from common import db, live
from common.config import get_settings
from common.messages import Snapshot, TaskOut

log = logging.getLogger(__name__)


class Connection:
    """One open socket and the messages waiting to be sent to it, in order."""

    def __init__(self, websocket: WebSocket, user_id: str, is_admin: bool):
        self.websocket = websocket
        self.user_id = user_id
        self.is_admin = is_admin
        self._outbox: asyncio.Queue[str | None] = asyncio.Queue(get_settings().WS_OUTBOX_SIZE)

    def offer(self, text: str) -> bool:
        """Queue a message; False, and the writer stops, if the socket has fallen too far behind."""
        try:
            self._outbox.put_nowait(text)
        except asyncio.QueueFull:
            self.stop()
            return False
        return True

    def stop(self) -> None:
        # The backlog would never be sent anyway; dropping it makes room for the stop marker.
        while not self._outbox.empty():
            self._outbox.get_nowait()
        self._outbox.put_nowait(None)

    def drop_superseded(self, task: TaskOut | None) -> None:
        """Drop queued updates that a snapshot showing `task` makes stale, so progress never steps back."""
        if task is None or task.status != "running":
            return
        kept = [self._outbox.get_nowait() for _ in range(self._outbox.qsize())]
        for text in kept:
            if text is None or not _is_behind(text, task):
                self._outbox.put_nowait(text)

    async def write(self) -> None:
        """Send queued messages until stopped; raises if a send fails or takes too long."""
        timeout = get_settings().WS_SEND_TIMEOUT_S
        while (text := await self._outbox.get()) is not None:
            await asyncio.wait_for(self.websocket.send_text(text), timeout)


def _is_behind(text: str, task: TaskOut) -> bool:
    """True for an update of the running `task` sent before it started or at less progress."""
    message = json.loads(text)
    if message.get("type") != "task" or message.get("id") != str(task.task_id):
        return False
    return message["status"] == "queued" or message.get("progress", 0) < (task.progress or 0)


def _still_admins(user_ids: Iterable[str]) -> set[str]:
    admins = set()
    for user_id in user_ids:
        user = db.get_user(user_id)
        if user is not None and user.role == "admin":
            admins.add(user_id)
    return admins


class Hub:
    def __init__(self, snapshot: Callable[[str], Snapshot]):
        self._snapshot = snapshot
        self._by_user: dict[str, set[Connection]] = {}
        self._admins: set[Connection] = set()
        self._jobs: list[asyncio.Task] = []

    @property
    def sockets(self) -> int:
        return sum(len(connections) for connections in self._by_user.values())

    @property
    def admin_sockets(self) -> int:
        return len(self._admins)

    def add(self, connection: Connection) -> bool:
        """Register a socket; False if its user already has as many open as allowed."""
        connections = self._by_user.get(connection.user_id, set())
        if len(connections) >= get_settings().WS_MAX_SOCKETS_PER_USER:
            return False
        connections.add(connection)
        self._by_user[connection.user_id] = connections
        if connection.is_admin:
            self._admins.add(connection)
        return True

    def remove(self, connection: Connection) -> None:
        connections = self._by_user.get(connection.user_id)
        if connections is not None:
            connections.discard(connection)
            if not connections:
                del self._by_user[connection.user_id]
        self._admins.discard(connection)

    async def snapshot(self, user_id: str) -> Snapshot:
        return await run_in_threadpool(self._snapshot, user_id)

    def dispatch(self, channel: str, text: str) -> None:
        if channel == live.SYSTEM_CHANNEL:
            targets = [c for connections in self._by_user.values() for c in connections]
        elif channel == live.ADMIN_CHANNEL:
            targets = list(self._admins)
        else:
            targets = list(self._by_user.get(live.channel_user_id(channel), ()))
        for connection in targets:
            self._offer(connection, text)

    def _offer(self, connection: Connection, text: str) -> None:
        if not connection.offer(text):
            log.info("closing a websocket of user %s that stopped reading", connection.user_id)
            self.remove(connection)

    def start(self) -> None:
        self._jobs = [
            asyncio.create_task(self._listen(), name="websocket-hub"),
            asyncio.create_task(self._recheck_admins(), name="websocket-admins"),
        ]

    async def stop(self) -> None:
        for job in self._jobs:
            job.cancel()
        for job in self._jobs:
            with contextlib.suppress(asyncio.CancelledError):
                await job

    async def _listen(self) -> None:
        """Forward every message; if the subscription drops, resubscribe with backoff and keep the sockets."""
        backoff = get_settings().WS_RESUBSCRIBE_BACKOFF_S
        failures = 0
        while True:
            client = live.async_client()
            try:
                async with client.pubsub() as pubsub:
                    await pubsub.subscribe(live.SYSTEM_CHANNEL, live.ADMIN_CHANNEL)
                    await pubsub.psubscribe(live.USER_CHANNEL_PATTERN)
                    failures = 0
                    # Anything published while unsubscribed is lost: a task may have finished meanwhile.
                    await self._resend_snapshots()
                    async for message in pubsub.listen():
                        if message["type"] in ("message", "pmessage"):
                            self.dispatch(message["channel"], message["data"])
            except Exception:
                log.warning("lost the redis subscription", exc_info=True)
            finally:
                await client.aclose()
            delay = backoff[min(failures, len(backoff) - 1)]
            failures += 1
            log.info("resubscribing to redis in %s s", delay)
            await asyncio.sleep(delay)

    async def _resend_snapshots(self) -> None:
        users = list(self._by_user)
        snapshots = await asyncio.gather(*map(self.snapshot, users), return_exceptions=True)
        for user_id, snapshot in zip(users, snapshots, strict=True):
            if isinstance(snapshot, Exception):
                log.warning("could not refresh the snapshot of user %s", user_id, exc_info=snapshot)
                continue
            text = snapshot.model_dump_json()
            for connection in list(self._by_user.get(user_id, ())):
                connection.drop_superseded(snapshot.task)
                self._offer(connection, text)

    async def _recheck_admins(self) -> None:
        """Roles are read at connect; this takes the overview away from an admin who was demoted since."""
        interval = get_settings().ADMIN_RECHECK_INTERVAL_S
        while True:
            await asyncio.sleep(interval)
            user_ids = {connection.user_id for connection in self._admins}
            if not user_ids:
                continue
            try:
                admins = await run_in_threadpool(_still_admins, user_ids)
            except Exception:
                log.warning("could not recheck the admins' roles", exc_info=True)
                continue
            for connection in list(self._admins):
                if connection.user_id not in admins:
                    connection.is_admin = False
                    self._admins.discard(connection)
