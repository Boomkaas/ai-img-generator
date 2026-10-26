"""The generation task: one job at a time, progress after every step, cooperative cancellation."""

import gc
import logging
import time

from celery.exceptions import SoftTimeLimitExceeded
from celery.signals import worker_process_init
from redis import RedisError
from sqlalchemy.exc import OperationalError

from common import db, live, storage
from common.config import get_settings
from worker.app import celery_app
from worker.pipeline import load_pipeline

log = logging.getLogger(__name__)

settings = get_settings()
HOST = settings.WORKER_NAME
pipe = None


@worker_process_init.connect
def init_worker_process(**_):
    # Runs in the pool child, never in the parent: torch's thread pools don't survive a fork.
    global pipe
    live.start_heartbeat(HOST, state="loading")
    pipe = load_pipeline()
    live.set_worker_state(HOST, "idle")


class TaskCancelled(Exception):
    pass


# Progress and the cancel flag only make the page livelier and a cancel quicker; Postgres has the real state.
# A Redis restart in the middle of a job must not fail it.
def report_progress(user_id: str, task_id: str, progress: int, stage: str) -> None:
    try:
        live.publish_progress(user_id, task_id, progress=progress, stage=stage)
    except RedisError:
        log.warning("could not publish the progress of task %s", task_id, exc_info=True)


def cancel_requested(task_id: str) -> bool:
    """False while Redis can't answer; a cancel then shows at a later step, and the finish guard refuses
    'done' for a cancelled task in any case."""
    try:
        return live.cancel_requested(task_id)
    except RedisError:
        log.warning("could not check task %s for a cancel", task_id, exc_info=True)
        return False


def pause_unless_cancelled(task_id: str, seconds: float) -> None:
    """Sleep for `seconds`, checking for a cancel every half second."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cancel_requested(task_id):
            raise TaskCancelled()
        time.sleep(max(0.0, min(0.5, end - time.monotonic())))


def result_key(task_id: str) -> str:
    return f"results/{task_id}.png"


# Acks are late, but Celery still acks a task that raises, so a database outage at the claim would leave the
# row queued with no message behind it. Retrying is safe: once a claim succeeds, every later one is refused.
@celery_app.task(
    name="worker.tasks.generate_image",
    soft_time_limit=settings.TASK_SOFT_TIME_LIMIT,
    time_limit=settings.TASK_TIME_LIMIT,
    autoretry_for=(OperationalError,),
    max_retries=settings.WORKER_DB_MAX_RETRIES,
    retry_backoff=True,
    retry_backoff_max=settings.WORKER_DB_RETRY_BACKOFF_MAX_S,
    retry_jitter=False,
)
def generate_image(task_id: str, user_id: str, prompt: str, size: int) -> None:
    # Only a task that is still queued may start. This also skips one cancelled while queued, even on a
    # worker that started after the revoke was broadcast.
    if not db.claim_task(task_id, worker_host=HOST):
        return
    live.set_worker_state(HOST, "busy", task_id=task_id)
    steps, step_delay = settings.NUM_STEPS, settings.STEP_DELAY_SECONDS

    def on_step_end(pipeline, step, timestep, callback_kwargs):
        done = step + 1
        if cancel_requested(task_id):
            raise TaskCancelled()
        stage = "decoding image" if done == steps else f"step {done} of {steps}"
        report_progress(user_id, task_id, progress=done * 90 // steps, stage=stage)
        if step_delay:
            pause_unless_cancelled(task_id, step_delay)
        return callback_kwargs

    key = result_key(task_id)
    image = None
    try:
        report_progress(user_id, task_id, progress=0, stage="starting")
        image = pipe(
            prompt=prompt,
            num_inference_steps=steps,
            height=size,
            width=size,
            callback_on_step_end=on_step_end,
        ).images[0]
        report_progress(user_id, task_id, progress=95, stage="saving")
        storage.put_png(key, image)
        # Cancelled during the last step: the guard refuses 'done' and the image must not outlive the task.
        if not db.finish_task(task_id, db.DONE, result_key=key):
            storage.delete(key)
    except TaskCancelled:
        pass  # whoever set the flag has already set 'cancelled'
    except SoftTimeLimitExceeded:
        db.finish_task(task_id, db.FAILED, error="time limit exceeded")
    except Exception as exc:
        log.exception("task %s failed", task_id)
        db.finish_task(task_id, db.FAILED, error=str(exc)[:500])
    finally:
        image = None
        gc.collect()  # keeps the worker's RAM flat across jobs
        try:
            live.clear_task_keys(task_id)
        except Exception:
            # Both keys expire on their own; the idle state must not wait for Redis.
            log.warning("could not clear the live keys of task %s", task_id, exc_info=True)
        live.set_worker_state(HOST, "idle")
        # Read back rather than assume: a refused finish means someone else set the final status.
        task = db.get_task(task_id)
        if task is not None and task.status in db.FINISHED_STATUSES:
            live.publish_task_final(user_id, task_id, task.status)
