import itertools
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError

from common import db

pytestmark = pytest.mark.api


def make_user(email=None):
    return db.create_user(email or f"{uuid4()}@example.com", "argon2-hash")


# Each test task is created one second after the previous one, so ordering by created_at never ties.
CREATED_BASE = datetime.now(UTC) - timedelta(days=1)
_ticks = itertools.count()


def make_task(user, size=256, key=None):
    stmt = (
        insert(db.tasks)
        .values(
            user_id=user.id,
            status="queued",
            prompt="a cat",
            size=size,
            idempotency_key=key or uuid4(),
            created_at=CREATED_BASE + timedelta(seconds=next(_ticks)),
        )
        .returning(*db.tasks.c)
    )
    with db.transaction() as tx:
        return tx.execute(stmt).one()


def status(task):
    return db.get_task(task.id).status


# --- Guarded status changes ---


def test_claim_succeeds_once():
    task = make_task(make_user())
    assert db.claim_task(task.id, worker_host="nodeB-worker-1") is True
    assert db.claim_task(task.id, worker_host="nodeC-worker-1") is False
    row = db.get_task(task.id)
    assert (row.status, row.worker_host) == ("running", "nodeB-worker-1")
    assert row.started_at is not None


def test_claim_accepts_string_ids():
    task = make_task(make_user())
    assert db.claim_task(str(task.id), worker_host="w") is True


def test_finish_works_only_from_running():
    task = make_task(make_user())
    assert db.finish_task(task.id, "done", result_key="results/x.png") is False
    assert status(task) == "queued"
    db.claim_task(task.id, worker_host="w")
    assert db.finish_task(task.id, "done", result_key="results/x.png") is True
    row = db.get_task(task.id)
    assert (row.status, row.result_key) == ("done", "results/x.png")
    assert row.finished_at is not None
    assert db.finish_task(task.id, "failed", error="late") is False
    assert status(task) == "done"


def test_finish_after_cancel_keeps_cancelled():
    user = make_user()
    task = make_task(user)
    db.claim_task(task.id, worker_host="w")
    assert db.cancel_task(task.id, owner_id=user.id) == user.id
    assert db.finish_task(task.id, "done", result_key="results/x.png") is False
    row = db.get_task(task.id)
    assert (row.status, row.result_key) == ("cancelled", None)


def test_finish_refuses_other_target_statuses():
    task = make_task(make_user())
    with pytest.raises(ValueError):
        db.finish_task(task.id, "cancelled")


def test_failed_records_the_error():
    task = make_task(make_user())
    db.claim_task(task.id, worker_host="w")
    assert db.finish_task(task.id, "failed", error="time limit exceeded") is True
    assert db.get_task(task.id).error_message == "time limit exceeded"


def test_cancel_with_wrong_owner_changes_nothing():
    owner, other = make_user(), make_user()
    task = make_task(owner)
    assert db.cancel_task(task.id, owner_id=other.id) is None
    assert status(task) == "queued"


def test_cancel_without_owner_works_for_any_owner():
    owner = make_user()
    queued, running = make_task(owner), make_task(make_user())
    db.claim_task(running.id, worker_host="w")
    assert db.cancel_task(queued.id, owner_id=None) == owner.id
    assert db.cancel_task(running.id, owner_id=None) == running.user_id
    assert status(queued) == status(running) == "cancelled"
    assert db.get_task(queued.id).finished_at is not None


def test_cancel_on_finished_task_returns_none():
    user = make_user()
    task = make_task(user)
    db.claim_task(task.id, worker_host="w")
    db.finish_task(task.id, "done", result_key="k")
    assert db.cancel_task(task.id, owner_id=user.id) is None
    assert db.cancel_task(task.id, owner_id=None) is None
    assert status(task) == "done"


def test_fail_queued_only_from_queued():
    queued, running = make_task(make_user()), make_task(make_user())
    db.claim_task(running.id, worker_host="w")
    assert db.fail_queued(queued.id, "queue unavailable") is True
    assert db.fail_queued(running.id, "queue unavailable") is False
    row = db.get_task(queued.id)
    assert (row.status, row.error_message) == ("failed", "queue unavailable")
    assert status(running) == "running"


# --- Constraints ---


def test_size_check_rejects_500():
    with pytest.raises(IntegrityError):
        make_task(make_user(), size=500)


def test_idempotency_key_unique_per_user():
    key = uuid4()
    first, second = make_user(), make_user()
    make_task(first, key=key)
    make_task(second, key=key)  # another user may reuse it
    with pytest.raises(IntegrityError):
        make_task(first, key=key)


def test_email_unique_case_insensitively():
    make_user("Cat@Example.com")
    with pytest.raises(IntegrityError):
        make_user("cat@example.COM")


def test_role_check():
    with pytest.raises(IntegrityError):
        db.create_user("a@example.com", "h", role="root")


# --- Queue reads ---


def test_queue_follows_created_at():
    user = make_user()
    t1, t2, t3 = make_task(user), make_task(make_user()), make_task(make_user())
    assert [t.id for t in db.queued_tasks()] == [t1.id, t2.id, t3.id]
    assert [db.queue_position(t) for t in (t1, t2, t3)] == [1, 2, 3]

    db.claim_task(t1.id, worker_host="w")
    assert db.queue_position(t1) is None
    assert [db.queue_position(t) for t in (t2, t3)] == [1, 2]


def test_queued_tasks_created_before_filters():
    t1, t2, t3 = (make_task(make_user()) for _ in range(3))
    assert [t.id for t in db.queued_tasks(created_before=t3.created_at)] == [t1.id, t2.id]
    assert [t.id for t in db.queued_tasks(created_before=t1.created_at)] == []
    assert len(db.queued_tasks(created_before=time.time() + 60)) == 3


def test_running_tasks_started_before_filters():
    task = make_task(make_user())
    db.claim_task(task.id, worker_host="w")
    assert [t.id for t in db.running_tasks()] == [task.id]
    assert [t.id for t in db.running_tasks(started_before=time.time() + 60)] == [task.id]
    assert db.running_tasks(started_before=time.time() - 3600) == []


def test_counts_and_active_tasks():
    queued, running, done = (make_task(make_user()) for _ in range(3))
    for t in (running, done):
        db.claim_task(t.id, worker_host="w")
    db.finish_task(done.id, "done", result_key="k")
    assert (db.count_tasks("queued"), db.count_tasks("running"), db.count_tasks("done")) == (1, 1, 1)
    assert [t.id for t in db.active_tasks()] == [queued.id, running.id]
    assert [t.id for t in db.recent_done_tasks(limit=50)] == [done.id]


def test_submit_helpers_in_one_transaction():
    user = make_user()
    key = uuid4()
    with db.transaction() as tx:
        db.advisory_lock(tx)
        assert db.find_by_idempotency_key(tx, user.id, key) is None
        task = db.insert_task(tx, user.id, "a cat", 512, key)
        assert db.find_by_idempotency_key(tx, user.id, key).id == task.id
        assert db.count_active_for_user(tx, user.id) == 1
        assert db.count_queued(tx) == 1
    assert task.status == "queued"


# --- History ---


def test_get_task_hides_other_users_tasks():
    owner, other = make_user(), make_user()
    task = make_task(owner)
    assert db.get_task(task.id, user_id=owner.id).id == task.id
    assert db.get_task(task.id, user_id=other.id) is None


def test_history_page_newest_first_with_cursor():
    user = make_user()
    tasks = [make_task(user) for _ in range(5)]
    make_task(make_user())  # someone else's
    first = db.history_page(user.id, limit=3)
    assert [t.id for t in first] == [t.id for t in reversed(tasks)][:3]
    second = db.history_page(user.id, before=first[-1].created_at, limit=3)
    assert [t.id for t in second] == [tasks[1].id, tasks[0].id]


def test_tasks_beyond_history_limit_skips_active_tasks():
    heavy, light = make_user(), make_user()
    oldest_running = make_task(heavy)
    db.claim_task(oldest_running.id, worker_host="w")
    finished = []
    for _ in range(52):
        task = make_task(heavy)
        db.cancel_task(task.id, owner_id=None)
        finished.append(task)
    newest_queued = make_task(heavy)
    for _ in range(3):
        db.cancel_task(make_task(light).id, owner_id=None)

    beyond = db.tasks_beyond_history_limit(50)
    assert {t.id for t in beyond} == {finished[0].id, finished[1].id}
    assert oldest_running.id not in {t.id for t in beyond}
    assert newest_queued.id not in {t.id for t in beyond}


def test_delete_task():
    task = make_task(make_user())
    assert db.delete_task(task.id) is True
    assert db.get_task(task.id) is None
    assert db.delete_task(task.id) is False


# --- Users and refresh tokens ---


def test_users():
    user = make_user("Someone@Example.com")
    assert user.role == "user"
    assert db.get_user(user.id).email == "Someone@Example.com"
    assert db.get_user_by_email("someone@example.COM").id == user.id
    assert db.get_user_by_email("nobody@example.com") is None
    assert db.set_role(user.id, "admin") is True
    assert db.get_user(user.id).role == "admin"


def test_refresh_tokens():
    user = make_user()
    expires = datetime.now(UTC) + timedelta(days=7)
    db.insert_refresh_token(user.id, "hash-1", expires)
    db.insert_refresh_token(user.id, "hash-2", expires)
    assert db.get_refresh_token("hash-1").expires_at == expires
    assert db.revoke_refresh_token("hash-1") is True
    assert db.revoke_refresh_token("hash-1") is False
    assert db.get_refresh_token("hash-1").revoked_at is not None
    assert db.revoke_all_refresh_tokens(user.id) == 1
    assert db.get_refresh_token("missing") is None
