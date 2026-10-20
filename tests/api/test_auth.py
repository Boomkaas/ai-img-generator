import logging
import time
from datetime import timedelta
from http.cookies import SimpleCookie
from uuid import uuid4

import jwt
import pytest
from argon2 import PasswordHasher
from fastapi import Depends
from freezegun import freeze_time
from sqlalchemy import func, select, update

from api import security
from api.deps import require_admin
from api.main import create_app
from common import db
from common.config import get_settings

pytestmark = pytest.mark.api

PASSWORD = "correct horse battery"


def register(client, email=None, password=PASSWORD):
    email = email or f"{uuid4()}@example.com"
    return client.post("/api/auth/register", json={"email": email, "password": password})


def login(client, email, password=PASSWORD):
    return client.post("/api/auth/login", json={"email": email, "password": password})


def post(client, path, **kwargs):
    return client.post(path, json={}, **kwargs)  # an empty JSON body: POSTs must be JSON


def set_cookies(response) -> dict:
    """The Set-Cookie headers of one response, parsed: name -> Morsel."""
    cookies = {}
    for header in response.headers.get_list("set-cookie"):
        jar = SimpleCookie()
        jar.load(header)
        cookies.update(jar)
    return cookies


def cookie_header(**cookies) -> dict:
    return {"Cookie": "; ".join(f"{name}={value}" for name, value in cookies.items())}


def tokens_of(email):
    user = db.get_user_by_email(email)
    stmt = select(db.refresh_tokens).where(db.refresh_tokens.c.user_id == user.id)
    with db.transaction() as tx:
        return tx.execute(stmt).all()


# --- Register -----------------------------------------------------------------------------------------------


def test_register_sets_both_cookies_with_the_right_flags(client):
    response = register(client, "new@example.com")
    assert response.status_code == 201
    assert response.json() == {"email": "new@example.com", "role": "user"}

    cookies = set_cookies(response)
    settings = get_settings()
    access, refresh = cookies["access_token"], cookies["refresh_token"]
    for morsel in (access, refresh):
        assert morsel["httponly"] is True
        assert morsel["samesite"].lower() == "strict"
        assert not morsel["secure"]  # COOKIE_SECURE=false in tests
    assert access["path"] == "/"
    assert int(access["max-age"]) == settings.ACCESS_TOKEN_TTL_S == 15 * 60
    assert refresh["path"] == "/api/auth"
    assert int(refresh["max-age"]) == settings.REFRESH_TOKEN_TTL_S == 7 * 24 * 3600

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json() == {"email": "new@example.com", "role": "user"}


def test_cookies_are_secure_when_cookie_secure_is_on(client, monkeypatch):
    monkeypatch.setenv("COOKIE_SECURE", "true")
    get_settings.cache_clear()
    cookies = set_cookies(register(client))
    assert cookies["access_token"]["secure"] is True
    assert cookies["refresh_token"]["secure"] is True


def test_register_twice_with_the_same_email_in_another_case_conflicts(client):
    assert register(client, "Ann@Example.com").status_code == 201
    response = register(client, "ann@example.COM")
    assert response.status_code == 409
    assert response.json() == {"detail": "An account with this email already exists"}


def test_register_stores_only_an_argon2_hash(client):
    register(client, "hash@example.com")
    stored = db.get_user_by_email("hash@example.com").password_hash
    assert stored.startswith("$argon2id$")
    assert PASSWORD not in stored


@pytest.mark.parametrize(
    "email, password, word",
    [
        ("not-an-email", PASSWORD, "email"),
        ("a@b", PASSWORD, "email"),
        ("two words@example.com", PASSWORD, "email"),
        ("ok@example.com", "short", "8 characters"),
    ],
)
def test_register_refuses_a_bad_email_or_short_password(client, email, password, word):
    response = register(client, email, password)
    assert response.status_code == 422
    assert word in response.json()["detail"]


def test_a_missing_field_gives_one_sentence(client):
    response = client.post("/api/auth/register", json={"email": "a@example.com"})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, str)
    assert "password" in detail


def test_malformed_json_gives_one_sentence(client):
    response = client.post(
        "/api/auth/login", content='{"email": ', headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422
    assert response.json()["detail"].startswith("Check the request body:")


def test_a_post_that_is_not_json_is_refused(client):
    response = client.post(
        "/api/auth/login", content="email=a&password=b", headers={"Content-Type": "text/plain"}
    )
    assert response.status_code == 415
    assert "application/json" in response.json()["detail"]
    assert client.post("/api/auth/logout").status_code == 415  # no Content-Type at all


# --- Login --------------------------------------------------------------------------------------------------


def test_login_sets_both_cookies(make_client):
    register(make_client(), "log@example.com")
    client = make_client()
    response = login(client, "LOG@example.com")
    assert response.status_code == 200
    assert {"access_token", "refresh_token"} <= set(set_cookies(response))
    assert client.get("/api/auth/me").json()["email"] == "log@example.com"


def test_wrong_password_and_unknown_email_look_the_same(client):
    register(client, "known@example.com")
    wrong_password = login(client, "known@example.com", "not the password")
    unknown_email = login(client, "nobody@example.com")
    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json() == {"detail": "Email or password is incorrect"}


def test_login_rehashes_a_password_with_old_parameters(client):
    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(PASSWORD)
    user = db.create_user("old@example.com", weak)
    assert login(client, "old@example.com").status_code == 200
    stored = db.get_user(user.id).password_hash
    assert stored != weak
    assert not security.needs_rehash(stored)
    assert login(client, "old@example.com").status_code == 200


def test_passwords_and_hashes_never_reach_the_log(client, caplog):
    caplog.set_level(logging.DEBUG)
    register(client, "quiet@example.com")
    login(client, "quiet@example.com")
    login(client, "quiet@example.com", "wrong password here")
    assert PASSWORD not in caplog.text
    assert "$argon2" not in caplog.text
    assert "password_hash" not in caplog.text


# --- Current user -------------------------------------------------------------------------------------------


def test_me_without_a_cookie_is_401(client):
    response = client.get("/api/auth/me")
    assert response.status_code == 401
    assert response.json() == {"detail": "Please sign in again"}


def test_me_with_an_expired_access_token_is_401(client):
    register(client, "late@example.com")
    user = db.get_user_by_email("late@example.com")
    with freeze_time(timedelta(seconds=-(get_settings().ACCESS_TOKEN_TTL_S + 60))):
        token = security.create_access_token(user.id, user.role)
    client.cookies.clear()
    response = client.get("/api/auth/me", headers=cookie_header(access_token=token))
    assert response.status_code == 401
    assert response.json() == {"detail": "Please sign in again"}


@pytest.mark.parametrize("token", ["garbage", "a.b.c"])
def test_me_with_a_malformed_token_is_401(client, token):
    response = client.get("/api/auth/me", headers=cookie_header(access_token=token))
    assert response.status_code == 401


def test_a_token_signed_with_another_key_is_401(client):
    register(client, "forged@example.com")
    user = db.get_user_by_email("forged@example.com")
    now = int(time.time())
    claims = {"sub": str(user.id), "role": "admin", "iat": now, "exp": now + 60}
    forged = jwt.encode(claims, "another-secret-that-is-long-enough-0123", algorithm="HS256")
    client.cookies.clear()
    assert client.get("/api/auth/me", headers=cookie_header(access_token=forged)).status_code == 401


# --- Refresh ------------------------------------------------------------------------------------------------


def test_refresh_replaces_the_token_and_a_reused_one_ends_every_session(client):
    register(client, "rot@example.com")
    first = client.cookies["refresh_token"]

    response = post(client, "/api/auth/refresh")
    assert response.status_code == 200
    cookies = set_cookies(response)
    second = cookies["refresh_token"].value
    assert second != first
    assert "access_token" in cookies
    assert client.get("/api/auth/me").status_code == 200

    client.cookies.clear()
    stolen = post(client, "/api/auth/refresh", headers=cookie_header(refresh_token=first))
    assert stolen.status_code == 401
    # The first token was used before, so someone copied it: every token of the user is now revoked.
    assert all(row.revoked_at is not None for row in tokens_of("rot@example.com"))
    assert post(client, "/api/auth/refresh", headers=cookie_header(refresh_token=second)).status_code == 401


def test_refresh_without_a_cookie_or_with_an_unknown_one_is_401(client):
    assert post(client, "/api/auth/refresh").status_code == 401
    unknown = post(client, "/api/auth/refresh", headers=cookie_header(refresh_token="made-up"))
    assert unknown.status_code == 401


def test_refresh_with_an_expired_token_is_401(client):
    register(client, "old-refresh@example.com")
    with db.transaction() as tx:
        tx.execute(update(db.refresh_tokens).values(expires_at=func.now() - timedelta(seconds=1)))
    assert post(client, "/api/auth/refresh").status_code == 401


# --- Logout -------------------------------------------------------------------------------------------------


def test_logout_revokes_the_token_and_clears_the_cookies(client):
    register(client, "bye@example.com")
    token = client.cookies["refresh_token"]

    response = post(client, "/api/auth/logout")
    assert response.status_code == 204
    cleared = set_cookies(response)
    assert cleared["access_token"]["path"] == "/"
    assert cleared["refresh_token"]["path"] == "/api/auth"
    assert int(cleared["access_token"]["max-age"]) == int(cleared["refresh_token"]["max-age"]) == 0
    assert [row.revoked_at is not None for row in tokens_of("bye@example.com")] == [True]

    client.cookies.clear()
    assert post(client, "/api/auth/refresh", headers=cookie_header(refresh_token=token)).status_code == 401


def test_logout_without_a_cookie_is_still_204(client):
    assert post(client, "/api/auth/logout").status_code == 204


# --- Statelessness and roles --------------------------------------------------------------------------------


def test_a_cookie_from_one_api_instance_works_on_another(make_client):
    api_a, api_b = make_client(), make_client()
    register(api_a, "both@example.com")
    access = api_a.cookies["access_token"]

    response = api_b.get("/api/auth/me", headers=cookie_header(access_token=access))
    assert response.status_code == 200
    assert response.json()["email"] == "both@example.com"


def test_require_admin_refuses_users_and_admits_admins(make_client):
    app = create_app()
    app.add_api_route("/api/test/admin-only", lambda: {"ok": True}, dependencies=[Depends(require_admin)])
    user_client, admin_client = make_client(app), make_client(app)

    register(user_client)
    forbidden = user_client.get("/api/test/admin-only")
    assert forbidden.status_code == 403
    assert forbidden.json() == {"detail": "Admins only"}

    register(admin_client, "boss@example.com")
    db.set_role(db.get_user_by_email("boss@example.com").id, "admin")
    # The role comes from the database, so the cookie issued before the promotion already works.
    assert admin_client.get("/api/test/admin-only").status_code == 200
