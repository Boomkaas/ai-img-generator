"""Fans pub/sub messages out to this process's WebSockets.

The process holds one Redis subscription however many browsers are connected. Each socket has its own outbox
and writer, so a slow or dead browser only ever delays itself.
"""

import asyncio
import contextlib
import logging
from collections import defaultdict

from starlette.websockets import WebSocket

from common import live
from common.config import get_settings

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

    async def write(self) -> None:
        """Send queued messages until stopped; raises if a send fails or takes too long."""
        timeout = get_settings().WS_SEND_TIMEOUT_S
        while (text := await self._outbox.get()) is not None:
            await asyncio.wait_for(self.websocket.send_text(text), timeout)


class Hub:
    def __init__(self):
        self._by_user: dict[str, set[Connection]] = defaultdict(set)
        self._admins: set[Connection] = set()
        self._listener: asyncio.Task | None = None

    @property
    def sockets(self) -> int:
        return sum(len(connections) for connections in self._by_user.values())

    def add(self, connection: Connection) -> None:
        self._by_user[connection.user_id].add(connection)
        if connection.is_admin:
            self._admins.add(connection)

    def remove(self, connection: Connection) -> None:
        connections = self._by_user.get(connection.user_id)
        if connections is not None:
            connections.discard(connection)
            if not connections:
                del self._by_user[connection.user_id]
        self._admins.discard(connection)

    def dispatch(self, channel: str, text: str) -> None:
        if channel == live.SYSTEM_CHANNEL:
            targets = [c for connections in self._by_user.values() for c in connections]
        elif channel == live.ADMIN_CHANNEL:
            targets = list(self._admins)
        else:
            targets = list(self._by_user.get(live.channel_user_id(channel), ()))
        for connection in targets:
            if not connection.offer(text):
                log.info("closing a websocket of user %s that stopped reading", connection.user_id)
                self.remove(connection)

    def start(self) -> None:
        self._listener = asyncio.create_task(self._listen(), name="websocket-hub")

    async def stop(self) -> None:
        if self._listener is not None:
            self._listener.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._listener

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
