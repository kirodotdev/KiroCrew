"""A picture an app tool attaches rides on THAT call's answer through the real stdio loop.

``mcp_tool_image.attach`` keeps one checked PNG for the worker thread that runs the
call; the loop takes it when the call returns and frames it beside the text, once.
Only a real PNG inside the byte and edge caps is kept, and an error answer drops it.
"""

from __future__ import annotations

import base64
import struct
import zlib

from mcp_shared_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _framing_undetected,
    _LoopHarness,
    _tools_call,
)

from kiro_crew import mcp_tool_image
from kiro_crew.validation_parts.wire_envelopes import build_tool_response


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def _png(width: int = 2, height: int = 2, pad: int = 0) -> str:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * 2 for _ in range(2))
    data = b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(raw))
    if pad:
        data += _chunk(b"tEXt", b"x" * pad)
    return base64.b64encode(data + _chunk(b"IEND", b"")).decode()


def test_the_picture_rides_on_its_own_call_only(monkeypatch):
    def call_tool(name, args):
        if name == "draw":
            assert mcp_tool_image.attach(_png())
            return "drew it"
        return "plain"

    harness = _LoopHarness(monkeypatch, call_tool)
    try:
        harness.send(_tools_call(1, "draw"))
        harness.send(_tools_call(2, "plain"))
        assert harness.wait_for(lambda: len(harness.responses) >= 2)
        by_id = {r[0]: r[1] for r in harness.responses}
        assert [c["type"] for c in by_id[1]["content"]] == ["text", "image"]
        assert by_id[1]["content"][1] == {"type": "image", "data": _png(), "mimeType": "image/png"}
        assert [c["type"] for c in by_id[2]["content"]] == ["text"]
    finally:
        harness.close()


def test_a_png_inside_the_caps_is_kept_once():
    assert mcp_tool_image.attach(_png(width=2000, height=2000))
    assert mcp_tool_image.take() is not None
    assert mcp_tool_image.take() is None


def test_a_png_over_the_byte_cap_is_refused():
    big = _png(pad=mcp_tool_image.MAX_PNG_BYTES)
    assert not mcp_tool_image.attach(big)
    assert mcp_tool_image.take() is None
    # Just under the cap passes, so the refusal above is the byte cap and not the shape.
    assert mcp_tool_image.attach(_png(pad=mcp_tool_image.MAX_PNG_BYTES - 200))


def test_a_png_over_the_edge_cap_or_not_a_png_is_refused():
    for b64 in (
        _png(width=2001),
        _png(height=2001),
        base64.b64encode(b"GIF89a" + b"\x00" * 64).decode(),
        "not base64!",
        42,
    ):
        assert not mcp_tool_image.attach(b64), b64
        assert mcp_tool_image.take() is None


def test_a_refused_picture_replaces_an_earlier_one():
    assert mcp_tool_image.attach(_png())
    assert not mcp_tool_image.attach("nope")
    assert mcp_tool_image.take() is None


def test_an_error_answer_carries_no_picture():
    frame = build_tool_response("Error: no", is_error=True, image=_png())
    assert [c["type"] for c in frame["content"]] == ["text"]
