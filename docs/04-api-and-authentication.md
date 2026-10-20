# Step 04 of 14 — API foundation and authentication

You are building **imagegen** (see `CLAUDE.md` and `docs/architecture.md`). This is step 04 of fourteen. Do only this step.

## Goal

The FastAPI application skeleton and complete authentication: register, sign in, refresh, log out, the current user, and the admin role, using cookies so the same login works on every API server and on the WebSocket.

## Before you start

- Read `docs/architecture.md` §3.1 (endpoints), §3.3 (statelessness), §4.4 (authorisation and HTTPS), §8 (security notes).
- Check step 03 is merged into `develop` and all tests pass. If not, stop and tell me.
- Branch: `feature/api-auth` from `develop`.

## Build

### 1. Application skeleton — `api/main.py`

- `create_app()` factory and a module-level `app = create_app()`.
- A lifespan that builds the DB engine and Redis client and closes them on shutdown.
- `GET /api/health` → `200 {"db": "ok", "redis": "ok"}`, or `503` naming what is down.
- A middleware that counts requests in a rolling one-minute window (step 07 reports it in the API heartbeat).
- Error responses keep FastAPI's `{"detail": "..."}` shape. Every message is a sentence a user can act on.
- `make run-api` runs `uvicorn api.main:app --reload --port 8000`.

### 2. Security helpers — `api/security.py`

- Passwords: argon2 via `argon2-cffi` `PasswordHasher`, with `check_needs_rehash` on sign-in.
- Access token: JWT (HS256, `JWT_SECRET`), claims `sub` (user id), `role`, `iat`, `exp` = 15 minutes.
- Refresh token: 32 random bytes (`secrets.token_urlsafe`), valid 7 days. Only its SHA-256 hash is stored (`refresh_tokens`).
- Cookies:
  - `access_token`: `HttpOnly`, `SameSite=Strict`, `Secure` when `COOKIE_SECURE`, `Path=/`, max-age 15 minutes.
  - `refresh_token`: same flags, `Path=/api/auth` (so both refresh and logout receive it), max-age 7 days.

### 3. Dependencies — `api/deps.py`

- `current_user`: reads the `access_token` cookie, verifies it, loads the user; `401 "Please sign in again"` when missing, invalid or expired.
- `require_admin`: `403 "Admins only"` unless `role == 'admin'`.
- `check_origin(origin: str | None) -> bool` against `ALLOWED_ORIGINS` (used for the WebSocket in step 07).
- Reject `POST` requests whose `Content-Type` isn't JSON with `415`.

### 4. Endpoints — `api/routes/auth.py`

| Endpoint | Behaviour |
|---|---|
| `POST /api/auth/register` `{email, password}` | Email must look valid, password at least 8 characters. Creates the user and signs in (sets both cookies). `409 "An account with this email already exists"`. |
| `POST /api/auth/login` `{email, password}` | Sets both cookies. Wrong email or password → `401 "Email or password is incorrect"` (same message for both, so emails can't be probed). |
| `POST /api/auth/refresh` | Replaces the refresh token with a new one and issues a new access cookie. If a token that was already used or revoked is presented, revoke **all** of that user's refresh tokens (someone copied it) and return `401`. |
| `POST /api/auth/logout` | Revokes the presented refresh token and clears both cookies. Always `204`, even without a cookie. |
| `GET /api/auth/me` | `{email, role}` for the current user. |

Return users without `password_hash` ever appearing in a response or a log line.

### 5. Admin creation — `scripts/create_admin.py`

`python -m scripts.create_admin admin@example.com` asks for a password (getpass), creates the user with role `admin`, or promotes an existing user.

### Not built here

The login rate limit belongs in Nginx (§4.4, step 12). Don't add an in-app limiter: kept in each API server's memory, it would be multiplied by the number of servers.

## Tests (`api`, with Postgres and Redis from testcontainers)

Tests run with `COOKIE_SECURE=false`, because the test client talks plain HTTP and would drop `Secure` cookies; one test checks that the flag is set when `COOKIE_SECURE=true`.

- Register sets both cookies with the right flags, paths and max-ages; `/api/auth/me` then works.
- Register twice with the same email in different case → `409`.
- Wrong password and unknown email give the same `401` body.
- `/api/auth/me` without a cookie → `401`; with an expired access token (use `freezegun`) → `401`.
- Refresh returns a new refresh cookie; the old one is then rejected; presenting the old one again revokes every token of that user.
- Logout revokes the token; a later refresh fails.
- **Cross-instance:** create two app instances with `create_app()` sharing the same database and secret; sign in through one and call `/api/auth/me` through the other with the same cookie → `200` (§9.5).
- An endpoint behind `require_admin` gives `403` to a normal user and `200` to an admin.
- No response body in the test suite contains `password_hash`.

## Out of scope

Task endpoints, WebSocket, frontend.

## Done when

- `make lint`, `make test-unit` and `make test-api` pass.
- `make run-api` starts; `curl -i localhost:8000/api/health` returns `200` with dev services up.
- `scripts/create_admin.py` creates an admin in the dev database.

## Finish

Merge `feature/api-auth` into `develop`. Reply with what you built and how you verified it.
