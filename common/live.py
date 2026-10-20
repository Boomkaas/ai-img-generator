"""Live state in Redis: progress, cancel flags, heartbeats, controller state and pub/sub.

This is the only module that names Redis keys and channels. Nothing here is the source of truth for a task:
Postgres is. Everything in Redis either expires or is rewritten every few seconds, so losing it costs at most
a few seconds of stale numbers.
"""

import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache

import psutil
import redis
import redis.asyncio
from pydantic import BaseModel

from common.config import get_settings
from common.messages import Alert, FinishedStatus, TaskFinal, TaskUpdate, WorkerState

log = logging.getLogger(__name__)

# --- Keys and channels --------------------------------------------------------------------------------------

GENERATION_QUEUE = "image_generation"  # the Celery queue, a Redis list under this name
SYSTEM_CHANNEL = "system"
ADMIN_CHANNEL = "admin"
USER_CHANNEL_PATTERN = "user:*"  # each API process subscribes to this once
AUTOSCALER_KEY = "controller:autoscaler"
ALERTS_KEY = "controller:alerts"
_WORKER_PREFIX = "hb:worker:"
_API_PREFIX = "hb:api:"
_MACHINE_PREFIX = "hb:machine:"


def user_channel(user_id) -> str:
    return f"user:{user_id}"


def progress_key(task_id) -> str:
    return f"progress:{task_id}"


def cancel_key(task_id) -> str:
    return f"cancel:{task_id}"


def worker_key(host: str) -> str:
    return f"{_WORKER_PREFIX}{host}"


def api_key(machine: str, pid: int) -> str:
    return f"{_API_PREFIX}{machine}:{pid}"


def machine_key(name: str) -> str:
    return f"{_MACHINE_PREFIX}{name}"


# --- Clients ------------------------------------------------------------------------------------------------


@lru_cache
def client() -> redis.Redis:
    """The process-wide sync client (workers, controller, HTTP handlers); thread-safe, pooled.

    Every call is bounded, so a Redis that stops answering raises instead of hanging a worker or a handler.
    """
    settings = get_settings()
    return redis.Redis.from_url(
        settings.REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=settings.REDIS_CONNECT_TIMEOUT_S,
        socket_timeout=settings.REDIS_SOCKET_TIMEOUT_S,
    )


def reset_client() -> None:
    """Close the pool and forget the client (tests, and after a fork)."""
    if client.cache_info().currsize:
        client().close()
    client.cache_clear()


def async_client() -> redis.asyncio.Redis:
    """A new asyncio client; the caller owns it and closes it (the WebSocket fan-out).

    Only connecting is bounded: a pub/sub subscription can rightly wait minutes for its next message.
    """
    settings = get_settings()
    return redis.asyncio.Redis.from_url(
        settings.REDIS_URL, decode_responses=True, socket_connect_timeout=settings.REDIS_CONNECT_TIMEOUT_S
    )


def redis_run_id() -> str:
    """Changes whenever Redis restarts, which tells the sweeper queued messages may be lost."""
    return client().info("server")["run_id"]


# --- Pub/sub and progress -----------------------------------------------------------------------------------


def publish(channel: str, message: BaseModel) -> None:
    client().publish(channel, message.model_dump_json())


def publish_progress(user_id, task_id, progress: int, stage: str) -> None:
    """Store the latest progress for reconnecting clients and the admin panel, then tell the owner."""
    message = TaskUpdate(id=task_id, status="running", progress=progress, stage=stage)
    key = progress_key(task_id)
    pipe = client().pipeline()
    pipe.hset(key, mapping={"progress": progress, "stage": stage, "updated_at": time.time()})
    pipe.expire(key, get_settings().TASK_TIME_LIMIT)
    pipe.publish(user_channel(user_id), message.model_dump_json())
    pipe.execute()


def progress(task_id) -> int:
    """The latest progress in percent; 0 if the task hasn't reported any."""
    detail = progress_detail(task_id)
    return detail[0] if detail else 0


def progress_detail(task_id) -> tuple[int, str] | None:
    data = client().hgetall(progress_key(task_id))
    if not data:
        return None
    return int(data["progress"]), data["stage"]


def publish_task_final(user_id, task_id, status: FinishedStatus) -> None:
    publish(user_channel(user_id), TaskFinal(id=task_id, status=status))


# --- Cancellation -------------------------------------------------------------------------------------------


def request_cancel(task_id, ttl: int | None = None) -> None:
    client().set(cancel_key(task_id), "1", ex=ttl or get_settings().CANCEL_FLAG_TTL_S)


def cancel_requested(task_id) -> bool:
    return bool(client().exists(cancel_key(task_id)))


def clear_task_keys(task_id) -> None:
    client().delete(progress_key(task_id), cancel_key(task_id))


# --- Heartbeats ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkerHeartbeat:
    host: str
    state: WorkerState
    task_id: str | None
    since: float  # epoch seconds of the last state change
    machine: str
    machine_cpu: float
    machine_ram: float
    rss_mb: int


@dataclass(frozen=True)
class ApiHeartbeat:
    machine: str
    pid: int
    sockets: int
    requests_per_min: int
    machine_cpu: float
    machine_ram: float


@dataclass(frozen=True)
class MachineHeartbeat:
    machine: str
    role: str
    machine_cpu: float
    machine_ram: float


# In-process state of each worker this process runs, keyed by host; the heartbeat thread publishes it.
# The lock is held through each write, so writes reach Redis in order and an older beat never lands last.
_workers: dict[str, dict] = {}
_workers_lock = threading.Lock()


def machine_of(host: str) -> str:
    """The machine a worker runs on: "nodeB-worker-2" -> "nodeB"; without "-worker-", MACHINE_NAME."""
    if "-worker-" in host:
        return host.split("-worker-")[0]
    return get_settings().MACHINE_NAME


def _machine_figures() -> dict:
    # Whole-machine figures, even inside a container: what the Machines block shows.
    return {"machine_cpu": psutil.cpu_percent(interval=None), "machine_ram": psutil.virtual_memory().percent}


def _write_hash(key: str, mapping: dict, ttl: int) -> None:
    pipe = client().pipeline()
    pipe.hset(key, mapping=mapping)
    pipe.expire(key, ttl)
    pipe.execute()


def _write_worker_heartbeat(host: str) -> None:
    """Write one worker heartbeat; any error is logged, and the next beat tries again.

    Nothing may escape: a heartbeat thread that died would make a working worker look lost to the sweeper.
    """
    try:
        with _workers_lock:
            current = _workers[host]
            mapping = {
                "state": current["state"],
                "task_id": current["task_id"],
                "since": current["since"],
                "machine": machine_of(host),
                **_machine_figures(),
                "rss_mb": psutil.Process().memory_info().rss // 2**20,
            }
            _write_hash(worker_key(host), mapping, current["ttl"])
    except Exception:
        log.warning("could not write the heartbeat of %s", host, exc_info=True)


class Heartbeat(threading.Thread):
    """The daemon thread that rewrites one worker's heartbeat every `interval` seconds."""

    def __init__(self, host: str, interval: float):
        super().__init__(name=f"heartbeat-{host}", daemon=True)
        self.host = host
        self.interval = interval
        self._stopping = threading.Event()

    def run(self) -> None:
        while not self._stopping.wait(self.interval):
            _write_worker_heartbeat(self.host)

    def stop(self, timeout: float | None = None) -> None:
        """Stop beating and wait for a write in progress to finish (tests; a worker just exits)."""
        self._stopping.set()
        self.join(timeout)


def start_heartbeat(
    host: str, state: WorkerState, interval: float | None = None, ttl: int | None = None
) -> Heartbeat:
    """Write the worker's heartbeat now and then every `interval` seconds from a daemon thread."""
    settings = get_settings()
    with _workers_lock:
        _workers[host] = {
            "state": state,
            "task_id": "",
            "since": time.time(),
            "ttl": ttl or settings.HEARTBEAT_TTL_S,
        }
    _write_worker_heartbeat(host)
    beat = Heartbeat(host, interval or settings.HEARTBEAT_INTERVAL_S)
    beat.start()
    return beat


def set_worker_state(host: str, state: WorkerState, task_id=None) -> None:
    """Change the worker's state and write the heartbeat at once instead of within `interval`."""
    task_id = str(task_id) if task_id else ""
    with _workers_lock:
        current = _workers.setdefault(
            host,
            {"state": state, "task_id": task_id, "since": time.time(), "ttl": get_settings().HEARTBEAT_TTL_S},
        )
        if (current["state"], current["task_id"]) != (state, task_id):
            current.update(state=state, task_id=task_id, since=time.time())
    _write_worker_heartbeat(host)


def _read_hashes(prefix: str) -> list[tuple[str, dict]]:
    """Every live hash under `prefix`, as (key suffix, fields); one that expired meanwhile is skipped."""
    keys = list(client().scan_iter(match=f"{prefix}*"))
    pipe = client().pipeline(transaction=False)
    for key in keys:
        pipe.hgetall(key)
    return [(key[len(prefix) :], data) for key, data in zip(keys, pipe.execute(), strict=True) if data]


def worker_heartbeats() -> list[WorkerHeartbeat]:
    return [
        WorkerHeartbeat(
            host=host,
            state=data["state"],
            task_id=data["task_id"] or None,
            since=float(data["since"]),
            machine=data["machine"],
            machine_cpu=float(data["machine_cpu"]),
            machine_ram=float(data["machine_ram"]),
            rss_mb=int(data["rss_mb"]),
        )
        for host, data in _read_hashes(_WORKER_PREFIX)
    ]


def count_workers(states: tuple[str, ...]) -> int:
    return sum(hb.state in states for hb in worker_heartbeats())


def any_worker_idle_for(seconds: float) -> bool:
    now = time.time()
    return any(hb.state == "idle" and now - hb.since >= seconds for hb in worker_heartbeats())


def write_api_heartbeat(
    machine: str, pid: int, sockets: int, requests_per_min: int, ttl: int | None = None
) -> None:
    mapping = {
        "machine": machine,
        "sockets": sockets,
        "requests_per_min": requests_per_min,
        **_machine_figures(),
    }
    _write_hash(api_key(machine, pid), mapping, ttl or get_settings().HEARTBEAT_TTL_S)


def api_heartbeats() -> list[ApiHeartbeat]:
    return [
        ApiHeartbeat(
            machine=data["machine"],
            pid=int(suffix.rsplit(":", 1)[1]),
            sockets=int(data["sockets"]),
            requests_per_min=int(data["requests_per_min"]),
            machine_cpu=float(data["machine_cpu"]),
            machine_ram=float(data["machine_ram"]),
        )
        for suffix, data in _read_hashes(_API_PREFIX)
    ]


def write_machine_heartbeat(machine: str, role: str, ttl: int | None = None) -> None:
    mapping = {"machine": machine, "role": role, **_machine_figures()}
    _write_hash(machine_key(machine), mapping, ttl or get_settings().HEARTBEAT_TTL_S)


def machine_heartbeats() -> list[MachineHeartbeat]:
    return [
        MachineHeartbeat(
            machine=data["machine"],
            role=data["role"],
            machine_cpu=float(data["machine_cpu"]),
            machine_ram=float(data["machine_ram"]),
        )
        for _, data in _read_hashes(_MACHINE_PREFIX)
    ]


# --- Controller state ---------------------------------------------------------------------------------------

_AUTOSCALER_INTS = ("desired", "ready", "starting", "maximum")


def save_autoscaler_state(**fields) -> None:
    """Merge `fields` (desired, ready, starting, maximum, last_action) into the stored state."""
    mapping = {name: "" if value is None else value for name, value in fields.items()}
    client().hset(AUTOSCALER_KEY, mapping={**mapping, "updated_at": time.time()})


def autoscaler_state() -> dict:
    """The stored state with numbers parsed; empty before the controller's first tick."""
    state: dict = {name: value or None for name, value in client().hgetall(AUTOSCALER_KEY).items()}
    for name in _AUTOSCALER_INTS:
        if state.get(name) is not None:
            state[name] = int(state[name])
    if state.get("updated_at") is not None:
        state["updated_at"] = float(state["updated_at"])
    return state


def alert(message: str) -> None:
    """Record an alert for the admin panel, keeping only the newest MAX_ALERTS."""
    entry = Alert(at=datetime.now(UTC), message=message).model_dump_json()
    pipe = client().pipeline()
    pipe.lpush(ALERTS_KEY, entry)
    pipe.ltrim(ALERTS_KEY, 0, get_settings().MAX_ALERTS - 1)
    pipe.execute()


def recent_alerts() -> list[Alert]:
    """Newest first."""
    return [Alert.model_validate_json(raw) for raw in client().lrange(ALERTS_KEY, 0, -1)]
