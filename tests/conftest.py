import os

os.environ.setdefault("JWT_SECRET", "test-only-jwt-secret-0123456789abcdef")

from tests.stack import clean_stack, client, clients, make_client, stack  # noqa: E402, F401


def pytest_ignore_collect(collection_path, config):
    expression = config.getoption("markexpr")
    if collection_path.name == "e2e" and expression and "e2e" not in expression:
        return True
    return None
