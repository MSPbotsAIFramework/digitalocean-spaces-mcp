"""boto3-backed Spaces client.

A fresh boto3 S3 client is built per call from the request's own credentials
(never cached, never shared across requests — AC14). Every network call goes
through `call_with_retry`, which runs the sync boto3 call in a thread, retries
throttling/5xx with backoff honoring `Retry-After` when present, and maps
`botocore.exceptions.ClientError` onto the SOP's structured error vocabulary.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any, Callable, TypeVar

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, EndpointConnectionError, ReadTimeoutError

from .errors import SpacesError
from .middleware import SpacesCredentials
from .settings import settings

T = TypeVar("T")

_THROTTLE_CODES = {"SlowDown", "TooManyRequests", "RequestLimitExceeded", "Throttling"}
_NOT_FOUND_CODES = {"NoSuchBucket", "NoSuchKey"}
_UNAUTHORIZED_CODES = {"AccessDenied", "SignatureDoesNotMatch", "InvalidAccessKeyId"}
_INVALID_ARGUMENT_CODES = {"BucketAlreadyExists", "BucketAlreadyOwnedByYou", "InvalidBucketName"}


def endpoint_for(region: str) -> str:
    return f"https://{region}.digitaloceanspaces.com"


def build_s3_client(creds: SpacesCredentials, region: str | None = None):
    """A fresh S3 client for this one call.

    A DigitalOcean Spaces access/secret key pair is valid across every Spaces
    region for the account (it is not region-bound), so most tools accept a
    per-call `region` argument and that argument — not the credential
    header's region — decides which regional endpoint this call hits
    (PRD-21346 review round 1). `region` is only `None` for tools that have
    no region argument of their own (`spaces_list_buckets`), in which case
    the credential header's region is the only one available.

    botocore's own retry machinery is disabled (max_attempts=1): retry/backoff
    is owned by call_with_retry below, which is the only place that can see
    and honor a Retry-After header.
    """
    effective_region = region if region is not None else creds.region
    config = Config(
        connect_timeout=settings.connect_timeout_seconds,
        read_timeout=settings.read_timeout_seconds,
        retries={"max_attempts": 1, "mode": "standard"},
        s3={"addressing_style": "virtual"},
        # DigitalOcean Spaces requires SigV4; without a pinned signature
        # version, botocore can fall back to SigV2 query signing for a
        # custom endpoint_url, which Spaces does not accept.
        signature_version="s3v4",
    )
    return boto3.client(
        "s3",
        endpoint_url=endpoint_for(effective_region),
        region_name=effective_region,
        aws_access_key_id=creds.access_key,
        aws_secret_access_key=creds.secret_key,
        config=config,
    )


def map_client_error(exc: ClientError, *, bucket_name: str | None = None) -> SpacesError:
    """Translate a botocore ClientError into the SOP §4 error envelope."""
    error = exc.response.get("Error", {})
    code = error.get("Code", "")
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    message = error.get("Message") or str(exc)

    if code == "BucketNotEmpty":
        name = bucket_name or error.get("BucketName", "the bucket")
        return SpacesError(
            "invalid_argument",
            f"Bucket '{name}' is not empty; delete its objects first "
            "(spaces_list_objects + spaces_delete_objects), then retry spaces_delete_bucket.",
        )
    if code in _INVALID_ARGUMENT_CODES:
        return SpacesError("invalid_argument", message)
    if code in _NOT_FOUND_CODES or status == 404:
        return SpacesError("not_found", message)
    if code in _UNAUTHORIZED_CODES or status in (401, 403):
        return SpacesError("unauthorized", message)
    if code in _THROTTLE_CODES or status == 429:
        return SpacesError("rate_limited", message)
    return SpacesError("upstream_error", message)


def _is_retryable(exc: ClientError) -> bool:
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    code = exc.response.get("Error", {}).get("Code", "")
    return code in _THROTTLE_CODES or status == 429 or (status is not None and status >= 500)


def _retry_after_seconds(exc: ClientError) -> float | None:
    headers = exc.response.get("ResponseMetadata", {}).get("HTTPHeaders", {})
    value = headers.get("retry-after")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def call_with_retry(
    func: Callable[..., T],
    *args: Any,
    bucket_name: str | None = None,
    max_retries: int | None = None,
    **kwargs: Any,
) -> T:
    """Run a sync boto3 call in a thread; retry throttling/5xx with backoff.

    Non-retryable errors (NoSuchBucket, AccessDenied, ...) are mapped and
    raised immediately. Raises SpacesError once the retry budget is spent.
    """
    budget = settings.max_retries if max_retries is None else max_retries
    attempt = 0
    while True:
        try:
            return await asyncio.to_thread(func, *args, **kwargs)
        except ClientError as exc:
            if not _is_retryable(exc):
                raise map_client_error(exc, bucket_name=bucket_name) from exc
            attempt += 1
            if attempt > budget:
                raise map_client_error(exc, bucket_name=bucket_name) from exc
            delay = _retry_after_seconds(exc)
            if delay is None:
                delay = min(2**attempt, 20) + random.uniform(0, 0.5)
            await asyncio.sleep(delay)
        except (EndpointConnectionError, ReadTimeoutError) as exc:
            attempt += 1
            if attempt > budget:
                raise SpacesError("upstream_error", str(exc)) from exc
            await asyncio.sleep(min(2**attempt, 20) + random.uniform(0, 0.5))


def generate_presigned_url(
    creds: SpacesCredentials,
    *,
    method: str,
    bucket: str,
    key: str,
    expires_in_seconds: int,
    content_type: str | None = None,
    region: str | None = None,
) -> str:
    """Pure local SigV4 signing — makes zero network calls (AC12)."""
    client = build_s3_client(creds, region)
    if method == "get":
        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=expires_in_seconds,
        )
    if method == "put":
        params: dict[str, str] = {"Bucket": bucket, "Key": key}
        if content_type:
            params["ContentType"] = content_type
        return client.generate_presigned_url(
            "put_object", Params=params, ExpiresIn=expires_in_seconds
        )
    raise SpacesError("invalid_argument", f"Unsupported method '{method}'; expected 'get' or 'put'.")


__all__ = [
    "build_s3_client",
    "call_with_retry",
    "generate_presigned_url",
    "map_client_error",
    "endpoint_for",
]
