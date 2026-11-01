"""Provider-neutral stdlib HTTP/SSE transport helpers.

This module owns mechanics that are independent of any model vendor: request
opening, HTTP error normalization, cooperative interruption, response reads and
SSE line iteration. Provider adapters keep only their wire-format semantics.
"""
from __future__ import annotations

import http.client
import json
import socket
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from .base import ProviderError, ProviderInterrupted, ProviderTransportError


class InterruptibleResponse:
    """Close a live urllib response when the host asks the turn to yield."""

    def __init__(self, response: Any, should_interrupt: Callable[[], bool] | None) -> None:
        self.response = response
        self.should_interrupt = should_interrupt
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None

    def __enter__(self) -> Any:
        if self.should_interrupt is not None:
            def _watch() -> None:
                while not self.stop.wait(0.05):
                    try:
                        interrupted = bool(self.should_interrupt and self.should_interrupt())
                    except Exception:
                        interrupted = False
                    if not interrupted:
                        continue
                    try:
                        self.response.close()
                    except Exception:
                        pass
                    return

            self.thread = threading.Thread(
                target=_watch,
                name="kiro-byo-http-interrupt",
                daemon=True,
            )
            self.thread.start()
        return self.response

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.stop.set()
        try:
            self.response.close()
        finally:
            if self.thread is not None and self.thread is not threading.current_thread():
                self.thread.join(timeout=0.2)


def http_error_message(exc: urllib.error.HTTPError) -> str:
    detail = ""
    try:
        raw = exc.read().decode("utf-8", errors="replace")
        if raw:
            try:
                payload = json.loads(raw)
                if isinstance(payload, dict):
                    error = payload.get("error")
                    if isinstance(error, dict) and isinstance(error.get("message"), str):
                        detail = error["message"]
                    elif isinstance(payload.get("message"), str):
                        detail = payload["message"]
                if not detail:
                    detail = raw[:600]
            except json.JSONDecodeError:
                detail = raw[:600]
    except Exception:
        detail = ""
    base = f"HTTP {exc.code} {exc.reason}"
    return f"{base}: {detail}" if detail else base


def read_response_bytes(
    response: Any,
    should_interrupt: Callable[[], bool] | None,
    *,
    interrupted_type: type[ProviderInterrupted] | None = None,
) -> bytes:
    """Read one non-streaming body without leaking another adapter's exception."""

    try:
        return response.read()
    except (OSError, ValueError, http.client.IncompleteRead) as exc:
        if should_interrupt is not None and should_interrupt() and interrupted_type is not None:
            raise interrupted_type("") from exc
        detail = _transport_failure_detail(exc)
        raise ProviderTransportError(
            f"upstream response body ended before completion: {detail}"
        ) from exc


def iter_response_lines(response: Any, should_interrupt: Callable[[], bool] | None):
    """Yield response lines; a watcher-triggered close ends the iterator cleanly."""

    try:
        yield from response
    except (OSError, ValueError, http.client.IncompleteRead) as exc:
        if should_interrupt is not None and should_interrupt():
            return
        detail = _transport_failure_detail(exc)
        raise ProviderTransportError(
            f"upstream streaming response ended before completion: {detail}"
        ) from exc


def _transport_failure_detail(exc: BaseException) -> str:
    """Return a concise content-free description for transport diagnostics."""

    if isinstance(exc, http.client.IncompleteRead):
        partial = getattr(exc, "partial", b"")
        expected = getattr(exc, "expected", None)
        partial_len = len(partial) if isinstance(partial, (bytes, bytearray)) else 0
        if isinstance(expected, int):
            return f"incomplete read ({partial_len} bytes received, {expected} more expected)"
        return f"incomplete read ({partial_len} bytes received)"
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def post_json(
    *,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout_seconds: float,
    error_type: type[ProviderError],
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> Any:
    """Open a JSON POST and normalize only transport-level failures."""

    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        return opener(request, timeout=timeout_seconds)
    except urllib.error.HTTPError as exc:
        raise error_type(http_error_message(exc)) from exc
    except urllib.error.URLError as exc:
        raise ProviderTransportError(f"connection failed: {exc.reason}") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise ProviderTransportError(f"request timed out after {timeout_seconds:g}s") from exc
    except OSError as exc:
        raise ProviderTransportError(
            f"connection failed while opening request: {_transport_failure_detail(exc)}"
        ) from exc
