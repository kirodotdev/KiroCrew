"""Give a tool result whose payload is only in ``structuredContent`` a text block.

An MCP ``CallToolResult`` may carry its payload in ``structuredContent`` (a JSON
object, for a tool that declares an ``outputSchema``). The spec asks the server
to also return that object serialized in a ``text`` block, for clients that read
only ``content``; some servers do not (Airtable's hosted server answers
``content: []`` once the client sends ``MCP-Protocol-Version``). The engines Crew
runs read only ``content`` for the model, so such a call reached the agent with
no output at all.

Crew adds the missing block where it relays a server's answers: the MCP
gateway's stdout pump (:mod:`kiro_crew.mcp_gateway.backend_routing`) and the KAS
remote-server bridge (:mod:`kiro_crew.kas_http_bridge`). A result that already
has a text block is left alone, so nothing is sent twice. ``structuredContent``
itself is kept as the server sent it: a client validates it against the tool's
``outputSchema``.

Standard library only: the KAS bridge imports it, once per server process.
"""

from __future__ import annotations

import json
from typing import Any

from kiro_crew.json_line import parse_json_object_line

#: The key's spelling on the wire. A line without it needs no parse.
_KEY_BYTES = b'"structuredContent"'


def with_structured_text(result: Any) -> dict[str, Any] | None:
    """*result* with ``structuredContent`` added to ``content`` as a JSON text block.

    ``None`` when nothing is added: *result* is not an object, has no object
    ``structuredContent``, has a ``content`` that is not a list, or already has a
    text block in ``content``.
    """
    if not isinstance(result, dict):
        return None
    structured = result.get("structuredContent")
    if not isinstance(structured, dict):
        return None
    content = result.get("content", [])
    if not isinstance(content, list):
        return None
    if any(isinstance(item, dict) and item.get("type") == "text" for item in content):
        return None
    try:
        text = json.dumps(structured, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return None
    return {**result, "content": [*content, {"type": "text", "text": text}]}


def line_may_need_text(line: bytes) -> bool:
    """Whether *line* may carry a result :func:`add_text_to_line` would change.

    A cheap byte test, run on the gateway's shared pump: no false negative, so
    only a line that passes is parsed."""
    return _KEY_BYTES in line


def add_text_to_line(line: bytes) -> bytes:
    """*line* (one JSON-RPC message) with its result given a text block, if needed.

    Returns *line* itself when the message is not a response whose result
    :func:`with_structured_text` changes, or does not parse.
    """
    msg = parse_json_object_line(line)
    if msg is None or "method" in msg:
        return line
    result = with_structured_text(msg.get("result"))
    if result is None:
        return line
    return json.dumps({**msg, "result": result}, separators=(",", ":")).encode("utf-8") + b"\n"
