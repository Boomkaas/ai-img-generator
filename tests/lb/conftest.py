import pytest


@pytest.fixture(autouse=True)
def fresh_stack(clean_stack):
    """Every test starts with both API servers behind Nginx, two idle workers and empty tables."""
    return clean_stack
