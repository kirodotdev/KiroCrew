"""One picture a tool call may hand back beside its text, for the model to look at.

The MCP transport's one exit (``validation.build_tool_response``) frames text. An app
tool declared ``strictSession`` may also answer one PNG of the calling session's own
page (``mcp_tools.app_tools``): the handler calls :func:`attach` in the worker thread
that runs the call, and the dispatch loop calls :func:`take` in that same thread when
the call returns (``mcp_shared_dispatch._run_tool``), so the picture can never ride on
another call's answer.

Only a real PNG within :data:`MAX_PNG_BYTES` and :data:`MAX_EDGE_PX` passes; anything
else is refused and the text goes alone. The gateway relay's image budget
(``mcp_gateway.image_budget``) still applies on top.
"""

from __future__ import annotations

import base64
import binascii
import struct
import threading

MAX_PNG_BYTES = 1_500_000
MAX_EDGE_PX = 2000
_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Signature (8) + IHDR length and type (8) + width and height (8) + the rest of IHDR.
_MIN_PNG_BYTES = 33

_local = threading.local()


def png_ok(data: bytes) -> bool:
    """Whether *data* is a PNG inside the size and edge caps (read from its IHDR)."""
    if len(data) > MAX_PNG_BYTES or len(data) < _MIN_PNG_BYTES:
        return False
    if not data.startswith(_SIGNATURE) or data[12:16] != b"IHDR":
        return False
    width, height = struct.unpack(">II", data[16:24])
    return 0 < width <= MAX_EDGE_PX and 0 < height <= MAX_EDGE_PX


def attach(b64: object) -> bool:
    """Keep *b64* (a base64 PNG) for this thread's answer; ``False`` when it is refused."""
    _local.image = None
    # Base64 of MAX_PNG_BYTES, plus padding slack: refuse before decoding a huge string.
    if not isinstance(b64, str) or len(b64) > MAX_PNG_BYTES * 4 // 3 + 8:
        return False
    try:
        data = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        return False
    if not png_ok(data):
        return False
    _local.image = base64.b64encode(data).decode("ascii")
    return True


def take() -> str | None:
    """The picture kept for this thread's answer (once), or ``None``."""
    image = getattr(_local, "image", None)
    _local.image = None
    return image


def content_block(b64: str) -> dict[str, str]:
    """The MCP image content block for a picture :func:`take` returned."""
    return {"type": "image", "data": b64, "mimeType": "image/png"}
