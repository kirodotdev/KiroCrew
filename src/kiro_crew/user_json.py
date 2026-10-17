"""Parse JSON from a config file a person may have saved by hand.

MCP configs (``~/.kiro/settings/mcp.json``, Kiro Crew's own ``mcp.json``, a
project's ``.kiro/settings/mcp.json``, ``~/.mcp.json``), the agent specs that
carry ``mcpServers``, Kiro Crew's ``agent.json`` overrides, ``~/.claude.json``
and a project's Claude settings are edited by people, and Windows editors save
UTF-8 "with BOM" by default. That file is valid UTF-8 whose first character is
U+FEFF, which is not content, and ``json.loads`` refuses it ("Unexpected UTF-8
BOM"). Every reader of such a file parses through :func:`loads_user_json` so one
leading mark is dropped the same way everywhere. A reader that hands
``json.loads`` the undecoded bytes needs nothing: its encoding detection already
drops the mark.

Only reading changes. Writers keep emitting ``json.dumps`` text, so a file read
here and written back comes out as plain BOM-free UTF-8.

The module imports nothing from the package, so any layer can use it.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: The decoded form of the UTF-8 byte-order mark.
_BOM_CHAR = "\ufeff"


def strip_utf8_bom(text: str) -> str:
    """Drop ONE leading UTF-8 byte-order mark from decoded text, if present."""
    return text.removeprefix(_BOM_CHAR)


def loads_user_json(text: str) -> Any:
    """``json.loads`` that accepts one leading UTF-8 byte-order mark.

    Everything else is plain ``json.loads``: the same exceptions, the same result.
    """
    return json.loads(strip_utf8_bom(text))


def strip_json_comments(text: str) -> str:
    """Drop ``//`` and ``/* */`` comments outside string literals.

    String-aware: a ``//`` inside a quoted value (a URL, say) is kept, and so are
    escaped quotes. An unterminated block comment runs to the end of the text.
    """
    output: list[str] = []
    index = 0
    quote = ""
    while index < len(text):
        char = text[index]
        if quote:
            output.append(char)
            if char == "\\" and index + 1 < len(text):
                index += 1
                output.append(text[index])
            elif char == quote:
                quote = ""
            index += 1
            continue
        if char in ('"', "'"):
            quote = char
            output.append(char)
            index += 1
            continue
        if text[index : index + 2] == "//":
            index += 2
            while index < len(text) and text[index] not in "\r\n":
                index += 1
            continue
        if text[index : index + 2] == "/*":
            end = text.find("*/", index + 2)
            index = len(text) if end < 0 else end + 2
            continue
        output.append(char)
        index += 1
    return "".join(output)


def _drop_trailing_commas(text: str) -> str:
    """Drop a comma that only whitespace separates from a closing ``}`` or ``]``.

    Runs on comment-free text and skips string literals, so a comma inside a
    value is never touched.
    """
    output: list[str] = []
    index = 0
    in_string = False
    while index < len(text):
        char = text[index]
        if in_string:
            output.append(char)
            if char == "\\" and index + 1 < len(text):
                index += 1
                output.append(text[index])
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
        elif char == ",":
            ahead = index + 1
            while ahead < len(text) and text[ahead] in " \t\r\n":
                ahead += 1
            if ahead < len(text) and text[ahead] in "}]":
                index += 1
                continue
        output.append(char)
        index += 1
    return "".join(output)


def loads_user_jsonc(text: str) -> Any:
    """:func:`loads_user_json` that also accepts JSONC: comments and trailing commas.

    Kiro's own tools read ``mcp.json`` as JSONC, so a person may comment out a
    server there. Strict JSON is tried first; only when it fails is the text
    parsed again with comments and trailing commas removed. If that fails too,
    the ORIGINAL strict error is raised, so its line numbers match the file.

    For READ paths only. A writer must not parse with this and write the result
    back: the plain JSON it emits would drop every comment. See
    :func:`has_json_comments`.
    """
    try:
        return loads_user_json(text)
    except json.JSONDecodeError as exc:
        try:
            return json.loads(_drop_trailing_commas(strip_json_comments(strip_utf8_bom(text))))
        except json.JSONDecodeError:
            raise exc from None


def has_json_comments(text: str) -> bool:
    """Whether *text* parses only as JSONC (see :func:`loads_user_jsonc`).

    A writer that would rewrite such a file as plain JSON refuses instead.
    """
    try:
        loads_user_json(text)
        return False
    except json.JSONDecodeError:
        pass
    try:
        loads_user_jsonc(text)
    except json.JSONDecodeError:
        return False
    return True


def loads_mcp_config(text: str, *, jsonc: bool = False) -> dict[str, Any]:
    """Parse an MCP config document, refusing a shape its readers cannot index.

    The root must be an object, and ``mcpServers``, when present, must be one
    too. Anything else raises ``json.JSONDecodeError``, so every reader's
    existing "cannot parse" branch handles a wrong shape exactly as it handles
    malformed JSON, instead of crashing on ``.get`` further down.

    Only for a reader whose "cannot parse" branch writes nothing. A reader that
    falls back to an empty document and writes it back parses with
    :func:`loads_user_json`, so a wrong shape fails at the mutation and the
    file survives.

    ``jsonc=True`` also accepts comments and trailing commas
    (:func:`loads_user_jsonc`), as Kiro does for these files. Only a read-only
    caller asks for it: a writer that parsed a commented file would rewrite it
    as plain JSON and drop the comments.
    """
    data = loads_user_jsonc(text) if jsonc else loads_user_json(text)
    if not isinstance(data, dict):
        raise json.JSONDecodeError("top-level JSON is not an object", text, 0)
    if not isinstance(data.get("mcpServers", {}), dict):
        raise json.JSONDecodeError("mcpServers is not an object", text, 0)
    return data


def load_mcp_servers(path: Path) -> dict[str, Any]:
    """The ``mcpServers`` map of a hand-edited MCP config, or ``{}``.

    A missing, unreadable or malformed file, a non-object root, and an
    ``mcpServers`` that is not an object all contribute no servers.
    """
    servers = load_user_json_object(path).get("mcpServers", {})
    if not isinstance(servers, dict):
        logger.warning("Ignoring %s: mcpServers is not an object", path)
        return {}
    return servers


def load_user_json_object(path: Path) -> dict[str, Any]:
    """Load a hand-edited JSON object, returning ``{}`` on any error or non-dict root.

    The same contract as ``kiro_crew.agent._load_json``, with
    :func:`loads_user_jsonc` as the parser, for callers that read an MCP config.
    """
    if not path.is_file():
        return {}
    try:
        data = loads_user_jsonc(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        logger.warning("Ignoring invalid %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        logger.warning("Ignoring %s: top-level JSON is not an object", path)
        return {}
    return data


#: Deepest container nesting an installed settings document may carry. The config
#: readers walk a document recursively (``copy.deepcopy`` in the config cache, the
#: overlay deep-merge), so one nested a few hundred levels deep parses fine and then
#: raises ``RecursionError`` on every later load. Real settings documents nest under
#: ten levels; this bound leaves ample room while staying far below the depth at which
#: those recursive readers exhaust the interpreter's stack.
MAX_DOCUMENT_NESTING = 64


def exceeds_nesting(value: object, limit: int = MAX_DOCUMENT_NESTING) -> bool:
    """Whether *value* nests dicts/lists more than *limit* levels deep.

    Iterative, so measuring a hostile document cannot itself overflow the stack.
    A scalar has depth 0; ``{}`` and ``[]`` have depth 1.
    """
    stack: list[tuple[object, int]] = [(value, 0)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, dict):
            children: Any = node.values()
        elif isinstance(node, list):
            children = node
        else:
            continue
        depth += 1
        if depth > limit:
            return True
        stack.extend((child, depth) for child in children)
    return False


def strict_json_equal(a: object, b: object) -> bool:
    """Type-strict equality over two decoded JSON documents: ``1 != True``,
    ``1 != 1.0``.

    Python's ``==`` conflates ``bool`` with ``int`` and ``int`` with ``float``,
    so a plain compare of two decoded documents calls ``true`` and ``1`` the
    same value. A reader that parses the file itself does not, so a caller
    deciding whether two documents are ONE document (an unchanged stored value,
    two spec files claiming one name) must require identical types. Erring
    toward "different" is the safe side for every such caller: an update or a
    conflict instead of a silent skip, a refusal instead of an arbitrary pick.
    Containers are compared member by member, scalars by type then value.

    Iterative, like :func:`exceeds_nesting`: the documents come from files a
    person or a tool wrote, and a recursive walk over one nested a few hundred
    levels deep would raise ``RecursionError`` out of the caller, which is
    neither of the answers a comparison has. An explicit stack makes depth cost
    memory the document already paid for, never interpreter frames.
    """
    stack: list[tuple[object, object]] = [(a, b)]
    while stack:
        x, y = stack.pop()
        if type(x) is not type(y):
            return False
        if isinstance(x, dict):
            assert isinstance(y, dict)
            if x.keys() != y.keys():
                return False
            stack.extend((x[k], y[k]) for k in x)
        elif isinstance(x, list):
            assert isinstance(y, list)
            if len(x) != len(y):
                return False
            stack.extend(zip(x, y))
        elif x != y:
            return False
    return True


def copy_json_document(value: Any) -> Any:
    """A deep copy of a decoded JSON document the caller may mutate freely.

    ``copy.deepcopy`` recurses several frames per nesting level, so a document
    a few hundred levels deep (well inside the size caps the spec readers
    apply) raises ``RecursionError`` out of whoever hands a memoized parse to
    a caller. This walks an explicit stack instead, like :func:`exceeds_nesting`
    and :func:`strict_json_equal`. Containers (``dict``, ``list``) are rebuilt;
    scalars are shared, which is safe because every JSON scalar is immutable.
    """
    if isinstance(value, dict):
        root: Any = {}
    elif isinstance(value, list):
        root = []
    else:
        return value
    stack: list[tuple[Any, Any]] = [(value, root)]
    while stack:
        src, dst = stack.pop()
        if isinstance(src, dict):
            for key, item in src.items():
                if isinstance(item, (dict, list)):
                    child: Any = {} if isinstance(item, dict) else []
                    dst[key] = child
                    stack.append((item, child))
                else:
                    dst[key] = item
        else:
            for item in src:
                if isinstance(item, (dict, list)):
                    child = {} if isinstance(item, dict) else []
                    dst.append(child)
                    stack.append((item, child))
                else:
                    dst.append(item)
    return root
