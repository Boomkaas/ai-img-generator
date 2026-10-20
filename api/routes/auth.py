"""Register, sign in, refresh, log out and the current user."""

import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Cookie, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import Row
from sqlalchemy.exc import IntegrityError

from api import security
from api.deps import SIGN_IN_AGAIN, CurrentUser
from common import db
from common.config import get_settings

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])

RefreshCookie = Annotated[str | None, Cookie(alias=security.REFRESH_COOKIE)]


class Credentials(BaseModel):
    email: str
    password: str


class PublicUser(BaseModel):
    """What a response may say about a user; password_hash is deliberately absent."""

    email: str
    role: str


def _public(user: Row) -> PublicUser:
    return PublicUser(email=user.email, role=user.role)


def _sign_in(response: Response, user: Row) -> PublicUser:
    token, token_hash, expires_at = security.new_refresh_token()
    db.insert_refresh_token(user.id, token_hash, expires_at)
    security.set_auth_cookies(response, security.create_access_token(user.id, user.role), token)
    return _public(user)


@router.post("/register", status_code=201, response_model=PublicUser)
def register(body: Credentials, response: Response) -> PublicUser:
    settings = get_settings()
    email = security.normalise_email(body.email)
    if not security.email_looks_valid(email):
        raise HTTPException(422, "Enter a valid email address, like name@example.com")
    if len(body.password) < settings.MIN_PASSWORD_LEN:
        raise HTTPException(422, f"Choose a password of at least {settings.MIN_PASSWORD_LEN} characters")
    try:
        user = db.create_user(email, security.hash_password(body.password))
    except IntegrityError:  # users_email_unique, case-insensitive; also settles two racing registrations
        raise HTTPException(409, "An account with this email already exists") from None
    return _sign_in(response, user)


@router.post("/login", response_model=PublicUser)
def login(body: Credentials, response: Response) -> PublicUser:
    user = db.get_user_by_email(security.normalise_email(body.email))
    # The same message, and the same argon2 work, for an unknown email and a wrong password.
    if not security.verify_password(user.password_hash if user else None, body.password):
        raise HTTPException(401, "Email or password is incorrect")
    if security.needs_rehash(user.password_hash):
        db.set_password_hash(user.id, security.hash_password(body.password))
    return _sign_in(response, user)


@router.post("/refresh", response_model=PublicUser)
def refresh(response: Response, refresh_token: RefreshCookie = None) -> PublicUser:
    if not refresh_token:
        raise HTTPException(401, SIGN_IN_AGAIN)
    token_hash = security.hash_refresh_token(refresh_token)
    row = db.get_refresh_token(token_hash)
    if row is None or row.expires_at <= datetime.now(UTC):
        raise HTTPException(401, SIGN_IN_AGAIN)
    # Revoking is the guard: of two requests with the same token, exactly one gets True. A token that was
    # already used or revoked means someone else holds a copy, so every session of that user ends.
    if row.revoked_at is not None or not db.revoke_refresh_token(token_hash):
        revoked = db.revoke_all_refresh_tokens(row.user_id)
        log.warning("refresh token reused for user %s; revoked %d tokens", row.user_id, revoked)
        raise HTTPException(401, SIGN_IN_AGAIN)
    user = db.get_user(row.user_id)
    if user is None:
        raise HTTPException(401, SIGN_IN_AGAIN)
    return _sign_in(response, user)


@router.post("/logout", status_code=204)
def logout(refresh_token: RefreshCookie = None) -> Response:
    if refresh_token:
        db.revoke_refresh_token(security.hash_refresh_token(refresh_token))
    response = Response(status_code=204)
    security.clear_auth_cookies(response)
    return response


@router.get("/me", response_model=PublicUser)
def me(user: CurrentUser) -> PublicUser:
    return _public(user)
