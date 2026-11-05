"""Structured error envelope shared by every tool (SOP §4).

Tools never raise; they catch everything and return this envelope,
serialized to a JSON string, so the agent can branch on `code` and decide
whether to retry.
"""

from __future__ import annotations

import json
from typing import Literal

ErrorCode = Literal[
    "not_configured",
    "unauthorized",
    "not_found",
    "invalid_argument",
    "rate_limited",
    "upstream_error",
]

_RETRYABLE_CODES: frozenset[ErrorCode] = frozenset({"rate_limited", "upstream_error"})


class SpacesError(Exception):
    """A structured, user-facing error. Raised internally, caught at the tool boundary."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = code in _RETRYABLE_CODES

    def to_envelope(self) -> dict:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": self.retryable,
            }
        }

    def to_json(self) -> str:
        return json.dumps(self.to_envelope(), separators=(",", ":"), ensure_ascii=False)


def dumps(payload: dict | list) -> str:
    """Compact, non-ASCII-escaped JSON for tool results (SOP §2.5)."""
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
