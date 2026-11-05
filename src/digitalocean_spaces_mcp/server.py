"""FastMCP app factory.

Wires together: the stateless FastMCP server (SOP §1.3), the 10 spaces_*
tools, a local-only /health probe (SOP §1.1), and the credentials middleware
(middleware.py) — mounted so the MCP session manager's lifespan runs on the
*outer* Starlette app (Mount() alone does not propagate a sub-app's lifespan;
missing this wiring is the "Task group is not initialized" failure mode).
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from . import tools
from .middleware import CredentialsMiddleware

INSTRUCTIONS = (
    "DigitalOcean Spaces is S3-compatible object storage: buckets contain objects "
    "(files) addressed by key. Use spaces_list_buckets / spaces_create_bucket / "
    "spaces_delete_bucket to manage buckets, spaces_list_objects to browse a bucket "
    "(supports prefix filtering and pagination), spaces_get_object / "
    "spaces_get_object_metadata to read small objects or just their metadata, "
    "spaces_put_object / spaces_delete_objects / spaces_copy_object to write, "
    "delete, or duplicate objects, and spaces_create_presigned_url to hand out a "
    "short-lived direct link for GET or PUT when a transfer is too large for a "
    "tool-call payload. Every call requires three headers carrying one tenant's "
    "access key, secret key, and region; there is no shared or default account."
)


def build_mcp() -> FastMCP:
    mcp = FastMCP(
        name="digitalocean-spaces-mcp",
        instructions=INSTRUCTIONS,
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    tools.register(mcp)
    return mcp


async def health(_request: Request) -> JSONResponse:
    """Pure local probe — never calls DigitalOcean (SOP §1.1)."""
    return JSONResponse({"status": "ok"})


def build_app() -> Starlette:
    mcp = build_mcp()
    mcp_app = mcp.streamable_http_app()

    app = Starlette(
        routes=[
            Route("/health", health),
            Mount("/", app=mcp_app),
        ],
        # The MCP session manager's lifespan must run on this outer app:
        # Mount() does not invoke a mounted sub-app's own lifespan (SOP §7).
        lifespan=lambda _app: mcp.session_manager.run(),
    )
    return CredentialsMiddleware(app, mcp_path=mcp.settings.streamable_http_path)


__all__ = ["build_mcp", "build_app", "health"]
