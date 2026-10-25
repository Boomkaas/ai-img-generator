"""Chromium against the stack through Nginx, every test once at desktop width and once at 360 px.

CHROMIUM_PATH points Playwright at an installed Chromium instead of the one `playwright install` downloads.
A failed test leaves its trace in test-results/e2e; open it with `playwright show-trace <file>`.
"""

import os
import re

import pytest
from playwright.sync_api import expect, sync_playwright

from tests.stack import LOG_DIR, NGINX

VIEWPORTS = {"desktop": {"width": 1280, "height": 800}, "phone": {"width": 360, "height": 740}}
TRACE_DIR = LOG_DIR / "e2e"

expect.set_options(timeout=15_000)


@pytest.fixture(autouse=True)
def fresh_stack(clean_stack):
    return clean_stack


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH") or None)
        yield browser
        browser.close()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    if report.when == "call":
        item.call_failed = report.failed
    return report


@pytest.fixture(params=list(VIEWPORTS))
def page(request, browser, fresh_stack):
    context = browser.new_context(base_url=NGINX, viewport=VIEWPORTS[request.param], ignore_https_errors=True)
    context.tracing.start(screenshots=True, snapshots=True)
    page = context.new_page()
    yield page
    if getattr(request.node, "call_failed", True):
        TRACE_DIR.mkdir(parents=True, exist_ok=True)
        context.tracing.stop(path=TRACE_DIR / f"{re.sub(r'[^\w.-]', '_', request.node.name)}.zip")
    else:
        context.tracing.stop()
    context.close()
