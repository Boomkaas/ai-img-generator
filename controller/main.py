"""The controller: one process on machine A that publishes the live status and cleans up, every tick.

While it is down, jobs keep running and finishing; only the live numbers, autoscaling and cleanup pause.
"""

import logging
import signal
import threading
import time
from dataclasses import dataclass, field

from common import live, overview
from common.config import get_settings
from controller import alerts, autoscaler, status, sweeper
from controller.scalers import Scaler

log = logging.getLogger(__name__)


@dataclass
class ControllerState:
    redis_run_id: str | None = None
    scaler: Scaler | None = None  # created on the first tick, so a Docker error is logged like any other
    last_scale_at: float = 0.0
    starting_since: float | None = None  # when the autoscaler first saw a launched worker not yet ready
    last_cleanup: float = 0.0
    redis_changed_at: float = 0.0  # when the sweeper last saw a new Redis run id
    force_stopped: set[str] = field(default_factory=set)  # tasks whose worker was already killed
    alert_since: dict[str, float] = field(default_factory=dict)  # when each alert's condition began to hold
    alerts_raised: set[str] = field(default_factory=set)  # alerts already sent for the current episode
    workers_seen: dict[str, float] | None = None  # each worker's start time on the previous tick
    worker_starts: dict[str, list[float]] = field(default_factory=dict)  # per machine, within the window
    workers_launched: int = 0  # started by the autoscaler and not yet seen, so not counted as restarts


def tick(now: float, state: ControllerState) -> None:
    """One round, in a fixed order; a part that fails is logged and the next one still runs."""
    machine = get_settings().MACHINE_NAME
    parts = (
        ("heartbeat", lambda: live.write_machine_heartbeat(machine, overview.INFRA)),
        ("sweep", lambda: sweeper.sweep(now, state)),
        ("autoscale", lambda: autoscaler.autoscale_tick(now, state)),
        ("alerts", lambda: alerts.check(now, state)),
        ("status", status.publish_status),
    )
    for name, part in parts:
        try:
            part()
        except Exception:
            log.exception("controller: %s failed", name)


def run(stop: threading.Event) -> None:
    """Tick every CONTROLLER_TICK_S until `stop` is set; a tick in progress always finishes."""
    state = ControllerState()
    interval = get_settings().CONTROLLER_TICK_S
    while not stop.is_set():
        started = time.monotonic()
        tick(time.time(), state)
        stop.wait(max(interval - (time.monotonic() - started), 0))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    stop = threading.Event()

    def on_signal(signum, frame) -> None:
        log.info("received %s, stopping after this tick", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    log.info("controller started")
    run(stop)
    log.info("controller stopped")


if __name__ == "__main__":
    main()
