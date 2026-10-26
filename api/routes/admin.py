"""Admin-only endpoints."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import Row

from api.deps import require_admin
from api.services import tasks as service
from common import overview
from common.messages import AdminOverview

router = APIRouter(prefix="/api/admin", tags=["admin"])

Admin = Annotated[Row, Depends(require_admin)]


@router.delete("/tasks/{task_id}", response_model=service.CancelOut)
def cancel_any_task(task_id: uuid.UUID, admin: Admin) -> service.CancelOut:
    return service.cancel(task_id, owner_id=None)


@router.get("/overview", response_model=AdminOverview)
def admin_overview(admin: Admin) -> AdminOverview:
    """The admin page's first paint; the admin channel carries the same object every few seconds after."""
    return overview.admin_overview()
