import pytest
from alembic import command
from sqlalchemy import inspect

from common import db

pytestmark = pytest.mark.api

TABLES = {"users", "refresh_tokens", "tasks"}


def test_downgrade_then_upgrade(alembic_cfg):
    db.dispose_engine()
    command.downgrade(alembic_cfg, "base")
    assert not TABLES & set(inspect(db.get_engine()).get_table_names())
    db.dispose_engine()
    command.upgrade(alembic_cfg, "head")
    assert set(inspect(db.get_engine()).get_table_names()) >= TABLES


def plain_indexes(inspector, table):
    """Index names, without the ones Postgres creates behind UNIQUE constraints."""
    return {i["name"] for i in inspector.get_indexes(table) if "duplicates_constraint" not in i}


def test_table_objects_mirror_the_migration():
    inspector = inspect(db.get_engine())
    for table in (db.users, db.refresh_tokens, db.tasks):
        assert [c["name"] for c in inspector.get_columns(table.name)] == list(table.c.keys())
    assert plain_indexes(inspector, "tasks") == {"tasks_user_history", "tasks_active"}
    assert plain_indexes(inspector, "users") == {"users_email_unique"}
    assert [c["column_names"] for c in inspector.get_unique_constraints("tasks")] == [
        ["user_id", "idempotency_key"]
    ]
