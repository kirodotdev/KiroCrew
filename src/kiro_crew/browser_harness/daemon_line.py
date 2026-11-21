"""What an op's own arguments cost on the Harness daemon's request line.

Harness's daemon reads each request as ONE line of at most 64 KiB (asyncio's
default ``StreamReader`` limit), and a longer line fails the call with
``Separator is found, but chunk is longer than limit``. Harness writes the
request with ``json.dumps``, so a character costs its JSON-escaped size there,
not one: a quote or backslash two bytes, a non-ASCII character six (twelve past
the BMP). ``TEXT_CAP`` counts characters, so it cannot keep a line short alone.
"""

from __future__ import annotations

import json

#: The daemon's line limit: asyncio's default ``StreamReader`` limit.
DAEMON_LINE_BYTES = 64 * 1024
#: Bytes kept for the request around an expression: the method name, the
#: execution context or session id, the other params, the line's own newline.
ENVELOPE_BYTES = 1024
#: Most bytes of text one ``type_text`` call carries; a longer text is typed in
#: several ``Input.insertText`` calls, each far under :data:`DAEMON_LINE_BYTES`.
TYPE_CHUNK_BYTES = 16 * 1024


def wire_bytes(text: str) -> int:
    """The bytes *text* takes inside a JSON string on the daemon's line."""
    return len(json.dumps(text)) - 2


def expression_fits(expression: str) -> bool:
    """A ``js`` / ``js_isolated`` call carrying *expression* fits the daemon's line."""
    return wire_bytes(expression) + ENVELOPE_BYTES <= DAEMON_LINE_BYTES


def type_chunks(text: str, limit: int = TYPE_CHUNK_BYTES) -> list[str]:
    """*text* cut into pieces of at most *limit* wire bytes, in order; ``[""]`` for none."""
    chunks: list[str] = []
    start = size = 0
    for index, char in enumerate(text):
        cost = wire_bytes(char)
        if size + cost > limit and index > start:
            chunks.append(text[start:index])
            start, size = index, 0
        size += cost
    chunks.append(text[start:])
    return chunks
