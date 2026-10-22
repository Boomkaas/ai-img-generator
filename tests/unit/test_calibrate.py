import pytest

from common.config import Settings
from scripts.calibrate import GB, Measurement, recommend, report

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in Settings.model_fields:
        monkeypatch.delenv(name, raising=False)


def settings(**overrides) -> Settings:
    return Settings(_env_file=None, JWT_SECRET="j" * 32, **overrides)


def measurement(**overrides) -> Measurement:
    values = dict(
        load_s=30,
        unit_s=300.2,
        max_size=768,
        max_s=700.4,
        peak_bytes=int(5.3 * GB),
        ram_bytes=16 * GB,
        cores=8,
    )
    return Measurement(**{**values, **overrides})


def test_timeouts_follow_the_measured_runtime():
    r = recommend(measurement(), settings())
    t = r.timeouts
    assert (t.MEASURED_MAX_RUNTIME_S, t.CALIBRATED_SECONDS_PER_COST_UNIT) == (701, 301)
    assert t.TASK_SOFT_TIME_LIMIT == 1051  # 701 * 1.5
    assert t.WORKER_STOP_GRACE_S == t.TASK_SOFT_TIME_LIMIT + 60 + 120
    assert r.stop_grace_period == "21m"  # 1231 s, rounded up to whole minutes


def test_derived_timeouts_set_in_the_environment_do_not_leak_in(monkeypatch):
    monkeypatch.setenv("TASK_TIME_LIMIT", "99999")
    r = recommend(measurement(), settings())
    assert r.timeouts.TASK_TIME_LIMIT == r.timeouts.TASK_SOFT_TIME_LIMIT + 60


def test_memory_reservation_is_peak_plus_margin_rounded_up():
    r = recommend(measurement(peak_bytes=int(5.3 * GB)), settings())
    assert (r.reservation_gb, r.limit_gb) == (7, 8)
    r = recommend(measurement(peak_bytes=5 * GB), settings())
    assert (r.reservation_gb, r.limit_gb) == (6, 7)


@pytest.mark.parametrize(
    ("ram_gb", "cores", "by_ram", "by_cpu", "workers"),
    [
        (16, 8, 2, 3, 2),  # RAM decides
        (64, 4, 8, 1, 1),  # CPU decides
        (8, 2, 0, 0, 0),  # nothing fits
    ],
)
def test_workers_that_fit(ram_gb, cores, by_ram, by_cpu, workers):
    r = recommend(measurement(ram_bytes=ram_gb * GB, cores=cores), settings())
    assert (r.workers_by_ram, r.workers_by_cpu, r.workers) == (by_ram, by_cpu, workers)


def test_report_lists_every_number_to_paste():
    s = settings()
    m = measurement()
    text = report(m, recommend(m, s), s)
    lines = text.splitlines()
    for line in (
        "MEASURED_MAX_RUNTIME_S = 701",
        "CALIBRATED_SECONDS_PER_COST_UNIT = 301",
        "stop_grace_period: 21m",
        'reservations: {cpus: "2", memory: 7G}',
        'limits: {cpus: "2", memory: 8G}',
    ):
        assert line in lines
    assert lines[-1].endswith("= min(2, 3) = 2")
