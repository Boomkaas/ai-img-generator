import asyncio

import pytest

from api.hub import Connection, Hub
from common import live
from common.config import get_settings

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
def small_outbox(monkeypatch):
    monkeypatch.setenv("WS_OUTBOX_SIZE", "2")
    monkeypatch.setenv("WS_SEND_TIMEOUT_S", "0.1")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def queued(connection: Connection) -> list[str]:
    return [connection._outbox.get_nowait() for _ in range(connection._outbox.qsize())]


async def test_a_stuck_socket_delays_no_one_and_is_dropped():
    hub = Hub()
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
    hub = Hub()
    user = Connection(FakeSocket(), "u1", is_admin=False)
    other = Connection(FakeSocket(), "u2", is_admin=False)
    admin = Connection(FakeSocket(), "u3", is_admin=True)
    for connection in (user, other, admin):
        hub.add(connection)

    hub.dispatch(live.user_channel("u1"), "mine")
    hub.dispatch(live.ADMIN_CHANNEL, "overview")
    hub.dispatch(live.SYSTEM_CHANNEL, "status")

    assert queued(user) == ["mine", "status"]
    assert queued(other) == ["status"]
    assert queued(admin) == ["overview", "status"]


def test_removing_the_last_socket_forgets_the_user():
    hub = Hub()
    connection = Connection(FakeSocket(), "u1", is_admin=True)
    hub.add(connection)
    hub.remove(connection)
    hub.remove(connection)
    assert hub.sockets == 0
    assert not hub._by_user
    assert not hub._admins
