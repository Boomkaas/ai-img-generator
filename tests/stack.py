"""The compose test stack (infra/compose.test.yml) and clients for it, shared by the integration, lb and e2e
tests.

The stack starts once per session. Before every test `Stack.reset` puts it back the way it started: both API
servers up with their usual settings, two idle workers, nothing queued, empty tables.
"""

import json
import os
import shutil
import ssl
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import yaml
from sqlalchemy import text
from websockets.sync.client import connect

from common import db, live, storage
from common.config import get_settings

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "infra" / "compose.test.yml"
PROJECT = "imagegen-test"
LOG_DIR = ROOT / "test-results"

API_A = "http://localhost:8001"
API_B = "http://localhost:8002"
NGINX = "https://localhost:8443"
NGINX_HTTP = "http://localhost:8080"
ORIGIN = NGINX  # the stack's ALLOWED_ORIGINS
WORKERS = 2
APIS = ("api_a", "api_b")
PASSWORD = "correct horse battery"
STOP_GRACE_S = 120  # the worker's stop_grace_period

# How the test process reaches the stack's Postgres, Redis and MinIO, on the ports compose publishes.
DIRECT_ENV = {
    "DATABASE_URL": "postgresql+psycopg://imagegen:imagegen@localhost:5442/imagegen",
    "REDIS_URL": "redis://localhost:6389/0",
    "BROKER_URL": "redis://localhost:6389/0",
    "S3_ENDPOINT": "http://localhost:9010",
    "S3_ACCESS_KEY": "imagegen",
    "S3_SECRET_KEY": "imagegen-test-secret",
    "S3_BUCKET": "imagegen",
}


def wait_for(condition, timeout: float, describe, interval: float = 0.1):
    """Poll until `condition()` is truthy and return it; on timeout fail with `describe()`."""
    end = time.monotonic() + timeout
    while True:
        result = condition()
        if result:
            return result
        if time.monotonic() > end:
            raise AssertionError(f"timed out after {timeout} s: {describe()}")
        time.sleep(interval)


def insecure_ssl() -> ssl.SSLContext:
    """For the stack's self-signed certificate."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


class Database:
    def task(self, task_id):
        return db.get_task(task_id)

    def tasks(self):
        with db.transaction() as tx:
            return tx.execute(db.tasks.select().order_by(db.tasks.c.created_at)).all()

    def truncate(self) -> None:
        with db.transaction() as tx:
            tx.execute(text("TRUNCATE users, refresh_tokens, tasks CASCADE"))


class Stack:
    def __init__(self):
        self.db = Database()
        self.storage = storage
        self.workers = WORKERS
        self._env: dict[str, dict[str, str]] = {}  # per service, settings changed for the current test
        self._stopped_apis: set[str] = set()
        self._background: list[subprocess.Popen] = []
        self._override = Path(tempfile.mkdtemp(prefix="imagegen-test-")) / "override.yml"

    @property
    def redis(self):
        return live.client()

    # --- docker compose -------------------------------------------------------------------------------------

    def compose(self, *args: str, timeout: float = 300) -> str:
        files = ["-f", str(COMPOSE_FILE)]
        if self._env:
            overrides = {"services": {name: {"environment": env} for name, env in self._env.items()}}
            self._override.write_text(yaml.safe_dump(overrides))
            files += ["-f", str(self._override)]
        result = subprocess.run(
            ["docker", "compose", "-p", PROJECT, *files, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode:
            raise RuntimeError(f"docker compose {' '.join(args)} failed:\n{result.stdout}{result.stderr}")
        return result.stdout

    def up(self) -> None:
        self.compose("up", "-d", "--wait")
        wait_for(self._nginx_answers, 60, lambda: "Nginx did not answer on 8443")
        self._wait_workers_ready()

    def down(self, keep_logs: bool) -> None:
        for process in self._background:
            process.wait()
        if keep_logs:
            LOG_DIR.mkdir(exist_ok=True)
            (LOG_DIR / "compose.log").write_text(self.compose("logs", "--no-color", "--timestamps"))
        if not os.environ.get("STACK_KEEP"):
            self.compose("down", "--volumes", "--remove-orphans")
        shutil.rmtree(self._override.parent, ignore_errors=True)

    def logs(self, service: str, tail: int = 60) -> str:
        return self.compose("logs", "--no-color", "--tail", str(tail), service)

    def _containers(self, service: str) -> list[tuple[str, str]]:
        """The running containers of `service`, as (id, host name)."""
        ids = subprocess.run(
            ["docker", "ps", "-q",
             "--filter", f"label=com.docker.compose.project={PROJECT}",
             "--filter", f"label=com.docker.compose.service={service}"],
            capture_output=True, text=True, check=True,
        ).stdout.split()  # fmt: skip
        if not ids:
            return []
        out = subprocess.run(
            ["docker", "inspect", "--format", "{{.Id}} {{.Config.Hostname}}", *ids],
            capture_output=True, text=True, check=True,
        ).stdout  # fmt: skip
        return [tuple(line.split()) for line in out.splitlines()]

    def worker_hosts(self) -> list[str]:
        return [host for _, host in self._containers("worker")]

    def _worker_container(self, host: str) -> str:
        for container, name in self._containers("worker"):
            if name == host:
                return container
        raise AssertionError(f"no running worker container has the host name {host!r}")

    # --- Workers --------------------------------------------------------------------------------------------

    def scale_workers(self, n: int) -> None:
        """Run exactly `n` workers and wait until each has loaded and reports idle."""
        self.compose("up", "-d", "--no-deps", "--scale", f"worker={n}", "worker")
        self.workers = n
        self._wait_workers_ready()

    def restart_workers(self, env: dict[str, str]) -> None:
        """Recreate the workers with `env` added to their settings; `reset` restores the usual ones."""
        self._env["worker"] = env
        self.compose(
            "up", "-d", "--no-deps", "--force-recreate", "--scale", f"worker={self.workers}", "worker"
        )
        self._wait_workers_ready()

    def kill_worker_running(self, task_id) -> None:
        """SIGKILL the worker that runs the task: no finally block, no last heartbeat."""
        container = self._worker_container(self.db.task(task_id).worker_host)
        subprocess.run(["docker", "kill", "--signal", "KILL", container], capture_output=True, check=True)

    def stop_worker_running(self, task_id) -> None:
        """SIGTERM the worker that runs the task, as a scale-down does; it exits once its job is done."""
        container = self._worker_container(self.db.task(task_id).worker_host)
        self._background.append(
            subprocess.Popen(
                ["docker", "stop", "--time", str(STOP_GRACE_S), container], stdout=subprocess.DEVNULL
            )
        )

    def _worker_states(self) -> dict[str, tuple[str, str | None] | None]:
        beats = {hb.host: (hb.state, hb.task_id) for hb in live.worker_heartbeats()}
        return {host: beats.get(host) for host in self.worker_hosts()}

    def _heartbeats_idle(self) -> bool:
        states = self._worker_states()
        return len(states) == self.workers and all(s == ("idle", None) for s in states.values())

    def _wait_workers_ready(self, timeout: float = 60) -> None:
        wait_for(self._heartbeats_idle, timeout, lambda: f"workers not idle: {self._worker_states()}\n"
                 f"{self.logs('worker')}")  # fmt: skip

    def _broker_empty(self) -> bool:
        return not self.redis.exists(live.GENERATION_QUEUE, *live.BROKER_UNACKED_KEYS)

    def wait_worker_idle(self, timeout: float = 60) -> None:
        """Until every worker is idle and no message is waiting or being worked on, twice in a row."""

        def settled() -> bool:
            if not (self._heartbeats_idle() and self._broker_empty()):
                return False
            time.sleep(0.5)
            return self._heartbeats_idle() and self._broker_empty()

        wait_for(settled, timeout, lambda: f"workers not idle: {self._worker_states()}, "
                 f"queue length {self.redis.llen(live.GENERATION_QUEUE)}\n{self.logs('worker')}")  # fmt: skip

    # --- Waiting for tasks ----------------------------------------------------------------------------------

    def describe(self, task_id) -> str:
        row = self.db.task(task_id)
        return f"task row: {dict(row._mapping) if row else None}\nworker logs:\n{self.logs('worker')}"

    def wait_status(self, task_id, status: str, timeout: float = 60) -> None:
        wait_for(
            lambda: (row := self.db.task(task_id)) is not None and row.status == status,
            timeout,
            lambda: f"task {task_id} never became {status}\n{self.describe(task_id)}",
        )

    def wait_progress(self, task_id, at_least: int, timeout: float = 60) -> int:
        def reached():
            value = live.progress(task_id)
            if value >= at_least:
                return value
            row = self.db.task(task_id)
            if row is not None and row.status in db.FINISHED_STATUSES:
                raise AssertionError(f"task {task_id} ended before {at_least}%\n{self.describe(task_id)}")
            return None

        return wait_for(reached, timeout, lambda: f"task {task_id} never reached {at_least}%\n"
                        f"{self.describe(task_id)}", interval=0.05)  # fmt: skip

    def wait_all(self, task_ids, status: str, timeout: float = 120) -> None:
        def pending():
            return [t for t in task_ids if (row := self.db.task(t)) is None or row.status != status]

        wait_for(lambda: not pending(), timeout, lambda: f"not {status}:\n"
                 + "\n".join(self.describe(t) for t in pending()[:1])
                 + f"\nall: {[(str(r.id), r.status, r.worker_host) for r in self.db.tasks()]}")  # fmt: skip

    def wait_worker_log(self, needle: str, timeout: float = 60) -> None:
        wait_for(lambda: needle in self.logs("worker", tail=500), timeout,
                 lambda: f"no worker logged {needle!r}\n{self.logs('worker')}", interval=0.5)  # fmt: skip

    # --- Redis, API servers and Nginx -----------------------------------------------------------------------

    def restart_redis(self, keep_data: bool) -> None:
        if keep_data:
            self.compose("restart", "redis")
        else:
            self.compose(
                "up", "-d", "--no-deps", "--force-recreate", "--renew-anon-volumes", "--wait", "redis"
            )
        live.reset_client()

    def restart_apis(self, env: dict[str, str]) -> None:
        """Recreate both API servers with `env` added to their settings; `reset` restores the usual ones."""
        for name in APIS:
            self._env[name] = env
        self.compose("up", "-d", "--no-deps", "--force-recreate", "--wait", *APIS)
        self.reload_nginx()

    def stop_api(self, name: str) -> None:
        self._stopped_apis.add(name)
        self.compose("stop", name)

    def reload_nginx(self) -> None:
        """Make Nginx look up the API servers' addresses again: a recreated container gets a new one."""
        self.compose("exec", "-T", "nginx", "nginx", "-s", "reload")
        time.sleep(1)  # the old worker processes finish their requests first
        wait_for(self._nginx_answers, 30, lambda: "Nginx did not answer after the reload")

    def _nginx_answers(self) -> bool:
        try:
            return httpx.get(f"{NGINX}/api/health", verify=False, timeout=5).status_code == 200
        except httpx.HTTPError:
            return False

    def api_requests(self) -> dict[str, int]:
        """Requests in the last minute per API machine, from their heartbeats."""
        counts: dict[str, int] = {}
        for hb in live.api_heartbeats():
            counts[hb.machine] = counts.get(hb.machine, 0) + hb.requests_per_min
        return counts

    def api_sockets(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for hb in live.api_heartbeats():
            counts[hb.machine] = counts.get(hb.machine, 0) + hb.sockets
        return counts

    # --- Between tests --------------------------------------------------------------------------------------

    def reset(self) -> None:
        for process in self._background:
            process.wait()
        self._background.clear()

        changed = set(self._env) | self._stopped_apis
        self._env.clear()
        self._stopped_apis.clear()
        if changed & set(APIS):
            self.compose("up", "-d", "--no-deps", "--wait", *APIS)
            self.reload_nginx()
        self.compose("up", "-d", "--no-deps", "--scale", f"worker={WORKERS}", "worker")
        self.workers = WORKERS

        # Stop whatever the last test left running, so the workers are free within a second.
        for task in db.active_tasks():
            db.cancel_task(task.id, None)
            live.request_cancel(task.id)
        self._wait_workers_ready()
        self.redis.delete(live.GENERATION_QUEUE, *live.BROKER_UNACKED_KEYS)
        for pattern in (live.progress_key("*"), live.cancel_key("*")):
            for key in self.redis.scan_iter(match=pattern):
                self.redis.delete(key)
        self.db.truncate()


class ApiClient:
    """One signed-up user talking to one API server (or to Nginx)."""

    def __init__(self, base_url: str, email: str | None = None):
        self.email = email or f"{uuid4()}@example.com"
        self.http = httpx.Client(base_url=base_url, verify=False, timeout=15)

    def register(self) -> "ApiClient":
        response = self.http.post("/api/auth/register", json={"email": self.email, "password": PASSWORD})
        assert response.status_code == 201, response.text
        return self

    def submit_response(self, prompt: str = "a cat", size: int = 256, key=None) -> httpx.Response:
        return self.http.post(
            "/api/tasks",
            json={"prompt": prompt, "size": size},
            headers={"Idempotency-Key": str(key or uuid4())},
        )

    def submit(self, prompt: str = "a cat", size: int = 256, key=None) -> str:
        response = self.submit_response(prompt, size, key)
        assert response.status_code == 202, response.text
        return response.json()["task_id"]

    def task(self, task_id) -> dict:
        response = self.http.get(f"/api/tasks/{task_id}")
        assert response.status_code == 200, response.text
        return response.json()

    def cancel(self, task_id) -> dict:
        response = self.http.delete(f"/api/tasks/{task_id}")
        assert response.status_code == 200, response.text
        return response.json()

    def image(self, task_id) -> httpx.Response:
        return self.http.get(f"/api/tasks/{task_id}/image")

    def cookie_header(self) -> str:
        return "; ".join(f"{c.name}={c.value}" for c in self.http.cookies.jar if c.path == "/")

    @contextmanager
    def websocket(self, base_url: str = NGINX):
        url = base_url.replace("https://", "wss://").replace("http://", "ws://") + "/ws"
        with connect(
            url,
            additional_headers={"Cookie": self.cookie_header(), "Origin": ORIGIN},
            ssl=insecure_ssl() if url.startswith("wss://") else None,
            open_timeout=10,
        ) as socket:
            yield socket

    def close(self) -> None:
        self.http.close()


def receive(socket, timeout: float = 10) -> dict:
    return json.loads(socket.recv(timeout=timeout))


# --- Fixtures, registered once in tests/conftest.py so one session starts one stack ------------------------


@pytest.fixture(scope="session")
def stack(request):
    patch = pytest.MonkeyPatch()
    for name, value in DIRECT_ENV.items():
        patch.setenv(name, value)
    get_settings.cache_clear()
    db.dispose_engine()
    live.reset_client()
    storage.reset_client()
    running = Stack()
    failed_before = request.session.testsfailed
    started = False
    try:
        running.up()
        started = True
        yield running
    finally:
        # A stack that never came up has the most telling logs of all.
        running.down(keep_logs=not started or request.session.testsfailed > failed_before)
        db.dispose_engine()
        live.reset_client()
        storage.reset_client()
        patch.undo()
        get_settings.cache_clear()


@pytest.fixture
def clean_stack(stack) -> Stack:
    stack.reset()
    return stack


@pytest.fixture
def make_client(clean_stack):
    """Builds signed-up users, each with its own HTTP client, alternating between the two API servers."""
    made: list[ApiClient] = []

    def make(base_url: str | None = None) -> ApiClient:
        client = ApiClient(base_url or (API_A, API_B)[len(made) % 2]).register()
        made.append(client)
        return client

    yield make
    for client in made:
        client.close()


@pytest.fixture
def client(make_client) -> ApiClient:
    return make_client()


@pytest.fixture
def clients(make_client) -> list[ApiClient]:
    return [make_client() for _ in range(6)]
