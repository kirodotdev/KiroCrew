"""LSP wire framing and the conversions from LSP results to the dashboard's shapes."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import IO, Any
from urllib.parse import unquote, urlparse

# A header block longer than this is not an LSP frame.
_MAX_HEADER_BYTES = 8 * 1024
# A single message larger than this is refused rather than buffered: a server
# answering a workspace-wide request can otherwise grow the gateway without bound.
MAX_MESSAGE_BYTES = 32 * 1024 * 1024

# LSP SymbolKind, 1-based.
_SYMBOL_KINDS = (
    "file", "module", "namespace", "package", "class", "method", "property", "field",
    "constructor", "enum", "interface", "function", "variable", "constant", "string",
    "number", "boolean", "array", "object", "key", "null", "enum_member", "struct",
    "event", "operator", "type_parameter",
)  # fmt: skip


class FramingError(Exception):
    """The server wrote something that is not an LSP frame."""


def encode_message(payload: dict) -> bytes:
    # A lone surrogate (a half of a UTF-16 pair in the viewer's buffer, which
    # JSON carries happily) has no UTF-8 form: it goes out as "?", one UTF-16
    # unit for one, so the positions the viewer sent still hold.
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8", "replace")
    return b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n" + body


def read_message(stream: IO[bytes]) -> dict | None:
    """Read one frame from *stream*; None at EOF. Blocking."""
    length: int | None = None
    header_bytes = 0
    while True:
        line = stream.readline(_MAX_HEADER_BYTES + 1)
        if not line:
            return None
        header_bytes += len(line)
        if header_bytes > _MAX_HEADER_BYTES:
            raise FramingError("header too long")
        if line in (b"\r\n", b"\n"):
            break
        name, _, value = line.decode("ascii", "replace").partition(":")
        if name.strip().lower() == "content-length":
            try:
                length = int(value.strip())
            except ValueError as exc:
                raise FramingError("bad Content-Length") from exc
    if length is None or length < 0:
        raise FramingError("missing Content-Length")
    if length > MAX_MESSAGE_BYTES:
        raise FramingError("message too large")
    body = stream.read(length)
    if len(body) < length:
        return None
    try:
        message = json.loads(body.decode("utf-8"))
    except ValueError as exc:
        raise FramingError("body is not JSON") from exc
    if not isinstance(message, dict):
        raise FramingError("body is not an object")
    return message


def path_to_uri(path: str) -> str:
    return Path(path).as_uri()


def uri_to_path(uri: str) -> str | None:
    """The local path a ``file:`` URI names, or None for any other scheme."""
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    if parsed.netloc not in ("", "localhost"):
        return None
    return os.path.normpath(unquote(parsed.path))


def _position(raw: Any) -> tuple[int, int]:
    if not isinstance(raw, dict):
        return 0, 0
    line = raw.get("line")
    character = raw.get("character")
    return (
        line if isinstance(line, int) and line >= 0 else 0,
        character if isinstance(character, int) and character >= 0 else 0,
    )


def _range(raw: Any) -> tuple[int, int, int, int]:
    if not isinstance(raw, dict):
        return 0, 0, 0, 0
    start = _position(raw.get("start"))
    end = _position(raw.get("end"))
    return start[0], start[1], end[0], end[1]


def locations_of(result: Any) -> list[tuple[str, tuple[int, int, int, int]]]:
    """``(uri, range)`` pairs from a Location, Location[], LocationLink[] or null."""
    if result is None:
        return []
    items = result if isinstance(result, list) else [result]
    out: list[tuple[str, tuple[int, int, int, int]]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if "targetUri" in item:
            uri = item.get("targetUri")
            rng = item.get("targetSelectionRange") or item.get("targetRange")
        else:
            uri = item.get("uri")
            rng = item.get("range")
        if isinstance(uri, str):
            out.append((uri, _range(rng)))
    return out


def hover_markdown(result: Any) -> str:
    """A hover result's contents as one markdown string ('' when empty)."""
    if not isinstance(result, dict):
        return ""
    return _marked(result.get("contents")).strip()


def _marked(contents: Any) -> str:
    if contents is None:
        return ""
    if isinstance(contents, str):
        return contents
    if isinstance(contents, list):
        return "\n\n".join(part for part in (_marked(c) for c in contents) if part)
    if isinstance(contents, dict):
        value = contents.get("value")
        if not isinstance(value, str):
            return ""
        if "language" in contents:  # MarkedString {language, value}
            language = contents.get("language") or ""
            return f"```{language}\n{value}\n```"
        return value  # MarkupContent; plaintext reads fine as markdown
    return ""


def symbol_kind_name(kind: Any) -> str:
    if isinstance(kind, int) and 1 <= kind <= len(_SYMBOL_KINDS):
        return _SYMBOL_KINDS[kind - 1]
    return "symbol"


def flatten_symbols(result: Any, max_items: int) -> tuple[list[dict], bool]:
    """A documentSymbol answer as a flat outline in document order.

    Accepts both shapes the spec allows: hierarchical ``DocumentSymbol[]`` (depth
    from nesting) and flat ``SymbolInformation[]`` (depth 0, ``container`` from
    ``containerName``). Lines are 1-based. Returns ``(items, truncated)``.
    """
    out: list[dict] = []
    if not isinstance(result, list):
        return out, False

    def visit(symbols: list, depth: int) -> bool:
        for symbol in symbols:
            if len(out) >= max_items:
                return True
            if not isinstance(symbol, dict) or not isinstance(symbol.get("name"), str):
                continue
            if "location" in symbol:
                rng = _range((symbol.get("location") or {}).get("range"))
                select = rng
            else:
                rng = _range(symbol.get("range"))
                select = _range(symbol.get("selectionRange") or symbol.get("range"))
            out.append(
                {
                    "name": symbol["name"],
                    "kind": symbol_kind_name(symbol.get("kind")),
                    "detail": symbol.get("detail") if isinstance(symbol.get("detail"), str) else "",
                    "container": (
                        symbol.get("containerName")
                        if isinstance(symbol.get("containerName"), str)
                        else ""
                    ),
                    "depth": depth,
                    "line": select[0] + 1,
                    "character": select[1],
                    "endLine": rng[2] + 1,
                }
            )
            children = symbol.get("children")
            if isinstance(children, list) and visit(children, depth + 1):
                return True
        return False

    truncated = visit(result, 0)
    if out and all(item["depth"] == 0 for item in out):
        out.sort(key=lambda item: (item["line"], item["character"]))
    return out, truncated
