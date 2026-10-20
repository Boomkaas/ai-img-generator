# 0003. Scope the refresh cookie to /api/auth

- Status: accepted
- Date: 2026-10-21

## Context

The architecture (§4.4) limited the refresh-token cookie to the path `/api/auth/refresh`. The browser
then sends it only to the refresh endpoint, and `POST /api/auth/logout` never receives it. Logout could
clear the cookies, but it couldn't revoke the token in `refresh_tokens`, so a copied token would stay
valid for the rest of its seven days. That breaks the promise in §4.4 that logout really ends a session.

## Decision

Set the refresh cookie with `Path=/api/auth`. Refresh and logout both receive it; every other endpoint,
including `/ws`, still doesn't. The other flags are unchanged: `HttpOnly`, `SameSite=Strict`, `Secure`
(when `COOKIE_SECURE`), max-age seven days. §4.4 is updated to match.

## Consequences

- Logout revokes the presented refresh token, so a later refresh with a copy fails.
- The cookie also reaches register, login and `/api/auth/me`. They ignore it. All of them are on our own
  origin over HTTPS, so this doesn't expose it any further.
