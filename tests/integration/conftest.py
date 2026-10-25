import pytest


@pytest.fixture(autouse=True)
def fresh_stack(clean_stack):
    """Every test starts with two idle workers, nothing queued and empty tables."""
    return clean_stack
