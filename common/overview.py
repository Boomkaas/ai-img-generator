"""The admin overview, built from heartbeats and the database.

The controller publishes it on the admin channel every tick, and the API serves it for the admin page's
first paint, so it lives here rather than in either of them.
"""

import logging
import time
from collections import defaultdict

from pydantic import ValidationError

from common import db, live
from common.config import get_settings
from common.messages import AdminOverview, ApiStats, AutoscalerItem, MachineItem, TaskItem, WorkerItem

log = logging.getLogger(__name__)

INFRA, APP = "infra", "app"


def admin_overview() -> AdminOverview:
    workers = sorted(live.worker_heartbeats(), key=lambda hb: hb.host)
    progress: dict[str, int] = {}

    def progress_of(task_id) -> int:
        key = str(task_id)
        if key not in progress:
            progress[key] = live.progress(key)
        return progress[key]

    items = []
    for hb in workers:
        try:
            items.append(
                WorkerItem(
                    name=hb.host,
                    machine=hb.machine,
                    state=hb.state,
                    task_id=hb.task_id,
                    progress=progress_of(hb.task_id) if hb.state == "busy" and hb.task_id else None,
                    rss_mb=hb.rss_mb,
                )
            )
        except ValidationError:
            # One bad heartbeat mustn't freeze the whole admin panel.
            log.warning("skipping the malformed heartbeat of %s", hb.host, exc_info=True)

    return AdminOverview(
        machines=_machines(workers),
        workers=items,
        tasks=_tasks(progress_of),
        autoscaler=_autoscaler(workers),
    )


def _machines(workers: list[live.WorkerHeartbeat]) -> list[MachineItem]:
    apis = live.api_heartbeats()
    # Every heartbeat on a machine carries the same whole-machine figures, at most one interval apart, so the
    # controller's own beat is taken first, then an API server's, then a worker's.
    figures: dict[str, tuple[float, float]] = {}
    roles: dict[str, str] = {}
    for hb in workers:
        figures[hb.machine] = (hb.machine_cpu, hb.machine_ram)
        roles[hb.machine] = APP
    for hb in apis:
        figures[hb.machine] = (hb.machine_cpu, hb.machine_ram)
        roles[hb.machine] = APP
    for hb in live.machine_heartbeats():
        figures[hb.machine] = (hb.machine_cpu, hb.machine_ram)
        roles[hb.machine] = hb.role

    sockets: dict[str, int] = defaultdict(int)
    requests: dict[str, int] = defaultdict(int)
    for hb in apis:
        sockets[hb.machine] += hb.sockets
        requests[hb.machine] += hb.requests_per_min

    return [
        MachineItem(
            name=name,
            role=roles[name],
            cpu=figures[name][0],
            ram=figures[name][1],
            api=ApiStats(sockets=sockets[name], requests_per_min=requests[name]) if name in sockets else None,
        )
        for name in sorted(figures)
    ]


def _tasks(progress_of) -> list[TaskItem]:
    active = db.active_tasks_with_email()
    now = time.time()
    running = sorted((t for t in active if t.status == db.RUNNING), key=lambda t: (t.started_at, t.id))
    queued = [t for t in active if t.status == db.QUEUED]
    items = [
        TaskItem(
            id=t.id,
            user_email=t.user_email,
            size=t.size,
            status=db.RUNNING,
            worker_host=t.worker_host,
            progress=progress_of(t.id),
            seconds_so_far=max(now - t.started_at.timestamp(), 0.0),
        )
        for t in running
    ]
    items += [
        TaskItem(
            id=t.id,
            user_email=t.user_email,
            size=t.size,
            status=db.QUEUED,
            position=position,
            seconds_so_far=max(now - t.created_at.timestamp(), 0.0),
        )
        for position, t in enumerate(queued, start=1)
    ]
    return items


def _autoscaler(workers: list[live.WorkerHeartbeat]) -> AutoscalerItem:
    state = live.autoscaler_state()

    def stored(name: str, default: int) -> int:
        value = state.get(name)
        return default if value is None else value

    # Until the autoscaler has reported, the heartbeats are the best account of the workers.
    ready = sum(hb.state in ("idle", "busy") for hb in workers)
    return AutoscalerItem(
        desired=stored("desired", ready),
        ready=stored("ready", ready),
        starting=stored("starting", 0),
        maximum=stored("maximum", get_settings().MAX_WORKERS),
        last_action=state.get("last_action"),
        alerts=live.recent_alerts(),
    )
