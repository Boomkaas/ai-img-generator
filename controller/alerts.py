"""Alerts for states a person should look at: the autoscaler stuck at its limit, a full queue, a crash-loop.

Each fires once when its condition has held long enough, and again only after the condition has cleared.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from common import db, live
from common.config import get_settings

if TYPE_CHECKING:
    from controller.main import ControllerState

AT_MAX_WORKERS = "The autoscaler has been at its limit of {maximum} workers for {minutes} minutes"
QUEUE_FULL = "The queue has been full ({limit} waiting) for {minutes} minutes; new jobs are refused"
WORKER_RESTARTS = "Workers on {machine} started {count} times in {minutes} minutes; one may be crash-looping"


def check(now: float, state: ControllerState) -> None:
    settings = get_settings()
    # Redis first: a database outage stops only the queue check, not the crash-loop one.
    _check_worker_starts(now, state)
    desired = live.autoscaler_state().get("desired") or 0
    if _held(now, state, "at_max", desired >= settings.MAX_WORKERS, settings.ALERT_AT_MAX_WORKERS_S):
        live.alert(
            AT_MAX_WORKERS.format(maximum=settings.MAX_WORKERS, minutes=settings.ALERT_AT_MAX_WORKERS_S // 60)
        )
    queue_full = db.count_tasks(db.QUEUED) >= settings.MAX_QUEUED_TOTAL
    if _held(now, state, "queue_full", queue_full, settings.ALERT_QUEUE_FULL_S):
        minutes = settings.ALERT_QUEUE_FULL_S // 60
        live.alert(QUEUE_FULL.format(limit=settings.MAX_QUEUED_TOTAL, minutes=minutes))


def _held(now: float, state: ControllerState, name: str, holds: bool, after_s: float) -> bool:
    """True on the first tick on which `holds` has been true for `after_s` without a break."""
    if not holds:
        state.alert_since.pop(name, None)
        state.alerts_raised.discard(name)
        return False
    since = state.alert_since.setdefault(name, now)
    if now - since >= after_s and name not in state.alerts_raised:
        state.alerts_raised.add(name)
        return True
    return False


def _check_worker_starts(now: float, state: ControllerState) -> None:
    """Counts worker starts per machine: a heartbeat name not seen before, or one whose start time changed.
    Swarm restarts a worker under the same name, often before its old heartbeat has expired, so a new name
    alone would miss most restarts. Starts the autoscaler asked for don't count."""
    settings = get_settings()
    window = settings.ALERT_WORKER_STARTS_WINDOW_S
    beats = live.worker_heartbeats()
    if state.workers_seen is not None:  # on the first tick every worker would look new
        for hb in sorted(beats, key=lambda hb: hb.host):
            if state.workers_seen.get(hb.host) == hb.started:
                continue
            if state.workers_launched > 0:
                state.workers_launched -= 1
            else:
                state.worker_starts.setdefault(hb.machine, []).append(now)
    state.workers_seen = {hb.host: hb.started for hb in beats}

    for machine in list(state.worker_starts):
        starts = [t for t in state.worker_starts[machine] if now - t < window]
        name = f"worker_starts:{machine}"
        if len(starts) > settings.ALERT_WORKER_STARTS:
            if name not in state.alerts_raised:
                state.alerts_raised.add(name)
                live.alert(WORKER_RESTARTS.format(machine=machine, count=len(starts), minutes=window // 60))
        else:
            state.alerts_raised.discard(name)
        if starts:
            state.worker_starts[machine] = starts
        else:
            del state.worker_starts[machine]
