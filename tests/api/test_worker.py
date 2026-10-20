"""A real Celery worker with the fake pipeline, driven through the API."""

import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

from common import celery_client, db, live, storage
from common.config import get_settings

pytestmark = pytest.mark.api

ROOT = Path(__file__).resolve().parents[2]
PASSWORD = "correct horse battery"


def wait_for(condition, timeout, what):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return
        time.sleep(0.1)
    raise AssertionError(f"timed out after {timeout} s waiting for {what}")


def worker_idle(host):
    hb = next((hb for hb in live.worker_heartbeats() if hb.host == host), None)
    return hb is not None and hb.state == "idle" and hb.task_id is None


@pytest.fixture
def worker(migrated_db, redis_live, bucket, redis_url, monkeypatch, tmp_path):
    """One worker process on the test containers; yields its name."""
    monkeypatch.setenv("BROKER_URL", redis_url)
    get_settings.cache_clear()
    celery_client.reset_app()
    host = f"test-worker-{uuid4().hex[:6]}"
    env = {
        **os.environ,
        "DATABASE_URL": migrated_db,
        "REDIS_URL": redis_url,
        "BROKER_URL": redis_url,
        "WORKER_NAME": host,
        "PIPELINE": "fake",
        "FAKE_STEP_SECONDS": "0.2",
        "STEP_DELAY_SECONDS": "0",
        "NUM_STEPS": "25",
    }
    log_path = tmp_path / "worker.log"
    with open(log_path, "w") as log_file:
        proc = subprocess.Popen(
            [sys.executable, "-m", "celery", "-A", "worker.app:celery_app", "worker",
             "--pool=prefork", "--concurrency=1", "-Q", live.GENERATION_QUEUE,
             "--without-gossip", "--without-mingle", "--loglevel=info"],
            cwd=ROOT, env=env, stdout=log_file, stderr=subprocess.STDOUT,
        )  # fmt: skip
        try:
            wait_for(lambda: worker_idle(host), 30, "the worker to load")
            yield host
        except BaseException:
            print(log_path.read_text())
            raise
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            celery_client.reset_app()


@pytest.fixture
def user(make_client, worker):
    client = make_client()
    response = client.post(
        "/api/auth/register", json={"email": f"{uuid4()}@example.com", "password": PASSWORD}
    )
    assert response.status_code == 201
    return client


def submit(client):
    response = client.post(
        "/api/tasks", json={"prompt": "a cat", "size": 256}, headers={"Idempotency-Key": str(uuid4())}
    )
    assert response.status_code == 202, response.text
    return response.json()["task_id"]


def status_of(task_id):
    return db.get_task(task_id).status


def test_submitted_task_is_generated_and_stored(user, worker):
    task_id = submit(user)
    wait_for(lambda: status_of(task_id) == "done", 30, "the task to finish")
    task = db.get_task(task_id)
    assert task.worker_host == worker
    assert task.result_key == f"results/{task_id}.png"
    assert storage.exists(task.result_key)
    assert user.get(f"/api/tasks/{task_id}/image").status_code == 200


def test_cancel_mid_run_cleans_up(user, worker):
    task_id = submit(user)
    wait_for(lambda: live.progress(task_id) >= 20, 30, "progress to reach 20 %")
    assert user.delete(f"/api/tasks/{task_id}").json()["status"] == "cancelled"
    wait_for(lambda: worker_idle(worker), 10, "the worker to go idle")
    assert status_of(task_id) == "cancelled"
    assert not storage.exists(f"results/{task_id}.png")
    assert not live.client().exists(live.progress_key(task_id), live.cancel_key(task_id))
