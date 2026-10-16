"""Initial schema: users, refresh_tokens, tasks (architecture §4.3, verbatim).

Revision ID: 0001
Revises:
Create Date: 2026-10-16
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE users (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email         TEXT NOT NULL,
    password_hash TEXT NOT NULL,                     -- argon2
    role          TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('user', 'admin')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX users_email_unique ON users (lower(email));

CREATE TABLE refresh_tokens (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash  TEXT NOT NULL UNIQUE,                -- only a hash is stored
    expires_at  TIMESTAMPTZ NOT NULL,
    revoked_at  TIMESTAMPTZ
);

CREATE TABLE tasks (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status          TEXT NOT NULL CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled')),
    prompt          TEXT NOT NULL,
    size            SMALLINT NOT NULL CHECK (size IN (256, 384, 512, 640, 768)),
    idempotency_key UUID NOT NULL,
    worker_host     TEXT,                            -- which worker, on which machine, ran it
    result_key      TEXT,                            -- object-storage key of the image
    error_message   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    UNIQUE (user_id, idempotency_key)
);
CREATE INDEX tasks_user_history ON tasks (user_id, created_at DESC);
CREATE INDEX tasks_active ON tasks (status, created_at) WHERE status IN ('queued', 'running');
""")


def downgrade() -> None:
    op.execute("DROP TABLE tasks")
    op.execute("DROP TABLE refresh_tokens")
    op.execute("DROP TABLE users")
