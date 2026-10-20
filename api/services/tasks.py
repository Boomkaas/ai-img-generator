"""Submitting, reading and cancelling tasks."""

import logging
import math
import uuid
from datetime import datetime

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import Row

from common import celery_client, db, estimate, live
from common.config import get_settings
from common.messages import SystemStatus, TaskOut, TaskPage, TaskStatus
from common.validation import SubmitBody, ValidationProblem, validate

log = logging.getLogger(__name__)

NO_SUCH_TASK = "There is no such task"


class CancelOut(BaseModel):
    status: TaskStatus


class QueueFull(Exception):
    """Every queue slot is taken; the client may try again after `retry_in_s` seconds."""

    def __init__(self, retry_in_s: int):
        super().__init__(retry_in_s)
        self.retry_in_s = retry_in_s

    def response(self) -> JSONResponse:
        return JSONResponse(
            {"detail": "The queue is full", "retry_in_s": self.retry_in_s},
            status_code=503,
            headers={"Retry-After": str(self.retry_in_s)},
        )


def submit(user: Row, body: SubmitBody, idempotency_key: uuid.UUID) -> TaskOut:
    """Create and enqueue a task, or return the one this key already created; raises QueueFull."""
    try:
        prompt = validate(body)
    except ValidationProblem as problem:
        raise HTTPException(422, problem.message) from None
    task, created = _reserve(user.id, prompt, body.size, idempotency_key)
    if created:
        _enqueue(task)
    return task_out(task)


def _reserve(user_id, prompt: str, size: int, idempotency_key: uuid.UUID) -> tuple[Row, bool]:
    settings = get_settings()
    with db.transaction() as tx:
        # Serialises every submit in the system for a few milliseconds, so two clicks that land on
        # different API servers can't both slip past the caps.
        db.advisory_lock(tx)
        existing = db.find_by_idempotency_key(tx, user_id, idempotency_key)
        if existing is not None:
            return existing, False  # a retried request: the same task, no duplicate
        if db.count_active_for_user(tx, user_id) >= settings.MAX_ACTIVE_TASKS_PER_USER:
            raise HTTPException(429, "You already have an image in progress")
        if db.count_queued(tx) < settings.MAX_QUEUED_TOTAL:
            return db.insert_task(tx, user_id, prompt, size, idempotency_key), True
    # Estimated after the lock is released: it reads every active task and the workers' heartbeats.
    raise QueueFull(max(1, math.ceil(estimate.estimate_wait_seconds())))


def _enqueue(task: Row) -> None:
    # Only after the commit, so the worker's claim always finds the row.
    try:
        celery_client.send_generate(task)
    except Exception:
        log.exception("could not enqueue task %s", task.id)
        db.fail_queued(task.id, "queue unavailable")
        raise HTTPException(503, "The queue is unavailable, try again shortly") from None


def cancel(task_id: uuid.UUID, owner_id) -> CancelOut:
    """Cancel a queued or running task; owner_id=None cancels any user's. Safe to repeat."""
    user_id = db.cancel_task(task_id, owner_id)
    if user_id is None:
        task = db.get_task(task_id, user_id=owner_id)
        if task is None:
            raise HTTPException(404, NO_SUCH_TASK)  # another user's task looks like a missing one
        return CancelOut(status=task.status)  # already finished: nothing to do
    # The row is already cancelled, and the worker's claim and finish guards respect that on their own.
    # The flag and the revoke only make a worker stop sooner, so a Redis outage mustn't fail the request.
    try:
        live.request_cancel(task_id, ttl=get_settings().CANCEL_FLAG_TTL_S)
        celery_client.revoke(task_id)
        live.publish_task_final(user_id, task_id, db.CANCELLED)
    except Exception:
        log.warning("task %s is cancelled, but the workers could not be told", task_id, exc_info=True)
    return CancelOut(status=db.CANCELLED)


def task_out(task: Row) -> TaskOut:
    out = TaskOut(
        task_id=task.id,
        status=task.status,
        prompt=task.prompt,
        size=task.size,
        created_at=task.created_at,
        started_at=task.started_at,
        finished_at=task.finished_at,
        error_message=task.error_message if task.status == db.FAILED else None,
    )
    if task.status == db.QUEUED:
        out.position = db.queue_position(task)
        out.queue_length = db.count_tasks(db.QUEUED)
    elif task.status == db.RUNNING:
        out.progress, out.stage = live.progress_detail(task.id) or (0, None)
    if task.status in db.ACTIVE_STATUSES:
        out.ready_in_s = estimate.ready_in_seconds(task)
    return out


def system_status() -> SystemStatus:
    return SystemStatus(
        running=db.count_tasks(db.RUNNING),
        waiting=db.count_tasks(db.QUEUED),
        new_image_ready_in_s=estimate.new_image_ready_in_s(),
    )


def history(user_id, before: datetime | None, limit: int | None) -> TaskPage:
    settings = get_settings()
    limit = min(limit or settings.HISTORY_PAGE_SIZE, settings.HISTORY_PAGE_MAX)
    # One row more than the page tells whether another page follows, so the last page has no cursor.
    rows = db.history_page(user_id, before=before, limit=limit + 1)
    page = rows[:limit]
    next_before = page[-1].created_at if len(rows) > limit else None
    return TaskPage(items=[task_out(task) for task in page], next_before=next_before)
