import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import docker.errors
import pytest

from common import db
from common.config import get_settings
from controller import autoscaler, scalers
from controller.main import ControllerState
from controller.scalers import NullScaler, ScalerError, SwarmScaler

pytestmark = pytest.mark.unit

NOW = 1_800_000_000.0  # 08:00 UTC
STARTUP_TIMEOUT_S = 600
COOLDOWN_S = 120
SCALE_DOWN_IDLE_S = 300
MIN_WORKERS = 1
MAX_WORKERS = 4


class FakeScaler:
    def __init__(self, replicas):
        self.count = replicas
        self.calls = []

    def replicas(self):
        return self.count

    def set_replicas(self, n):
        self.calls.append(n)
        self.count = n


class FakeLive:
    def __init__(self):
        self.workers = []  # one state per worker heartbeat
        self.idle_for = 0.0
        self.saved = {}
        self.alerts = []

    def count_workers(self, states):
        return sum(state in states for state in self.workers)

    def any_worker_idle_for(self, seconds):
        return "idle" in self.workers and self.idle_for >= seconds

    def save_autoscaler_state(self, **fields):
        self.saved.update(fields)

    def alert(self, message):
        self.alerts.append(message)


class FakeDb:
    QUEUED = db.QUEUED

    def __init__(self):
        self.queued = 0

    def count_tasks(self, status):
        assert status == db.QUEUED
        return self.queued


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    # The defaults, whatever a local .env says, and a fixed time zone for last_action.
    for name, value in {
        "SCALER": "none",
        "MIN_WORKERS": str(MIN_WORKERS),
        "MAX_WORKERS": str(MAX_WORKERS),
        "SCALE_UP_WAITING_PER_WORKER": "2",
        "COOLDOWN_S": str(COOLDOWN_S),
        "STARTUP_TIMEOUT_S": str(STARTUP_TIMEOUT_S),
        "SCALE_DOWN_IDLE_S": str(SCALE_DOWN_IDLE_S),
        "TZ": "UTC",
    }.items():
        monkeypatch.setenv(name, value)
    time.tzset()
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
    monkeypatch.undo()
    time.tzset()


@pytest.fixture
def fake(monkeypatch):
    f = SimpleNamespace(live=FakeLive(), db=FakeDb(), scaler=FakeScaler(1))
    f.state = ControllerState(scaler=f.scaler)
    monkeypatch.setattr(autoscaler, "live", f.live)
    monkeypatch.setattr(autoscaler, "db", f.db)

    def setup(replicas, ready=0, loading=0, queued=0, idle=None):
        f.scaler.count = replicas
        f.live.workers = ["busy"] * ready + ["loading"] * loading
        if idle is not None:
            f.live.workers[0] = "idle"
            f.live.idle_for = idle
        f.db.queued = queued

    f.setup = setup
    f.tick = lambda now=NOW: autoscaler.autoscale_tick(now, f.state)
    return f


def test_scales_up_when_queue_builds(fake):
    fake.setup(replicas=1, ready=1, queued=3)
    fake.tick()
    assert fake.scaler.count == 2


def test_no_scale_up_at_the_threshold(fake):
    fake.setup(replicas=1, ready=1, queued=2)
    fake.tick()
    assert fake.scaler.calls == []


def test_no_second_launch_while_one_is_loading(fake):
    fake.setup(replicas=2, ready=1, queued=10)
    fake.tick()
    assert fake.scaler.calls == []


def test_loading_workers_do_not_count_as_ready(fake):
    fake.setup(replicas=2, ready=1, loading=1, queued=10)
    fake.tick()
    assert fake.scaler.calls == []
    assert fake.live.saved["ready"] == 1 and fake.live.saved["starting"] == 1


def test_gives_up_on_a_worker_that_cannot_be_placed(fake):
    fake.setup(replicas=3, ready=2)
    fake.state.starting_since = NOW - STARTUP_TIMEOUT_S - 1
    fake.tick()
    assert fake.scaler.count == 2
    assert fake.live.alerts == [autoscaler.CANNOT_START]
    assert fake.live.saved["last_action"] == "Gave up on a worker that could not start (3 → 2) at 08:00"


def test_waits_the_full_startup_timeout_from_when_a_worker_was_first_seen_loading(fake):
    # The last change was long ago; a worker that restarts now still gets the whole timeout.
    fake.setup(replicas=2, ready=1, loading=1)
    fake.tick(NOW)
    fake.tick(NOW + STARTUP_TIMEOUT_S)
    assert fake.scaler.calls == []
    fake.tick(NOW + STARTUP_TIMEOUT_S + 1)
    assert fake.scaler.calls == [1]


def test_startup_clock_resets_once_the_worker_is_ready(fake):
    fake.setup(replicas=2, ready=1, loading=1)
    fake.tick(NOW)
    fake.setup(replicas=2, ready=2)
    fake.tick(NOW + 10)
    assert fake.state.starting_since is None


def test_without_swarm_a_worker_that_never_appears_is_not_given_up_on(fake):
    fake.state.scaler = NullScaler(1)
    fake.state.starting_since = NOW - STARTUP_TIMEOUT_S - 1
    fake.tick()
    assert fake.state.scaler.replicas() == 1
    assert fake.live.alerts == []


def test_a_draining_worker_does_not_count_against_scaling_up(fake):
    # Scaled down from 3 to 2; the stopped worker is still finishing its job.
    fake.setup(replicas=2, ready=3, queued=5)
    fake.tick()
    assert fake.scaler.calls == [3]


def test_respects_cooldown(fake):
    fake.setup(replicas=1, ready=1, queued=10)
    fake.state.last_scale_at = NOW
    fake.tick(NOW + COOLDOWN_S - 1)
    assert fake.scaler.calls == []
    fake.tick(NOW + COOLDOWN_S)
    assert fake.scaler.calls == [2]


def test_cooldown_applies_after_giving_up(fake):
    fake.setup(replicas=3, ready=2, queued=10)
    fake.state.starting_since = NOW - STARTUP_TIMEOUT_S - 1
    fake.tick(NOW)
    assert fake.scaler.calls == [2]
    fake.tick(NOW + COOLDOWN_S - 1)
    assert fake.scaler.calls == [2]
    fake.tick(NOW + COOLDOWN_S)
    assert fake.scaler.calls == [2, 3]


def test_respects_max(fake):
    fake.setup(replicas=MAX_WORKERS, ready=MAX_WORKERS, queued=999)
    fake.tick()
    assert fake.scaler.calls == []


def test_scales_down_only_when_nothing_waits_and_a_worker_idles(fake):
    fake.setup(replicas=3, ready=3, idle=SCALE_DOWN_IDLE_S + 1)
    fake.tick()
    assert fake.scaler.count == 2


def test_no_scale_down_before_a_worker_has_idled_long_enough(fake):
    fake.setup(replicas=3, ready=3, idle=SCALE_DOWN_IDLE_S - 1)
    fake.tick()
    assert fake.scaler.calls == []


def test_no_scale_down_while_a_task_waits(fake):
    fake.setup(replicas=3, ready=3, queued=1, idle=SCALE_DOWN_IDLE_S + 1)
    fake.tick()
    assert fake.scaler.calls == []


def test_no_scale_down_below_min(fake):
    fake.setup(replicas=MIN_WORKERS, ready=MIN_WORKERS, idle=SCALE_DOWN_IDLE_S + 1)
    fake.tick()
    assert fake.scaler.calls == []


def test_desired_below_min_is_raised_to_min(fake, monkeypatch):
    monkeypatch.setenv("MIN_WORKERS", "2")
    get_settings.cache_clear()
    fake.setup(replicas=0)
    fake.tick()
    assert fake.scaler.calls == [2]
    assert fake.live.saved["last_action"] == "Raised the workers to the minimum (0 → 2) at 08:00"


@pytest.mark.parametrize(
    ("setup", "action"),
    [
        (dict(replicas=2, ready=2, queued=5), "Added a worker (2 → 3) at 08:00"),
        (dict(replicas=3, ready=3, idle=SCALE_DOWN_IDLE_S + 1), "Removed a worker (3 → 2) at 08:00"),
    ],
)
def test_every_change_updates_the_saved_state(fake, setup, action):
    fake.setup(**setup)
    fake.tick()
    new = fake.scaler.count
    assert fake.live.saved == {
        "desired": new,
        "ready": setup["ready"],
        "starting": max(new - setup["ready"], 0),
        "maximum": MAX_WORKERS,
        "last_action": action,
    }
    assert fake.state.last_scale_at == NOW


def test_state_is_saved_even_when_nothing_changes(fake):
    fake.setup(replicas=2, ready=1, loading=1)
    fake.tick()
    assert fake.live.saved == {"desired": 2, "ready": 1, "starting": 1, "maximum": MAX_WORKERS}


def test_scaler_is_created_from_settings_on_the_first_tick(fake):
    fake.state.scaler = None
    fake.setup(replicas=0)
    fake.tick()
    assert isinstance(fake.state.scaler, NullScaler)
    assert fake.state.scaler.replicas() == MIN_WORKERS


# --- Scalers ---


def test_null_scaler_keeps_the_count():
    scaler = NullScaler(1)
    scaler.set_replicas(3)
    assert scaler.replicas() == 3


def swarm_with(service=None, error=None):
    client = MagicMock()
    if error is not None:
        client.services.get.side_effect = error
    else:
        client.services.get.return_value = service
    return SwarmScaler("imagegen_worker", client=client), client


def test_swarm_scaler_reads_the_replica_count():
    service = MagicMock(attrs={"Spec": {"Mode": {"Replicated": {"Replicas": 3}}}})
    scaler, client = swarm_with(service)
    assert scaler.replicas() == 3
    client.services.get.assert_called_with("imagegen_worker")


def test_swarm_scaler_changes_only_the_replica_count():
    template = {"ContainerSpec": {"Image": "imagegen-worker:1.0.0"}, "Placement": {"Constraints": ["a"]}}
    spec = {"Name": "imagegen_worker", "Labels": {"l": "1"}, "TaskTemplate": template,
            "Mode": {"Replicated": {"Replicas": 1}}}  # fmt: skip
    service = MagicMock(id="svc", version=41, attrs={"Spec": spec})
    scaler, client = swarm_with(service)
    scaler.set_replicas(2)
    service.scale.assert_not_called()  # it would send Docker's defaults and restart every worker
    client.api.update_service.assert_called_once_with(
        "svc",
        41,
        name="imagegen_worker",
        labels={"l": "1"},
        task_template=template,
        mode={"Replicated": {"Replicas": 2}},
        update_config=None,
        rollback_config=None,
        endpoint_spec=None,
    )


def test_swarm_scaler_missing_service_is_a_clear_error():
    scaler, _ = swarm_with(error=docker.errors.NotFound("service imagegen_worker not found"))
    with pytest.raises(ScalerError, match="no Swarm service named imagegen_worker"):
        scaler.replicas()


def test_swarm_scaler_off_a_manager_node_is_a_clear_error():
    error = docker.errors.APIError("This node is not a swarm manager.", response=MagicMock(status_code=503))
    scaler, _ = swarm_with(error=error)
    with pytest.raises(ScalerError, match="manager node"):
        scaler.set_replicas(2)


def test_swarm_scaler_global_service_is_a_clear_error():
    scaler, _ = swarm_with(MagicMock(attrs={"Spec": {"Mode": {"Global": {}}}}))
    with pytest.raises(ScalerError, match="not a replicated service"):
        scaler.replicas()


def test_scaler_chosen_by_setting(monkeypatch):
    monkeypatch.setattr(scalers.docker, "from_env", lambda: MagicMock())
    assert isinstance(scalers.from_settings(), NullScaler)
    monkeypatch.setenv("SCALER", "swarm")
    get_settings.cache_clear()
    scaler = scalers.from_settings()
    assert isinstance(scaler, SwarmScaler) and scaler.service == "imagegen_worker"
