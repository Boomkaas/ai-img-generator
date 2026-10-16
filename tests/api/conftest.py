"""Postgres from testcontainers, migrated once per session; every test starts with empty tables."""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from testcontainers.postgres import PostgresContainer

from common import db
from common.config import get_settings

ROOT = Path(__file__).resolve().parents[2]


def alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


@pytest.fixture(scope="session")
def postgres_url():
    with PostgresContainer("postgres:16", driver="psycopg") as pg:
        yield pg.get_connection_url()


@pytest.fixture(scope="session")
def migrated_db(postgres_url):
    patch = pytest.MonkeyPatch()
    patch.setenv("DATABASE_URL", postgres_url)
    get_settings.cache_clear()
    db.dispose_engine()
    command.upgrade(alembic_config(postgres_url), "head")
    yield postgres_url
    db.dispose_engine()
    get_settings.cache_clear()
    patch.undo()


@pytest.fixture
def alembic_cfg(migrated_db) -> Config:
    return alembic_config(migrated_db)


@pytest.fixture(autouse=True)
def clean_tables(migrated_db):
    with db.transaction() as tx:
        tx.execute(text("TRUNCATE users, refresh_tokens, tasks CASCADE"))
