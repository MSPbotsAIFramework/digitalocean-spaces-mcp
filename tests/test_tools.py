"""TC1, TC3..TC11: tools/list snapshot and one happy-path + error-mapping
test per tool, against a mocked boto3 S3 client (per the design's test
strategy — no real DigitalOcean account needed for this suite)."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

import digitalocean_spaces_mcp.tools as tools_mod
from digitalocean_spaces_mcp.settings import settings
from tests.conftest import call_tool

EXPECTED_TOOL_NAMES = {
    "spaces_list_buckets",
    "spaces_create_bucket",
    "spaces_delete_bucket",
    "spaces_list_objects",
    "spaces_get_object",
    "spaces_put_object",
    "spaces_delete_objects",
    "spaces_copy_object",
    "spaces_get_object_metadata",
    "spaces_create_presigned_url",
}


def _client_error(code: str, message: str = "boom", status: int = 400, **extra_error):
    return ClientError(
        {
            "Error": {"Code": code, "Message": message, **extra_error},
            "ResponseMetadata": {"HTTPStatusCode": status, "HTTPHeaders": {}},
        },
        "SomeOperation",
    )


@pytest.fixture
def fake_client(monkeypatch):
    client = MagicMock()
    regions_used: list[str | None] = []

    def _build_s3_client(creds, region=None):
        regions_used.append(region)
        return client

    monkeypatch.setattr(tools_mod, "build_s3_client", _build_s3_client)
    client.regions_used = regions_used
    return client


# ---- TC1 / AC1 --------------------------------------------------------------


async def test_tools_list_has_all_spaces_tools_with_valid_descriptions(mcp):
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert names == EXPECTED_TOOL_NAMES
    for t in tools:
        description = t.description or ""
        first_line = description.split("\n", 1)[0]
        assert len(first_line) <= 100, f"{t.name} first line too long: {len(first_line)}"
        assert len(description) <= 500, f"{t.name} description too long: {len(description)}"


# ---- TC3 / AC3 ---------------------------------------------------------------


async def test_list_buckets_returns_all_buckets(mcp, creds, fake_client):
    fake_client.list_buckets.return_value = {
        "Buckets": [
            {"Name": "alpha", "CreationDate": datetime(2026, 1, 1, tzinfo=timezone.utc)},
            {"Name": "beta", "CreationDate": datetime(2026, 1, 2, tzinfo=timezone.utc), "Region": "ams3"},
            {"Name": "gamma", "CreationDate": datetime(2026, 1, 3, tzinfo=timezone.utc)},
        ]
    }

    result = await call_tool(mcp, "spaces_list_buckets", creds)

    assert [b["name"] for b in result] == ["alpha", "beta", "gamma"]
    # No per-bucket Region in the mocked payload -> falls back to the request's region.
    assert result[0]["region"] == "nyc3"
    assert result[1]["region"] == "ams3"
    # spaces_list_buckets has no region argument of its own -> no override to pass.
    assert fake_client.regions_used == [None]


# ---- TC4 / AC4 ---------------------------------------------------------------


async def test_create_bucket_happy_path(mcp, creds, fake_client):
    # creds carry region "nyc3"; the call asks for a *different* region ("ams3")
    # to prove the call-level region wins, not the credential header's region
    # (PRD-21346 review round 1 — a single credential set is valid across
    # every Spaces region, so callers must be able to target any of them).
    fake_client.create_bucket.return_value = {}

    result = await call_tool(mcp, "spaces_create_bucket", creds, name="my-bucket", region="ams3")

    assert result == {"name": "my-bucket", "region": "ams3"}
    fake_client.create_bucket.assert_called_once()
    assert fake_client.create_bucket.call_args.kwargs["ACL"] == "private"
    assert fake_client.regions_used == ["ams3"]


async def test_create_bucket_duplicate_name_returns_structured_error(mcp, creds, fake_client):
    fake_client.create_bucket.side_effect = _client_error("BucketAlreadyExists", status=409)

    result = await call_tool(mcp, "spaces_create_bucket", creds, name="taken", region="nyc3")

    assert result["error"]["code"] == "invalid_argument"
    assert result["error"]["retryable"] is False


# ---- TC5 / AC5 ---------------------------------------------------------------


async def test_delete_bucket_happy_path(mcp, creds, fake_client):
    fake_client.delete_bucket.return_value = {}

    # region differs from creds.region ("nyc3") to prove the call-level region wins.
    result = await call_tool(mcp, "spaces_delete_bucket", creds, name="empty-bucket", region="ams3")

    assert result == {"deleted": True}
    assert fake_client.regions_used == ["ams3"]


async def test_delete_bucket_not_empty_returns_structured_error(mcp, creds, fake_client):
    fake_client.delete_bucket.side_effect = _client_error("BucketNotEmpty", status=409)

    result = await call_tool(mcp, "spaces_delete_bucket", creds, name="full-bucket", region="nyc3")

    assert result["error"]["code"] == "invalid_argument"
    assert "full-bucket" in result["error"]["message"]
    assert result["error"]["retryable"] is False


# ---- TC6 / AC6 ---------------------------------------------------------------


async def test_list_objects_pagination_no_duplicate_keys(mcp, creds, fake_client):
    fake_client.list_objects_v2.side_effect = [
        {
            "Contents": [
                {"Key": "a.txt", "Size": 1, "LastModified": datetime(2026, 1, 1, tzinfo=timezone.utc), "ETag": '"e1"'},
                {"Key": "b.txt", "Size": 2, "LastModified": datetime(2026, 1, 1, tzinfo=timezone.utc), "ETag": '"e2"'},
            ],
            "IsTruncated": True,
            "NextContinuationToken": "page-2",
        },
        {
            "Contents": [
                {"Key": "c.txt", "Size": 3, "LastModified": datetime(2026, 1, 1, tzinfo=timezone.utc), "ETag": '"e3"'},
            ],
            "IsTruncated": False,
        },
    ]

    page1 = await call_tool(mcp, "spaces_list_objects", creds, bucket="b", region="nyc3", limit=2)
    page2 = await call_tool(
        mcp,
        "spaces_list_objects",
        creds,
        bucket="b",
        region="nyc3",
        limit=2,
        continuation_token=page1["continuation_token"],
    )

    keys_page1 = {o["key"] for o in page1["objects"]}
    keys_page2 = {o["key"] for o in page2["objects"]}
    assert keys_page1 == {"a.txt", "b.txt"}
    assert keys_page2 == {"c.txt"}
    assert not (keys_page1 & keys_page2)
    assert page1["has_more"] is True
    assert page2["has_more"] is False
    assert "continuation_token" not in page2


async def test_list_objects_prefix_filters_keys(mcp, creds, fake_client):
    fake_client.list_objects_v2.return_value = {
        "Contents": [
            {"Key": "logs/a.txt", "Size": 1, "LastModified": datetime(2026, 1, 1, tzinfo=timezone.utc), "ETag": '"e1"'},
        ],
        "IsTruncated": False,
    }

    # region differs from creds.region ("nyc3") to prove the call-level region wins.
    result = await call_tool(mcp, "spaces_list_objects", creds, bucket="b", region="ams3", prefix="logs/")

    assert fake_client.list_objects_v2.call_args.kwargs["Prefix"] == "logs/"
    assert [o["key"] for o in result["objects"]] == ["logs/a.txt"]
    assert fake_client.regions_used == ["ams3"]


# ---- TC7 / AC7 ---------------------------------------------------------------


async def test_get_object_returns_inline_content_under_cap(mcp, creds, fake_client):
    fake_client.head_object.return_value = {"ContentLength": 13, "ContentType": "text/plain"}
    body = MagicMock()
    body.read.return_value = b"hello, world!"
    fake_client.get_object.return_value = {"Body": body}

    # region differs from creds.region ("nyc3") to prove the call-level region wins.
    result = await call_tool(mcp, "spaces_get_object", creds, bucket="b", region="ams3", key="k.txt")

    assert result == {"content": "hello, world!", "content_type": "text/plain", "size": 13, "truncated": False}
    fake_client.get_object.assert_called_once()
    assert fake_client.regions_used == ["ams3"]


async def test_get_object_over_cap_returns_metadata_only(mcp, creds, fake_client):
    fake_client.head_object.return_value = {
        "ContentLength": settings.get_inline_max_bytes + 1,
        "ContentType": "text/plain",
    }

    result = await call_tool(mcp, "spaces_get_object", creds, bucket="b", region="nyc3", key="big.txt")

    assert result["content"] is None
    assert result["size"] == settings.get_inline_max_bytes + 1
    assert "spaces_create_presigned_url" in result["note"]
    fake_client.get_object.assert_not_called()


# ---- TC8 / AC8 ---------------------------------------------------------------


async def test_put_object_happy_path_then_visible_in_metadata(mcp, creds, fake_client):
    fake_client.put_object.return_value = {"ETag": '"abc123"'}
    fake_client.head_object.return_value = {
        "ContentLength": 5,
        "ContentType": "text/plain",
        "ETag": '"abc123"',
        "LastModified": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }

    # region differs from creds.region ("nyc3") to prove the call-level region wins.
    put_result = await call_tool(mcp, "spaces_put_object", creds, bucket="b", region="ams3", key="k", content="hello")
    assert put_result == {"key": "k", "etag": "abc123", "size": 5}

    meta = await call_tool(mcp, "spaces_get_object_metadata", creds, bucket="b", region="ams3", key="k")
    assert meta["etag"] == "abc123"
    assert meta["size"] == 5
    assert fake_client.regions_used == ["ams3", "ams3"]


async def test_put_object_oversized_payload_rejected_before_upstream_call(mcp, creds, fake_client):
    oversized = base64.b64encode(b"x" * (settings.put_max_bytes + 1)).decode("ascii")

    result = await call_tool(
        mcp,
        "spaces_put_object",
        creds,
        bucket="b",
        region="nyc3",
        key="k",
        content=oversized,
        content_encoding="base64",
    )

    assert result["error"]["code"] == "invalid_argument"
    fake_client.put_object.assert_not_called()


# ---- TC9 / AC9 ---------------------------------------------------------------


async def test_delete_objects_batch_reports_per_key_result(mcp, creds, fake_client):
    fake_client.delete_objects.return_value = {
        "Deleted": [{"Key": "a"}, {"Key": "missing"}],
        "Errors": [{"Key": "locked", "Code": "AccessDenied", "Message": "nope"}],
    }

    # region differs from creds.region ("nyc3") to prove the call-level region wins.
    result = await call_tool(mcp, "spaces_delete_objects", creds, bucket="b", region="ams3", keys=["a", "missing", "locked"])

    assert set(result["deleted"]) == {"a", "missing"}
    assert result["errors"] == [{"key": "locked", "code": "AccessDenied", "message": "nope"}]
    assert fake_client.regions_used == ["ams3"]


# ---- TC10 / AC10 --------------------------------------------------------------


async def test_copy_object_happy_path(mcp, creds, fake_client):
    fake_client.copy_object.return_value = {"CopyObjectResult": {"ETag": '"copy-etag"'}}

    # region differs from creds.region ("nyc3") to prove the call-level region wins.
    result = await call_tool(
        mcp,
        "spaces_copy_object",
        creds,
        source_bucket="src",
        source_key="k",
        dest_bucket="dst",
        dest_key="k2",
        region="ams3",
    )

    assert result == {"dest_bucket": "dst", "dest_key": "k2", "etag": "copy-etag"}
    assert fake_client.regions_used == ["ams3"]


async def test_copy_object_missing_destination_bucket_returns_not_found(mcp, creds, fake_client):
    fake_client.copy_object.side_effect = _client_error("NoSuchBucket", status=404)

    result = await call_tool(
        mcp,
        "spaces_copy_object",
        creds,
        source_bucket="src",
        source_key="k",
        dest_bucket="missing-bucket",
        dest_key="k2",
        region="nyc3",
    )

    assert result["error"]["code"] == "not_found"


# ---- TC11 / AC11 --------------------------------------------------------------


async def test_get_object_metadata_uses_head_not_get(mcp, creds, fake_client):
    fake_client.head_object.return_value = {
        "ContentLength": 42,
        "ContentType": "application/json",
        "ETag": '"m1"',
        "LastModified": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "Metadata": {"x-custom": "1"},
    }

    # region differs from creds.region ("nyc3") to prove the call-level region wins.
    result = await call_tool(mcp, "spaces_get_object_metadata", creds, bucket="b", region="ams3", key="k")

    assert result["size"] == 42
    assert result["etag"] == "m1"
    assert result["metadata"] == {"x-custom": "1"}
    fake_client.get_object.assert_not_called()
    fake_client.head_object.assert_called_once()
    assert fake_client.regions_used == ["ams3"]


# ---- TC12 / AC12 (tool layer) ------------------------------------------------


async def test_create_presigned_url_honors_call_level_region(mcp, creds):
    # creds carry region "nyc3"; spaces_create_presigned_url signs locally via
    # the real build_s3_client (not the fake_client fixture), so this is the
    # one tool whose region bug the mocked-client tests above cannot catch.
    # The call asks for a different region ("ams3") to prove the signed
    # endpoint follows the call-level region, not the credential header's
    # (PRD-21346 review round 1).
    result = await call_tool(
        mcp,
        "spaces_create_presigned_url",
        creds,
        bucket="my-bucket",
        region="ams3",
        key="my-key",
        method="get",
        expires_in_seconds=900,
    )

    assert "ams3.digitaloceanspaces.com" in result["url"]
    assert "nyc3.digitaloceanspaces.com" not in result["url"]
