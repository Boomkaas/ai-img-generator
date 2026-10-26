"""One schema for every message the server sends.

The API, the controller and the workers build these models; `messages.md` next to this file shows one
example of each, and the frontend's TypeScript types are written from it. `type` is the discriminator,
so a raw message from pub/sub or a WebSocket parses back with `parse_message`.
"""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, SerializerFunctionWrapHandler, TypeAdapter, model_serializer

TaskStatus = Literal["queued", "running", "done", "failed", "cancelled"]
FinishedStatus = Literal["done", "failed", "cancelled"]
WorkerState = Literal["loading", "idle", "busy"]


class SystemStatus(BaseModel):
    type: Literal["system"] = "system"
    running: int
    waiting: int
    new_image_ready_in_s: float


class TaskUpdate(BaseModel):
    """A partial update: workers send progress and stage, the controller position and ready time.

    Fields left as None are omitted from the JSON, and the frontend merges what it receives.
    """

    type: Literal["task"] = "task"
    id: uuid.UUID
    status: Literal["queued", "running"]
    position: int | None = None
    queue_length: int | None = None
    progress: int | None = Field(None, ge=0, le=100)
    stage: str | None = None
    ready_in_s: float | None = None

    @model_serializer(mode="wrap")
    def _omit_unsent(self, handler: SerializerFunctionWrapHandler) -> dict:
        return {key: value for key, value in handler(self).items() if value is not None}


class TaskFinal(BaseModel):
    type: Literal["task_final"] = "task_final"
    id: uuid.UUID
    status: FinishedStatus


class TaskOut(BaseModel):
    """What the HTTP API returns for a task."""

    task_id: uuid.UUID
    status: TaskStatus
    prompt: str
    size: int
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    position: int | None = None
    queue_length: int | None = None
    progress: int | None = Field(None, ge=0, le=100)
    stage: str | None = None
    ready_in_s: float | None = None
    error_message: str | None = None


class TaskPage(BaseModel):
    """One page of history, newest first; pass `next_before` back as `before` for the next page."""

    items: list[TaskOut]
    next_before: datetime | None = None


class Snapshot(BaseModel):
    """Sent right after a WebSocket connects: the system status and the user's active task, if any."""

    type: Literal["snapshot"] = "snapshot"
    system: SystemStatus
    task: TaskOut | None = None


class Ping(BaseModel):
    """Sent on a fixed interval, so the client can tell a quiet socket from a dead one."""

    type: Literal["ping"] = "ping"


# --- Admin overview -----------------------------------------------------------------------------------------


class ApiStats(BaseModel):
    sockets: int
    requests_per_min: int


class MachineItem(BaseModel):
    name: str
    role: str
    cpu: float
    ram: float
    api: ApiStats | None = None  # only on machines that run an API server


class WorkerItem(BaseModel):
    name: str
    machine: str
    state: WorkerState
    task_id: uuid.UUID | None = None
    progress: int | None = None
    rss_mb: int


class TaskItem(BaseModel):
    id: uuid.UUID
    user_email: str
    size: int
    status: Literal["queued", "running"]
    worker_host: str | None = None
    progress: int | None = None
    position: int | None = None
    seconds_so_far: float


class Alert(BaseModel):
    at: datetime
    message: str


class AutoscalerItem(BaseModel):
    desired: int
    ready: int
    starting: int
    maximum: int
    last_action: str | None = None
    alerts: list[Alert] = []


class AdminOverview(BaseModel):
    type: Literal["admin"] = "admin"
    machines: list[MachineItem]
    workers: list[WorkerItem]
    tasks: list[TaskItem]
    autoscaler: AutoscalerItem


Message = Annotated[
    SystemStatus | TaskUpdate | TaskFinal | Snapshot | Ping | AdminOverview,
    Field(discriminator="type"),
]
_message_adapter: TypeAdapter[Message] = TypeAdapter(Message)


def parse_message(raw: str | bytes) -> Message:
    """Parse a JSON message into the model its `type` names."""
    return _message_adapter.validate_json(raw)
