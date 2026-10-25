"""The user page, from signing up to the finished image, in Chromium."""

import re

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e.helpers import assert_fits, sign_up, submit_directly
from tests.stack import API_A, PASSWORD

pytestmark = pytest.mark.e2e

DURATION = r"(less than a minute|about \d+ min)"


def generate(page: Page, prompt: str, size: int = 256) -> None:
    page.get_by_label("Prompt").fill(prompt)
    page.get_by_label("Size").select_option(str(size))
    page.get_by_role("button", name="Generate").click()


def open_history(page: Page):
    page.get_by_role("button", name="History").click()
    return page.get_by_role("dialog", name="History")


def test_sign_up_and_sign_in(page):
    email, password = "new-user@example.com", PASSWORD
    page.goto("/")
    page.get_by_role("button", name="Create an account").click()
    page.get_by_label("Email").fill(email)
    page.get_by_label("Password").fill(password)
    page.get_by_role("button", name="Create account").click()
    expect(page.get_by_label("Prompt")).to_be_visible()
    assert_fits(page)

    page.get_by_role("button", name="Log out").click()
    page.get_by_label("Email").fill(email)
    page.get_by_label("Password").fill("not the password")
    page.get_by_role("button", name="Sign in").click()
    expect(page.get_by_role("alert")).to_have_text("Email or password is incorrect")

    page.get_by_label("Password").fill(password)
    page.get_by_role("button", name="Sign in").click()
    expect(page.get_by_label("Prompt")).to_be_visible()
    assert_fits(page)


def test_status_line_counts_generating_and_waiting(page, stack, make_client):
    sign_up(page)
    stack.scale_workers(0)
    page.goto("/")
    status = page.locator(".status")
    expect(status).to_have_text(re.compile(rf"^0 generating · 0 waiting · {DURATION} for a new image$"))

    make_client().submit()
    expect(status).to_have_text(re.compile(r"^0 generating · 1 waiting · "))
    stack.scale_workers(1)
    expect(status).to_have_text(re.compile(r"^1 generating · 0 waiting · "))
    assert_fits(page)


def test_submit_shows_the_queue_then_the_percentage_then_the_image(page, stack):
    sign_up(page)
    stack.scale_workers(0)
    page.goto("/")
    generate(page, "a lighthouse at dusk", size=256)
    expect(page.locator(".queue-line")).to_have_text(re.compile(rf"^Position 1 of 1 · ready in {DURATION}$"))
    assert_fits(page)

    stack.scale_workers(1)
    expect(page.locator(".percent")).to_have_text(re.compile(r"^\d+%$"))
    expect(page.locator(".detail")).to_have_text(re.compile(rf"^step \d+ of 25 · {DURATION} left$"))
    expect(page.get_by_role("progressbar", name="Progress")).to_be_visible()
    assert_fits(page)

    image = page.get_by_role("img", name="a lighthouse at dusk")
    expect(image).to_be_visible(timeout=60_000)
    expect(image).to_have_js_property("naturalWidth", 256)
    expect(page.locator("figcaption")).to_have_text("a lighthouse at dusk · 256 px")
    assert_fits(page)


def test_cancel_while_queued_and_while_running(page, stack):
    sign_up(page)
    stack.scale_workers(0)
    page.goto("/")
    generate(page, "first try")
    expect(page.locator(".queue-line")).to_be_visible()
    page.get_by_role("button", name="Cancel").click()
    expect(page.get_by_label("Prompt")).to_be_visible()

    stack.scale_workers(1)
    generate(page, "second try")
    expect(page.locator(".percent")).to_be_visible()
    page.get_by_role("button", name="Cancel").click()
    expect(page.get_by_label("Prompt")).to_be_visible()

    history = open_history(page)
    for prompt in ("first try", "second try"):
        entry = history.locator("li", has_text=prompt)
        expect(entry.locator(".entry-meta")).to_have_text(re.compile(r"^Cancelled · 256 px · "))
    assert_fits(page)
    assert {t.status for t in stack.db.tasks()} == {"cancelled"}


def test_empty_prompt_shows_an_error_and_sends_nothing(page):
    sign_up(page)
    page.goto("/")
    submits = []
    page.on("request", lambda r: submits.append(r) if r.method == "POST" and "/api/tasks" in r.url else None)
    for prompt in ("", "   "):
        generate(page, prompt)
        expect(page.get_by_role("alert")).to_have_text("Write what you want to see first.")
    assert_fits(page)
    assert submits == []


def test_full_queue_says_when_to_try_again(page, stack, make_client):
    stack.restart_apis({"MAX_QUEUED_TOTAL": "2"})
    stack.scale_workers(0)
    for _ in range(2):
        make_client().submit()
    sign_up(page)
    page.goto("/")
    generate(page, "one too many")
    expect(page.get_by_role("alert")).to_have_text(
        re.compile(rf"^The queue is full right now\. Try again in {DURATION}\.$")
    )
    expect(page.get_by_label("Prompt")).to_have_value("one too many")
    assert_fits(page)


def test_reload_during_a_job_comes_back_to_it(page):
    sign_up(page)
    page.goto("/")
    generate(page, "a slow sunrise")
    percent = page.locator(".percent")
    expect(percent).to_have_text(re.compile(r"^([1-9]\d*)%$"))
    before = int(percent.inner_text().rstrip("%"))

    page.reload()
    expect(percent).to_be_visible()
    expect(page.get_by_label("Prompt")).to_have_count(0)
    expect(percent).not_to_have_text(f"{before}%")
    assert int(percent.inner_text().rstrip("%")) > before
    assert_fits(page)


def test_a_history_entry_opens_its_image(page, stack):
    sign_up(page)
    t = submit_directly(page, "a red bicycle")
    stack.wait_status(t, "done")
    page.goto("/")
    history = open_history(page)
    entry = history.get_by_role("button", name=re.compile("a red bicycle"))
    expect(entry).to_contain_text("Done · 256 px")
    entry.click()
    image = history.get_by_role("img", name="a red bicycle")
    expect(image).to_be_visible()
    expect(image).to_have_js_property("naturalWidth", 256)
    assert_fits(page)


def test_log_out_returns_to_sign_in_and_ends_the_session(page):
    sign_up(page)
    page.goto("/")
    expect(page.get_by_label("Prompt")).to_be_visible()
    refresh = next(c for c in page.context.cookies() if c["name"] == "refresh_token")

    page.get_by_role("button", name="Log out").click()
    expect(page.get_by_role("button", name="Sign in")).to_be_visible()
    page.reload()
    expect(page.get_by_role("button", name="Sign in")).to_be_visible()
    assert_fits(page)

    stale = httpx.post(f"{API_A}/api/auth/refresh", json={}, cookies={"refresh_token": refresh["value"]})
    assert stale.status_code == 401
