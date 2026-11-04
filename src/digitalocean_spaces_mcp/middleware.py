"""Request-scoped credential extraction (SOP §3).

Credentials come only from these three headers, never from environment
variables or a config file, and are isolated per in-flight request via a
ContextVar — never a global or module-level singleton (AC14).
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

ACCESS_KEY_HEADER = "X-DigitalOcean-Spaces-Access-Key"
SECRET_KEY_HEADER = "X-DigitalOcean-Spaces-Secret-Key"
REGION_HEADER = "X-DigitalOcean-Spaces-Region"

_REQUIRED_HEADERS = (ACCESS_KEY_HEADER, SECRET_KEY_HEADER, REGION_HEADER)


@dataclass(frozen=True, slots=True)
class SpacesCredentials:
    access_key: str
    secret_key: str
    region: str


_credentials_var: contextvars.ContextVar[SpacesCredentials | None] = contextvars.ContextVar(
    "spaces_credentials", default=None
)


def current_credentials() -> SpacesCredentials | None:
    """Read the credentials for the in-flight request. None outside a request."""
    return _credentials_var.get()


def _missing_headers(request: Request) -> list[str]:
    return [name for name in _REQUIRED_HEADERS if not request.headers.get(name)]


class CredentialsMiddleware:
    """Pure-ASGI middleware: only intercepts the /mcp path, passes everything else through."""

    def __init__(self, app: ASGIApp, mcp_path: str = "/mcp") -> None:
        self._app = app
        self._mcp_path = mcp_path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(self._mcp_path):
            await self._app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        missing = _missing_headers(request)
        if missing:
            response = JSONResponse(
                {
                    "error": {
                        "code": "not_configured",
                        "message": f"Missing required header(s): {', '.join(missing)}",
                        "retryable": False,
                    }
                },
                status_code=401,
            )
            await response(scope, receive, send)
            return

        creds = SpacesCredentials(
            access_key=request.headers[ACCESS_KEY_HEADER],
            secret_key=request.headers[SECRET_KEY_HEADER],
            region=request.headers[REGION_HEADER],
        )
        token = _credentials_var.set(creds)
        try:
            await self._app(scope, receive, send)
        finally:
            _credentials_var.reset(token)


def build_app_with_credentials_middleware(inner_app: ASGIApp, mcp_path: str = "/mcp") -> ASGIApp:
    return CredentialsMiddleware(inner_app, mcp_path=mcp_path)


__all__ = [
    "ACCESS_KEY_HEADER",
    "SECRET_KEY_HEADER",
    "REGION_HEADER",
    "SpacesCredentials",
    "CredentialsMiddleware",
    "current_credentials",
    "build_app_with_credentials_middleware",
]
