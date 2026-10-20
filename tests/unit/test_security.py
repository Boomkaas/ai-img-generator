import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest
from freezegun import freeze_time

from api import security
from api.deps import check_origin, is_json
from api.metrics import RequestCounter
from common.config import get_settings

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def fresh_settings(monkeypatch):
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://imagegen.example,http://localhost:5173")
    get_settings.cache_clear()
    security._new_hash_slots.cache_clear()
    yield
    get_settings.cache_clear()
    security._new_hash_slots.cache_clear()


def test_password_round_trip():
    stored = security.hash_password("s3cret-pass")
    assert stored.startswith("$argon2id$")
    assert security.verify_password(stored, "s3cret-pass")
    assert not security.verify_password(stored, "other-pass")
    assert not security.needs_rehash(stored)


def test_verify_password_without_a_hash_or_with_a_broken_one_is_false():
    assert not security.verify_password(None, "anything")
    assert not security.verify_password("not-an-argon2-hash", "anything")


def test_no_more_password_hashes_run_at_once_than_configured(monkeypatch):
    monkeypatch.setenv("MAX_CONCURRENT_PASSWORD_HASHES", "2")
    get_settings.cache_clear()
    running, peak, lock = 0, 0, threading.Lock()

    class SlowHasher:
        def _work(self):
            nonlocal running, peak
            with lock:
                running += 1
                peak = max(peak, running)
            time.sleep(0.05)
            with lock:
                running -= 1

        def hash(self, password):
            self._work()
            return "hash"

        def verify(self, password_hash, password):
            self._work()
            return True

    monkeypatch.setattr(security, "_hasher", SlowHasher())
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(security.hash_password, ["pw"] * 4))
        list(pool.map(security.verify_password, ["hash"] * 4, ["pw"] * 4))
    assert peak == 2


@pytest.mark.parametrize(
    "email, ok",
    [
        ("name@example.com", True),
        ("not-an-email", False),
        ("a@b", False),
        ("two words@example.com", False),
        ("a@@example.com", False),
        ("x" * 250 + "@example.com", False),  # longer than MAX_EMAIL_LEN
    ],
)
def test_email_looks_valid(email, ok):
    assert security.email_looks_valid(email) is ok


def test_normalise_email():
    assert security.normalise_email("  Ann@Example.COM ") == "ann@example.com"


def test_access_token_carries_the_claims_and_lasts_fifteen_minutes():
    user_id = uuid4()
    with freeze_time("2026-10-21 12:00:00"):
        claims = security.decode_access_token(security.create_access_token(user_id, "admin"))
    assert claims["sub"] == str(user_id)
    assert claims["role"] == "admin"
    assert claims["exp"] - claims["iat"] == 15 * 60


def test_access_token_expires():
    with freeze_time("2026-10-21 12:00:00"):
        token = security.create_access_token(uuid4(), "user")
    with freeze_time("2026-10-21 12:15:01"), pytest.raises(jwt.ExpiredSignatureError):
        security.decode_access_token(token)


def test_access_token_with_another_key_or_algorithm_is_refused():
    claims = {"sub": "x", "role": "admin", "iat": 0, "exp": 2**40}
    with pytest.raises(jwt.InvalidSignatureError):
        security.decode_access_token(jwt.encode(claims, "x" * 32, algorithm="HS256"))
    with pytest.raises(jwt.InvalidAlgorithmError):
        security.decode_access_token(jwt.encode(claims, None, algorithm="none"))


def test_refresh_token_is_random_and_only_its_hash_is_kept():
    token, token_hash, expires_at = security.new_refresh_token()
    assert len(token) >= 43  # 32 bytes, URL-safe base64
    assert token_hash == hashlib.sha256(token.encode()).hexdigest() != token
    assert security.new_refresh_token()[0] != token
    assert abs(expires_at - (datetime.now(UTC) + timedelta(days=7))) < timedelta(seconds=5)


@pytest.mark.parametrize(
    "origin, allowed",
    [
        ("https://imagegen.example", True),
        ("http://localhost:5173", True),
        ("https://evil.example", False),
        ("https://imagegen.example.evil.example", False),
        (None, False),
        ("", False),
    ],
)
def test_check_origin(origin, allowed):
    assert check_origin(origin) is allowed


@pytest.mark.parametrize(
    "content_type, ok",
    [
        ("application/json", True),
        ("application/json; charset=utf-8", True),
        ("Application/JSON", True),
        ("text/plain", False),
        ("application/x-www-form-urlencoded", False),
        ("multipart/form-data; boundary=x", False),
        (None, False),
    ],
)
def test_is_json(content_type, ok):
    assert is_json(content_type) is ok


def test_request_counter_keeps_a_rolling_window():
    now = [1000.0]
    counter = RequestCounter(60, clock=lambda: now[0])
    for _ in range(3):
        counter.record()
    now[0] = 1030.5
    counter.record()
    assert counter.count() == 4
    now[0] = 1060.0  # the first three fell out of the window
    assert counter.count() == 1
    now[0] = 1091.0
    assert counter.count() == 0
