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
    desired = state.scaler.replicas()
    ready = live.count_workers(("idle", "busy"))
    starting = max(desired - ready, 0)
    waiting = db.count_tasks(db.QUEUED)
    live.save_autoscaler_state(desired=desired, ready=ready, starting=starting, maximum=settings.MAX_WORKERS)

    if starting == 0:
        state.starting_since = None
    elif state.starting_since is None:
        state.starting_since = now

    if starting > 0:
        timed_out = now - state.starting_since > settings.STARTUP_TIMEOUT_S
        if timed_out and not isinstance(state.scaler, scalers.NullScaler):
            _change(now, state, desired, ready, ready, "Gave up on a worker that could not start")
            live.alert(CANNOT_START)
        return

    if now - state.last_scale_at < settings.COOLDOWN_S:
        return

    if desired < settings.MIN_WORKERS:
        _change(now, state, desired, settings.MIN_WORKERS, ready, "Raised the workers to the minimum")
    elif (
        waiting > settings.SCALE_UP_WAITING_PER_WORKER * min(ready, desired)
        and desired < settings.MAX_WORKERS
    ):
        _change(now, state, desired, desired + 1, ready, "Added a worker")
    elif (
        waiting == 0
        and desired > settings.MIN_WORKERS
        and live.any_worker_idle_for(settings.SCALE_DOWN_IDLE_S)
    ):
        _change(now, state, desired, desired - 1, ready, "Removed a worker")


def _change(now: float, state: ControllerState, old: int, new: int, ready: int, what: str) -> None:
    state.scaler.set_replicas(new)
    state.last_scale_at = now
    state.workers_launched = state.workers_launched + new - old if new > old else 0
    action = f"{what} ({old} → {new}) at {time.strftime('%H:%M', time.localtime(now))}"
    log.info("%s", action)
    live.save_autoscaler_state(desired=new, starting=max(new - ready, 0), last_action=action)
