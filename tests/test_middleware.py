"""TC2 (AC2) and TC14 (AC14): credential-header gating and request isolation."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from digitalocean_spaces_mcp.middleware import (
    ACCESS_KEY_HEADER,
    REGION_HEADER,
    SECRET_KEY_HEADER,
    CredentialsMiddleware,
    current_credentials,
)


async def _echo_region(_request):
    # Yield control so a concurrent second request gets a chance to run
    # before this one reads the ContextVar back — this is what would surface
    # cross-contamination if credentials were ever stored in a shared place.
    await asyncio.sleep(0.01)
    creds = current_credentials()
    return JSONResponse({"region": creds.region if creds else None, "access_key": creds.access_key})


def _build_test_app():
    inner = Starlette(routes=[Route("/mcp", _echo_region, methods=["POST"])])
    return CredentialsMiddleware(inner, mcp_path="/mcp")


@pytest.mark.asyncio
async def test_missing_credential_headers_returns_401_listing_missing_names():
    app = _build_test_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/mcp", headers={REGION_HEADER: "nyc3"})

    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] == "not_configured"
    assert body["error"]["retryable"] is False
    assert ACCESS_KEY_HEADER in body["error"]["message"]
    assert SECRET_KEY_HEADER in body["error"]["message"]
    assert REGION_HEADER not in body["error"]["message"]


@pytest.mark.asyncio
async def test_all_headers_present_reaches_the_handler():
    app = _build_test_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/mcp",
            headers={
                ACCESS_KEY_HEADER: "AKIA_ONE",
                SECRET_KEY_HEADER: "secret-one",
                REGION_HEADER: "nyc3",
            },
        )

    assert resp.status_code == 200
    assert resp.json() == {"region": "nyc3", "access_key": "AKIA_ONE"}


@pytest.mark.asyncio
async def test_concurrent_requests_with_different_credentials_do_not_cross_contaminate():
    app = _build_test_app()
    transport = httpx.ASGITransport(app=app)
    headers_a = {ACCESS_KEY_HEADER: "AKIA_A", SECRET_KEY_HEADER: "secret-a", REGION_HEADER: "nyc3"}
    headers_b = {ACCESS_KEY_HEADER: "AKIA_B", SECRET_KEY_HEADER: "secret-b", REGION_HEADER: "ams3"}

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp_a, resp_b = await asyncio.gather(
            client.post("/mcp", headers=headers_a),
            client.post("/mcp", headers=headers_b),
        )

    assert resp_a.json() == {"region": "nyc3", "access_key": "AKIA_A"}
    assert resp_b.json() == {"region": "ams3", "access_key": "AKIA_B"}
