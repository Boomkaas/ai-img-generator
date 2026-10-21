import contextlib
import json
import os
import time
from uuid import uuid4

import jwt
import pytest
from starlette.websockets import WebSocketDisconnect

from api import security
from common import db, live
from common.config import get_settings
from common.messages import SystemStatus, TaskFinal, TaskUpdate

pytestmark = pytest.mark.api

ORIGIN = "https://localhost"
PASSWORD = "correct horse battery"
SYSTEM = SystemStatus(running=1, waiting=2, new_image_ready_in_s=60.0)


def sign_up(client):
    response = client.post(
        "/api/auth/register", json={"email": f"{uuid4()}@example.com", "password": PASSWORD}
    )
    assert response.status_code == 201
    return client


def user_id_of(client):
    return db.get_user_by_email(client.get("/api/auth/me").json()["email"]).id


def connect(client, origin=ORIGIN):
    return client.websocket_connect("/ws", headers={"origin": origin})


def receive(socket) -> dict:
    return json.loads(socket.receive_text())


def eventually(condition, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.05)


def pubsub_clients(redis) -> list[dict]:
    return [c for c in redis.client_list() if int(c["sub"]) or int(c["psub"])]


def wait_for_hubs(redis, count: int) -> None:
    """Until `count` hubs hold their whole subscription; anything published before that would be lost."""
    eventually(lambda: [(c["sub"], c["psub"]) for c in pubsub_clients(redis)] == [("2", "1")] * count)


@pytest.fixture(autouse=True)
def allowed_origin(monkeypatch):
    monkeypatch.setenv("ALLOWED_ORIGINS", ORIGIN)
    get_settings.cache_clear()


@pytest.fixture
def user(make_client):
    return sign_up(make_client())


@pytest.fixture
def other(make_client):
    return sign_up(make_client())


@pytest.fixture
def admin(make_client):
    client = sign_up(make_client())
    db.set_role(user_id_of(client), "admin")
    return client


def assert_closed_with(socket, code: int) -> None:
    with pytest.raises(WebSocketDisconnect) as closed:
        socket.receive_text()
    assert closed.value.code == code


# --- Handshake ----------------------------------------------------------------------------------------------


def test_no_cookie_is_closed_with_4401(make_client):
    with connect(make_client()) as socket:
        assert_closed_with(socket, 4401)


def test_expired_token_is_closed_with_4401(user):
    now = int(time.time())
    claims = {"sub": str(user_id_of(user)), "role": "user", "iat": now - 120, "exp": now - 60}
    expired = jwt.encode(claims, get_settings().JWT_SECRET, algorithm=security.JWT_ALGORITHM)
    user.cookies.set(security.ACCESS_COOKIE, expired)
    with connect(user) as socket:
        assert_closed_with(socket, 4401)


def test_other_origin_is_closed_with_4403(user):
    with connect(user, origin="https://evil.example") as socket:
        assert_closed_with(socket, 4403)


def test_first_message_is_a_snapshot_without_a_task(user):
    with connect(user) as socket:
        message = receive(socket)
    assert message["type"] == "snapshot"
    assert message["system"]["type"] == "system"
    assert message["task"] is None


def test_snapshot_carries_the_active_task(user):
    with db.transaction() as tx:
        task = db.insert_task(tx, user_id_of(user), "a cat", 256, uuid4())
    with connect(user) as socket:
        message = receive(socket)
    assert message["type"] == "snapshot"
    assert message["system"]["waiting"] == 1
    assert message["task"]["task_id"] == str(task.id)
    assert message["task"]["status"] == "queued"


def test_ping_arrives(make_client, monkeypatch):
    monkeypatch.setenv("WS_PING_INTERVAL_S", "0.1")
    get_settings.cache_clear()
    client = sign_up(make_client())
    with connect(client) as socket:
        assert receive(socket)["type"] == "snapshot"
        assert receive(socket) == {"type": "ping"}


# --- Routing ------------------------------------------------------------------------------------------------
# A socket's messages arrive in publishing order, so a socket whose next message is the `system` sent after
# another one did not get that other one.


def test_user_messages_reach_only_that_users_sockets(user, other, redis_live):
    wait_for_hubs(redis_live, 2)
    update = TaskUpdate(id=uuid4(), status="running", progress=40, stage="step 10 of 25")
    with connect(user) as a1, connect(user) as a2, connect(other) as b:
        for socket in (a1, a2, b):
            receive(socket)
        live.publish(live.user_channel(user_id_of(user)), update)
        live.publish(live.SYSTEM_CHANNEL, SYSTEM)
        for socket in (a1, a2):
            assert receive(socket) == json.loads(update.model_dump_json())
            assert receive(socket)["type"] == "system"
        assert receive(b)["type"] == "system"


def test_admin_messages_reach_only_admin_sockets(user, admin, redis_live):
    wait_for_hubs(redis_live, 2)
    with connect(admin) as admin_socket, connect(user) as user_socket:
        receive(admin_socket)
        receive(user_socket)
        redis_live.publish(live.ADMIN_CHANNEL, '{"type": "admin"}')
        live.publish(live.SYSTEM_CHANNEL, SYSTEM)
        assert receive(admin_socket)["type"] == "admin"
        assert receive(admin_socket)["type"] == "system"
        assert receive(user_socket)["type"] == "system"


def test_system_messages_reach_everyone(user, other, admin, redis_live):
    wait_for_hubs(redis_live, 3)
    with connect(user) as a, connect(other) as b, connect(admin) as c:
        for socket in (a, b, c):
            receive(socket)
        live.publish(live.SYSTEM_CHANNEL, SYSTEM)
        for socket in (a, b, c):
            assert receive(socket) == json.loads(SYSTEM.model_dump_json())


# --- Redis --------------------------------------------------------------------------------------------------


def test_twenty_sockets_add_no_redis_connections(make_client, redis_live, monkeypatch):
    # The heartbeat writes once at startup and not again during the test, so it can't open a connection.
    monkeypatch.setenv("HEARTBEAT_INTERVAL_S", "14")
    monkeypatch.setenv("WS_MAX_SOCKETS_PER_USER", "21")
    get_settings.cache_clear()
    client = sign_up(make_client())
    wait_for_hubs(redis_live, 1)
    with connect(client) as first:
        receive(first)
        before = len(redis_live.client_list())
        with contextlib.ExitStack() as stack:
            for _ in range(20):
                receive(stack.enter_context(connect(client)))
            assert len(redis_live.client_list()) == before
            assert len(pubsub_clients(redis_live)) == 1


def test_after_losing_its_subscription_the_hub_resends_snapshots(user, redis_live):
    with db.transaction() as tx:
        task = db.insert_task(tx, user_id_of(user), "a cat", 256, uuid4())
    wait_for_hubs(redis_live, 1)
    with connect(user) as socket:
        assert receive(socket)["task"]["task_id"] == str(task.id)
        [subscriber] = pubsub_clients(redis_live)
        redis_live.client_kill_filter(_id=subscriber["id"])

        # The task ends while the hub is away, so its final message reaches nobody.
        db.fail_queued(task.id, "queue unavailable")
        final = TaskFinal(id=task.id, status="failed").model_dump_json()
        assert redis_live.publish(live.user_channel(user_id_of(user)), final) == 0

        # The socket was never closed; the fresh snapshot tells it the task is over.
        resent = receive(socket)
        assert resent["type"] == "snapshot"
        assert resent["task"] is None
        live.publish(live.SYSTEM_CHANNEL, SYSTEM)
        assert receive(socket)["type"] == "system"


def test_sockets_beyond_the_per_user_limit_are_refused(make_client, monkeypatch):
    monkeypatch.setenv("WS_MAX_SOCKETS_PER_USER", "2")
    get_settings.cache_clear()
    client = sign_up(make_client())
    with connect(client) as a, connect(client) as b:
        receive(a)
        receive(b)
        with connect(client) as refused:
            assert_closed_with(refused, 1008)
    with connect(client) as again:
        assert receive(again)["type"] == "snapshot"


def test_a_demoted_admin_stops_getting_the_overview(make_client, redis_live, monkeypatch):
    monkeypatch.setenv("ADMIN_RECHECK_INTERVAL_S", "0.1")
    get_settings.cache_clear()
    admin = sign_up(make_client())
    admin_id = user_id_of(admin)
    db.set_role(admin_id, "admin")
    wait_for_hubs(redis_live, 1)
    hub = admin.app.state.hub
    with connect(admin) as socket:
        receive(socket)
        assert hub.admin_sockets == 1
        db.set_role(admin_id, "user")
        eventually(lambda: hub.admin_sockets == 0)
        redis_live.publish(live.ADMIN_CHANNEL, '{"type": "admin"}')
        live.publish(live.SYSTEM_CHANNEL, SYSTEM)
        assert receive(socket)["type"] == "system"


def test_api_heartbeat_reports_the_open_sockets(make_client, redis_live, monkeypatch):
    monkeypatch.setenv("HEARTBEAT_INTERVAL_S", "0.1")
    get_settings.cache_clear()
    client = sign_up(make_client())
    key = live.api_key(get_settings().MACHINE_NAME, os.getpid())
    with connect(client) as a, connect(client) as b:
        receive(a)
        receive(b)
        eventually(lambda: redis_live.hget(key, "sockets") == "2")
        assert 0 < redis_live.ttl(key) <= 15
        assert int(redis_live.hget(key, "requests_per_min")) >= 1
    eventually(lambda: redis_live.hget(key, "sockets") == "0")
