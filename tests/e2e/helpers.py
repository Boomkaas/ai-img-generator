"""What the browser tests do over and over."""

from uuid import uuid4

from playwright.sync_api import Page

from tests.stack import API_A, PASSWORD


def sign_up(page: Page) -> str:
    """A new account, signed in in this browser; created on one API server directly, which keeps the sign-ups
    clear of Nginx's login rate limit. The cookies are for localhost, so the page on 8443 sends them too."""
    email = f"{uuid4()}@example.com"
    response = page.request.post(f"{API_A}/api/auth/register", data={"email": email, "password": PASSWORD})
    assert response.status == 201, response.text()
    return email


def submit_directly(page: Page, prompt: str, size: int = 256) -> str:
    response = page.request.post(
        f"{API_A}/api/tasks",
        data={"prompt": prompt, "size": size},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert response.status == 202, response.text()
    return response.json()["task_id"]


def assert_fits(page: Page) -> None:
    """The page never scrolls sideways; wide tables scroll inside their own box."""
    overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
    assert overflow <= 0, f"the page is {overflow} px wider than the window"
