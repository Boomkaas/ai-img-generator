from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from common import db, estimate, live
from common.config import Settings, get_settings
from common.estimate import cost, simulate_wait

pytestmark = pytest.mark.unit

T0 = datetime(2026, 1, 1, tzinfo=UTC)
SPU = (
    480  # seconds per cost unit in these tests, passed explicitly so a calibrated value in .env can't leak in
)

# Cost relative to a 512 px job, rounded to two places.
COST_TABLE = {256: 0.25, 384: 0.56, 512: 1.0, 640: 1.56, 768: 2.25}


def test_wait_example():
    # One worker halfway through a 512 px job, one idle; a 512 px and a 768 px job waiting.
    assert simulate_wait([0.5], [1.0, 2.25], 2, 480) == 480


def test_no_workers_is_treated_as_one():
    assert simulate_wait([0.5], [1.0], 0, 480) == simulate_wait([0.5], [1.0], 1, 480) == 720


def test_empty_queue_returns_the_earliest_free_worker():
    assert simulate_wait([0.5, 0.25], [], 2, 480) == 120
    assert simulate_wait([0.5], [], 2, 480) == 0  # one worker is idle


def test_more_running_tasks_than_workers_counts_the_oldest():
    # A third task is running on a worker whose heartbeat is gone; only the first two count.
    assert simulate_wait([1.0, 0.5, 0.1], [], 2, 480) == 240
    assert simulate_wait([1.0, 0.5, 0.1], [0.25], 2, 480) == 360


def test_queue_is_served_oldest_first_on_the_earliest_free_worker():
    # One worker: everything runs one after another.
    assert simulate_wait([0.5], [1.0, 2.25], 1, 100) == pytest.approx(50 + 100 + 225)


def test_cost_matches_the_table_for_every_allowed_size():
    # The code's default sizes, not whatever the local .env overrides them with.
    assert set(Settings.model_fields["ALLOWED_SIZES"].default) == set(COST_TABLE)
    for size, expected in COST_TABLE.items():
        assert cost(size) == pytest.approx(expected, abs=0.005)
    assert cost(768) / cost(256) == 9


# --- The parts that read Postgres and Redis, with both faked ------------------------------------------------


def task(size, status, created=0, started=None, finished=None, progress=0):
    return SimpleNamespace(
        id=uuid4(),
        size=size,
        status=status,
        created_at=T0 + timedelta(seconds=created),
        started_at=None if started is None else T0 + timedelta(seconds=started),
        finished_at=None if finished is None else T0 + timedelta(seconds=finished),
        progress=progress,
    )


@pytest.fixture
def fake_state(monkeypatch):
    def setup(workers=1, done=(), running=(), queued=()):
        progress = {t.id: t.progress for t in running}
        monkeypatch.setattr(db, "recent_done_tasks", lambda limit: list(done)[:limit])
        monkeypatch.setattr(db, "running_tasks", lambda: list(running))
        monkeypatch.setattr(
            db,
            "queued_tasks",
            lambda created_before=None: [
                t for t in queued if created_before is None or t.created_at < created_before
            ],
        )
        monkeypatch.setattr(live, "count_workers", lambda states: workers)
        monkeypatch.setattr(live, "progress", lambda task_id: progress.get(task_id, 0))

    return setup


def test_seconds_per_unit_falls_back_to_the_calibrated_value(fake_state):
    fake_state(done=[task(512, "done", started=0, finished=100)] * 4)
    assert estimate.seconds_per_cost_unit() == get_settings().CALIBRATED_SECONDS_PER_COST_UNIT


def test_seconds_per_unit_is_the_median_per_cost_unit(fake_state):
    # Run times per cost unit: 100, 100, 200, 400, 1000 -> median 200.
    fake_state(
        done=[
            task(512, "done", started=0, finished=100),
            task(256, "done", started=0, finished=25),
            task(768, "done", started=0, finished=450),
            task(512, "done", started=0, finished=400),
            task(256, "done", started=0, finished=250),
        ]
    )
    assert estimate.seconds_per_cost_unit() == pytest.approx(200)


def test_wait_estimate_simulates_two_workers(fake_state):
    # The same example, with the services faked.
    fake_state(
        workers=2,
        running=[task(512, "running", progress=50)],
        queued=[task(512, "queued", created=1), task(768, "queued", created=2)],
    )
    assert estimate.estimate_wait_seconds(spu=SPU) == 480


def test_wait_estimate_without_spu_uses_seconds_per_cost_unit(fake_state):
    fake_state(workers=1, queued=[task(512, "queued")])
    assert estimate.estimate_wait_seconds() == estimate.seconds_per_cost_unit()


def test_tasks_created_at_the_same_moment_queue_in_id_order(fake_state):
    a, b = task(512, "queued", created=1), task(768, "queued", created=1)
    first, second = sorted([a, b], key=lambda t: t.id)
    fake_state(workers=1, queued=[first, second])
    assert estimate.ready_in_seconds(first, spu=SPU) == cost(first.size) * SPU
    assert estimate.ready_in_seconds(second, spu=SPU) == (cost(first.size) + cost(second.size)) * SPU


def test_no_ready_worker_still_gives_an_estimate(fake_state):
    fake_state(workers=0, queued=[task(512, "queued")])
    assert estimate.estimate_wait_seconds(spu=SPU) == 480


def test_ready_in_seconds_for_each_status(fake_state):
    first, second = task(512, "queued", created=1), task(768, "queued", created=2)
    running = task(512, "running", progress=50)
    fake_state(workers=1, running=[running], queued=[first, second])
    # The running job frees the worker at 240 s; the first queued job then runs for 480 s.
    assert estimate.ready_in_seconds(first, spu=SPU) == 240 + 480
    assert estimate.ready_in_seconds(second, spu=SPU) == 240 + 480 + 1080
    assert estimate.ready_in_seconds(running, spu=SPU) == 240
    assert estimate.ready_in_seconds(task(512, "done"), spu=SPU) == 0


def test_new_image_waits_for_the_whole_queue(fake_state):
    fake_state(workers=1, running=[task(512, "running", progress=50)], queued=[task(256, "queued")])
    assert estimate.new_image_ready_in_s(spu=SPU) == 240 + 120 + 480


def test_forecast_agrees_with_ready_in_seconds(fake_state):
    running = [task(512, "running", started=5, progress=50), task(768, "running", started=1, progress=10)]
    queued = [task(256, "queued", created=1), task(768, "queued", created=2), task(512, "queued", created=3)]
    fake_state(workers=2, running=running, queued=queued)
    forecast = estimate.forecast(running + queued, SPU)
    for t in running + queued:
        assert forecast.ready_in_s[t.id] == pytest.approx(estimate.ready_in_seconds(t, spu=SPU))
    assert forecast.new_image_ready_in_s == pytest.approx(estimate.new_image_ready_in_s(spu=SPU))
