"""CLAUDE.md rule: task statuses change only through the guarded functions in common/db.py (§6.1)."""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = ["api", "worker", "controller", "common", "scripts", "migrations", "tests"]
FORBIDDEN = re.compile(r"UPDATE\s+tasks\b|update\(\s*(db\.)?tasks\s*\)", re.IGNORECASE)


def test_tasks_are_updated_only_in_common_db():
    allowed = ROOT / "common" / "db.py"
    offenders = [
        f"{path.relative_to(ROOT)}:{number}"
        for package in PACKAGES
        for path in (ROOT / package).rglob("*.py")
        if path != allowed
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if FORBIDDEN.search(line)
    ]
    assert not offenders, f"tasks updated outside common/db.py: {offenders}"
