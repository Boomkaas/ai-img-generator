import pytest

from common import db
from common.config import get_settings

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def fresh_engine():
    get_settings.cache_clear()
    db.dispose_engine()
    yield
    db.dispose_engine()
    get_settings.cache_clear()


def test_connecting_to_the_database_is_bounded(monkeypatch):
    monkeypatch.setenv("DB_CONNECT_TIMEOUT_S", "3")
    captured = {}

    class FakeEngine:
        def dispose(self):
            pass

    def fake_create_engine(url, **kwargs):
        captured.update(kwargs)
        return FakeEngine()

    monkeypatch.setattr(db, "create_engine", fake_create_engine)
    db.get_engine()
    assert captured["connect_args"] == {"connect_timeout": 3}
