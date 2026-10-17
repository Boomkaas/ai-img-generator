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

_MISSING = ("404", "NoSuchKey", "NoSuchBucket", "NotFound")


@lru_cache
def _client():
    settings = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=settings.S3_ENDPOINT,
        aws_access_key_id=settings.S3_ACCESS_KEY,
        aws_secret_access_key=settings.S3_SECRET_KEY,
        region_name=settings.S3_REGION,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),  # path-style for MinIO
    )


def reset_client() -> None:
    """Forget the client (tests, after the settings change)."""
    _client.cache_clear()


def _bucket() -> str:
    return get_settings().S3_BUCKET


def _is_missing(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") in _MISSING


def ensure_bucket() -> None:
    try:
        _client().head_bucket(Bucket=_bucket())
    except ClientError as exc:
        if not _is_missing(exc):
            raise
        try:
            _client().create_bucket(Bucket=_bucket())
        except ClientError as create_exc:  # another process created it first
            if create_exc.response.get("Error", {}).get("Code") != "BucketAlreadyOwnedByYou":
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


def open(key: str) -> Iterator[bytes]:
    """The object's bytes in chunks, for streaming.

    The object is fetched before this returns, so a missing key raises here, not mid-stream.
    """
    body = _client().get_object(Bucket=_bucket(), Key=key)["Body"]

    def chunks() -> Iterator[bytes]:
        with body:
            yield from body.iter_chunks(chunk_size=get_settings().S3_STREAM_CHUNK_BYTES)

    return chunks()


def exists(key: str) -> bool:
    try:
        _client().head_object(Bucket=_bucket(), Key=key)
    except ClientError as exc:
        if _is_missing(exc):
            return False
        raise
    return True


def delete(key: str) -> None:
    """Delete one object; deleting a missing key is not an error."""
    _client().delete_object(Bucket=_bucket(), Key=key)
