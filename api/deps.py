"""Request dependencies: who is calling, whether they may, and what they may send."""

from typing import Annotated

import jwt
from fastapi import Cookie, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import Row

from api import security
from common import db
from common.config import get_settings

SIGN_IN_AGAIN = "Please sign in again"


def current_user(access_token: Annotated[str | None, Cookie(alias=security.ACCESS_COOKIE)] = None) -> Row:
    """The signed-in user, from the access cookie; 401 when it is missing, invalid or expired."""
    if not access_token:
        raise HTTPException(401, SIGN_IN_AGAIN)
    try:
        claims = security.decode_access_token(access_token)
        user = db.get_user(claims["sub"])
    except (jwt.PyJWTError, ValueError):  # ValueError: a validly signed sub that isn't a UUID
        raise HTTPException(401, SIGN_IN_AGAIN) from None
    if user is None:
        raise HTTPException(401, SIGN_IN_AGAIN)
    return user


CurrentUser = Annotated[Row, Depends(current_user)]


def require_admin(user: CurrentUser) -> Row:
    """The role is read from the database, not the token, so a demoted admin loses access at once."""
    if user.role != "admin":
        raise HTTPException(403, "Admins only")
    return user


def check_origin(origin: str | None) -> bool:
    """True if a WebSocket handshake comes from one of our own pages."""
    return origin is not None and origin in get_settings().ALLOWED_ORIGINS


def is_json(content_type: str | None) -> bool:
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    return media_type == "application/json"


async def require_json(request: Request, call_next):
    """HTTP middleware: refuse a POST that isn't JSON with 415.

    A middleware rather than a route dependency, so it runs before FastAPI tries to parse the body. It also
    stops cross-site forms, which can't send JSON without a CORS preflight.
    """
    if request.method == "POST" and not is_json(request.headers.get("content-type")):
        return JSONResponse(
            {"detail": "Send the request as JSON, with the header Content-Type: application/json"},
            status_code=415,
        )
    return await call_next(request)
