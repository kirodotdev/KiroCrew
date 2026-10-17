"""Structured error types for kirocrew-client."""
from __future__ import annotations

from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    AUTH_REQUIRED = "AUTH_REQUIRED"
    AUTH_EXPIRED = "AUTH_EXPIRED"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    RATE_LIMITED = "RATE_LIMITED"
    CONTEXT_NOT_QUEUED = "CONTEXT_NOT_QUEUED"
    CONTEXT_SECRET_SHAPED = "CONTEXT_SECRET_SHAPED"
    SERVER_ERROR = "SERVER_ERROR"
    NETWORK_ERROR = "NETWORK_ERROR"
    WS_DISCONNECTED = "WS_DISCONNECTED"


class KiroCrewError(Exception):
    """Structured error with code, message, status, and optional body."""

    def __init__(
        self,
        code: ErrorCode | str,
        message: str,
        status: int | None = None,
        body: Any = None,
    ):
        super().__init__(message)
        self.code = ErrorCode(code) if isinstance(code, str) else code
        self.status = status
        self.body = body

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"code": self.code.value, "message": str(self)}
        if self.status is not None:
            d["status"] = self.status
        if self.body is not None:
            d["body"] = self.body
        return d


def http_status_to_code(status: int) -> ErrorCode:
    if status == 401:
        return ErrorCode.AUTH_EXPIRED
    if status == 403:
        return ErrorCode.AUTH_EXPIRED
    if status == 404:
        return ErrorCode.NOT_FOUND
    if status == 429:
        return ErrorCode.RATE_LIMITED
    if status >= 500:
        return ErrorCode.SERVER_ERROR
    return ErrorCode.VALIDATION_ERROR


def http_error(status: int, body: Any = None) -> KiroCrewError:
    code = http_status_to_code(status)
    if isinstance(body, str):
        message = body
    elif isinstance(body, dict) and "error" in body:
        message = str(body["error"])
    else:
        message = f"HTTP {status}"
    # A full pending-context queue answers 429 with code ``context_not_queued``.
    # It is not transient: only a turn drains the queue, and a turn cannot run
    # while the client is blocked retrying, so retrying is futile. Give it a
    # distinct non-retryable code the client can carve out.
    if status == 429 and isinstance(body, dict) and body.get("code") == "context_not_queued":
        code = ErrorCode.CONTEXT_NOT_QUEUED
    # A credential-shaped pending-context payload answers 400 with code
    # ``context_secret_shaped``. Re-sending the same bytes is refused the same
    # way, so it gets its own code the client can drop instead of re-buffering.
    if status == 400 and isinstance(body, dict) and body.get("code") == "context_secret_shaped":
        code = ErrorCode.CONTEXT_SECRET_SHAPED
    return KiroCrewError(code, message, status=status, body=body)
