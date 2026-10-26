"""Cleaning up after lost workers, stuck cancels, Redis restarts and old history."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from common import celery_client, db, live, storage
from common.config import get_settings

if TYPE_CHECKING:
    from controller.main import ControllerState

log = logging.getLogger(__name__)

LOST_WORKER = "the worker stopped unexpectedly"


def sweep(now: float, state: ControllerState) -> None:
    """Run every part; one that fails is logged and never stops the others."""
    for part in (fail_orphans, force_stop_cancelled, resend_after_redis_restart, trim_history):
        try:
            part(now, state)
        except Exception:
            log.exception("sweeper: %s failed", part.__name__)


def fail_orphans(now: float, state: ControllerState) -> None:
    """Fail running tasks no worker is working on: its container died, or the hard time limit killed it.

    The finish guard makes this safe even when the worker is just finishing.
    """
    settings = get_settings()
    # A Redis that restarted without its data has no heartbeats until each worker's next beat, and every
    # running task would look lost; wait until they have had time to come back.
    if live.redis_run_id() != state.redis_run_id or now - state.redis_changed_at < settings.HEARTBEAT_TTL_S:
        return
    working_on = {hb.task_id for hb in live.worker_heartbeats() if hb.task_id}
    for task in db.running_tasks(started_before=now - settings.ORPHAN_AFTER_S):
        if str(task.id) in working_on:
            continue
        if db.finish_task(task.id, db.FAILED, error=LOST_WORKER):
            log.warning("task %s failed: no worker has it", task.id)
            live.clear_task_keys(task.id)
            live.publish_task_final(task.user_id, task.id, db.FAILED)


def force_stop_cancelled(now: float, state: ControllerState) -> None:
    """Kill the worker process still busy with a task cancelled long ago (a step stuck in native code).

    Celery's pool starts a fresh process in its place, which loads the model again.
    """
    busy = {hb.task_id for hb in live.worker_heartbeats() if hb.state == "busy" and hb.task_id}
    state.force_stopped &= busy  # once no worker has it, nothing more can be stopped
    after = get_settings().FORCE_STOP_AFTER_S
    for task_id in busy - state.force_stopped:
        try:
            task = db.get_task(task_id)
        except ValueError:
            log.warning("a busy heartbeat names %r, which is not a task id", task_id)
            continue
        if task is None or task.status != db.CANCELLED or now - task.finished_at.timestamp() <= after:
            continue
        log.warning("task %s was cancelled %d s ago and is still running: killing its worker", task_id, after)
        celery_client.revoke(task_id, terminate=True)
        state.force_stopped.add(task_id)


def resend_after_redis_restart(now: float, state: ControllerState) -> None:
    """Send every queued task again when the controller starts or Redis restarted: messages may be gone.

    Duplicates are harmless, because only one claim can succeed.
    """
    run_id = live.redis_run_id()
    if run_id == state.redis_run_id:
        return
    queued = db.queued_tasks()
    for task in queued:
        celery_client.send_generate(task)
    log.info("redis run id is %s: sent %d queued tasks again", run_id, len(queued))
    state.redis_run_id = run_id
    state.redis_changed_at = now


def trim_history(now: float, state: ControllerState) -> None:
    """Delete each user's finished tasks beyond the history limit, image first, then row."""
    settings = get_settings()
    if now - state.last_cleanup < settings.HISTORY_CLEANUP_INTERVAL_S:
        return
    state.last_cleanup = now
    deleted = 0
    for task in db.tasks_beyond_history_limit(settings.MAX_HISTORY_PER_USER):
        if task.result_key:
            try:
                storage.delete(task.result_key)
            except Exception:
                # Keep the row, so the next cleanup tries the image again instead of losing track of it.
                log.warning("could not delete the image of task %s", task.id, exc_info=True)
                continue
        db.delete_task(task.id)
        deleted += 1
    if deleted:
        log.info("deleted %d tasks beyond the history limit", deleted)
