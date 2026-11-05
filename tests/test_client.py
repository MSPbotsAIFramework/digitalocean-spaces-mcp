"""TC12 (AC12) and TC13 (AC13): presigned-URL signing and throttling retry."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from digitalocean_spaces_mcp.client import build_s3_client, call_with_retry, generate_presigned_url
from digitalocean_spaces_mcp.errors import SpacesError
from digitalocean_spaces_mcp.middleware import SpacesCredentials

CREDS = SpacesCredentials(access_key="AKIATEST", secret_key="secret", region="nyc3")


# ---- AC4/AC6-AC12: build_s3_client must honor the per-call region ------------


def test_build_s3_client_uses_call_level_region_not_credential_region():
    # One credential set is valid across every Spaces region; a caller
    # targeting a region other than the credential header's must be routed
    # there, not silently redirected to the header's region (PRD-21346
    # review round 1).
    client = build_s3_client(CREDS, "ams3")

    assert client.meta.region_name == "ams3"
    assert "ams3.digitaloceanspaces.com" in client.meta.endpoint_url


def test_build_s3_client_falls_back_to_credential_region_when_no_override():
    # spaces_list_buckets has no region argument of its own, so None means
    # "no override" and the credential header's region is the only one
    # available.
    client = build_s3_client(CREDS, None)

    assert client.meta.region_name == "nyc3"
    assert "nyc3.digitaloceanspaces.com" in client.meta.endpoint_url


def _throttle_error(retry_after: str | None = None, status: int = 503):
    headers = {"retry-after": retry_after} if retry_after else {}
    return ClientError(
        {
            "Error": {"Code": "SlowDown", "Message": "slow down"},
            "ResponseMetadata": {"HTTPStatusCode": status, "HTTPHeaders": headers},
        },
        "PutObject",
    )


# ---- TC12 / AC12 --------------------------------------------------------------


def test_presigned_url_generation_makes_no_network_calls():
    # botocore's connection pool is where any real HTTP call would originate;
    # patching it and asserting it is never touched proves this is pure local
    # SigV4 signing (boto3's own documented behavior for generate_presigned_url).
    with patch("botocore.httpsession.URLLib3Session.send") as send:
        url = generate_presigned_url(
            CREDS, method="get", bucket="my-bucket", key="my-key", expires_in_seconds=900
        )

    send.assert_not_called()
    assert "my-bucket" in url and "my-key" in url
    assert "digitaloceanspaces.com" in url
    assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in url
    assert "X-Amz-Expires=900" in url
    assert "X-Amz-Signature=" in url


def test_presigned_url_put_includes_content_type():
    with patch("botocore.httpsession.URLLib3Session.send") as send:
        url = generate_presigned_url(
            CREDS,
            method="put",
            bucket="my-bucket",
            key="my-key",
            expires_in_seconds=60,
            content_type="application/json",
        )

    send.assert_not_called()
    assert "X-Amz-Signature=" in url


def test_presigned_url_honors_region_override():
    # CREDS carry region "nyc3"; passing a different region must change the
    # signed endpoint (PRD-21346 review round 1).
    with patch("botocore.httpsession.URLLib3Session.send") as send:
        url = generate_presigned_url(
            CREDS, method="get", bucket="my-bucket", key="my-key", expires_in_seconds=900, region="ams3"
        )

    send.assert_not_called()
    assert "ams3.digitaloceanspaces.com" in url
    assert "nyc3.digitaloceanspaces.com" not in url


def test_presigned_url_rejects_unsupported_method():
    with pytest.raises(SpacesError) as exc_info:
        generate_presigned_url(CREDS, method="delete", bucket="b", key="k", expires_in_seconds=60)
    assert exc_info.value.code == "invalid_argument"


# ---- TC13 / AC13 --------------------------------------------------------------


async def test_throttling_retries_with_backoff_then_rate_limited(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("digitalocean_spaces_mcp.client.asyncio.sleep", fake_sleep)

    call_count = 0

    def always_throttled():
        nonlocal call_count
        call_count += 1
        raise _throttle_error(retry_after="2")

    with pytest.raises(SpacesError) as exc_info:
        await call_with_retry(always_throttled, max_retries=3)

    assert exc_info.value.code == "rate_limited"
    assert exc_info.value.retryable is True
    assert call_count == 4  # 1 initial attempt + 3 retries
    assert sleeps == [2.0, 2.0, 2.0]  # Retry-After honored on every retry


async def test_throttling_then_success_returns_result(monkeypatch):
    async def fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("digitalocean_spaces_mcp.client.asyncio.sleep", fake_sleep)

    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise _throttle_error()  # no Retry-After -> exponential backoff path
        return "ok"

    result = await call_with_retry(flaky, max_retries=5)

    assert result == "ok"
    assert attempts["n"] == 3


async def test_non_retryable_error_raises_immediately(monkeypatch):
    calls = {"n": 0}

    def not_found():
        calls["n"] += 1
        raise ClientError(
            {
                "Error": {"Code": "NoSuchKey", "Message": "nope"},
                "ResponseMetadata": {"HTTPStatusCode": 404, "HTTPHeaders": {}},
            },
            "GetObject",
        )

    with pytest.raises(SpacesError) as exc_info:
        await call_with_retry(not_found, max_retries=5)

    assert exc_info.value.code == "not_found"
    assert calls["n"] == 1  # no retry attempted for a non-retryable error
