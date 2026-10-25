"""Real worker containers with the fake pipeline (1 s per step, 25 steps), driven through the API servers."""

import pytest

from common import live
from tests.stack import receive

pytestmark = pytest.mark.integration


def result_key(task_id) -> str:
    return f"results/{task_id}.png"


def test_full_lifecycle(stack, client):
    with client.websocket(str(client.http.base_url)) as socket:
        assert receive(socket)["type"] == "snapshot"
        t = client.submit("a cat", size=256)
        progress, final = [], None
        while final is None:
            message = receive(socket, timeout=60)
            if message["type"] == "task" and message["id"] == t and "progress" in message:
                progress.append(message["progress"])
            elif message["type"] == "task_final" and message["id"] == t:
                final = message["status"]

    assert final == "done"
    assert progress == sorted(progress)
    assert progress[0] == 0 and progress[-1] == 95 and len(set(progress)) > 25
    task = client.task(t)
    assert task["status"] == "done" and task["started_at"] and task["finished_at"]
    image = client.image(t)
    assert image.status_code == 200
    assert image.content.startswith(b"\x89PNG")
    assert stack.storage.exists(result_key(t))


def test_mid_run_cancel_cleans_up(stack, client):
    t = client.submit("a cat", size=256)
    stack.wait_progress(t, at_least=20)
    client.cancel(t)
    stack.wait_worker_idle()
    assert client.task(t)["status"] == "cancelled"
    assert not stack.storage.exists(result_key(t))
    assert not stack.redis.exists(live.cancel_key(t), live.progress_key(t))


def test_cancel_during_last_step_stays_cancelled(stack, client):
    t = client.submit("a cat", size=256)
    stack.wait_progress(t, at_least=85)
    client.cancel(t)
    stack.wait_worker_idle()
    assert client.task(t)["status"] == "cancelled"
    assert not stack.storage.exists(result_key(t))


def test_task_cancelled_while_queued_never_runs_on_a_new_worker(stack, client):
    stack.scale_workers(0)  # nothing consumes yet
    t = client.submit("a cat", size=256)
    client.cancel(t)
    stack.scale_workers(1)  # a worker that never heard the revoke
    stack.wait_worker_log(f"[{t}] succeeded")  # it did take the message
    stack.wait_worker_idle()
    task = stack.db.task(t)
    assert task.status == "cancelled"
    assert task.started_at is None and task.worker_host is None


def test_soft_time_limit_marks_failed(stack, client):
    # The measured runtime must stay below the soft limit, or the settings refuse to load.
    stack.restart_workers(
        env={"MEASURED_MAX_RUNTIME_S": "2", "TASK_SOFT_TIME_LIMIT": "3", "TASK_TIME_LIMIT": "10"}
    )
    t = client.submit("a cat", size=768)
    stack.wait_status(t, "failed")
    assert "time limit" in client.task(t)["error_message"]
    stack.wait_worker_idle()
    assert not stack.storage.exists(result_key(t))


def test_killed_worker_task_is_marked_failed(stack, client):
    t = client.submit("a cat", size=256)
    stack.wait_status(t, "running")
    stack.kill_worker_running(t)  # SIGKILL: no finally block runs
    stack.wait_status(t, "failed", timeout=90)  # the sweeper notices
    assert client.task(t)["error_message"] == "the worker stopped unexpectedly"


def test_stopping_a_busy_worker_lets_its_job_finish(stack, client):
    t = client.submit("a cat", size=256)
    stack.wait_status(t, "running")
    host = stack.db.task(t).worker_host
    stack.stop_worker_running(t)  # SIGTERM with the grace period, as on scale-down
    stack.wait_status(t, "done")
    assert client.image(t).status_code == 200
    stack.workers = 1
    stack.wait_worker_idle()
    assert host not in stack.worker_hosts()  # it exited once the job was done


def test_tasks_spread_across_workers(stack, clients):
    ids = [c.submit("a cat", size=256) for c in clients[:6]]  # six users, one task each
    stack.wait_all(ids, "done", timeout=180)
    assert len({stack.db.task(t).worker_host for t in ids}) == 2


def test_redis_restart_does_not_lose_queued_tasks(stack, clients):
    ids = [c.submit("a cat", size=256) for c in clients[:4]]
    stack.wait_all(ids[:2], "running")
    stack.restart_redis(keep_data=False)  # worst case: queue gone
    stack.wait_all(ids, "done", timeout=180)  # the controller re-sent them
