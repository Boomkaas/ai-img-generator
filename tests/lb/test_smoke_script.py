"""scripts/smoke.py, the check the staging deploy runs, against the stack behind Nginx."""

import subprocess
import sys

import pytest

from tests.stack import NGINX, ROOT

pytestmark = pytest.mark.lb


def smoke(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "smoke.py"), *args],
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_smoke_script_passes_against_a_working_stack():
    result = smoke(NGINX, "--insecure", "--timeout", "120")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "smoke test passed" in result.stdout


def test_smoke_script_fails_with_the_reason_when_a_job_does_not_finish_in_time():
    result = smoke(NGINX, "--insecure", "--timeout", "1")
    assert result.returncode == 1
    assert "not finished after 1 s" in result.stderr


def test_smoke_script_fails_when_the_site_is_unreachable():
    result = smoke("https://localhost:9", "--insecure", "--health-wait", "1")
    assert result.returncode == 1
    assert "/api/health still is unreachable" in result.stderr
