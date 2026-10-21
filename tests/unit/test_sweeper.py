from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from common import db
from common.config import get_settings
from controller import main, sweeper
from controller.main import ControllerState

pytestmark = pytest.mark.unit

NOW = 1_800_000_000.0  # the fixed clock every tick here runs on
RUN_ID = "run-1"


def at(offset: float) -> datetime:
    return datetime.fromtimestamp(NOW + offset, UTC)


def task(status, started=None, finished=None, created=0.0, result_key=None):
    return SimpleNamespace(
        id=uuid4(),
        user_id=uuid4(),
        status=status,
        size=512,
        prompt="a fox",
        created_at=at(created),
        started_at=None if started is None else at(started),
        finished_at=None if finished is None else at(finished),
        result_key=result_key,
    )


def heartbeat(task_id, state="busy"):
    return SimpleNamespace(host=f"nodeB-worker-{uuid4().hex[:4]}", state=state, task_id=str(task_id))


class FakeDb:
    QUEUED, RUNNING, FAILED, CANCELLED = db.QUEUED, db.RUNNING, db.FAILED, db.CANCELLED

    def __init__(self, log):
        self.tasks = []
        self.beyond_limit = []
        self.finish_result = True
        self.finished = []
        self.log = log

    def running_tasks(self, started_before=None):
        cutoff = datetime.fromtimestamp(started_before, UTC)
        return [t for t in self.tasks if t.status == db.RUNNING and t.started_at < cutoff]

    def queued_tasks(self):
        return [t for t in self.tasks if t.status == db.QUEUED]

    def get_task(self, task_id):
        return next((t for t in self.tasks if str(t.id) == str(task_id)), None)

    def finish_task(self, task_id, status, error=None):
        self.finished.append((task_id, status, error))
        return self.finish_result

    def tasks_beyond_history_limit(self, limit):
        self.log.append(("query", limit))
        return list(self.beyond_limit)

    def delete_task(self, task_id):
        self.log.append(("row", task_id))
        self.tasks = [t for t in self.tasks if t.id != task_id]


class FakeLive:
    def __init__(self):
        self.run_id = RUN_ID
        self.heartbeats = []
        self.finals = []
        self.cleared = []

    def redis_run_id(self):
        return self.run_id

    def worker_heartbeats(self):
        return list(self.heartbeats)

    def publish_task_final(self, user_id, task_id, status):
        self.finals.append((user_id, task_id, status))

    def clear_task_keys(self, task_id):
        self.cleared.append(task_id)


class FakeCelery:
    def __init__(self):
        self.sent = []
        self.revoked = []

    def send_generate(self, t):
        self.sent.append(t.id)

    def revoke(self, task_id, terminate=False):
        self.revoked.append((task_id, terminate))


class FakeStorage:
    def __init__(self, log):
        self.log = log
        self.broken = set()

    def delete(self, key):
        if key in self.broken:
            raise OSError("minio is down")
        self.log.append(("image", key))


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    # The defaults, whatever a local .env says.
    for name, value in {
        "ORPHAN_AFTER_S": "60",
        "FORCE_STOP_AFTER_S": "60",
        "HISTORY_CLEANUP_INTERVAL_S": "600",
        "HEARTBEAT_TTL_S": "15",
        "MAX_HISTORY_PER_USER": "50",
    }.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def fakes(monkeypatch):
    log = []
    fake = SimpleNamespace(
        db=FakeDb(log), live=FakeLive(), celery=FakeCelery(), storage=FakeStorage(log), log=log
    )
    monkeypatch.setattr(sweeper, "db", fake.db)
    monkeypatch.setattr(sweeper, "live", fake.live)
    monkeypatch.setattr(sweeper, "celery_client", fake.celery)
    monkeypatch.setattr(sweeper, "storage", fake.storage)
    return fake


@pytest.fixture
def state():
    """A controller that has been running a while: it knows the current Redis and cleaned up just now."""
    return ControllerState(redis_run_id=RUN_ID, redis_changed_at=NOW - 3600, last_cleanup=NOW)


# --- 1. Running tasks no worker is working on ---------------------------------------------------------------


def test_orphan_is_failed_only_after_60_seconds(fakes, state):
    orphan = task(db.RUNNING, started=0)
    fakes.db.tasks = [orphan]

    sweeper.sweep(NOW + 59, state)
    assert fakes.db.finished == []

    sweeper.sweep(NOW + 61, state)
    assert fakes.db.finished == [(orphan.id, db.FAILED, "the worker stopped unexpectedly")]
    assert fakes.live.finals == [(orphan.user_id, orphan.id, db.FAILED)]


def test_running_task_a_heartbeat_lists_is_left_alone(fakes, state):
    working = task(db.RUNNING, started=0)
    fakes.db.tasks = [working]
    fakes.live.heartbeats = [heartbeat(working.id)]

    sweeper.sweep(NOW + 600, state)

    assert fakes.db.finished == []
    assert fakes.live.finals == []


def test_nothing_is_published_when_the_worker_finished_first(fakes, state):
    fakes.db.tasks = [task(db.RUNNING, started=0)]
    fakes.db.finish_result = False

    sweeper.sweep(NOW + 61, state)

    assert len(fakes.db.finished) == 1
    assert fakes.live.finals == []


def test_orphans_wait_for_heartbeats_after_redis_restarts(fakes, state):
    working = task(db.RUNNING, started=0)
    fakes.db.tasks = [working]
    fakes.live.run_id = "run-2"  # Redis came back empty: no heartbeats yet

    sweeper.sweep(NOW + 100, state)
    sweeper.sweep(NOW + 110, state)
    assert fakes.db.finished == []

    fakes.live.heartbeats = [heartbeat(working.id)]
    sweeper.sweep(NOW + 120, state)
    assert fakes.db.finished == []


def test_orphan_is_failed_once_the_restart_pause_is_over(fakes, state):
    orphan = task(db.RUNNING, started=0)
    fakes.db.tasks = [orphan]
    fakes.live.run_id = "run-2"

    sweeper.sweep(NOW + 100, state)  # sees the restart
    sweeper.sweep(NOW + 114, state)
    assert fakes.db.finished == []

    sweeper.sweep(NOW + 115, state)  # HEARTBEAT_TTL_S later, still no heartbeat lists it
    assert [entry[0] for entry in fakes.db.finished] == [orphan.id]


# --- 2. A worker still busy with a task cancelled long ago --------------------------------------------------


def test_stuck_cancel_gets_one_terminating_revoke(fakes, state):
    stuck = task(db.CANCELLED, started=-30, finished=0)
    fakes.db.tasks = [stuck]
    fakes.live.heartbeats = [heartbeat(stuck.id)]

    sweeper.sweep(NOW + 59, state)
    assert fakes.celery.revoked == []

    sweeper.sweep(NOW + 61, state)
    sweeper.sweep(NOW + 66, state)
    assert fakes.celery.revoked == [(str(stuck.id), True)]


def test_malformed_task_id_does_not_skip_the_other_workers(fakes, state, caplog):
    def get_task(task_id):
        if task_id == "not-a-uuid":
            raise ValueError("badly formed hexadecimal UUID string")
        return FakeDb.get_task(fakes.db, task_id)

    stuck = task(db.CANCELLED, started=-30, finished=0)
    fakes.db.tasks = [stuck]
    fakes.db.get_task = get_task
    fakes.live.heartbeats = [heartbeat("not-a-uuid"), heartbeat(stuck.id)]

    sweeper.sweep(NOW + 61, state)

    assert fakes.celery.revoked == [(str(stuck.id), True)]
    assert "force_stop_cancelled failed" not in caplog.text


def test_busy_worker_on_a_live_task_is_not_stopped(fakes, state):
    running = task(db.RUNNING, started=0)
    fakes.db.tasks = [running]
    fakes.live.heartbeats = [heartbeat(running.id)]

    sweeper.sweep(NOW + 600, state)

    assert fakes.celery.revoked == []


# --- 3. Re-sending queued tasks after a Redis restart -------------------------------------------------------


def test_changed_run_id_resends_every_queued_task_once(fakes, state):
    queued = [task(db.QUEUED), task(db.QUEUED)]
    fakes.db.tasks = [*queued, task(db.RUNNING, started=0)]
    fakes.live.heartbeats = [heartbeat(fakes.db.tasks[2].id)]
    fakes.live.run_id = "run-2"

    sweeper.sweep(NOW, state)
    sweeper.sweep(NOW + 5, state)

    assert fakes.celery.sent == [t.id for t in queued]
    assert state.redis_run_id == "run-2"


def test_unchanged_run_id_sends_nothing(fakes, state):
    fakes.db.tasks = [task(db.QUEUED)]

    sweeper.sweep(NOW, state)

    assert fakes.celery.sent == []


def test_first_tick_after_start_resends(fakes):
    queued = task(db.QUEUED)
    fakes.db.tasks = [queued]

    sweeper.sweep(NOW, ControllerState())

    assert fakes.celery.sent == [queued.id]


def test_run_id_is_kept_until_every_task_was_sent(fakes, state):
    def broker_down(t):
        raise ConnectionError("broker down")

    fakes.db.tasks = [task(db.QUEUED)]
    fakes.live.run_id = "run-2"
    fakes.celery.send_generate = broker_down

    sweeper.sweep(NOW, state)

    assert state.redis_run_id == RUN_ID  # tried again next tick


# --- 4. History cleanup -------------------------------------------------------------------------------------


def test_cleanup_runs_at_most_every_ten_minutes(fakes):
    state = ControllerState(redis_run_id=RUN_ID)

    sweeper.sweep(NOW, state)
    sweeper.sweep(NOW + 300, state)
    sweeper.sweep(NOW + 599, state)
    sweeper.sweep(NOW + 600, state)

    assert [entry for entry in fakes.log if entry[0] == "query"] == [("query", 50), ("query", 50)]


def test_cleanup_deletes_the_image_before_the_row(fakes, state):
    with_image, without_image = task(db.DONE, result_key="images/a.png"), task(db.FAILED)
    fakes.db.beyond_limit = [with_image, without_image]

    sweeper.sweep(NOW + 600, state)

    assert fakes.log == [
        ("query", 50),
        ("image", "images/a.png"),
        ("row", with_image.id),
        ("row", without_image.id),
    ]


def test_cleanup_keeps_the_row_when_its_image_cannot_be_deleted(fakes, state):
    stuck, fine = task(db.DONE, result_key="images/stuck.png"), task(db.DONE, result_key="images/fine.png")
    fakes.db.beyond_limit = [stuck, fine]
    fakes.storage.broken = {"images/stuck.png"}

    sweeper.sweep(NOW + 600, state)

    assert ("row", stuck.id) not in fakes.log
    assert ("row", fine.id) in fakes.log


def test_cleanup_never_touches_active_tasks(fakes, state):
    active = [task(db.QUEUED), task(db.RUNNING, started=0)]
    old = task(db.DONE, result_key="images/old.png")
    fakes.db.tasks = [*active, old]
    fakes.db.beyond_limit = [old]
    fakes.live.heartbeats = [heartbeat(active[1].id)]

    sweeper.sweep(NOW + 600, state)

    assert fakes.db.tasks == active


# --- Independence of the parts ------------------------------------------------------------------------------


def test_one_sweeper_part_raising_does_not_stop_the_others(fakes, monkeypatch):
    def broken(started_before=None):
        raise RuntimeError("postgres hiccup")

    monkeypatch.setattr(fakes.db, "running_tasks", broken)
    queued = task(db.QUEUED)
    fakes.db.tasks = [queued]
    state = ControllerState(redis_run_id=RUN_ID, redis_changed_at=NOW - 3600)
    fakes.live.run_id = "run-2"

    sweeper.sweep(NOW, state)

    assert fakes.celery.sent == [queued.id]  # part 3 ran
    assert ("query", 50) in fakes.log  # part 4 ran


def test_one_controller_part_raising_does_not_stop_the_others(monkeypatch):
    calls = []

    def broken(*args):
        calls.append("sweep")
        raise RuntimeError("sweep failed")

    monkeypatch.setattr(main.live, "write_machine_heartbeat", lambda *a: calls.append("heartbeat"))
    monkeypatch.setattr(main.sweeper, "sweep", broken)
    monkeypatch.setattr(main.autoscaler, "autoscale_tick", lambda *a: calls.append("autoscale"))
    monkeypatch.setattr(main.status, "publish_status", lambda: calls.append("status"))

    main.tick(NOW, ControllerState())

    assert calls == ["heartbeat", "sweep", "autoscale", "status"]


def test_stop_ends_the_loop_after_the_current_tick(monkeypatch):
    stop = main.threading.Event()
    ticks = []

    def one_tick(now, state):
        ticks.append(now)
        stop.set()  # as SIGTERM would, mid-tick

    monkeypatch.setattr(main, "tick", one_tick)
    main.run(stop)

    assert len(ticks) == 1
