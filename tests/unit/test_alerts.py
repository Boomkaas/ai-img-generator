from types import SimpleNamespace

import pytest

from common import db
from common.config import get_settings
from controller import alerts, autoscaler
from controller.main import ControllerState

pytestmark = pytest.mark.unit

NOW = 1_800_000_000.0
MAX_WORKERS = 4
MAX_QUEUED_TOTAL = 30


class FakeLive:
    def __init__(self):
        self.desired = 1
        self.workers = {}  # host -> machine
        self.alerts = []

    def autoscaler_state(self):
        return {"desired": self.desired}

    def worker_heartbeats(self):
        return [SimpleNamespace(host=host, machine=machine) for host, machine in self.workers.items()]

    def alert(self, message):
        self.alerts.append(message)

    def save_autoscaler_state(self, **fields):
        pass


class FakeDb:
    QUEUED = db.QUEUED

    def __init__(self):
        self.queued = 0

    def count_tasks(self, status):
        assert status == db.QUEUED
        return self.queued


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    for name, value in {"MAX_WORKERS": str(MAX_WORKERS), "MAX_QUEUED_TOTAL": str(MAX_QUEUED_TOTAL)}.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def fake(monkeypatch):
    f = SimpleNamespace(live=FakeLive(), db=FakeDb(), state=ControllerState())
    monkeypatch.setattr(alerts, "live", f.live)
    monkeypatch.setattr(alerts, "db", f.db)
    monkeypatch.setattr(autoscaler, "live", f.live)
    f.check = lambda now: alerts.check(now, f.state)
    return f


def at_max_message():
    return alerts.AT_MAX_WORKERS.format(maximum=MAX_WORKERS, minutes=15)


def test_at_max_workers_alerts_once_after_fifteen_minutes(fake):
    fake.live.desired = MAX_WORKERS
    fake.check(NOW)
    fake.check(NOW + 15 * 60 - 1)
    assert fake.live.alerts == []
    fake.check(NOW + 15 * 60)
    fake.check(NOW + 30 * 60)
    assert fake.live.alerts == [at_max_message()]


def test_at_max_workers_clock_restarts_when_it_drops(fake):
    fake.live.desired = MAX_WORKERS
    fake.check(NOW)
    fake.live.desired = MAX_WORKERS - 1
    fake.check(NOW + 10 * 60)
    fake.live.desired = MAX_WORKERS
    fake.check(NOW + 11 * 60)
    fake.check(NOW + 25 * 60)
    assert fake.live.alerts == []
    fake.check(NOW + 26 * 60)
    assert fake.live.alerts == [at_max_message()]


def test_at_max_workers_alerts_again_after_clearing(fake):
    fake.live.desired = MAX_WORKERS
    fake.check(NOW)
    fake.check(NOW + 15 * 60)
    fake.live.desired = 1
    fake.check(NOW + 20 * 60)
    fake.live.desired = MAX_WORKERS
    fake.check(NOW + 21 * 60)
    fake.check(NOW + 36 * 60)
    assert fake.live.alerts == [at_max_message()] * 2


def test_full_queue_alerts_after_ten_minutes(fake):
    fake.db.queued = MAX_QUEUED_TOTAL
    fake.check(NOW)
    fake.check(NOW + 10 * 60 - 1)
    assert fake.live.alerts == []
    fake.check(NOW + 10 * 60)
    assert fake.live.alerts == [alerts.QUEUE_FULL.format(limit=MAX_QUEUED_TOTAL, minutes=10)]


def test_a_queue_below_the_cap_never_alerts(fake):
    fake.db.queued = MAX_QUEUED_TOTAL - 1
    fake.check(NOW)
    fake.check(NOW + 60 * 60)
    assert fake.live.alerts == []


def restart(fake, now, host="nodeB-worker-1", machine="nodeB"):
    """The worker's heartbeat expires while it is down, then comes back under the same name."""
    fake.live.workers.pop(host, None)
    fake.check(now)
    fake.live.workers[host] = machine
    fake.check(now + 30)


def restarts_message(count, machine="nodeB"):
    return alerts.WORKER_RESTARTS.format(machine=machine, count=count, minutes=10)


def test_four_restarts_in_ten_minutes_alert(fake):
    fake.live.workers = {"nodeB-worker-1": "nodeB"}
    fake.check(NOW)
    for i in range(3):
        restart(fake, NOW + 60 + i * 120)
    assert fake.live.alerts == []
    restart(fake, NOW + 60 + 3 * 120)
    assert fake.live.alerts == [restarts_message(4)]
    restart(fake, NOW + 60 + 4 * 120)
    assert fake.live.alerts == [restarts_message(4)]


def test_restarts_spread_over_more_than_ten_minutes_do_not_alert(fake):
    fake.live.workers = {"nodeB-worker-1": "nodeB"}
    fake.check(NOW)
    for i in range(6):
        restart(fake, NOW + 60 + i * 210)
    assert fake.live.alerts == []


def test_restarts_are_counted_per_machine(fake):
    fake.live.workers = {"nodeB-worker-1": "nodeB", "nodeC-worker-2": "nodeC"}
    fake.check(NOW)
    for i in range(2):
        restart(fake, NOW + 60 + i * 120, "nodeB-worker-1", "nodeB")
        restart(fake, NOW + 120 + i * 120, "nodeC-worker-2", "nodeC")
    assert fake.live.alerts == []


def test_workers_already_running_when_the_controller_starts_are_not_restarts(fake):
    fake.live.workers = {f"nodeB-worker-{i}": "nodeB" for i in range(1, 6)}
    fake.check(NOW)
    fake.check(NOW + 5)
    assert fake.live.alerts == []
    assert fake.state.worker_starts == {}


class FakeScaler:
    def __init__(self, replicas):
        self.count = replicas

    def replicas(self):
        return self.count

    def set_replicas(self, n):
        self.count = n


def test_workers_the_autoscaler_launched_are_not_restarts(fake):
    fake.live.workers = {"nodeB-worker-1": "nodeB"}
    fake.check(NOW)
    fake.state.scaler = FakeScaler(1)
    for n in range(2, 6):
        autoscaler._change(NOW, fake.state, n - 1, n, n - 1, "Added a worker")
    for n in range(2, 6):
        fake.live.workers[f"nodeB-worker-{n}"] = "nodeB"
        fake.check(NOW + n * 60)
    assert fake.live.alerts == []
    assert fake.state.workers_launched == 0


def test_a_scale_down_forgets_launches_that_never_came_up(fake):
    fake.state.scaler = FakeScaler(1)
    autoscaler._change(NOW, fake.state, 1, 2, 1, "Added a worker")
    assert fake.state.workers_launched == 1
    autoscaler._change(NOW, fake.state, 2, 1, 1, "Gave up on a worker that could not start")
    assert fake.state.workers_launched == 0
