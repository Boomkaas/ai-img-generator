import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from api import ws
from api.hub import Connection, Hub
from common import live
from common.config import get_settings
from common.messages import SystemStatus, TaskOut, TaskUpdate

pytestmark = pytest.mark.unit


class FakeSocket:
    def __init__(self, stuck=False):
        self.stuck = stuck
        self.sent = []

    async def send_text(self, text):
        if self.stuck:
            await asyncio.Event().wait()
        self.sent.append(text)


@pytest.fixture(autouse=True)
def small_limits(monkeypatch):
    monkeypatch.setenv("WS_OUTBOX_SIZE", "2")
    monkeypatch.setenv("WS_SEND_TIMEOUT_S", "0.1")
    monkeypatch.setenv("WS_MAX_SOCKETS_PER_USER", "2")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def new_hub() -> Hub:
    return Hub(snapshot=lambda user_id: None)


async def sent_after_writing(connection: Connection) -> list[str]:
    """What the connection's writer sends of the messages queued so far."""
    writer = asyncio.create_task(connection.write())
    await asyncio.sleep(0.01)
    writer.cancel()
    return connection.websocket.sent


async def test_a_stuck_socket_delays_no_one_and_is_dropped():
    hub = new_hub()
    stuck = Connection(FakeSocket(stuck=True), "a", is_admin=False)
    fine = Connection(FakeSocket(), "b", is_admin=False)
    hub.add(stuck)
    hub.add(fine)
    writers = [asyncio.create_task(c.write()) for c in (stuck, fine)]

    for n in range(5):
        hub.dispatch(live.SYSTEM_CHANNEL, str(n))
        await asyncio.sleep(0.01)

    assert fine.websocket.sent == ["0", "1", "2", "3", "4"]
    assert hub.sockets == 1  # the stuck one fell a full outbox behind
    with pytest.raises(TimeoutError):
        await writers[0]
    writers[1].cancel()


async def test_messages_are_routed_by_channel():
    hub = new_hub()
    user = Connection(FakeSocket(), "u1", is_admin=False)
    other = Connection(FakeSocket(), "u2", is_admin=False)
    admin = Connection(FakeSocket(), "u3", is_admin=True)
    for connection in (user, other, admin):
        hub.add(connection)

    hub.dispatch(live.user_channel("u1"), "mine")
    hub.dispatch(live.ADMIN_CHANNEL, "overview")
    hub.dispatch(live.SYSTEM_CHANNEL, "status")

    assert await sent_after_writing(user) == ["mine", "status"]
    assert await sent_after_writing(other) == ["status"]
    assert await sent_after_writing(admin) == ["overview", "status"]


def test_a_user_gets_at_most_the_allowed_sockets():
    hub = new_hub()
    first, second, third = (Connection(FakeSocket(), "u1", is_admin=False) for _ in range(3))
    assert hub.add(first)
    assert hub.add(second)
    assert not hub.add(third)
    assert hub.add(Connection(FakeSocket(), "u2", is_admin=False))
    hub.remove(first)
    assert hub.add(third)


def test_removing_the_last_socket_forgets_the_user():
    hub = new_hub()
    connection = Connection(FakeSocket(), "u1", is_admin=True)
    hub.add(connection)
    hub.remove(connection)
    hub.remove(connection)
    assert hub.sockets == 0
    assert hub.admin_sockets == 0


async def test_updates_the_snapshot_supersedes_are_dropped(monkeypatch):
    monkeypatch.setenv("WS_OUTBOX_SIZE", "10")
    get_settings.cache_clear()
    task_id, other_id = uuid4(), uuid4()
    connection = Connection(FakeSocket(), "u1", is_admin=False)
    queued = TaskUpdate(id=task_id, status="queued", position=1).model_dump_json()
    behind = TaskUpdate(id=task_id, status="running", progress=20).model_dump_json()
    ahead = TaskUpdate(id=task_id, status="running", progress=60).model_dump_json()
    elsewhere = TaskUpdate(id=other_id, status="running", progress=10).model_dump_json()
    system = SystemStatus(running=1, waiting=0, new_image_ready_in_s=1.0).model_dump_json()
    for text in (queued, behind, system, ahead, elsewhere):
        connection.offer(text)

    task = TaskOut(
        task_id=task_id, status="running", prompt="a cat", size=256, created_at=datetime.now(UTC), progress=40
    )
    connection.drop_superseded(task)

    assert await sent_after_writing(connection) == [system, ahead, elsewhere]


def test_status_cache_computes_once_per_ttl(monkeypatch):
    calls = []
    status = SystemStatus(running=0, waiting=0, new_image_ready_in_s=1.0)
    monkeypatch.setattr(ws.service, "system_status", lambda: calls.append(1) or status)
    clock = [100.0]
    monkeypatch.setattr(ws.time, "monotonic", lambda: clock[0])
    cache = ws.SystemStatusCache(ttl=1)

    assert cache.get() is status
    cache.get()
    assert len(calls) == 1
    clock[0] += 1
    cache.get()
    assert len(calls) == 2
