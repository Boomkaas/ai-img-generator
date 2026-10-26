"""The streaming wrapper and client settings, without MinIO."""

import pytest

from common import storage
from common.config import get_settings

pytestmark = pytest.mark.unit


class FakeBody:
    def __init__(self, data: bytes):
        self.data = data
        self.closed = False

    def iter_chunks(self, chunk_size):
        for start in range(0, len(self.data), chunk_size):
            yield self.data[start : start + chunk_size]

    def close(self):
        self.closed = True


def test_reading_to_the_end_closes_the_stream():
    body = FakeBody(b"x" * 10)
    assert list(storage.Stream(body, 4)) == [b"xxxx", b"xxxx", b"xx"]
    assert body.closed


def test_close_without_reading_releases_the_connection():
    body = FakeBody(b"x" * 10)
    storage.Stream(body, 4).close()
    assert body.closed


def test_leaving_a_with_block_closes_a_partly_read_stream():
    body = FakeBody(b"x" * 10)
    with storage.Stream(body, 4) as stream:
        next(iter(stream))
    assert body.closed


def test_client_has_timeouts_and_bounded_retries():
    storage.reset_client()
    try:
        config = storage._client().meta.config
        settings = get_settings()
        assert config.connect_timeout == settings.S3_CONNECT_TIMEOUT_S
        assert config.read_timeout == settings.S3_READ_TIMEOUT_S
        assert config.retries == {"total_max_attempts": settings.S3_MAX_ATTEMPTS, "mode": "standard"}
    finally:
        storage.reset_client()
