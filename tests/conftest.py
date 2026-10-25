import os

# Settings require a JWT secret; tests never need a real one.
os.environ.setdefault("JWT_SECRET", "test-only-jwt-secret-0123456789abcdef")

# The compose stack fixtures live here rather than in each layer's conftest, so a session that runs several
# layers starts the stack once.
from tests.stack import clean_stack, client, clients, make_client, stack  # noqa: E402, F401


def pytest_ignore_collect(collection_path, config):
    # The browser tests import Playwright, whose native parts not every system can load; a run that selects
    # other layers with -m doesn't need them.
    expression = config.getoption("markexpr")
    if collection_path.name == "e2e" and expression and "e2e" not in expression:
        return True
    return None
