"""Shared test fixtures and the tool-invocation helper used across the suite.

Tools read credentials from a ContextVar rather than a parameter (AC14), so
tests set that var directly instead of going through the HTTP layer — faster
and it isolates "does the tool logic work" from "does the ASGI wiring work"
(the latter is covered by test_middleware.py).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from digitalocean_spaces_mcp.middleware import SpacesCredentials, _credentials_var
from digitalocean_spaces_mcp.server import build_mcp


@pytest.fixture
def mcp():
    return build_mcp()


@pytest.fixture
def creds() -> SpacesCredentials:
    return SpacesCredentials(access_key="AKIATEST1234", secret_key="supersecrettest", region="nyc3")


async def call_tool(mcp, tool_name: str, creds: SpacesCredentials, **arguments: Any) -> Any:
    """Invoke a registered tool as if a request carrying `creds` had arrived,
    and parse its JSON-string result (success payload or error envelope).

    The tool-name parameter is `tool_name`, not `name`, because several
    Spaces tools (spaces_create_bucket, spaces_delete_bucket) themselves take
    a `name` argument — `name` would collide with **arguments.
    """
    token = _credentials_var.set(creds)
    try:
        _blocks, structured = await mcp.call_tool(tool_name, arguments)
    finally:
        _credentials_var.reset(token)
    return json.loads(structured["result"])
