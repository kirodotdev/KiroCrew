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


def loads_mcp_config(text: str) -> dict[str, Any]:
    """Parse an MCP config document, refusing a shape its readers cannot index.

    The root must be an object, and ``mcpServers``, when present, must be one
    too. Anything else raises ``json.JSONDecodeError``, so every reader's
    existing "cannot parse" branch handles a wrong shape exactly as it handles
    malformed JSON, instead of crashing on ``.get`` further down.

    Only for a reader whose "cannot parse" branch writes nothing. A reader that
    falls back to an empty document and writes it back parses with
    :func:`loads_user_json`, so a wrong shape fails at the mutation and the
    file survives.
    """
    data = loads_user_json(text)
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
    :func:`loads_user_json` as the parser, for callers that read an MCP config.
    """
    if not path.is_file():
        return {}
    try:
        data = loads_user_json(path.read_text(encoding="utf-8"))
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
