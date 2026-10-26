"""Postgres, Redis and MinIO from testcontainers, one of each per session.

Postgres is migrated once and every test starts with empty tables; a test that uses `redis_live` starts with
an empty Redis, and one that uses `bucket` gets an existing bucket.
"""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text
from testcontainers.minio import MinioContainer
from testcontainers.postgres import PostgresContainer
from testcontainers.redis import RedisContainer

from common import db, live, storage
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


@pytest.fixture(scope="session")
def redis_url():
    with RedisContainer("redis:7") as container:
        yield f"redis://{container.get_container_host_ip()}:{container.get_exposed_port(6379)}/0"


@pytest.fixture
def redis_live(redis_url, monkeypatch):
    """The live module pointed at the test Redis, emptied first; yields the sync client."""
    monkeypatch.setenv("REDIS_URL", redis_url)
    get_settings.cache_clear()
    live.reset_client()
    live.client().flushdb()
    yield live.client()
    live.reset_client()
    get_settings.cache_clear()


def _no_password_hash(response) -> None:
    response.read()
    assert b"password_hash" not in response.content
    assert b"$argon2" not in response.content


@pytest.fixture
def make_client(migrated_db, redis_live, monkeypatch):
    """Builds clients, each on its own create_app() instance sharing the test database, Redis and secret.

    Cookies aren't Secure here (COOKIE_SECURE=false), because the test client talks plain HTTP and would drop
    them. Every response any client receives is checked for password hashes.
    """
    from api.main import create_app

    monkeypatch.setenv("COOKIE_SECURE", "false")
    get_settings.cache_clear()
    clients = []

    def make(app=None) -> TestClient:
        client = TestClient(app or create_app())
        client.event_hooks["response"] = [_no_password_hash]
        client.__enter__()  # runs the lifespan
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.__exit__(None, None, None)
    get_settings.cache_clear()


@pytest.fixture
def client(make_client) -> TestClient:
    return make_client()


@pytest.fixture(scope="session")
def minio():
    # Community build of MinIO, as in the compose files.
    with MinioContainer(
        "pgsty/minio", access_key="imagegen-test", secret_key="imagegen-test-secret"
    ) as container:
        yield container.get_config()


@pytest.fixture
def bucket(minio, monkeypatch):
    """The storage module pointed at the test MinIO, with its bucket created."""
    monkeypatch.setenv("S3_ENDPOINT", f"http://{minio['endpoint']}")
    monkeypatch.setenv("S3_ACCESS_KEY", minio["access_key"])
    monkeypatch.setenv("S3_SECRET_KEY", minio["secret_key"])
    monkeypatch.setenv("S3_BUCKET", "imagegen-test")
    get_settings.cache_clear()
    storage.reset_client()
    storage.ensure_bucket()
    yield "imagegen-test"
    storage.reset_client()
    get_settings.cache_clear()
