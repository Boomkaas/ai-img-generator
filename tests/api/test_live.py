import http.server
import json
import threading
import time
from uuid import uuid4

import pytest

from common import live
from common.config import get_settings
from common.messages import TaskFinal, TaskUpdate, parse_message

pytestmark = pytest.mark.api


def wait_until(condition, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.1)
    return False


@pytest.fixture
def subscribe(redis_live):
    """Subscribe to a channel; returns a function that reads the next message on it."""
    pubsubs = []

    def open_channel(channel):
        pubsub = redis_live.pubsub()
        pubsub.subscribe(channel)
        pubsub.get_message(timeout=1)  # the subscribe confirmation
        pubsubs.append(pubsub)

        def next_message():
            message = pubsub.get_message(ignore_subscribe_messages=True, timeout=2)
            assert message is not None, f"nothing published on {channel}"
            return parse_message(message["data"])

        return next_message

    yield open_channel
    for pubsub in pubsubs:
        pubsub.close()


# --- Progress and pub/sub -----------------------------------------------------------------------------------


def test_progress_is_stored_with_expiry_and_published(redis_live, subscribe):
    user_id, task_id = uuid4(), uuid4()
    next_message = subscribe(live.user_channel(user_id))

    live.publish_progress(user_id, task_id, progress=36, stage="step 10 of 25")

    ttl = redis_live.ttl(live.progress_key(task_id))
    assert get_settings().TASK_TIME_LIMIT - 2 <= ttl <= get_settings().TASK_TIME_LIMIT
    assert live.progress(task_id) == 36
    assert live.progress_detail(task_id) == (36, "step 10 of 25")
    message = next_message()
    assert isinstance(message, TaskUpdate)
    assert (message.id, message.status, message.progress, message.stage) == (
        task_id,
        "running",
        36,
        "step 10 of 25",
    )


def test_task_without_progress_reads_as_zero(redis_live):
    assert live.progress(uuid4()) == 0
    assert live.progress_detail(uuid4()) is None


def test_task_final_is_published_on_the_owners_channel(redis_live, subscribe):
    user_id, task_id = uuid4(), uuid4()
    next_message = subscribe(live.user_channel(user_id))
    live.publish_task_final(user_id, task_id, "cancelled")
    assert next_message() == TaskFinal(id=task_id, status="cancelled")


# --- Cancellation -------------------------------------------------------------------------------------------


def test_cancel_flag_is_set_and_expires(redis_live):
    task_id = uuid4()
    assert not live.cancel_requested(task_id)
    live.request_cancel(task_id, ttl=1)
    assert live.cancel_requested(task_id)
    assert wait_until(lambda: not live.cancel_requested(task_id), timeout=3)


def test_cancel_flag_defaults_to_the_configured_ttl(redis_live):
    task_id = uuid4()
    live.request_cancel(task_id)
    assert redis_live.ttl(live.cancel_key(task_id)) > get_settings().CANCEL_FLAG_TTL_S - 2


def test_clear_task_keys_removes_progress_and_flag(redis_live):
    user_id, task_id, other = uuid4(), uuid4(), uuid4()
    live.publish_progress(user_id, task_id, 50, "step 13 of 25")
    live.request_cancel(task_id)
    live.request_cancel(other)
    live.clear_task_keys(task_id)
    assert live.progress_detail(task_id) is None
    assert not live.cancel_requested(task_id)
    assert live.cancel_requested(other)


# --- Worker heartbeats --------------------------------------------------------------------------------------


def test_worker_heartbeat_disappears_after_its_expiry(redis_live):
    host = f"nodeB-worker-{uuid4().hex[:6]}"
    beat = live.start_heartbeat(host, "loading", interval=60, ttl=1)
    try:
        (hb,) = live.worker_heartbeats()
        assert (hb.host, hb.state, hb.task_id, hb.machine) == (host, "loading", None, "nodeB")
        assert hb.rss_mb > 0
        assert wait_until(lambda: not live.worker_heartbeats(), timeout=3)
    finally:
        beat.stop()


def test_heartbeat_thread_keeps_rewriting(redis_live):
    host = f"nodeB-worker-{uuid4().hex[:6]}"
    beat = live.start_heartbeat(host, "idle", interval=0.2, ttl=1)
    try:
        time.sleep(1.5)  # past the expiry, so only the thread can have kept it alive
        assert [hb.host for hb in live.worker_heartbeats()] == [host]
    finally:
        beat.stop()


def test_count_workers_ignores_loading_ones(redis_live):
    tag = uuid4().hex[:6]
    live.set_worker_state(f"a-worker-{tag}", "loading")
    live.set_worker_state(f"b-worker-{tag}", "idle")
    live.set_worker_state(f"c-worker-{tag}", "busy", task_id=uuid4())
    assert live.count_workers(("idle", "busy")) == 2
    assert live.count_workers(("loading",)) == 1


def test_set_worker_state_records_the_task(redis_live):
    host, task_id = f"nodeB-worker-{uuid4().hex[:6]}", uuid4()
    live.set_worker_state(host, "busy", task_id=task_id)
    (hb,) = live.worker_heartbeats()
    assert (hb.state, hb.task_id) == ("busy", str(task_id))
    live.set_worker_state(host, "idle")
    (hb,) = live.worker_heartbeats()
    assert (hb.state, hb.task_id) == ("idle", None)


def test_any_worker_idle_for_uses_since(redis_live):
    idle, busy = f"a-worker-{uuid4().hex[:6]}", f"b-worker-{uuid4().hex[:6]}"
    live.set_worker_state(idle, "idle")
    live.set_worker_state(busy, "busy", task_id=uuid4())
    assert not live.any_worker_idle_for(60)

    long_ago = time.time() - 120
    redis_live.hset(live.worker_key(busy), "since", long_ago)  # busy for long doesn't count
    assert not live.any_worker_idle_for(60)
    redis_live.hset(live.worker_key(idle), "since", long_ago)
    assert live.any_worker_idle_for(60)
    assert not live.any_worker_idle_for(300)


def test_since_only_moves_when_the_state_changes(redis_live):
    host = f"nodeB-worker-{uuid4().hex[:6]}"
    live.set_worker_state(host, "idle")
    first = live.worker_heartbeats()[0].since
    time.sleep(0.05)
    live.set_worker_state(host, "idle")
    assert live.worker_heartbeats()[0].since == first
    live.set_worker_state(host, "busy", task_id=uuid4())
    assert live.worker_heartbeats()[0].since > first


def test_host_without_worker_suffix_runs_on_machine_name(redis_live, monkeypatch):
    monkeypatch.setenv("MACHINE_NAME", "laptop")
    get_settings.cache_clear()
    live.set_worker_state(f"plain-host-{uuid4().hex[:6]}", "idle")
    assert live.worker_heartbeats()[0].machine == "laptop"


# --- API and machine heartbeats -----------------------------------------------------------------------------


def test_api_and_machine_heartbeats_round_trip(redis_live):
    live.write_api_heartbeat("nodeA", 4242, sockets=3, requests_per_min=17)
    live.write_machine_heartbeat("nodeA", "manager")
    (api,) = live.api_heartbeats()
    assert (api.machine, api.pid, api.sockets, api.requests_per_min) == ("nodeA", 4242, 3, 17)
    assert 0 <= api.machine_ram <= 100
    (machine,) = live.machine_heartbeats()
    assert (machine.machine, machine.role) == ("nodeA", "manager")
    assert redis_live.ttl(live.api_key("nodeA", 4242)) > 0
    assert redis_live.ttl(live.machine_key("nodeA")) > 0


# --- Controller state ---------------------------------------------------------------------------------------


def test_autoscaler_state_round_trips(redis_live):
    assert live.autoscaler_state() == {}
    live.save_autoscaler_state(desired=2, ready=1, starting=1, maximum=4)
    live.save_autoscaler_state(last_action="scaled up to 2")
    state = live.autoscaler_state()
    assert {k: state[k] for k in ("desired", "ready", "starting", "maximum", "last_action")} == {
        "desired": 2,
        "ready": 1,
        "starting": 1,
        "maximum": 4,
        "last_action": "scaled up to 2",
    }
    assert state["updated_at"] == pytest.approx(time.time(), abs=5)


def test_alert_keeps_only_the_newest_twenty(redis_live):
    for i in range(25):
        live.alert(f"alert {i}")
    alerts = live.recent_alerts()
    assert len(alerts) == get_settings().MAX_ALERTS == 20
    assert alerts[0].message == "alert 24"
    assert alerts[-1].message == "alert 5"


@pytest.fixture
def webhook(redis_live, monkeypatch):
    """A local HTTP server as the alert webhook; yields the list of bodies it received."""
    received = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("ALERT_WEBHOOK_URL", f"http://127.0.0.1:{server.server_port}/hook")
    get_settings.cache_clear()
    yield received
    server.shutdown()
    get_settings.cache_clear()


def test_alert_is_posted_to_the_webhook(webhook):
    live.alert("queue full")
    assert wait_until(lambda: webhook == [{"text": "queue full"}])
    assert live.recent_alerts()[0].message == "queue full"


def test_the_same_alert_reaches_the_webhook_once_in_thirty_minutes(webhook, redis_live):
    live.alert("queue full")
    live.alert("queue full")
    live.alert("worker crash-loop")
    assert wait_until(lambda: len(webhook) == 2)
    time.sleep(0.5)
    assert sorted(body["text"] for body in webhook) == ["queue full", "worker crash-loop"]
    assert [a.message for a in live.recent_alerts()] == ["worker crash-loop", "queue full", "queue full"]
    ttl = redis_live.ttl(live.alert_sent_key("queue full"))
    assert get_settings().ALERT_REPEAT_AFTER_S - 5 < ttl <= get_settings().ALERT_REPEAT_AFTER_S == 30 * 60


def test_an_unreachable_webhook_neither_blocks_nor_raises(redis_live, monkeypatch, caplog):
    # A blackholed address: connecting would hang until the timeout.
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://10.255.255.1:9/hook")
    monkeypatch.setenv("ALERT_WEBHOOK_TIMEOUT_S", "0.5")
    get_settings.cache_clear()
    started = time.monotonic()
    live.alert("queue full")
    assert time.monotonic() - started < 0.2
    assert live.recent_alerts()[0].message == "queue full"
    assert wait_until(lambda: "could not send the alert" in caplog.text)


def test_no_webhook_sends_nothing(redis_live):
    live.alert("queue full")
    assert redis_live.exists(live.alert_sent_key("queue full")) == 0


def test_redis_run_id_is_stable_while_redis_runs(redis_live):
    run_id = live.redis_run_id()
    assert run_id
    assert live.redis_run_id() == run_id


async def test_async_client_talks_to_the_same_redis(redis_live):
    redis_live.set("probe", "1")
    client = live.async_client()
    try:
        assert await client.get("probe") == "1"
    finally:
        await client.aclose()
