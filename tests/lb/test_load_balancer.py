"""Nginx in front of two API servers, reached on https://localhost:8443 as a browser would."""

import time
from collections import Counter
from contextlib import ExitStack
from uuid import uuid4

import httpx
import pytest
from websockets.exceptions import ConnectionClosed

from tests.stack import API_A, API_B, NGINX, NGINX_HTTP, PASSWORD, ApiClient, receive, wait_for

pytestmark = pytest.mark.lb


def through_nginx(user: ApiClient) -> ApiClient:
    """The same user, with the same cookies, talking to Nginx instead of one server."""
    proxied = ApiClient(NGINX, email=user.email)
    proxied.http.cookies = user.http.cookies
    return proxied


def test_a_cookie_from_one_server_works_on_the_other(make_client):
    user = make_client(API_A)
    other = httpx.Client(base_url=API_B, cookies=user.http.cookies)
    response = other.get("/api/auth/me")
    assert response.status_code == 200
    assert response.json()["email"] == user.email
    t = user.submit()
    assert other.get(f"/api/tasks/{t}").json()["task_id"] == t


def test_requests_are_spread_over_both_servers(stack, make_client):
    user = through_nginx(make_client(API_A))
    before = stack.api_requests()
    for _ in range(100):
        assert user.http.get("/api/auth/me").status_code == 200

    def risen():
        now = stack.api_requests()
        return {machine: now.get(machine, 0) - before.get(machine, 0) for machine in ("api-a", "api-b")}

    # Each heartbeat reports a rolling minute, so both counters must have gone up by a good share of the 100.
    wait_for(lambda: min(risen().values()) >= 30, 10, lambda: f"requests counted per server: {risen()}")


def test_when_an_api_server_dies_requests_sockets_and_retries_carry_on(stack, make_client):
    user = make_client(API_A)
    proxied = through_nginx(user)
    key = uuid4()
    first = user.submit_response(key=key)  # this one reached api_a before it died
    assert first.status_code == 202

    with ExitStack() as opened:
        # Each Nginx worker process counts connections on its own, so two sockets can land on the same server;
        # open them until both servers hold some.
        sockets = []
        while not (on := stack.api_sockets()).get("api-a") or not on.get("api-b"):
            assert len(sockets) < 10, f"Nginx sent every socket to one server: {on}"
            sockets.append(opened.enter_context(proxied.websocket()))
            receive(sockets[-1])  # the snapshot
            wait_for(lambda: sum(stack.api_sockets().values()) == len(sockets), 10, stack.api_sockets)
        stack.stop_api("api_a")

        closed = 0
        for socket in sockets:
            try:
                end = time.monotonic() + 5
                while time.monotonic() < end:
                    receive(socket, timeout=5)
            except ConnectionClosed:
                closed += 1
            except TimeoutError:
                pass
        assert closed == on["api-a"]  # exactly the sockets on api_a dropped

        with proxied.websocket() as again:  # what the page does after a drop
            assert receive(again)["type"] == "snapshot"

    for _ in range(20):
        assert proxied.http.get("/api/auth/me").status_code == 200

    retried = proxied.submit_response(key=key)
    assert retried.status_code == 202
    assert retried.json()["task_id"] == first.json()["task_id"]
    assert len(stack.db.tasks()) == 1


def test_a_queued_users_socket_outlives_the_default_proxy_timeout(stack, make_client):
    stack.scale_workers(0)
    user = through_nginx(make_client())
    t = user.submit()
    seen = Counter()
    with user.websocket() as socket:
        end = time.monotonic() + 90  # Nginx's default proxy_read_timeout is 60 s
        while time.monotonic() < end:
            message = receive(socket, timeout=30)
            seen[message["type"]] += 1
            if message["type"] == "task":
                assert message["id"] == t and message["status"] == "queued" and message["position"] == 1
        assert receive(socket, timeout=30)  # still open
    assert seen["snapshot"] == 1
    assert seen["system"] >= 10
    assert seen["ping"] >= 3


def test_http_redirects_to_https():
    response = httpx.get(f"{NGINX_HTTP}/some/page?x=1")
    assert response.status_code == 301
    assert response.headers["location"] == "https://localhost/some/page?x=1"
    secure = httpx.get(f"{NGINX}/", verify=False)
    assert secure.status_code == 200
    assert "max-age" in secure.headers["strict-transport-security"]


def test_more_than_fifteen_logins_a_minute_get_429():
    wrong = {"email": "nobody@example.com", "password": PASSWORD}
    with httpx.Client(base_url=NGINX, verify=False) as http:
        statuses = [http.post("/api/auth/login", json=wrong).status_code for _ in range(16)]
    assert statuses[:6] == [401] * 6  # one at the rate plus a burst of five
    assert statuses[-1] == 429
    assert set(statuses) == {401, 429}
