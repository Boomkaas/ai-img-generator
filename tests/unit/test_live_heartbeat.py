"""Heartbeat thread behaviour and client settings, with Redis writes faked."""

import threading
import time
from uuid import uuid4

import pytest

from common import live
from common.config import get_settings

pytestmark = pytest.mark.unit

FIGURES = {"machine_cpu": 1.0, "machine_ram": 2.0}


def wait_until(condition, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def host():
    name = f"nodeB-worker-{uuid4().hex[:6]}"
    yield name
    with live._workers_lock:
        live._workers.pop(name, None)


def test_heartbeat_thread_survives_an_unexpected_error(monkeypatch, host):
    calls, written = [], []

    def figures_failing_twice():
        calls.append(1)
        if len(calls) <= 2:
            raise RuntimeError("psutil broke")
        return FIGURES

    monkeypatch.setattr(live, "_machine_figures", figures_failing_twice)
    monkeypatch.setattr(live, "_write_hash", lambda key, mapping, ttl: written.append(mapping["state"]))
    beat = live.start_heartbeat(host, "idle", interval=0.02)  # the first write fails, on the caller's thread
    try:
        assert wait_until(lambda: written)
        assert beat.is_alive()
    finally:
        beat.stop()
    assert not beat.is_alive()


def test_a_slow_beat_never_lands_after_a_newer_state(monkeypatch, host):
    written, slow_write_started = [], threading.Event()

    def write_hash(key, mapping, ttl):
        if mapping["state"] == "busy":  # the beat that read the old state is slow to reach Redis
            slow_write_started.set()
            time.sleep(0.3)
        written.append(mapping["state"])

    monkeypatch.setattr(live, "_machine_figures", lambda: FIGURES)
    monkeypatch.setattr(live, "_write_hash", write_hash)
    with live._workers_lock:
        live._workers[host] = {"state": "busy", "task_id": "t", "since": 0.0, "ttl": 15}

    beat = threading.Thread(target=live._write_worker_heartbeat, args=(host,))
    beat.start()
    assert slow_write_started.wait(1)
    live.set_worker_state(host, "idle")
    beat.join()

    assert written == ["busy", "idle"]


def test_sync_client_bounds_every_call():
    live.reset_client()
    try:
        kwargs = live.client().connection_pool.connection_kwargs
        assert kwargs["socket_connect_timeout"] == get_settings().REDIS_CONNECT_TIMEOUT_S
        assert kwargs["socket_timeout"] == get_settings().REDIS_SOCKET_TIMEOUT_S
    finally:
        live.reset_client()


async def test_async_client_bounds_only_connecting():
    client = live.async_client()
    try:
        kwargs = client.connection_pool.connection_kwargs
        assert kwargs["socket_connect_timeout"] == get_settings().REDIS_CONNECT_TIMEOUT_S
        assert kwargs.get("socket_timeout") is None  # a subscription may idle for minutes
    finally:
        await client.aclose()
