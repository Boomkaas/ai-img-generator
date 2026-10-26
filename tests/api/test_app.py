import pytest

from common import db, live
from common.config import get_settings

pytestmark = pytest.mark.api

UNREACHABLE_DB = "postgresql+psycopg://nobody:nothing@127.0.0.1:1/none"
UNREACHABLE_REDIS = "redis://127.0.0.1:1/0"


def test_health_is_ok_when_db_and_redis_answer(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"db": "ok", "redis": "ok"}


@pytest.mark.parametrize(
    "variable, url, down", [("REDIS_URL", UNREACHABLE_REDIS, "redis"), ("DATABASE_URL", UNREACHABLE_DB, "db")]
)
def test_health_is_503_and_names_what_is_down(make_client, monkeypatch, variable, url, down):
    monkeypatch.setenv(variable, url)
    get_settings.cache_clear()
    db.dispose_engine()
    live.reset_client()
    client = make_client()

    response = client.get("/api/health")
    assert response.status_code == 503
    body = response.json()
    assert body[down] == "down"
    assert body["db" if down == "redis" else "redis"] == "ok"
    assert down in body["detail"]


def test_requests_are_counted_per_instance_without_health_probes(make_client):
    api_a, api_b = make_client(), make_client()
    for _ in range(3):
        api_a.get("/api/auth/me")
    api_a.get("/api/health")
    api_b.get("/api/auth/me")
    assert api_a.app.state.requests.count() == 3
    assert api_b.app.state.requests.count() == 1
