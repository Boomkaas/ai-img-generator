"""The admin page, in Chromium: the machines, workers and tasks it lists, and its Cancel."""

import re

import pytest
from playwright.sync_api import expect

from common import db
from tests.e2e.helpers import assert_fits, sign_up

pytestmark = pytest.mark.e2e


def test_admin_page_lists_machines_workers_and_tasks_and_cancels(page, stack, make_client):
    db.set_role(db.get_user_by_email(sign_up(page)).id, "admin")
    stack.scale_workers(1)
    running, waiting = make_client(), make_client()
    running_id = running.submit()
    stack.wait_status(running_id, "running")
    waiting_id = waiting.submit()

    page.goto("/admin")
    expect(page.get_by_role("heading", name="Admin")).to_be_visible()

    machines = page.get_by_role("table", name="Machines")
    for name in ("api-a", "api-b", "app", "infra"):
        expect(machines.get_by_role("row").filter(has_text=re.compile(rf"^{name}"))).to_have_count(1)
    api_a = machines.get_by_role("row").filter(has_text="api-a")
    expect(api_a).to_contain_text(re.compile(r"\d+ sockets? · \d+ req/min"))

    (host,) = stack.worker_hosts()
    worker = page.get_by_role("table", name="Workers").get_by_role("row").filter(has_text=host)
    expect(worker).to_contain_text("busy")
    expect(worker).to_contain_text(running_id[:4])

    tasks = page.get_by_role("table", name="Tasks")
    expect(page.locator(".block-head", has_text="Tasks")).to_contain_text("1 running · 1 waiting")
    expect(tasks.get_by_role("row").filter(has_text=running.email)).to_contain_text(host)
    queued = tasks.get_by_role("row").filter(has_text=waiting.email)
    expect(queued).to_contain_text("#1 in queue")
    assert_fits(page)

    queued.get_by_role("button", name="Cancel").click()
    expect(queued).to_contain_text("Cancel this task?")
    queued.get_by_role("button", name="Yes").click()
    expect(queued).to_have_count(0)  # gone from the next overview
    assert stack.db.task(waiting_id).status == "cancelled"
    assert stack.db.task(running_id).status == "running"
    expect(page.locator(".block-head", has_text="Tasks")).to_contain_text("1 running · 0 waiting")
