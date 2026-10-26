"""The admin overview against the real stack: what it shows is what Postgres and the heartbeats say."""

import pytest

from common import db, live

pytestmark = pytest.mark.integration


def test_overview_matches_the_database_and_heartbeats(stack, clients):
    admin = clients[0]
    db.set_role(db.get_user_by_email(admin.email).id, "admin")
    ids = [c.submit("a cat", size=256) for c in clients[1:4]]  # two run, one waits
    stack.wait_all(ids[:2], "running")
    stack.wait_progress(ids[1], at_least=4)

    response = admin.http.get("/api/admin/overview")
    assert response.status_code == 200
    overview = response.json()

    rows = {str(t.id): t for t in db.active_tasks()}
    tasks = {item["id"]: item for item in overview["tasks"]}
    assert tasks.keys() == rows.keys() == set(ids)
    for task_id, item in tasks.items():
        row = rows[task_id]
        assert item["status"] == row.status
        assert item["user_email"] == clients[1 + ids.index(task_id)].email
        assert item["worker_host"] == row.worker_host
    assert tasks[ids[2]]["position"] == 1
    assert tasks[ids[1]]["progress"] >= 4

    beats = {hb.host: hb for hb in live.worker_heartbeats()}
    workers = {item["name"]: item for item in overview["workers"]}
    assert workers.keys() == beats.keys() == set(stack.worker_hosts())
    assert {w["task_id"] for w in workers.values()} == set(ids[:2])
    assert {workers[rows[t].worker_host]["task_id"] for t in ids[:2]} == set(ids[:2])
    assert {w["state"] for w in workers.values()} == {"busy"}

    machines = {item["name"]: item for item in overview["machines"]}
    assert machines.keys() == {"api-a", "api-b", "app", "infra"}
    assert machines["infra"]["role"] == "infra" and machines["app"]["role"] == "app"
    assert machines["api-a"]["api"] is not None and machines["app"]["api"] is None
    assert overview["autoscaler"]["ready"] == live.count_workers(("idle", "busy")) == 2
