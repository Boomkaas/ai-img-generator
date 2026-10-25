"""Many users at once against a deployed stack: does the ready time shown at submit match the real wait?

    .venv/bin/locust -f tests/load/locustfile.py --headless --host https://<domain> -u 30 -r 0.15 -t 40m

Each simulated user signs up, opens the WebSocket, submits one image of a random allowed size, follows it
until it ends, and stops. Nginx lets one address sign up ten times a minute (five more in a burst), so spawn
at most one user every six seconds (-r 0.15); a sign-up that still gets 429 waits and tries again. A 503 means
the queue is full: it counts as an expected refusal, and the user waits the Retry-After time and submits
again. When the run ends the summary says how many of the finished jobs were ready within 25% of the time
shown when they were submitted. Run it in one process (no --processes), so the summary sees every job.
"""

import json
import random
import ssl
import time
from uuid import uuid4

from locust import HttpUser, events, task
from locust.exception import StopUser
from websockets.sync.client import connect

SIZES = (256, 384, 512, 640, 768)
PROMPTS = ("a lighthouse at dusk", "a red bicycle in the rain", "a bowl of ramen", "a fox in the snow")
PASSWORD = "load-test-password"
SIGN_UP_RETRY_S = 7  # a little over Nginx's one sign-up every six seconds
TOLERANCE = 0.25
JOB_TIMEOUT_S = 3 * 3600

finished: list[tuple[int, float, float]] = []  # (size, shown ready_in_s, actual seconds)
refusals = 0
not_done: list[str] = []


def insecure_ssl() -> ssl.SSLContext:
    # Only the timing is under test; a lab deployment's certificate is often self-signed.
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def within_tolerance(shown: float, actual: float) -> bool:
    return abs(shown - actual) <= TOLERANCE * actual


class ImageUser(HttpUser):
    def on_start(self) -> None:
        self.client.verify = False
        self.email = f"load-{uuid4()}@example.com"
        while True:
            with self.client.post(
                "/api/auth/register",
                json={"email": self.email, "password": PASSWORD},
                catch_response=True,
            ) as response:
                if response.status_code == 201:
                    return
                if response.status_code == 429:
                    response.success()
                    time.sleep(SIGN_UP_RETRY_S)
                    continue
                response.failure(f"sign-up answered {response.status_code}: {response.text[:200]}")
                raise StopUser()

    @task
    def generate_one(self) -> None:
        global refusals
        size = random.choice(SIZES)
        key = str(uuid4())
        url = self.host.replace("https://", "wss://").replace("http://", "ws://") + "/ws"
        cookie = f"access_token={self.client.cookies.get('access_token')}"
        with connect(
            url,
            additional_headers={"Cookie": cookie, "Origin": self.host},
            ssl=insecure_ssl() if url.startswith("wss://") else None,
        ) as socket:
            json.loads(socket.recv(timeout=30))  # the snapshot
            while True:
                with self.client.post(
                    "/api/tasks",
                    json={"prompt": random.choice(PROMPTS), "size": size},
                    headers={"Idempotency-Key": key},
                    catch_response=True,
                ) as response:
                    if response.status_code == 401:  # a long wait outlived the access token
                        response.success()
                        self.client.post("/api/auth/refresh", json={})
                        continue
                    if response.status_code == 503:
                        response.success()
                        refusals += 1
                        time.sleep(int(response.headers.get("Retry-After", "30")))
                        continue
                    if response.status_code != 202:
                        response.failure(f"submit answered {response.status_code}: {response.text[:200]}")
                        raise StopUser()
                    submitted = time.monotonic()
                    task_id, shown = response.json()["task_id"], response.json()["ready_in_s"]
                    break
            status = self._follow(socket, task_id, submitted)

        actual = time.monotonic() - submitted
        events.request.fire(
            request_type="JOB",
            name=f"{size} px until {status}",
            response_time=actual * 1000,
            response_length=0,
            exception=None if status == "done" else RuntimeError(f"task {task_id} ended {status}"),
            context={},
        )
        if status == "done":
            finished.append((size, shown, actual))
        else:
            not_done.append(f"{task_id} {status}")
        raise StopUser()

    def _follow(self, socket, task_id: str, submitted: float) -> str:
        """Read the socket until the task ends; its final status."""
        while time.monotonic() - submitted < JOB_TIMEOUT_S:
            # The server pings every 25 s, so a minute of silence means the socket is gone.
            message = json.loads(socket.recv(timeout=60))
            if message["type"] == "task_final" and message["id"] == task_id:
                return message["status"]
        return "still running"


@events.test_stop.add_listener
def summary(environment, **_) -> None:
    close = sum(within_tolerance(shown, actual) for _, shown, actual in finished)
    share = close / len(finished) if finished else 0.0
    print(f"\nfinished jobs: {len(finished)}, refused with 503: {refusals}, not done: {len(not_done)}")
    for line in not_done:
        print(f"  not done: {line}")
    for size, shown, actual in sorted(finished):
        mark = "ok " if within_tolerance(shown, actual) else "off"
        print(f"  {mark} {size} px: shown {shown:6.0f} s, actual {actual:6.0f} s")
    print(f"shown ready time within {TOLERANCE:.0%} of the actual one: {close}/{len(finished)} = {share:.0%}")
