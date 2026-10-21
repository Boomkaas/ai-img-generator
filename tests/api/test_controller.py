import json
import time
from uuid import uuid4

import pytest

from common import db, live
from common.messages import AdminOverview, SystemStatus, TaskUpdate
from common.overview import admin_overview
from controller.status import publish_status

pytestmark = pytest.mark.api

PASSWORD = "correct horse battery"


@pytest.fixture(autouse=True)
def no_local_workers(monkeypatch):
    # set_worker_state keeps each host's state in the module; start every test without any.
    monkeypatch.setattr(live, "_workers", {})


def make_task(email=None):
    user = db.create_user(email or f"{uuid4()}@example.com", "not-a-real-hash")
    with db.transaction() as tx:
        return db.insert_task(tx, user.id, "a red fox", 512, uuid4())


@pytest.fixture
def two_running_three_queued(redis_live):
    tasks = [make_task() for _ in range(5)]
    for t in tasks[:2]:
        assert db.claim_task(t.id, worker_host="nodeB-worker-1")
    return tasks


def collect(pubsub, until_type: str, timeout: float = 5.0) -> list[tuple[str, dict]]:
    messages = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = pubsub.get_message(timeout=0.1)
        if message is None or message["type"] not in ("message", "pmessage"):
            continue
        body = json.loads(message["data"])
        messages.append((message["channel"], body))
        if body["type"] == until_type:
            return messages
    raise AssertionError(f"no {until_type} message within {timeout} s; got {messages}")


def test_publish_status_sends_system_tasks_and_overview(redis_live, two_running_three_queued):
    tasks = two_running_three_queued
    pubsub = redis_live.pubsub()
    pubsub.subscribe(live.SYSTEM_CHANNEL, live.ADMIN_CHANNEL)
    pubsub.psubscribe(live.USER_CHANNEL_PATTERN)

    publish_status()
    messages = collect(pubsub, "admin")
    pubsub.close()

    systems = [SystemStatus.model_validate(body) for _, body in messages if body["type"] == "system"]
    assert [(s.running, s.waiting) for s in systems] == [(2, 3)]

    updates = {
        body["id"]: (channel, TaskUpdate.model_validate(body))
        for channel, body in messages
        if body["type"] == "task"
    }
    assert set(updates) == {str(t.id) for t in tasks}
    for t in tasks:
        assert updates[str(t.id)][0] == live.user_channel(t.user_id)
    for t in tasks[:2]:
        update = updates[str(t.id)][1]
        assert update.status == "running"
        assert update.position is None and update.progress is None
        assert update.ready_in_s is not None
    for position, t in enumerate(tasks[2:], start=1):
        update = updates[str(t.id)][1]
        assert (update.status, update.position, update.queue_length) == ("queued", position, 3)

    overview = AdminOverview.model_validate(messages[-1][1])
    assert [item.status for item in overview.tasks] == ["running", "running", "queued", "queued", "queued"]
    assert [item.position for item in overview.tasks[2:]] == [1, 2, 3]


def test_overview_endpoint_is_for_admins_only(make_client, two_running_three_queued):
    admin, user = make_client(), make_client()
    for client in (admin, user):
        email = f"{uuid4()}@example.com"
        response = client.post("/api/auth/register", json={"email": email, "password": PASSWORD})
        assert response.status_code == 201
    db.set_role(db.get_user_by_email(admin.get("/api/auth/me").json()["email"]).id, "admin")

    response = admin.get("/api/admin/overview")
    assert response.status_code == 200
    overview = AdminOverview.model_validate(response.json())
    assert len(overview.tasks) == 5
    assert overview.tasks[0].user_email.endswith("@example.com")

    assert user.get("/api/admin/overview").status_code == 403


def test_machines_group_heartbeats_from_the_same_machine(redis_live):
    live.write_api_heartbeat("nodeB", 101, sockets=2, requests_per_min=7)
    live.write_api_heartbeat("nodeB", 102, sockets=1, requests_per_min=3)
    live.set_worker_state("nodeB-worker-1", "idle")
    live.set_worker_state("nodeB-worker-2", "loading")
    live.write_machine_heartbeat("nodeA", "infra")

    overview = admin_overview()

    machines = {m.name: m for m in overview.machines}
    assert set(machines) == {"nodeA", "nodeB"}
    assert machines["nodeB"].role == "app"
    assert (machines["nodeB"].api.sockets, machines["nodeB"].api.requests_per_min) == (3, 10)
    assert machines["nodeA"].role == "infra"
    assert machines["nodeA"].api is None
    assert [w.name for w in overview.workers] == ["nodeB-worker-1", "nodeB-worker-2"]
    assert {w.machine for w in overview.workers} == {"nodeB"}


def test_overview_shows_busy_workers_progress_and_owner(redis_live):
    t = make_task("ana@example.com")
    assert db.claim_task(t.id, worker_host="nodeB-worker-1")
    live.set_worker_state("nodeB-worker-1", "busy", task_id=t.id)
    live.publish_progress(t.user_id, t.id, 36, "denoising")

    overview = admin_overview()

    assert overview.workers[0].task_id == t.id
    assert overview.workers[0].progress == 36
    item = overview.tasks[0]
    assert (item.user_email, item.status, item.worker_host, item.progress) == (
        "ana@example.com",
        "running",
        "nodeB-worker-1",
        36,
    )
    # Before the autoscaler reports, its block falls back to the heartbeats.
    assert (overview.autoscaler.ready, overview.autoscaler.starting) == (1, 0)
