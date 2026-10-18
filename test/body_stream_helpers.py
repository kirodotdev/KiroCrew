"""Shared request body-stream stand-ins for handler unit tests.

Handlers converted to ``_shared.read_bounded_json``'s capped path enforce the
byte ceiling BEFORE decoding by draining ``request.content`` incrementally --
so a mocked ``request.json`` alone does not feed them, and every harness for
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
from unittest.mock import AsyncMock


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
        self._pos = 0

    async def iter_chunked(self, n: int):
        for i in range(0, len(self._raw), n):
            yield self._raw[i : i + n]

    async def read(self, n: int = -1) -> bytes:
        """Whole body for ``read()``; the next *n* bytes for ``read(n)``.

        ``read(n)`` is the incremental drain ``handlers/hooks_webhook_intake.py::_read_hook_body``
        runs, so it advances and answers ``b""`` at the end the way a real stream
        does. ``read()`` keeps returning the whole body, which is what the
        harnesses that predate ``read(n)`` rely on.
        """
        if n < 0:
            return self._raw
        chunk = self._raw[self._pos : self._pos + n]
        self._pos += len(chunk)
        return chunk

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


def attach_json_body(request: Any, raw: bytes, *, content_type: str = "application/json") -> None:
    """Attach *raw* to a mocked request as a JSON body the gated read accepts.

    For the ``MagicMock`` harnesses of handlers whose body helper now reads
    through ``read_bounded_json``: the stream double (:func:`attach_body`), the
    declared ``content_type`` the media-type gate checks, and ``read`` kept
    returning the same bytes for any code path that still calls it. A test of
    the 415 passes another *content_type*. Not for ``make_mocked_request``
    requests, whose ``content_type`` is derived from the headers: give those a
    ``Content-Type`` header and ``payload=BodyStreamPayload(raw)`` instead.
    """
    attach_body(request, None, raw=raw)
    request.content_type = content_type
    request.read = AsyncMock(return_value=raw)


class JsonBodyRequest:
    """Base for hand-written request fakes that serve one JSON body.

    A subclass defines :meth:`body_bytes`; this derives everything the gated
    ``read_bounded_json`` consults from it (stream, length, charset,
    ``can_read_body``, a JSON ``content_type``) plus ``read()`` for any path
    that still reads whole.
    """

    content_type = "application/json"
    charset: str | None = None

    def body_bytes(self) -> bytes:
        raise NotImplementedError

    @property
    def content(self) -> BodyStreamPayload:
        return BodyStreamPayload(self.body_bytes())

    @property
    def content_length(self) -> int:
        return len(self.body_bytes())

    @property
    def can_read_body(self) -> bool:
        return bool(self.body_bytes())

    async def read(self) -> bytes:
        return self.body_bytes()
