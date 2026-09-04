"""Shared request body-stream stand-ins for handler unit tests.

Handlers converted to ``_shared.read_bounded_json``'s capped path enforce the
byte ceiling BEFORE decoding by draining ``request.content`` incrementally --
so a mocked ``request.json`` alone no longer feeds them, and every harness for
such a handler must supply real body bytes through a stream double. This
module is that double, extracted from the near-identical ``_Payload`` copies
the converted test files each grew.

Only the *uncapped* path (``read_bounded_json(request, max_bytes=None)``)
still calls ``request.json()``; a harness that serves such a handler must keep
its ``request.json`` mock alongside the stream.
"""

from __future__ import annotations

import json
from typing import Any


class BodyStreamPayload:
    """Minimal stand-in for the aiohttp request body stream.

    The union of what the capped read path and ``request.read()`` consume:
    ``iter_chunked`` (the bounded incremental drain), ``read`` (whole-body),
    ``set_read_chunk_size`` (stream configuration before draining), and
    ``at_eof`` -- ``request.can_read_body`` is ``not payload.at_eof()``, and
    the ``allow_absent`` handlers branch on it, so reporting EOF while bytes
    are waiting would make every request look bodyless and silently default.
    """

    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    async def iter_chunked(self, n: int):
        for i in range(0, len(self._raw), n):
            yield self._raw[i : i + n]

    async def read(self) -> bytes:
        return self._raw

    def set_read_chunk_size(self, size: int) -> None:
        return None

    def at_eof(self) -> bool:
        return not self._raw


def attach_body(request: Any, body: Any, *, raw: bytes | None = None) -> None:
    """Attach a real body stream to a mocked request.

    JSON-encodes *body* (or uses *raw* verbatim when given) and sets the four
    attributes the bounded read consults: ``content``, ``content_length``,
    ``charset``, and ``can_read_body``.
    """
    if raw is None:
        raw = json.dumps(body).encode()
    request.content = BodyStreamPayload(raw)
    request.content_length = len(raw)
    request.charset = None
    request.can_read_body = bool(raw)
