"""Wait-time estimates (architecture §4.6).

`simulate_wait` is the pure core; the functions below it gather the state from Postgres and Redis. Jobs differ
in cost, so the estimate simulates the queue on the ready workers instead of multiplying a position by an
average.
"""

import heapq
import statistics
from collections.abc import Sequence
from datetime import datetime

from common import db, live
from common.config import get_settings

BASE_SIZE = 512  # one cost unit: a default 512 px job


def cost(size: int) -> float:
    return (size / BASE_SIZE) ** 2


def simulate_wait(
    running_remaining_costs: Sequence[float],
    queued_costs: Sequence[float],
    n_workers: int,
    seconds_per_unit: float,
) -> float:
    """Seconds until a worker is free for a new task, after the queued ones (oldest first) have started.

    Running tasks are given oldest first; with more of them than workers, only the first n_workers count.
    No workers is treated as one.
    """
    n = max(n_workers, 1)
    free_at = [remaining * seconds_per_unit for remaining in running_remaining_costs][:n]
    free_at += [0.0] * (n - len(free_at))
    heapq.heapify(free_at)
    for queued in queued_costs:
        heapq.heappush(free_at, heapq.heappop(free_at) + queued * seconds_per_unit)
    return free_at[0]


def seconds_per_cost_unit() -> float:
    """Median of (run time / cost) over the most recent done tasks, or the calibrated value."""
    settings = get_settings()
    recent = db.recent_done_tasks(limit=settings.ESTIMATE_SAMPLE_SIZE)
    if len(recent) < settings.ESTIMATE_MIN_SAMPLES:
        return settings.CALIBRATED_SECONDS_PER_COST_UNIT
    return statistics.median((t.finished_at - t.started_at).total_seconds() / cost(t.size) for t in recent)


def _remaining_cost(task) -> float:
    return cost(task.size) * (1 - live.progress(task.id) / 100)


def estimate_wait_seconds(before: datetime | None = None, spu: float | None = None) -> float:
    """Seconds until a task submitted at `before` (default: now) would start, on the workers ready now."""
    spu = seconds_per_cost_unit() if spu is None else spu
    return simulate_wait(
        [_remaining_cost(t) for t in db.running_tasks()],
        [cost(t.size) for t in db.queued_tasks(created_before=before)],
        live.count_workers(("idle", "busy")),
        spu,
    )


def ready_in_seconds(task, spu: float | None = None) -> float:
    """Seconds until the task's image should be ready; 0 once it has finished."""
    spu = seconds_per_cost_unit() if spu is None else spu
    if task.status == db.QUEUED:
        return estimate_wait_seconds(before=task.created_at, spu=spu) + cost(task.size) * spu
    if task.status == db.RUNNING:
        return _remaining_cost(task) * spu
    return 0.0


def new_image_ready_in_s(spu: float | None = None) -> float:
    """Seconds until a default-size task submitted now would be ready."""
    spu = seconds_per_cost_unit() if spu is None else spu
    return estimate_wait_seconds(spu=spu) + cost(BASE_SIZE) * spu
