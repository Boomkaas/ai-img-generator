"""The generation task called directly, with db, live and storage faked and a fake pipeline."""

import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy.exc import OperationalError

from common import db
from worker import tasks
from worker.pipeline import FakePipeline

pytestmark = pytest.mark.unit


class FakeDb:
    DONE, FAILED, FINISHED_STATUSES = db.DONE, db.FAILED, db.FINISHED_STATUSES

    def __init__(self):
        self.claim = True
        self.claim_errors = 0  # this many claims fail as if the database were unreachable
        self.finish = True
        self.status = db.QUEUED
        self.claims, self.finishes = [], []

    def claim_task(self, task_id, worker_host):
        self.claims.append((task_id, worker_host))
        if self.claim_errors:
            self.claim_errors -= 1
            raise OperationalError("claim", {}, ConnectionError("connection refused"))
        if self.claim:
            self.status = db.RUNNING
        return self.claim

    def finish_task(self, task_id, status, result_key=None, error=None):
        self.finishes.append({"status": status, "result_key": result_key, "error": error})
        if not self.finish:
            self.status = db.CANCELLED  # a cancel won the race
            return False
        self.status = status
        return True

    def get_task(self, task_id):
        return SimpleNamespace(id=task_id, status=self.status)


class FakeLive:
    def __init__(self, fake_db):
        self.db = fake_db
        self.progress = []  # (progress, stage)
        self.states = []
        self.cleared = []
        self.finals = []
        self.flag = threading.Event()
        self.cancel_after_stage = None

    def cancel(self):
        """What the API does: the status first, then the flag."""
        self.db.status = db.CANCELLED
        self.flag.set()

    def publish_progress(self, user_id, task_id, progress, stage):
        self.progress.append((progress, stage))
        if stage == self.cancel_after_stage:
            self.cancel()

    def cancel_requested(self, task_id):
        return self.flag.is_set()

    def set_worker_state(self, host, state, task_id=None):
        self.states.append((state, task_id))

    def clear_task_keys(self, task_id):
        self.cleared.append(task_id)

    def publish_task_final(self, user_id, task_id, status):
        self.finals.append(status)


class FakeStorage:
    def __init__(self):
        self.objects = {}
        self.deleted = []

    def put_png(self, key, image):
        self.objects[key] = image

    def delete(self, key):
        self.deleted.append(key)
        self.objects.pop(key, None)


class RaisingPipeline:
    def __init__(self, exc):
        self.exc = exc

    def __call__(self, **_):
        raise self.exc


@pytest.fixture
def fake_db(monkeypatch):
    fake = FakeDb()
    monkeypatch.setattr(tasks, "db", fake)
    return fake


@pytest.fixture
def fake_live(monkeypatch, fake_db):
    fake = FakeLive(fake_db)
    monkeypatch.setattr(tasks, "live", fake)
    return fake


@pytest.fixture
def fake_storage(monkeypatch):
    fake = FakeStorage()
    monkeypatch.setattr(tasks, "storage", fake)
    return fake


@pytest.fixture(autouse=True)
def worker(monkeypatch, fake_db, fake_live, fake_storage):
    monkeypatch.setattr(tasks.settings, "NUM_STEPS", 25)
    monkeypatch.setattr(tasks.settings, "STEP_DELAY_SECONDS", 0)
    monkeypatch.setattr(tasks, "pipe", FakePipeline(step_seconds=0))


@pytest.fixture
def task_id():
    return str(uuid4())


def run(task_id, size=256):
    tasks.generate_image(task_id, str(uuid4()), "a cat", size)


def assert_cleaned_up(fake_live, task_id, final):
    assert fake_live.cleared == [task_id]
    assert fake_live.states[-1] == ("idle", None)
    assert fake_live.finals == [final]


def test_refused_claim_never_runs_the_pipeline(monkeypatch, fake_db, fake_live, fake_storage, task_id):
    fake_db.claim = False
    monkeypatch.setattr(tasks, "pipe", RaisingPipeline(AssertionError("the pipeline ran")))
    run(task_id)
    assert fake_db.claims == [(task_id, tasks.HOST)]
    assert fake_db.finishes == []
    assert fake_live.progress == [] and fake_live.states == [] and fake_live.finals == []
    assert fake_storage.objects == {}


def test_success_stores_the_image_and_finishes_done(fake_db, fake_live, fake_storage, task_id):
    run(task_id, size=384)
    key = f"results/{task_id}.png"
    assert fake_db.finishes == [{"status": "done", "result_key": key, "error": None}]
    assert fake_storage.objects[key].size == (384, 384)
    steps = [(n * 90 // 25, f"step {n} of 25") for n in range(1, 25)]
    assert fake_live.progress == [(0, "starting"), *steps, (90, "decoding image"), (95, "saving")]
    assert fake_live.states[0] == ("busy", task_id)
    assert_cleaned_up(fake_live, task_id, "done")


def test_refused_finish_deletes_the_image(fake_db, fake_live, fake_storage, task_id):
    fake_db.finish = False
    run(task_id)
    key = f"results/{task_id}.png"
    assert fake_storage.deleted == [key]
    assert fake_storage.objects == {}
    assert_cleaned_up(fake_live, task_id, "cancelled")


def test_cancel_stops_at_the_next_step(fake_db, fake_live, fake_storage, task_id):
    fake_live.cancel_after_stage = "step 4 of 25"
    run(task_id)
    assert fake_live.progress[-1][1] in ("step 4 of 25", "step 5 of 25")
    assert fake_db.finishes == []
    assert fake_storage.objects == {}
    assert_cleaned_up(fake_live, task_id, "cancelled")


def test_cancel_during_the_step_delay_stops_within_half_a_second(monkeypatch, fake_live, task_id):
    monkeypatch.setattr(tasks.settings, "STEP_DELAY_SECONDS", 2)
    cancelled_at = []

    def cancel_soon():
        time.sleep(0.3)
        cancelled_at.append(time.monotonic())
        fake_live.cancel()

    canceller = threading.Thread(target=cancel_soon)
    canceller.start()
    run(task_id)
    stopped_at = time.monotonic()
    canceller.join()
    assert stopped_at - cancelled_at[0] < 1.0  # well before the 2 s pause would have ended
    assert fake_live.progress[-1] == (3, "step 1 of 25")
    assert_cleaned_up(fake_live, task_id, "cancelled")


def test_soft_time_limit_fails_the_task(monkeypatch, fake_db, fake_live, task_id):
    monkeypatch.setattr(tasks, "pipe", RaisingPipeline(SoftTimeLimitExceeded()))
    run(task_id)
    assert fake_db.finishes == [{"status": "failed", "result_key": None, "error": "time limit exceeded"}]
    assert_cleaned_up(fake_live, task_id, "failed")


def test_an_error_fails_the_task_with_a_shortened_message(monkeypatch, fake_db, fake_live, task_id):
    monkeypatch.setattr(tasks, "pipe", RaisingPipeline(RuntimeError("x" * 2000)))
    run(task_id)
    assert fake_db.finishes == [{"status": "failed", "result_key": None, "error": "x" * 500}]
    assert_cleaned_up(fake_live, task_id, "failed")


def test_database_outage_at_the_claim_is_retried(fake_db, fake_live, fake_storage, task_id):
    fake_db.claim_errors = 1
    # apply() runs the retry in place instead of sending it back to the broker.
    result = tasks.generate_image.apply(args=[task_id, str(uuid4()), "a cat", 256])
    assert result.successful()
    assert len(fake_db.claims) == 2
    assert fake_db.finishes[-1]["status"] == "done"
    assert_cleaned_up(fake_live, task_id, "done")


def test_retries_stop_at_the_configured_limit(monkeypatch, fake_db, task_id):
    monkeypatch.setattr(tasks.generate_image, "max_retries", 2)
    fake_db.claim_errors = 10
    result = tasks.generate_image.apply(args=[task_id, str(uuid4()), "a cat", 256])
    assert isinstance(result.result, OperationalError)
    assert len(fake_db.claims) == 3


def test_retry_policy_comes_from_the_config():
    task = tasks.generate_image
    assert task.autoretry_for == (OperationalError,)
    assert task.max_retries == tasks.settings.WORKER_DB_MAX_RETRIES
    assert task.retry_backoff_max == tasks.settings.WORKER_DB_RETRY_BACKOFF_MAX_S


def test_redis_failing_at_cleanup_still_leaves_the_worker_idle(monkeypatch, fake_db, fake_live, task_id):
    def unreachable(task_id):
        raise ConnectionError("redis unreachable")

    monkeypatch.setattr(fake_live, "clear_task_keys", unreachable)
    run(task_id)
    assert fake_live.states[-1] == ("idle", None)
    assert fake_live.finals == ["done"]


def test_pipeline_module_imports_without_torch():
    # A fresh interpreter where importing torch or diffusers fails, whether or not they are installed.
    code = (
        "import sys\n"
        "sys.modules['torch'] = None\n"
        "sys.modules['diffusers'] = None\n"
        "from worker.pipeline import FakePipeline\n"
        "FakePipeline(0)('a cat', 2, 64, 64, lambda *a: None)\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
