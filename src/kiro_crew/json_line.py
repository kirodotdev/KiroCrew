"""Parse one line a child process wrote into a JSON object, or say to skip it.

Every line-oriented JSON reader that sits on a child's stdout (an MCP backend,
an MCP server's stdin, an ACP agent, the iMessage bridge, a CLI in stream-json
mode) owes its caller the same contract: a line it cannot use costs that line,
never the reader. A reader that raises instead ends its loop, and on a shared
reader that is every session behind it.

A line that cannot be parsed may still be a JSON-RPC message its reader owes
an answer to, so :func:`recover_line_id` finds the id of one by scanning
rather than parsing, and only ever the top-level object's own.

This module is dependency-free on purpose, so a stdio MCP server can import it
without pulling the gateway in.
"""

from __future__ import annotations

import json
import re
from typing import Any

#: The whitespace ``json.loads`` itself skips around a value (RFC 8259).
_JSON_WHITESPACE = " \t\n\r"


def parse_json_object_line(line: bytes | str, *, errors: str = "strict") -> dict[str, Any] | None:
    """Return *line* parsed as a JSON object, or ``None`` when it should be skipped.

    ``None`` stands for every unusable line: bytes that are not UTF-8 (under
    the default ``errors="strict"``; pass ``"replace"`` to parse what decodes),
    a blank line, text that is not JSON, JSON that is not an object (``null``,
    ``123``, a list), an integer literal past the interpreter's int-string
    digit limit, and JSON nested past the decoder's ceiling. The last one
    raises ``RecursionError``, which is a ``RuntimeError`` rather than a
    ``ValueError``, so an ``except json.JSONDecodeError`` arm does not catch
    it; every other case here is a ``ValueError``.

    A framing character Python counts as whitespace but JSON does not (a
    json-seq ``\\x1e``, a form feed, U+2028) around an object does not cost
    the line: when the first parse fails and such a character sits at an edge,
    the ``str.strip`` text is parsed once more. Only that retry copies the
    text, so a newline-terminated line costs no more than ``json.loads`` does.

    ``None`` never means end of stream. A caller that has one signals EOF on
    its own, so a ``null`` line cannot be mistaken for it.
    """
    try:
        text = line.decode("utf-8", errors) if isinstance(line, bytes) else line
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            stripped = text.strip()
            if len(stripped) == len(text.strip(_JSON_WHITESPACE)):
                return None
            value = json.loads(stripped)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


#: One JSON string, escapes included.
_JSON_STRING = re.compile(r'"(?:[^"\\]|\\.)*"')
#: A JSON-RPC id value: a string, ``null``, or a number that a delimiter
#: follows, so a number the probe's edge cuts in half (``12`` of ``12345``)
#: is not read as an id of its own.
_ID_VALUE = r'("(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?(?=\s*[,}\]])|null)'
#: The id value after its member's colon.
_ID_SCAN_VALUE = re.compile(r"\s*:\s*" + _ID_VALUE)
#: The id as the LAST member of the top-level object, which is where the MCP
#: TypeScript SDK writes it (``{result, jsonrpc, id}``): on a complete line only
#: the top level's own brace can close the line right after it.
_ID_TAIL = re.compile(r'"id"\s*:\s*' + _ID_VALUE + r"\s*\}\s*$")


def _scan_top_level(text: str) -> tuple[Any, bool]:
    """The top-level object's ``"id"`` value in JSON *text*, and whether that
    object has a ``"method"`` member, as far as the scan can see.

    A scan, not a parse, so it works on text the decoder refuses (nested past
    its ceiling) and on a head cut off mid-value. Only members of the
    top-level object count: an ``"id"`` or ``"method"`` inside ``params`` or
    ``result`` is someone else's, and failing a request off it would fail an
    unrelated call.

    Strings are the only tokens visited one at a time; the brackets between
    two strings are counted in C, so nesting depth costs nothing per level.
    """
    msg_id: Any = None
    has_id = has_method = False
    pos = len(text) - len(text.lstrip())
    if not text.startswith("{", pos):
        return None, False
    depth = 0
    key_expected = False
    while not (has_id and has_method):
        quote = text.find('"', pos)
        between = text[pos:] if quote < 0 else text[pos:quote]
        depth += between.count("{") + between.count("[") - between.count("}") - between.count("]")
        if depth <= 0 or quote < 0:
            break
        if depth == 1:
            # What the top level last saw decides whether a key comes next: a
            # comma or its own opening brace does, a value that just closed
            # does not. Text with no structure in it leaves that unchanged.
            last = max(between.rfind(c) for c in ",{}]")
            if last >= 0:
                key_expected = between[last] in ",{"
        string = _JSON_STRING.match(text, quote)
        if string is None:
            break  # cut off mid-string: nothing past it is structure
        if depth == 1 and key_expected:
            key_expected = False
            if string.group() == '"method"':
                has_method = True
            elif string.group() == '"id"' and not has_id:
                has_id = True
                value = _ID_SCAN_VALUE.match(text, string.end())
                if value is None:
                    break
                try:
                    msg_id = json.loads(value.group(1))
                except ValueError:
                    break
        pos = string.end()
    return msg_id, has_method


def recover_top_level_id(head: bytes, tail: bytes = b"", *, requests_only: bool = False) -> Any:
    """Best-effort JSON-RPC id of a message that cannot be parsed whole.

    *head* is the message, or as much of its start as was kept; *tail* is the
    kept end of one too large to keep whole, and is read as the message's real
    end: a line cut off before its closing brace can show a nested member
    there. Only the top-level object's own ``"id"`` is recovered, never a
    nested one, so an id echoed inside ``params`` or ``result`` cannot fail an
    unrelated request. ``None`` when no id is recoverable. Bytes that are not
    UTF-8 are replaced, not refused.

    With *requests_only* the id is returned only when *head* also shows a
    top-level ``"method"``, i.e. the message is a request. A reader that
    answers with an error passes it: a response carries its peer's own request
    id, and an error sent under it would answer one of the peer's unrelated
    calls. The MCP SDKs write ``method`` before ``params``, so the head holds
    it; a request whose ``method`` is past the head is dropped, never
    misaddressed.
    """
    msg_id, is_request = _scan_top_level(head.decode("utf-8", errors="replace"))
    if requests_only and not is_request:
        return None
    if msg_id is None and tail:
        match = _ID_TAIL.search(tail.decode("utf-8", errors="replace"))
        if match is not None:
            try:
                msg_id = json.loads(match.group(1))
            except ValueError:
                msg_id = None
    return msg_id


#: How much of each end of a line :func:`recover_line_id` reads. A server
#: writes the id first or (the MCP TypeScript SDK) last, so the two ends hold
#: it, and a bounded probe keeps a multi-MiB line from holding the event loop
#: (and the GIL) for seconds in a regex over one huge string.
ID_PROBE_BYTES = 512


def recover_line_id(line: bytes, *, requests_only: bool = False) -> Any:
    """Best-effort top-level JSON-RPC id of one whole *line* that does not parse.

    Only the first and the last :data:`ID_PROBE_BYTES` are read, so the cost
    does not grow with the line and the probe can run inline on a shared
    reader. An id that sits in the middle of a long line is not found; the
    line is then dropped like any other. A line that fits in the probe is
    read by the head scan alone, which has then seen all of it. *requests_only*
    is :func:`recover_top_level_id`'s.
    """
    tail = line[-ID_PROBE_BYTES:] if len(line) > ID_PROBE_BYTES else b""
    return recover_top_level_id(line[:ID_PROBE_BYTES], tail, requests_only=requests_only)
