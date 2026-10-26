import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, insert, select

from common import celery_client, db, live, storage
from common.config import get_settings

pytestmark = pytest.mark.api

PASSWORD = "correct horse battery"


class FakeCelery:
    """Records what would have gone to the broker; `fail` makes enqueueing raise."""

    def __init__(self):
        self.sent = []
        self.revoked = []
        self.fail = False

    def send_generate(self, task):
        if self.fail:
            raise ConnectionError("broker unreachable")
        self.sent.append(task)

    def revoke(self, task_id, terminate=False):
        self.revoked.append((str(task_id), terminate))


@pytest.fixture(autouse=True)
def celery(monkeypatch) -> FakeCelery:
    fake = FakeCelery()
    monkeypatch.setattr(celery_client, "send_generate", fake.send_generate)
    monkeypatch.setattr(celery_client, "revoke", fake.revoke)
    return fake


def sign_up(client):
    response = client.post(
        "/api/auth/register", json={"email": f"{uuid4()}@example.com", "password": PASSWORD}
    )
    assert response.status_code == 201
    return client


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


def user_id_of(client) -> UUID:
    return db.get_user_by_email(client.get("/api/auth/me").json()["email"]).id


def submit(client, prompt="a cat", size=256, key=None):
    return client.post(
        "/api/tasks", json={"prompt": prompt, "size": size}, headers={"Idempotency-Key": str(key or uuid4())}
    )


def new_user_id() -> UUID:
    return db.create_user(f"{uuid4()}@example.com", "not-a-real-hash").id


def queued_task(user_id=None, size=256):
    with db.transaction() as tx:
        return db.insert_task(tx, user_id or new_user_id(), "a dog", size, uuid4())


def running_task(user_id=None):
    task = queued_task(user_id)
    assert db.claim_task(task.id, worker_host="nodeB-worker-1")
    return db.get_task(task.id)


def finished_rows(user_id, count, status="done"):
    """`count` finished tasks, one minute apart, the newest an hour ago."""
    start = datetime.now(UTC) - timedelta(hours=1)
    rows = [
        {
            "user_id": user_id,
            "status": status,
            "prompt": f"picture {i}",
            "size": 256,
            "idempotency_key": uuid4(),
            "created_at": start - timedelta(minutes=i),
            "finished_at": start - timedelta(minutes=i) + timedelta(seconds=30),
        }
        for i in range(count)
    ]
    with db.transaction() as tx:
        tx.execute(insert(db.tasks), rows)


def tasks_of(user_id):
    stmt = select(db.tasks).where(db.tasks.c.user_id == user_id)
    with db.transaction() as tx:
        return tx.execute(stmt).all()


# --- Submit -------------------------------------------------------------------------------------------------


def test_submit_returns_202_and_enqueues_with_the_database_id(user, celery):
    response = submit(user, prompt="  a lighthouse at dusk  ", size=512)
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["prompt"] == "a lighthouse at dusk"
    assert body["size"] == 512
    assert body["position"] == 1
    assert body["queue_length"] == 1
    assert body["ready_in_s"] > 0

    [sent] = celery.sent
    assert sent.id == UUID(body["task_id"])
    [row] = tasks_of(user_id_of(user))
    assert row.id == sent.id
    assert row.status == "queued"


def test_second_submit_while_one_is_active_is_refused(user):
    assert submit(user).status_code == 202
    response = submit(user)
    assert response.status_code == 429
    assert response.json() == {"detail": "You already have an image in progress"}


def test_full_queue_is_refused_with_a_retry_time(user, monkeypatch, celery):
    monkeypatch.setenv("MAX_QUEUED_TOTAL", "3")
    get_settings.cache_clear()
    for _ in range(3):
        queued_task()

    response = submit(user)
    assert response.status_code == 503
    body = response.json()
    assert body["detail"] == "The queue is full"
    assert body["retry_in_s"] > 0
    assert response.headers["Retry-After"] == str(body["retry_in_s"])
    assert tasks_of(user_id_of(user)) == []
    assert celery.sent == []


def test_the_same_idempotency_key_returns_the_same_task(user, celery):
    key = uuid4()
    first = submit(user, key=key)
    second = submit(user, key=key)
    assert first.status_code == second.status_code == 202
    assert first.json()["task_id"] == second.json()["task_id"]
    assert len(tasks_of(user_id_of(user))) == 1
    # Still queued, so the retry sends it again; both messages carry the one task id.
    assert [sent.id for sent in celery.sent] == [UUID(first.json()["task_id"])] * 2


def test_a_retry_enqueues_a_task_whose_first_request_died_before_enqueueing(user, celery):
    key = uuid4()
    with db.transaction() as tx:  # committed, but the API process died before send_generate
        lost = db.insert_task(tx, user_id_of(user), "a cat", 256, key)

    response = submit(user, key=key)
    assert response.status_code == 202
    assert response.json()["task_id"] == str(lost.id)
    assert [sent.id for sent in celery.sent] == [lost.id]


def test_a_retry_of_a_finished_task_is_not_enqueued_again(user, celery):
    key = uuid4()
    task_id = submit(user, key=key).json()["task_id"]
    user.delete(f"/api/tasks/{task_id}")

    response = submit(user, key=key)
    assert response.status_code == 202
    assert response.json()["status"] == "cancelled"
    assert len(celery.sent) == 1


@pytest.mark.parametrize("headers", [{}, {"Idempotency-Key": "not-a-uuid"}, {"Idempotency-Key": ""}])
def test_missing_or_malformed_idempotency_key_is_400(user, headers, celery):
    response = user.post("/api/tasks", json={"prompt": "a cat", "size": 256}, headers=headers)
    assert response.status_code == 400
    assert "Idempotency-Key" in response.json()["detail"]
    assert tasks_of(user_id_of(user)) == []
    assert celery.sent == []


def test_enqueue_failure_fails_the_task_and_answers_503(user, celery):
    celery.fail = True
    response = submit(user)
    assert response.status_code == 503
    assert response.json() == {"detail": "The queue is unavailable, try again shortly"}

    [row] = tasks_of(user_id_of(user))
    assert row.status == "failed"
    assert row.error_message == "queue unavailable"
    shown = user.get(f"/api/tasks/{row.id}").json()
    assert shown["status"] == "failed"
    assert shown["error_message"] == "queue unavailable"

    celery.fail = False
    assert submit(user).status_code == 202  # the failed task doesn't count against the cap


@pytest.mark.parametrize(
    "prompt, size, message",
    [
        ("a cat", 100, "Size must be one of (256, 384, 512, 640, 768)"),
        ("   ", 256, "The prompt is empty"),
        ("a" * 501, 256, "The prompt is longer than 500 characters"),
    ],
)
def test_invalid_submit_is_422_with_the_reason(user, prompt, size, message, celery):
    response = submit(user, prompt=prompt, size=size)
    assert response.status_code == 422
    assert response.json() == {"detail": message}
    assert tasks_of(user_id_of(user)) == []
    assert celery.sent == []


# --- Authorisation ------------------------------------------------------------------------------------------

TASK_ID = "00000000-0000-4000-8000-000000000000"


@pytest.mark.parametrize(
    "method, path",
    [
        ("GET", "/api/status"),
        ("POST", "/api/tasks"),
        ("GET", "/api/tasks/active"),
        ("GET", "/api/tasks"),
        ("GET", f"/api/tasks/{TASK_ID}"),
        ("GET", f"/api/tasks/{TASK_ID}/image"),
        ("DELETE", f"/api/tasks/{TASK_ID}"),
        ("DELETE", f"/api/admin/tasks/{TASK_ID}"),
    ],
)
def test_without_a_cookie_every_task_endpoint_is_401(client, method, path):
    kwargs = {"json": {"prompt": "a cat"}, "headers": {"Idempotency-Key": str(uuid4())}}
    response = client.request(method, path, **(kwargs if method == "POST" else {}))
    assert response.status_code == 401


def test_another_users_task_looks_missing(user, other, celery):
    task_id = submit(other).json()["task_id"]
    assert user.get(f"/api/tasks/{task_id}").status_code == 404
    assert user.get(f"/api/tasks/{task_id}/image").status_code == 404
    assert user.delete(f"/api/tasks/{task_id}").status_code == 404
    assert db.get_task(task_id).status == "queued"
    assert celery.revoked == []


# --- Status, active task, one task --------------------------------------------------------------------------


def test_status_counts_match_the_rows(user):
    queued_task()
    queued_task()
    running_task()
    finished_rows(new_user_id(), 2)

    response = user.get("/api/status")
    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "system"
    assert (body["running"], body["waiting"]) == (1, 2)
    assert body["new_image_ready_in_s"] > 0


def test_active_task_is_the_queued_one_then_null_after_cancel(user):
    assert user.get("/api/tasks/active").json() is None
    task_id = submit(user).json()["task_id"]

    active = user.get("/api/tasks/active")
    assert active.status_code == 200
    assert active.json()["task_id"] == task_id
    assert active.json()["status"] == "queued"

    assert user.delete(f"/api/tasks/{task_id}").json() == {"status": "cancelled"}
    assert user.get("/api/tasks/active").json() is None


def test_a_running_task_shows_its_progress_from_redis(user):
    task = running_task(user_id_of(user))
    live.publish_progress(task.user_id, task.id, progress=36, stage="step 10 of 25")

    body = user.get(f"/api/tasks/{task.id}").json()
    assert body["status"] == "running"
    assert (body["progress"], body["stage"]) == (36, "step 10 of 25")
    assert body["position"] is None
    assert body["ready_in_s"] > 0


def test_a_finished_task_has_no_live_fields(user):
    finished_rows(user_id_of(user), 1)
    [item] = user.get("/api/tasks").json()["items"]
    assert item["status"] == "done"
    for field in ("position", "queue_length", "progress", "stage", "ready_in_s", "error_message"):
        assert item[field] is None


# --- History ------------------------------------------------------------------------------------------------


def test_history_pages_newest_first(user):
    finished_rows(user_id_of(user), 25)

    first = user.get("/api/tasks").json()
    assert len(first["items"]) == 20
    created = [item["created_at"] for item in first["items"]]
    assert created == sorted(created, reverse=True)
    assert first["items"][0]["prompt"] == "picture 0"
    assert first["next_before"] is not None

    second = user.get("/api/tasks", params={"before": first["next_before"]}).json()
    assert [item["prompt"] for item in second["items"]] == [f"picture {i}" for i in range(20, 25)]
    assert second["next_before"] is None


def test_history_limit_is_capped(user):
    finished_rows(user_id_of(user), 55)
    page = user.get("/api/tasks", params={"limit": 100}).json()
    assert len(page["items"]) == 50
    assert page["next_before"] is not None
    assert len(user.get("/api/tasks", params={"limit": 3}).json()["items"]) == 3


def test_history_shows_only_your_own_tasks(user, other):
    finished_rows(user_id_of(other), 3)
    assert user.get("/api/tasks").json() == {"items": [], "next_before": None}


# --- Image --------------------------------------------------------------------------------------------------


def test_image_is_404_until_done_then_streams_the_stored_bytes(user, bucket):
    task_id = submit(user).json()["task_id"]
    assert user.get(f"/api/tasks/{task_id}/image").status_code == 404

    png = b"\x89PNG\r\n\x1a\n" + os.urandom(3 * get_settings().S3_STREAM_CHUNK_BYTES)
    key = f"results/{task_id}.png"
    storage.put_png(key, png)
    assert db.claim_task(task_id, worker_host="nodeB-worker-1")
    assert user.get(f"/api/tasks/{task_id}/image").status_code == 404  # running
    assert db.finish_task(task_id, "done", result_key=key)

    response = user.get(f"/api/tasks/{task_id}/image")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "private, max-age=86400"
    assert response.content == png


def test_image_missing_from_storage_is_404(user, bucket):
    task = running_task(user_id_of(user))
    assert db.finish_task(task.id, "done", result_key=f"results/{task.id}.png")
    assert user.get(f"/api/tasks/{task.id}/image").status_code == 404


# --- Cancel -------------------------------------------------------------------------------------------------


def test_cancel_a_queued_task(user, celery, redis_live):
    task_id = submit(user).json()["task_id"]
    channel = redis_live.pubsub(ignore_subscribe_messages=True)
    channel.subscribe(live.user_channel(user_id_of(user)))
    channel.get_message(timeout=1)  # the subscribe confirmation

    response = user.delete(f"/api/tasks/{task_id}")
    assert response.status_code == 200
    assert response.json() == {"status": "cancelled"}

    row = db.get_task(task_id)
    assert row.status == "cancelled"
    assert row.finished_at is not None
    assert live.cancel_requested(task_id)
    assert redis_live.ttl(live.cancel_key(task_id)) > 0
    assert celery.revoked == [(task_id, False)]
    message = channel.get_message(timeout=2)
    assert message is not None
    assert message["data"] == f'{{"type":"task_final","id":"{task_id}","status":"cancelled"}}'
    channel.close()

    again = user.delete(f"/api/tasks/{task_id}")
    assert again.json() == {"status": "cancelled"}
    assert len(celery.revoked) == 1


def test_cancel_a_running_task(user, celery):
    task = running_task(user_id_of(user))
    assert user.delete(f"/api/tasks/{task.id}").json() == {"status": "cancelled"}
    assert db.get_task(task.id).status == "cancelled"
    assert live.cancel_requested(task.id)
    assert not db.finish_task(task.id, "done", result_key="results/x.png")  # the worker's finish is refused


def test_cancelling_a_done_task_changes_nothing(user, celery):
    task = running_task(user_id_of(user))
    assert db.finish_task(task.id, "done", result_key=f"results/{task.id}.png")
    before = db.get_task(task.id)

    response = user.delete(f"/api/tasks/{task.id}")
    assert response.status_code == 200
    assert response.json() == {"status": "done"}
    assert db.get_task(task.id) == before
    assert not live.cancel_requested(task.id)
    assert celery.revoked == []


def test_cancel_still_succeeds_when_the_workers_cannot_be_told(user, celery, monkeypatch):
    def broker_down(task_id, terminate=False):
        raise ConnectionError("broker unreachable")

    monkeypatch.setattr(celery_client, "revoke", broker_down)
    task_id = submit(user).json()["task_id"]
    assert user.delete(f"/api/tasks/{task_id}").json() == {"status": "cancelled"}
    assert db.get_task(task_id).status == "cancelled"


def test_admin_can_cancel_another_users_task(admin, other, celery):
    task_id = submit(other).json()["task_id"]
    response = admin.delete(f"/api/admin/tasks/{task_id}")
    assert response.status_code == 200
    assert response.json() == {"status": "cancelled"}
    assert db.get_task(task_id).status == "cancelled"
    assert celery.revoked == [(task_id, False)]
    assert admin.delete(f"/api/admin/tasks/{uuid4()}").status_code == 404


def test_admin_cancel_is_403_for_a_normal_user(user, other):
    task_id = submit(other).json()["task_id"]
    assert user.delete(f"/api/admin/tasks/{task_id}").status_code == 403
    assert db.get_task(task_id).status == "queued"


# --- Concurrency --------------------------------------------------------------------------------------------


def test_concurrent_submits_across_instances_respect_the_cap(make_client, celery):
    api_a = sign_up(make_client())
    api_b = make_client()
    api_b.cookies = api_a.cookies

    def one_submit(i):
        return submit(api_a if i % 2 else api_b).status_code

    with ThreadPoolExecutor(10) as pool:
        codes = list(pool.map(one_submit, range(10)))

    assert codes.count(202) == 1
    assert codes.count(429) == 9
    stmt = select(func.count()).select_from(db.tasks).where(db.tasks.c.user_id == user_id_of(api_a))
    with db.transaction() as tx:
        assert tx.execute(stmt).scalar_one() == 1
    assert len(celery.sent) == 1
