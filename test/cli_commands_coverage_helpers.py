"""Fakes shared by the ``test_cli_commands_coverage*.py`` files.

``_ns`` builds the ``argparse.Namespace`` a handler reads; ``_http_error`` and
``_FakeResponse`` stand in for what ``cli_commands.loopback_urlopen`` and
``app_lifecycle_client.unix_socket_urlopen`` raise and return.
"""

from __future__ import annotations

import argparse
import io
import json
import urllib.error
from typing import Any

# ── helpers ──


def _ns(**kw: Any) -> argparse.Namespace:
    return argparse.Namespace(**kw)


def _http_error(
    code: int, body: bytes | None = None, reason: str = "Boom"
) -> urllib.error.HTTPError:
    """Build an ``HTTPError`` whose ``.read()`` yields *body*."""
    fp = io.BytesIO(body if body is not None else b"")
    return urllib.error.HTTPError("http://localhost/x", code, reason, {}, fp)  # type: ignore[arg-type]


class _FakeResponse:
    """Minimal context-manager stand-in for ``urlopen``'s return value."""

    def __init__(self, payload: Any) -> None:
        self._raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._raw
