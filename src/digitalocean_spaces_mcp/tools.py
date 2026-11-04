"""The 10 spaces_* MCP tools (SOP §2 — capabilities, not endpoint mirrors).

Every tool: reads credentials from the request's ContextVar (never a
parameter a caller could spoof across tenants), builds a fresh S3 client,
calls DigitalOcean through client.call_with_retry, and returns a compact
JSON string — either the result or a structured error envelope. Tools never
raise; FastMCP registration happens in server.py via register(mcp).
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from typing import Literal

from mcp.types import ToolAnnotations

from .client import build_s3_client, call_with_retry, generate_presigned_url
from .errors import SpacesError, dumps
from .middleware import current_credentials
from .settings import settings

_TEXT_LIKE_PREFIXES = ("text/", "application/json", "application/xml", "application/javascript")


def _text_like(content_type: str | None) -> bool:
    if not content_type:
        return False
    head = content_type.split(";", 1)[0].strip().lower()
    return head.startswith(_TEXT_LIKE_PREFIXES)


def _require_credentials():
    """Defensive fallback — CredentialsMiddleware already blocks requests
    missing any header with a 401 before a tool ever runs."""
    creds = current_credentials()
    if creds is None:
        raise SpacesError(
            "not_configured",
            "Missing required header(s): X-DigitalOcean-Spaces-Access-Key, "
            "X-DigitalOcean-Spaces-Secret-Key, X-DigitalOcean-Spaces-Region",
        )
    return creds


async def _run(coro_result_builder):
    """Shared try/except: SpacesError -> error envelope, anything upstream too."""
    try:
        return await coro_result_builder()
    except SpacesError as exc:
        return exc.to_json()


def register(mcp) -> None:
    @mcp.tool(
        description="List every Spaces bucket owned by the account (name, region, creation date).",
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    async def spaces_list_buckets() -> str:
        async def run() -> str:
            creds = _require_credentials()
            client = build_s3_client(creds)
            resp = await call_with_retry(client.list_buckets)
            buckets = [
                {
                    "name": b["Name"],
                    # DigitalOcean's ListBuckets XML can carry a per-bucket Region
                    # extension that botocore's AWS S3 model does not parse; fall
                    # back to the requesting region when it is not present.
                    "region": b.get("Region", creds.region),
                    "created": b["CreationDate"].isoformat(),
                }
                for b in resp.get("Buckets", [])
            ]
            return dumps(buckets)

        return await _run(run)

    @mcp.tool(
        description="Create a Spaces bucket. Fails with a structured error if the name is already taken globally.",
        annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=False),
    )
    async def spaces_create_bucket(
        name: str,
        region: str,
        acl: Literal["private", "public-read"] = "private",
    ) -> str:
        async def run() -> str:
            creds = _require_credentials()
            client = build_s3_client(creds)
            await call_with_retry(client.create_bucket, Bucket=name, ACL=acl, bucket_name=name)
            return dumps({"name": name, "region": region})

        return await _run(run)

    @mcp.tool(
        description=(
            "Delete an empty Spaces bucket.\n\n"
            "If the bucket still has objects, returns a structured error telling the "
            "agent to empty it first instead of a bare failure."
        ),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True),
    )
    async def spaces_delete_bucket(name: str, region: str) -> str:
        async def run() -> str:
            creds = _require_credentials()
            client = build_s3_client(creds)
            await call_with_retry(client.delete_bucket, Bucket=name, bucket_name=name)
            return dumps({"deleted": True})

        return await _run(run)

    @mcp.tool(
        description=(
            "List objects in a bucket, optionally filtered by prefix.\n\n"
            "Paginate with limit and the returned continuation_token."
        ),
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    async def spaces_list_objects(
        bucket: str,
        region: str,
        prefix: str | None = None,
        delimiter: str | None = None,
        continuation_token: str | None = None,
        limit: int = 50,
    ) -> str:
        async def run() -> str:
            creds = _require_credentials()
            client = build_s3_client(creds)
            kwargs: dict = {"Bucket": bucket, "MaxKeys": max(1, min(limit, 200))}
            if prefix:
                kwargs["Prefix"] = prefix
            if delimiter:
                kwargs["Delimiter"] = delimiter
            if continuation_token:
                kwargs["ContinuationToken"] = continuation_token
            resp = await call_with_retry(client.list_objects_v2, bucket_name=bucket, **kwargs)
            objects = [
                {
                    "key": o["Key"],
                    "size": o["Size"],
                    "last_modified": o["LastModified"].isoformat(),
                    "etag": o["ETag"].strip('"'),
                }
                for o in resp.get("Contents", [])
            ]
            common_prefixes = [p["Prefix"] for p in resp.get("CommonPrefixes", [])]
            result: dict = {
                "objects": objects,
                "common_prefixes": common_prefixes,
                "has_more": resp.get("IsTruncated", False),
            }
            if result["has_more"] and resp.get("NextContinuationToken"):
                result["continuation_token"] = resp["NextContinuationToken"]
            return dumps(result)

        return await _run(run)

    @mcp.tool(
        description=(
            "Read a Spaces object's content.\n\n"
            "Returns inline text if it is under the size cap and a text content type; "
            "otherwise returns metadata only plus a note to use spaces_create_presigned_url."
        ),
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    async def spaces_get_object(
        bucket: str, region: str, key: str, max_bytes: int = settings.get_inline_max_bytes
    ) -> str:
        async def run() -> str:
            creds = _require_credentials()
            client = build_s3_client(creds)
            head = await call_with_retry(client.head_object, Bucket=bucket, Key=key, bucket_name=bucket)
            size = head["ContentLength"]
            content_type = head.get("ContentType", "application/octet-stream")

            if size > max_bytes or not _text_like(content_type):
                return dumps(
                    {
                        "content": None,
                        "content_type": content_type,
                        "size": size,
                        "note": "Object exceeds the inline size cap or is not text; "
                        "use spaces_create_presigned_url to fetch it directly.",
                    }
                )

            resp = await call_with_retry(client.get_object, Bucket=bucket, Key=key, bucket_name=bucket)
            raw = resp["Body"].read()
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                return dumps(
                    {
                        "content": None,
                        "content_type": content_type,
                        "size": size,
                        "note": "Object is not valid UTF-8 text; "
                        "use spaces_create_presigned_url to fetch it directly.",
                    }
                )
            return dumps({"content": text, "content_type": content_type, "size": size, "truncated": False})

        return await _run(run)

    @mcp.tool(
        description=(
            "Write an object.\n\n"
            "Content under the size cap is accepted as UTF-8 text or base64; larger "
            "payloads are rejected before any upstream call."
        ),
        annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True),
    )
    async def spaces_put_object(
        bucket: str,
        region: str,
        key: str,
        content: str,
        content_encoding: Literal["utf8", "base64"] = "utf8",
        content_type: str | None = None,
        acl: Literal["private", "public-read"] = "private",
    ) -> str:
        async def run() -> str:
            creds = _require_credentials()
            if content_encoding == "base64":
                try:
                    body = base64.b64decode(content, validate=True)
                except Exception as exc:  # noqa: BLE001 - surfaced as invalid_argument
                    raise SpacesError("invalid_argument", f"content is not valid base64: {exc}") from exc
            else:
                body = content.encode("utf-8")

            if len(body) > settings.put_max_bytes:
                raise SpacesError(
                    "invalid_argument",
                    f"Payload is {len(body)} bytes, over the {settings.put_max_bytes}-byte cap; "
                    "use spaces_create_presigned_url with method 'put' for larger writes.",
                )

            client = build_s3_client(creds)
            kwargs: dict = {"Bucket": bucket, "Key": key, "Body": body, "ACL": acl}
            if content_type:
                kwargs["ContentType"] = content_type
            resp = await call_with_retry(client.put_object, bucket_name=bucket, **kwargs)
            return dumps({"key": key, "etag": resp["ETag"].strip('"'), "size": len(body)})

        return await _run(run)

    @mcp.tool(
        description=(
            "Delete one or many objects in one call.\n\n"
            "Reports per-key success/failure; deleting an already-absent key does not "
            "fail the batch."
        ),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True),
    )
    async def spaces_delete_objects(bucket: str, region: str, keys: list[str]) -> str:
        async def run() -> str:
            if not keys:
                raise SpacesError("invalid_argument", "keys must contain at least one key.")
            if len(keys) > 1000:
                raise SpacesError("invalid_argument", "keys must contain at most 1000 entries per call.")
            creds = _require_credentials()
            client = build_s3_client(creds)
            resp = await call_with_retry(
                client.delete_objects,
                Bucket=bucket,
                Delete={"Objects": [{"Key": k} for k in keys], "Quiet": False},
                bucket_name=bucket,
            )
            deleted = [d["Key"] for d in resp.get("Deleted", [])]
            errors = [
                {"key": e.get("Key"), "code": e.get("Code"), "message": e.get("Message")}
                for e in resp.get("Errors", [])
            ]
            return dumps({"deleted": deleted, "errors": errors})

        return await _run(run)

    @mcp.tool(
        description=(
            "Copy an object within the same region.\n\n"
            "Returns a structured not_found error if the destination bucket does not exist."
        ),
        annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True),
    )
    async def spaces_copy_object(
        source_bucket: str,
        source_key: str,
        dest_bucket: str,
        dest_key: str,
        region: str,
        acl: Literal["private", "public-read"] | None = None,
    ) -> str:
        async def run() -> str:
            creds = _require_credentials()
            client = build_s3_client(creds)
            kwargs: dict = {
                "Bucket": dest_bucket,
                "Key": dest_key,
                "CopySource": {"Bucket": source_bucket, "Key": source_key},
            }
            if acl:
                kwargs["ACL"] = acl
            resp = await call_with_retry(client.copy_object, bucket_name=dest_bucket, **kwargs)
            etag = resp["CopyObjectResult"]["ETag"].strip('"')
            return dumps({"dest_bucket": dest_bucket, "dest_key": dest_key, "etag": etag})

        return await _run(run)

    @mcp.tool(
        description="Get an object's size, content type, ETag, and last-modified time without transferring its body.",
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    async def spaces_get_object_metadata(bucket: str, region: str, key: str) -> str:
        async def run() -> str:
            creds = _require_credentials()
            client = build_s3_client(creds)
            head = await call_with_retry(client.head_object, Bucket=bucket, Key=key, bucket_name=bucket)
            return dumps(
                {
                    "size": head["ContentLength"],
                    "content_type": head.get("ContentType", "application/octet-stream"),
                    "etag": head.get("ETag", "").strip('"'),
                    "last_modified": head["LastModified"].isoformat(),
                    "metadata": head.get("Metadata", {}),
                }
            )

        return await _run(run)

    @mcp.tool(
        description=(
            "Get a short-lived pre-signed URL for a single GET or PUT on one object.\n\n"
            "Computed with zero network calls; use for transfers too large for a "
            "tool-call payload."
        ),
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    async def spaces_create_presigned_url(
        bucket: str,
        region: str,
        key: str,
        method: Literal["get", "put"],
        expires_in_seconds: int = 3600,
        content_type: str | None = None,
    ) -> str:
        async def run() -> str:
            if expires_in_seconds <= 0 or expires_in_seconds > 604800:
                raise SpacesError(
                    "invalid_argument", "expires_in_seconds must be between 1 and 604800 (7 days)."
                )
            creds = _require_credentials()
            try:
                url = generate_presigned_url(
                    creds,
                    method=method,
                    bucket=bucket,
                    key=key,
                    expires_in_seconds=expires_in_seconds,
                    content_type=content_type,
                )
            except Exception as exc:  # noqa: BLE001 - map_client_error only handles ClientError
                if isinstance(exc, SpacesError):
                    raise
                raise SpacesError("upstream_error", str(exc)) from exc
            expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expires_in_seconds)).isoformat()
            return dumps({"url": url, "expires_at": expires_at})

        return await _run(run)


__all__ = ["register"]
