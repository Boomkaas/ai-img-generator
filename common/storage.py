"""Image storage in MinIO through boto3 (architecture §2, §3.1).

Images are never exposed by URL: the API streams them from here with `open`, so MinIO stays private.
"""

import io
from collections.abc import Iterator
from functools import lru_cache

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from common.config import get_settings

# A HEAD response has no body, so a missing key and a missing bucket both arrive as a bare "404".
_NOT_FOUND = ("404", "NotFound")


@lru_cache
def _client():
    settings = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=settings.S3_ENDPOINT,
        aws_access_key_id=settings.S3_ACCESS_KEY,
        aws_secret_access_key=settings.S3_SECRET_KEY,
        region_name=settings.S3_REGION,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},  # path-style for MinIO
            connect_timeout=settings.S3_CONNECT_TIMEOUT_S,
            read_timeout=settings.S3_READ_TIMEOUT_S,
            # total_max_attempts counts the first try; botocore's max_attempts would count only retries.
            retries={"total_max_attempts": settings.S3_MAX_ATTEMPTS, "mode": "standard"},
        ),
    )


def reset_client() -> None:
    """Forget the client (tests, after the settings change)."""
    _client.cache_clear()


def _bucket() -> str:
    return get_settings().S3_BUCKET


def _code(exc: ClientError) -> str | None:
    return exc.response.get("Error", {}).get("Code")


def ensure_bucket() -> None:
    try:
        _client().head_bucket(Bucket=_bucket())
    except ClientError as exc:
        if _code(exc) not in _NOT_FOUND:
            raise
        try:
            _client().create_bucket(Bucket=_bucket())
        except ClientError as create_exc:  # another process created it first
            if _code(create_exc) != "BucketAlreadyOwnedByYou":
                raise


def put_png(key: str, image_or_bytes) -> None:
    """Store a PNG from a PIL image or from encoded bytes."""
    if isinstance(image_or_bytes, bytes | bytearray):
        body = bytes(image_or_bytes)
    else:
        buffer = io.BytesIO()
        image_or_bytes.save(buffer, format="PNG")
        body = buffer.getvalue()
    _client().put_object(Bucket=_bucket(), Key=key, Body=body, ContentType="image/png")


class Stream:
    """An object's bytes in chunks. Reading to the end closes it; so does `close()` or leaving a `with`,
    which gives the connection back to the pool when the bytes are never read (the client went away)."""

    def __init__(self, body, chunk_size: int):
        self._body = body
        self._chunk_size = chunk_size

    def __iter__(self) -> Iterator[bytes]:
        try:
            yield from self._body.iter_chunks(chunk_size=self._chunk_size)
        finally:
            self.close()

    def close(self) -> None:
        self._body.close()

    def __enter__(self) -> "Stream":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def open(key: str) -> Stream:
    """The object's bytes in chunks, for streaming.

    The object is fetched before this returns, so a missing key raises here, not mid-stream.
    """
    body = _client().get_object(Bucket=_bucket(), Key=key)["Body"]
    return Stream(body, get_settings().S3_STREAM_CHUNK_BYTES)


def exists(key: str) -> bool:
    """Whether the object exists; a missing bucket is a configuration error and raises."""
    try:
        _client().head_object(Bucket=_bucket(), Key=key)
    except ClientError as exc:
        if _code(exc) not in _NOT_FOUND:
            raise
        _client().head_bucket(Bucket=_bucket())  # raises if it was the bucket that is missing
        return False
    return True


def delete(key: str) -> None:
    """Delete one object; deleting a missing key is not an error."""
    _client().delete_object(Bucket=_bucket(), Key=key)
