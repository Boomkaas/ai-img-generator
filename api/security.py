"""Passwords, tokens and auth cookies (architecture §4.4).

The access token is a signed JWT, so any API server can check it on its own (§3.3). The refresh token is a
random value; only its SHA-256 hash is stored, so a copy of the database can't be used to sign in.
"""

import hashlib
import secrets
import time
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Response

from common.config import get_settings

ACCESS_COOKIE = "access_token"
REFRESH_COOKIE = "refresh_token"
ACCESS_COOKIE_PATH = "/"
REFRESH_COOKIE_PATH = "/api/auth"  # both refresh and logout receive it (ADR 0003)
JWT_ALGORITHM = "HS256"

_hasher = PasswordHasher()
# Checked when the email is unknown, so a sign-in takes as long whether or not the account exists.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))

# --- Passwords ----------------------------------------------------------------------------------------------


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    """True if the password matches; with no hash, does the same work and returns False."""
    try:
        _hasher.verify(password_hash or _DUMMY_HASH, password)
    except (VerificationError, InvalidHashError):
        return False
    return password_hash is not None


def needs_rehash(password_hash: str) -> bool:
    """True when the hash was made with other argon2 parameters than the current ones."""
    return _hasher.check_needs_rehash(password_hash)


# --- Access token -------------------------------------------------------------------------------------------


def create_access_token(user_id, role: str) -> str:
    settings = get_settings()
    now = int(time.time())
    claims = {"sub": str(user_id), "role": role, "iat": now, "exp": now + settings.ACCESS_TOKEN_TTL_S}
    return jwt.encode(claims, settings.JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    """The verified claims; raises jwt.PyJWTError if the token is malformed, forged or expired."""
    return jwt.decode(
        token,
        get_settings().JWT_SECRET,
        algorithms=[JWT_ALGORITHM],
        options={"require": ["sub", "role", "iat", "exp"]},
    )


# --- Refresh token ------------------------------------------------------------------------------------------


def new_refresh_token() -> tuple[str, str, datetime]:
    """A fresh token, the hash to store, and when it expires."""
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(UTC) + timedelta(seconds=get_settings().REFRESH_TOKEN_TTL_S)
    return token, hash_refresh_token(token), expires_at


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# --- Cookies ------------------------------------------------------------------------------------------------


def _cookie_flags() -> dict:
    return {"httponly": True, "samesite": "strict", "secure": get_settings().COOKIE_SECURE}


def set_auth_cookies(response: Response, access_token: str, refresh_token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        ACCESS_COOKIE,
        access_token,
        max_age=settings.ACCESS_TOKEN_TTL_S,
        path=ACCESS_COOKIE_PATH,
        **_cookie_flags(),
    )
    response.set_cookie(
        REFRESH_COOKIE,
        refresh_token,
        max_age=settings.REFRESH_TOKEN_TTL_S,
        path=REFRESH_COOKIE_PATH,
        **_cookie_flags(),
    )


def clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(ACCESS_COOKIE, path=ACCESS_COOKIE_PATH, **_cookie_flags())
    response.delete_cookie(REFRESH_COOKIE, path=REFRESH_COOKIE_PATH, **_cookie_flags())
