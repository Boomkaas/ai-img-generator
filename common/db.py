"""Postgres access with SQLAlchemy 2 Core (architecture §4.3, §6.1).

The functions under "Status changes" are the only code allowed to change `tasks.status`. Each one is a
single guarded UPDATE ... RETURNING, so when two parties race (a cancel and the last step, the sweeper and a
finishing worker) the first write wins and nothing overwrites done, failed or cancelled.
"""

import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import lru_cache

from sqlalchemy import (
    CheckConstraint,
    Column,
    Connection,
    Engine,
    ForeignKey,
    Index,
    MetaData,
    Row,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    delete,
    func,
    insert,
    select,
    tuple_,
    update,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID

from common.config import get_settings

QUEUED, RUNNING, DONE, FAILED, CANCELLED = "queued", "running", "done", "failed", "cancelled"
ACTIVE_STATUSES = (QUEUED, RUNNING)
FINISHED_STATUSES = (DONE, FAILED, CANCELLED)
SUBMIT_LOCK_ID = 1  # pg_advisory_xact_lock key that serialises every submit (§4.3)

# --- Tables: mirror migrations/versions/0001_initial_schema.py --------------------------------------------

metadata = MetaData()

users = Table(
    "users",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column("email", Text, nullable=False),
    Column("password_hash", Text, nullable=False),
    Column("role", Text, nullable=False, server_default="user"),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("role IN ('user', 'admin')"),
)
Index("users_email_unique", func.lower(users.c.email), unique=True)

refresh_tokens = Table(
    "refresh_tokens",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column("user_id", UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("token_hash", Text, nullable=False, unique=True),
    Column("expires_at", TIMESTAMP(timezone=True), nullable=False),
    Column("revoked_at", TIMESTAMP(timezone=True)),
)

tasks = Table(
    "tasks",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column("user_id", UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("status", Text, nullable=False),
    Column("prompt", Text, nullable=False),
    Column("size", SmallInteger, nullable=False),
    Column("idempotency_key", UUID(as_uuid=True), nullable=False),
    Column("worker_host", Text),
    Column("result_key", Text),
    Column("error_message", Text),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=func.now()),
    Column("started_at", TIMESTAMP(timezone=True)),
    Column("finished_at", TIMESTAMP(timezone=True)),
    CheckConstraint("status IN ('queued', 'running', 'done', 'failed', 'cancelled')"),
    CheckConstraint("size IN (256, 384, 512, 640, 768)"),
    UniqueConstraint("user_id", "idempotency_key"),
)
Index("tasks_user_history", tasks.c.user_id, tasks.c.created_at.desc())
Index(
    "tasks_active",
    tasks.c.status,
    tasks.c.created_at,
    postgresql_where=tasks.c.status.in_(ACTIVE_STATUSES),
)

# --- Engine and transactions --------------------------------------------------------------------------------


def normalise_url(url: str) -> str:
    """Use the psycopg 3 driver for plain postgresql:// URLs (the stack file uses those)."""
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


@lru_cache
def get_engine() -> Engine:
    settings = get_settings()
    # No overflow: DB_POOL_SIZE is the hard per-process connection count used in the sizing (§7.2).
    return create_engine(
        normalise_url(settings.DATABASE_URL),
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=0,
        pool_pre_ping=True,
    )


def dispose_engine() -> None:
    """Close the pool and forget the engine (tests, and after a fork)."""
    if get_engine.cache_info().currsize:
        get_engine().dispose()
    get_engine.cache_clear()


@contextmanager
def transaction() -> Iterator[Connection]:
    """One transaction: commits on success, rolls back on an exception."""
    with get_engine().begin() as tx:
        yield tx


def _one(stmt) -> Row | None:
    with transaction() as tx:
        return tx.execute(stmt).one_or_none()


def _all(stmt) -> Sequence[Row]:
    with transaction() as tx:
        return tx.execute(stmt).all()


def _scalar(stmt):
    with transaction() as tx:
        return tx.execute(stmt).scalar_one()


def _uuid(value) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _as_datetime(value: datetime | float | None) -> datetime | None:
    """Accept a datetime or epoch seconds (the controller works with time.time())."""
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromtimestamp(value, UTC)


# --- Users --------------------------------------------------------------------------------------------------


def create_user(email: str, password_hash: str, role: str = "user") -> Row:
    stmt = insert(users).values(email=email, password_hash=password_hash, role=role).returning(*users.c)
    return _one(stmt)


def get_user(user_id) -> Row | None:
    return _one(select(users).where(users.c.id == _uuid(user_id)))


def get_user_by_email(email: str) -> Row | None:
    return _one(select(users).where(func.lower(users.c.email) == func.lower(email)))


def set_role(user_id, role: str) -> bool:
    stmt = update(users).where(users.c.id == _uuid(user_id)).values(role=role).returning(users.c.id)
    return _one(stmt) is not None


# --- Refresh tokens -----------------------------------------------------------------------------------------


def insert_refresh_token(user_id, token_hash: str, expires_at: datetime) -> Row:
    stmt = (
        insert(refresh_tokens)
        .values(user_id=_uuid(user_id), token_hash=token_hash, expires_at=expires_at)
        .returning(*refresh_tokens.c)
    )
    return _one(stmt)


def get_refresh_token(token_hash: str) -> Row | None:
    return _one(select(refresh_tokens).where(refresh_tokens.c.token_hash == token_hash))


def revoke_refresh_token(token_hash: str) -> bool:
    """Revoke one token; False if it doesn't exist or was already revoked."""
    stmt = (
        update(refresh_tokens)
        .where(refresh_tokens.c.token_hash == token_hash, refresh_tokens.c.revoked_at.is_(None))
        .values(revoked_at=func.now())
        .returning(refresh_tokens.c.id)
    )
    return _one(stmt) is not None


def revoke_all_refresh_tokens(user_id) -> int:
    stmt = (
        update(refresh_tokens)
        .where(refresh_tokens.c.user_id == _uuid(user_id), refresh_tokens.c.revoked_at.is_(None))
        .values(revoked_at=func.now())
        .returning(refresh_tokens.c.id)
    )
    return len(_all(stmt))


# --- Submit helpers: run inside one transaction() together (§4.3) -------------------------------------------


def advisory_lock(tx: Connection) -> None:
    tx.execute(select(func.pg_advisory_xact_lock(SUBMIT_LOCK_ID)))


def find_by_idempotency_key(tx: Connection, user_id, key) -> Row | None:
    stmt = select(tasks).where(tasks.c.user_id == _uuid(user_id), tasks.c.idempotency_key == _uuid(key))
    return tx.execute(stmt).one_or_none()


def count_active_for_user(tx: Connection, user_id) -> int:
    stmt = (
        select(func.count())
        .select_from(tasks)
        .where(tasks.c.user_id == _uuid(user_id), tasks.c.status.in_(ACTIVE_STATUSES))
    )
    return tx.execute(stmt).scalar_one()


def count_queued(tx: Connection) -> int:
    stmt = select(func.count()).select_from(tasks).where(tasks.c.status == QUEUED)
    return tx.execute(stmt).scalar_one()


def insert_task(tx: Connection, user_id, prompt: str, size: int, idempotency_key) -> Row:
    stmt = (
        insert(tasks)
        .values(
            user_id=_uuid(user_id),
            status=QUEUED,
            prompt=prompt,
            size=size,
            idempotency_key=_uuid(idempotency_key),
        )
        .returning(*tasks.c)
    )
    return tx.execute(stmt).one()


# --- Reads --------------------------------------------------------------------------------------------------


def get_task(task_id, user_id=None) -> Row | None:
    """With a user id, another user's task looks like a missing one."""
    stmt = select(tasks).where(tasks.c.id == _uuid(task_id))
    if user_id is not None:
        stmt = stmt.where(tasks.c.user_id == _uuid(user_id))
    return _one(stmt)


def count_tasks(status: str) -> int:
    return _scalar(select(func.count()).select_from(tasks).where(tasks.c.status == status))


def queue_position(task: Row) -> int | None:
    """1-based place among queued tasks, oldest first; None if the task isn't queued."""
    stmt = (
        select(func.count())
        .select_from(tasks)
        .where(
            tasks.c.status == QUEUED,
            tuple_(tasks.c.created_at, tasks.c.id) <= tuple_(task.created_at, task.id),
        )
    )
    with transaction() as tx:
        if tx.execute(select(tasks.c.status).where(tasks.c.id == task.id)).scalar_one_or_none() != QUEUED:
            return None
        return tx.execute(stmt).scalar_one()


def active_tasks() -> Sequence[Row]:
    stmt = select(tasks).where(tasks.c.status.in_(ACTIVE_STATUSES)).order_by(tasks.c.created_at, tasks.c.id)
    return _all(stmt)


def running_tasks(started_before: datetime | float | None = None) -> Sequence[Row]:
    stmt = select(tasks).where(tasks.c.status == RUNNING)
    if started_before is not None:
        stmt = stmt.where(tasks.c.started_at < _as_datetime(started_before))
    return _all(stmt.order_by(tasks.c.started_at, tasks.c.id))


def queued_tasks(created_before: datetime | float | None = None) -> Sequence[Row]:
    """Queued tasks, oldest first."""
    stmt = select(tasks).where(tasks.c.status == QUEUED)
    if created_before is not None:
        stmt = stmt.where(tasks.c.created_at < _as_datetime(created_before))
    return _all(stmt.order_by(tasks.c.created_at, tasks.c.id))


def recent_done_tasks(limit: int) -> Sequence[Row]:
    stmt = select(tasks).where(tasks.c.status == DONE).order_by(tasks.c.finished_at.desc()).limit(limit)
    return _all(stmt)


def history_page(user_id, before: datetime | None = None, limit: int = 20) -> Sequence[Row]:
    """The user's tasks, newest first; `before` is the created_at cursor from the previous page."""
    stmt = select(tasks).where(tasks.c.user_id == _uuid(user_id))
    if before is not None:
        stmt = stmt.where(tasks.c.created_at < before)
    return _all(stmt.order_by(tasks.c.created_at.desc(), tasks.c.id.desc()).limit(limit))


def tasks_beyond_history_limit(limit: int) -> Sequence[Row]:
    """Finished tasks beyond each user's newest `limit` finished ones; never queued or running ones."""
    ranked = (
        select(
            tasks.c.id,
            tasks.c.user_id,
            tasks.c.result_key,
            tasks.c.created_at,
            func.row_number()
            .over(partition_by=tasks.c.user_id, order_by=(tasks.c.created_at.desc(), tasks.c.id.desc()))
            .label("rank"),
        )
        .where(tasks.c.status.in_(FINISHED_STATUSES))
        .subquery()
    )
    stmt = (
        select(ranked.c.id, ranked.c.user_id, ranked.c.result_key)
        .where(ranked.c.rank > limit)
        .order_by(ranked.c.user_id, ranked.c.created_at)
    )
    return _all(stmt)


# --- Status changes: the only places tasks.status may change (§6.1) -----------------------------------------


def claim_task(task_id, worker_host: str) -> bool:
    """queued -> running. False if the task was cancelled or already claimed."""
    stmt = (
        update(tasks)
        .where(tasks.c.id == _uuid(task_id), tasks.c.status == QUEUED)
        .values(status=RUNNING, started_at=func.now(), worker_host=worker_host)
        .returning(tasks.c.id)
    )
    return _one(stmt) is not None


def finish_task(task_id, status: str, result_key: str | None = None, error: str | None = None) -> bool:
    """running -> done or failed. False if the task is no longer running (e.g. cancelled)."""
    if status not in (DONE, FAILED):
        raise ValueError(f"finish_task sets 'done' or 'failed', not {status!r}")
    stmt = (
        update(tasks)
        .where(tasks.c.id == _uuid(task_id), tasks.c.status == RUNNING)
        .values(status=status, finished_at=func.now(), result_key=result_key, error_message=error)
        .returning(tasks.c.id)
    )
    return _one(stmt) is not None


def cancel_task(task_id, owner_id) -> uuid.UUID | None:
    """queued or running -> cancelled; returns the owner's id, or None if nothing changed.

    owner_id=None cancels any user's task (admin); otherwise another user's task is left alone.
    """
    stmt = update(tasks).where(tasks.c.id == _uuid(task_id), tasks.c.status.in_(ACTIVE_STATUSES))
    if owner_id is not None:
        stmt = stmt.where(tasks.c.user_id == _uuid(owner_id))
    row = _one(stmt.values(status=CANCELLED, finished_at=func.now()).returning(tasks.c.user_id))
    return row.user_id if row else None


def fail_queued(task_id, error: str) -> bool:
    """queued -> failed, when the job couldn't be enqueued."""
    stmt = (
        update(tasks)
        .where(tasks.c.id == _uuid(task_id), tasks.c.status == QUEUED)
        .values(status=FAILED, finished_at=func.now(), error_message=error)
        .returning(tasks.c.id)
    )
    return _one(stmt) is not None


# --- Deletion -----------------------------------------------------------------------------------------------


def delete_task(task_id) -> bool:
    return _one(delete(tasks).where(tasks.c.id == _uuid(task_id)).returning(tasks.c.id)) is not None
