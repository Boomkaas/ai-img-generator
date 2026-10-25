"""Checks a deployed stack from outside: health, a new account, one 256 px job followed to done, its image.

    python3 scripts/smoke.py https://<domain> [--insecure] [--timeout 600]

Only the standard library, so it runs on any machine with Python 3. Prints each step and exits 1 with the
step that failed and why. It signs up a throwaway user, so it belongs on a staging or test deployment.
"""

import argparse
import http.cookiejar
import json
import ssl
import struct
import sys
import time
import urllib.error
import urllib.request
import uuid

SIZE = 256
PROMPT = "a lighthouse on a cliff at dusk"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
POLL_S = 2
REQUEST_TIMEOUT_S = 30
SIGN_UP_RETRY_S = 7  # Nginx allows one sign-up every six seconds from an address, after a burst
FINISHED = ("done", "failed", "cancelled")


class SmokeFailure(Exception):
    pass


class Client:
    def __init__(self, base_url: str, insecure: bool):
        self.base_url = base_url.rstrip("/")
        context = ssl.create_default_context()
        if insecure:  # a lab deployment's certificate is often self-signed
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=context),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
        )

    def request(self, method: str, path: str, body: dict | None = None, headers: dict | None = None):
        """(status, headers, body bytes); an HTTP error status is returned, not raised."""
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base_url + path, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        try:
            with self.opener.open(request, timeout=REQUEST_TIMEOUT_S) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()
        except (urllib.error.URLError, OSError) as exc:
            raise SmokeFailure(f"{method} {path}: {getattr(exc, 'reason', exc)}") from exc

    def json(self, method: str, path: str, expect: int, body: dict | None = None, headers=None) -> dict:
        status, _, raw = self.request(method, path, body, headers)
        if status == 401 and path != "/api/auth/refresh":  # the 15-minute access token ran out
            self.json("POST", "/api/auth/refresh", 200, {})
            status, _, raw = self.request(method, path, body, headers)
        if status != expect:
            raise SmokeFailure(f"{method} {path} answered {status}, expected {expect}: {raw[:200]!r}")
        return json.loads(raw)


def wait_for_health(client: Client, wait_s: float) -> None:
    deadline = time.monotonic() + wait_s
    while True:
        try:
            status, _, raw = client.request("GET", "/api/health")
            problem = f"answers {status}: {raw[:200]!r}"
        except SmokeFailure as exc:
            status, problem = None, f"is unreachable: {exc}"
        if status == 200:
            return
        if time.monotonic() >= deadline:
            raise SmokeFailure(f"/api/health still {problem} after {wait_s:.0f} s")
        time.sleep(POLL_S)


def sign_up(client: Client) -> str:
    email = f"smoke-{uuid.uuid4()}@example.com"
    for _ in range(5):
        status, _, raw = client.request(
            "POST", "/api/auth/register", {"email": email, "password": uuid.uuid4().hex}
        )
        if status == 201:
            return email
        if status != 429:
            raise SmokeFailure(f"sign-up answered {status}: {raw[:200]!r}")
        time.sleep(SIGN_UP_RETRY_S)
    raise SmokeFailure("sign-up kept answering 429")


def follow(client: Client, task_id: str, timeout_s: float) -> dict:
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        task = client.json("GET", f"/api/tasks/{task_id}", 200)
        if task["status"] in FINISHED:
            return task
        shown = (task["status"], task.get("progress"))
        if shown != last:
            progress = task.get("progress")
            print(f"   {task['status']}" + ("" if progress is None else f" {progress}%"))
            last = shown
        time.sleep(POLL_S)
    raise SmokeFailure(f"task {task_id} not finished after {timeout_s:.0f} s (last {last})")


def check_image(raw: bytes, content_type: str) -> None:
    if content_type != "image/png" or not raw.startswith(PNG_SIGNATURE):
        raise SmokeFailure(f"the image is not a PNG ({content_type}, {len(raw)} bytes)")
    width, height = struct.unpack(">II", raw[16:24])  # IHDR, the first chunk
    if (width, height) != (SIZE, SIZE):
        raise SmokeFailure(f"the image is {width}x{height}, expected {SIZE}x{SIZE}")


def run(base_url: str, insecure: bool, timeout_s: float, health_wait_s: float) -> None:
    client = Client(base_url, insecure)
    started = time.monotonic()

    print(f"1. health of {client.base_url}")
    wait_for_health(client, health_wait_s)
    print("2. sign up")
    print(f"   {sign_up(client)}")
    print(f"3. submit a {SIZE} px job")
    submitted = client.json(
        "POST",
        "/api/tasks",
        202,
        {"prompt": PROMPT, "size": SIZE},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    task_id = submitted["task_id"]
    print(f"   task {task_id}, position {submitted.get('position')}")
    print("4. follow it to done")
    task = follow(client, task_id, timeout_s)
    if task["status"] != "done":
        raise SmokeFailure(f"task {task_id} ended {task['status']}: {task.get('error_message')}")
    print("5. download the image")
    status, headers, raw = client.request("GET", f"/api/tasks/{task_id}/image")
    if status != 200:
        raise SmokeFailure(f"the image answered {status}: {raw[:200]!r}")
    check_image(raw, headers.get_content_type())
    print(f"smoke test passed in {time.monotonic() - started:.0f} s ({len(raw)} byte image)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("base_url", help="e.g. https://imagegen.example.com")
    parser.add_argument("--insecure", action="store_true", help="accept a self-signed certificate")
    parser.add_argument("--timeout", type=float, default=600, help="seconds the job may take (default 600)")
    parser.add_argument(
        "--health-wait", type=float, default=180, help="seconds to wait for /api/health (default 180)"
    )
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(line_buffering=True)  # the steps stay in order with the error in a CI log
    try:
        run(args.base_url, args.insecure, args.timeout, args.health_wait)
    except SmokeFailure as exc:
        print(f"smoke test FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
