"""Changes how many worker containers run, one at a time, between MIN_WORKERS and MAX_WORKERS."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from common import db, live
from common.config import get_settings
from controller import scalers

if TYPE_CHECKING:
    from controller.main import ControllerState

log = logging.getLogger(__name__)

CANNOT_START = "A new worker could not start: no app machine has enough free memory"


def autoscale_tick(now: float, state: ControllerState) -> None:
    settings = get_settings()
    if state.scaler is None:
        state.scaler = scalers.from_settings()
    desired = state.scaler.replicas()  # what was last asked of Swarm, not what runs
    ready = live.count_workers(("idle", "busy"))  # model loaded, taking jobs
    starting = max(desired - ready, 0)  # launched but loading, or not placed at all
    waiting = db.count_tasks(db.QUEUED)
    # last_action is written only when it changes, so it survives a controller restart.
    live.save_autoscaler_state(desired=desired, ready=ready, starting=starting, maximum=settings.MAX_WORKERS)

    # Timed from when it was first seen rather than from the last change, so a worker that restarts hours
    # after the last change still gets the full time to load its model.
    if starting == 0:
        state.starting_since = None
    elif state.starting_since is None:
        state.starting_since = now

    if starting > 0:
        # Never stack launches while one is still loading. One that Swarm can't place stays pending forever,
        # so give up on it instead of waiting. Without Swarm nothing was launched, so there is nothing to
        # give up on, and the alert would only repeat while no local worker runs.
        timed_out = now - state.starting_since > settings.STARTUP_TIMEOUT_S
        if timed_out and not isinstance(state.scaler, scalers.NullScaler):
            _change(now, state, desired, ready, ready, "Gave up on a worker that could not start")
            live.alert(CANNOT_START)
        return

    if now - state.last_scale_at < settings.COOLDOWN_S:
        return

    if desired < settings.MIN_WORKERS:
        _change(now, state, desired, settings.MIN_WORKERS, ready, "Raised the workers to the minimum")
    # A worker stopped by a scale-down stays busy until its job is done, but will take no more jobs.
    elif (
        waiting > settings.SCALE_UP_WAITING_PER_WORKER * min(ready, desired)
        and desired < settings.MAX_WORKERS
    ):
        _change(now, state, desired, desired + 1, ready, "Added a worker")
    # Swarm picks which container to stop and may pick a busy one. It sends SIGTERM, Celery's warm shutdown
    # finishes the current job before exiting, and the worker's stop_grace_period (30 min, longer than
    # TASK_TIME_LIMIT) gives it the time, so choosing an idle container here would gain nothing.
    elif (
        waiting == 0
        and desired > settings.MIN_WORKERS
        and live.any_worker_idle_for(settings.SCALE_DOWN_IDLE_S)
    ):
        _change(now, state, desired, desired - 1, ready, "Removed a worker")


def _change(now: float, state: ControllerState, old: int, new: int, ready: int, what: str) -> None:
    state.scaler.set_replicas(new)
    state.last_scale_at = now
    # A worker that never came up, or one being removed, will never show up as launched.
    state.workers_launched = state.workers_launched + new - old if new > old else 0
    action = f"{what} ({old} → {new}) at {time.strftime('%H:%M', time.localtime(now))}"
    log.info("%s", action)
    live.save_autoscaler_state(desired=new, starting=max(new - ready, 0), last_action=action)
