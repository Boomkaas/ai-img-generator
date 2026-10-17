import io
import os
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError
from PIL import Image

from common import storage
from common.config import get_settings

pytestmark = pytest.mark.api


def png_bytes(size=64) -> bytes:
    """Random pixels, so the PNG doesn't compress to almost nothing."""
    buffer = io.BytesIO()
    Image.frombytes("RGB", (size, size), os.urandom(size * size * 3)).save(buffer, format="PNG")
    return buffer.getvalue()


def test_round_trip_put_exists_stream_delete(bucket):
    key, data = f"results/{uuid4()}.png", png_bytes()
    assert not storage.exists(key)

    storage.put_png(key, data)
    assert storage.exists(key)
    assert b"".join(storage.open(key)) == data

    storage.delete(key)
    assert not storage.exists(key)
    storage.delete(key)  # deleting again is not an error


def test_put_accepts_a_pil_image(bucket):
    key = f"results/{uuid4()}.png"
    storage.put_png(key, Image.new("RGB", (32, 32), "gray"))
    image = Image.open(io.BytesIO(b"".join(storage.open(key))))
    assert (image.format, image.size) == ("PNG", (32, 32))


def test_open_streams_in_chunks(bucket, monkeypatch):
    monkeypatch.setenv("S3_STREAM_CHUNK_BYTES", "1024")
    get_settings.cache_clear()
    key, data = f"results/{uuid4()}.png", png_bytes(256)
    storage.put_png(key, data)
    chunks = list(storage.open(key))
    assert len(chunks) > 1 and all(len(chunk) <= 1024 for chunk in chunks)
    assert b"".join(chunks) == data


def test_open_of_a_missing_key_raises_before_streaming(bucket):
    with pytest.raises(ClientError):
        storage.open(f"results/{uuid4()}.png")


def test_ensure_bucket_is_idempotent(bucket):
    storage.ensure_bucket()
    storage.ensure_bucket()
