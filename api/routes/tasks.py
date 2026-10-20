"""Submit, follow and cancel your own tasks, and the system status."""

import uuid
from datetime import datetime
from typing import Annotated

from botocore.exceptions import ClientError
from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from api.deps import CurrentUser
from api.services import tasks as service
from common import db, storage
from common.config import get_settings
from common.messages import SystemStatus, TaskOut, TaskPage
from common.validation import SubmitBody

router = APIRouter(prefix="/api", tags=["tasks"])

NO_IMAGE = "There is no image for this task"


def _idempotency_key(value: str | None) -> uuid.UUID:
    try:
        return uuid.UUID(value or "")
    except ValueError:
        raise HTTPException(400, "Send an Idempotency-Key header with a new UUID for each image") from None


@router.get("/status", response_model=SystemStatus)
def status(user: CurrentUser) -> SystemStatus:
    return service.system_status()


@router.post("/tasks", status_code=202, response_model=TaskOut)
def submit(
    body: SubmitBody, user: CurrentUser, idempotency_key: Annotated[str | None, Header()] = None
) -> TaskOut | JSONResponse:
    key = _idempotency_key(idempotency_key)
    try:
        return service.submit(user, body, key)
    except service.QueueFull as full:
        return full.response()


# Declared before /tasks/{task_id}, which would otherwise take "active" for an id.
@router.get("/tasks/active", response_model=TaskOut | None)
def active_task(user: CurrentUser) -> TaskOut | None:
    task = db.get_active_task(user.id)
    return service.task_out(task) if task else None


@router.get("/tasks", response_model=TaskPage)
def history(
    user: CurrentUser,
    before: datetime | None = None,
    limit: Annotated[int | None, Query(ge=1)] = None,
) -> TaskPage:
    return service.history(user.id, before, limit)


@router.get("/tasks/{task_id}", response_model=TaskOut)
def get_task(task_id: uuid.UUID, user: CurrentUser) -> TaskOut:
    task = db.get_task(task_id, user_id=user.id)
    if task is None:
        raise HTTPException(404, service.NO_SUCH_TASK)
    return service.task_out(task)


@router.get("/tasks/{task_id}/image", response_class=StreamingResponse)
def task_image(task_id: uuid.UUID, user: CurrentUser) -> StreamingResponse:
    task = db.get_task(task_id, user_id=user.id)  # another user's task looks like a missing one
    if task is None or task.status != db.DONE or not task.result_key:
        raise HTTPException(404, NO_IMAGE)
    try:
        stream = storage.open(task.result_key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "NoSuchKey":
            raise
        raise HTTPException(404, NO_IMAGE) from None
    return StreamingResponse(
        stream,
        media_type="image/png",
        headers={"Cache-Control": f"private, max-age={get_settings().IMAGE_CACHE_MAX_AGE_S}"},
        background=BackgroundTask(stream.close),  # also when the client leaves before the last chunk
    )


@router.delete("/tasks/{task_id}", response_model=service.CancelOut)
def cancel_own_task(task_id: uuid.UUID, user: CurrentUser) -> service.CancelOut:
    return service.cancel(task_id, owner_id=user.id)
