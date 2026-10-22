"""Time the real pipeline on this machine and print the timeouts and worker sizing to configure.

    docker run --rm --cpus 2 --memory 10g -e STEP_DELAY_SECONDS=2 <worker image, real target> \
        python -m scripts.calibrate

Runs one 512 px job (one cost unit) and one job at the largest allowed size, with this machine's
WORKER_THREADS and STEP_DELAY_SECONDS. Run it on every app machine and keep the slowest result.
Exits 1 when the largest job no longer fits under the configured TASK_SOFT_TIME_LIMIT.
"""

import math
import os
import resource
import secrets
import socket
import sys
import time
from dataclasses import dataclass

import psutil

from common.config import Settings, get_settings

PROMPT = "a lighthouse on a rocky coast at sunset, oil painting"
UNIT_SIZE = 512  # the size of one cost unit
GB = 1024**3
DERIVED = (
    "TASK_SOFT_TIME_LIMIT",
    "TASK_TIME_LIMIT",
    "BROKER_VISIBILITY_TIMEOUT",
    "WORKER_STOP_GRACE_S",
    "CANCEL_FLAG_TTL_S",
)


@dataclass(frozen=True)
class Measurement:
    load_s: float
    unit_s: float  # one 512 px job
    max_size: int
    max_s: float  # one job at max_size
    peak_bytes: int
    ram_bytes: int
    cores: int


@dataclass(frozen=True)
class Recommendation:
    timeouts: Settings  # MEASURED_MAX_RUNTIME_S, CALIBRATED_SECONDS_PER_COST_UNIT and the derived chain
    stop_grace_period: str
    reservation_gb: int
    limit_gb: int
    workers_by_ram: int
    workers_by_cpu: int

    @property
    def workers(self) -> int:
        return min(self.workers_by_ram, self.workers_by_cpu)


def recommend(m: Measurement, settings: Settings) -> Recommendation:
    values = settings.model_dump()
    values.update(
        {name: None for name in DERIVED},
        MEASURED_MAX_RUNTIME_S=math.ceil(m.max_s),
        CALIBRATED_SECONDS_PER_COST_UNIT=math.ceil(m.unit_s),
    )
    timeouts = Settings(_env_file=None, **values)
    reservation = math.ceil(m.peak_bytes / GB + settings.WORKER_MEMORY_MARGIN_GB)
    free_ram = m.ram_bytes / GB - settings.APP_MACHINE_RESERVED_RAM_GB
    free_cores = m.cores - settings.APP_MACHINE_RESERVED_CORES
    return Recommendation(
        timeouts=timeouts,
        stop_grace_period=f"{math.ceil(timeouts.WORKER_STOP_GRACE_S / 60)}m",
        reservation_gb=reservation,
        limit_gb=reservation + settings.WORKER_MEMORY_MARGIN_GB,
        workers_by_ram=max(math.floor(free_ram / reservation), 0),
        workers_by_cpu=max(free_cores // settings.WORKER_THREADS, 0),
    )


def report(m: Measurement, r: Recommendation, settings: Settings) -> str:
    t = r.timeouts
    derived = " ".join(f"{name}={getattr(t, name)}" for name in DERIVED)
    ram_gb = m.ram_bytes / GB
    return f"""\
Measured on {socket.gethostname()}: {m.cores} cores, {ram_gb:.1f} GB RAM, \
WORKER_THREADS={settings.WORKER_THREADS}, STEP_DELAY_SECONDS={settings.STEP_DELAY_SECONDS:g}
  model load {m.load_s:.0f} s, {UNIT_SIZE} px job {m.unit_s:.0f} s, {m.max_size} px job {m.max_s:.0f} s, \
peak memory {m.peak_bytes / GB:.1f} GB

# common/config.py (defaults)
MEASURED_MAX_RUNTIME_S = {t.MEASURED_MAX_RUNTIME_S}
CALIBRATED_SECONDS_PER_COST_UNIT = {t.CALIBRATED_SECONDS_PER_COST_UNIT}
# derived from it: {derived}

# infra/stack.yml, service worker
stop_grace_period: {r.stop_grace_period}
reservations: {{cpus: "{settings.WORKER_THREADS}", memory: {r.reservation_gb}G}}
limits: {{cpus: "{settings.WORKER_THREADS}", memory: {r.limit_gb}G}}

# Workers that fit on this machine; MAX_WORKERS is the sum over the app machines
min(floor(({ram_gb:.1f} - {settings.APP_MACHINE_RESERVED_RAM_GB}) / {r.reservation_gb}), \
floor(({m.cores} - {settings.APP_MACHINE_RESERVED_CORES}) / {settings.WORKER_THREADS})) \
= min({r.workers_by_ram}, {r.workers_by_cpu}) = {r.workers}
"""


def run_job(pipe, size: int, settings: Settings) -> float:
    """One job as the worker runs it, including the pause after every step."""

    def on_step_end(pipeline, step, timestep, callback_kwargs):
        if settings.STEP_DELAY_SECONDS:
            time.sleep(settings.STEP_DELAY_SECONDS)
        return callback_kwargs

    started = time.monotonic()
    pipe(
        prompt=PROMPT,
        num_inference_steps=settings.NUM_STEPS,
        height=size,
        width=size,
        callback_on_step_end=on_step_end,
    )
    return time.monotonic() - started


def measure(settings: Settings) -> Measurement:
    from worker.pipeline import load_pipeline

    started = time.monotonic()
    pipe = load_pipeline()
    load_s = time.monotonic() - started
    max_size = max(settings.ALLOWED_SIZES)
    print(f"Model loaded in {load_s:.0f} s; running a {UNIT_SIZE} px job...", flush=True)
    unit_s = run_job(pipe, UNIT_SIZE, settings)
    print(f"{UNIT_SIZE} px: {unit_s:.0f} s; running a {max_size} px job...", flush=True)
    max_s = run_job(pipe, max_size, settings)
    return Measurement(
        load_s=load_s,
        unit_s=unit_s,
        max_size=max_size,
        max_s=max_s,
        peak_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,  # Linux reports KiB
        ram_bytes=psutil.virtual_memory().total,
        cores=psutil.cpu_count(logical=True),
    )


def main() -> int:
    # Settings refuse to load without a signing key, and nothing is signed here.
    os.environ.setdefault("JWT_SECRET", secrets.token_hex(32))
    settings = get_settings()
    if settings.PIPELINE != "real":
        print("Calibration needs PIPELINE=real (the real worker image).", file=sys.stderr)
        return 2

    m = measure(settings)
    print()
    print(report(m, recommend(m, settings), settings))
    status = 0
    if m.max_s >= settings.TASK_SOFT_TIME_LIMIT:
        print(
            f"The {m.max_size} px job took longer than the configured TASK_SOFT_TIME_LIMIT "
            f"({settings.TASK_SOFT_TIME_LIMIT} s): update the configuration before deploying.",
            file=sys.stderr,
        )
        status = 1
    if m.load_s >= settings.WORKER_PROC_ALIVE_TIMEOUT:
        print(
            f"Loading the model took longer than WORKER_PROC_ALIVE_TIMEOUT "
            f"({settings.WORKER_PROC_ALIVE_TIMEOUT} s): Celery would kill the worker while it loads.",
            file=sys.stderr,
        )
        status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())
