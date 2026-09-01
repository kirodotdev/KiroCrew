"""Derive ACP ``ToolCallLocation`` entries from a tool call's raw params.

Zed and other ACP clients implement "follow the agent" by watching the
``locations`` field on ``session/update`` tool_call frames — see
[Following the Agent](https://agentclientprotocol.com/protocol/tool-calls#following-the-agent).
kiro-cli does not currently forward a native ``location`` on its tool events,
so this module derives it from the tool's raw input dict.

The ACP schema requires ``path`` to be an absolute file path (schema
``ToolCallLocation`` in ``test/conformance/vendor/acp-v1/schema.json``). Only
absolute POSIX paths (``/…``) and Windows drive-letter paths (``[A-Za-z]:…``)
are returned; relative or empty values yield no location so a client never
follows to the wrong place. Malformed inputs never raise — a broken tool call
must not abort the ACP stream.
"""

from __future__ import annotations

import re
from typing import Any

#: Keys under which a tool's raw params commonly carry the target file path.
#: Order matters only when a tool nests one inside the other; the first hit wins.
_PATH_KEYS: tuple[str, ...] = ("path", "file_path", "filename")

#: Keys under which a tool's raw params commonly carry a 1-based line number.
#: kiro-cli's ``fs_read`` uses ``start_line`` (inclusive), Anthropic-style edit
#: tools use ``line`` or ``line_number``, and grep-shaped results use ``line``.
#: ``offset`` is included because kiro-cli's ``Read`` maps a 1-based offset to
#: the same coordinate space, though it is byte-count-adjacent in some MCP
#: filesystem servers — treat any non-positive value as "no line".
_LINE_KEYS: tuple[str, ...] = ("line", "start_line", "line_number", "offset")

_WIN_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _is_absolute(path: str) -> bool:
    """Match the ACP ToolCallLocation schema requirement (absolute paths)."""
    return path.startswith("/") or bool(_WIN_DRIVE_RE.match(path))


def _coerce_line(value: Any) -> int | None:
    """Return a positive int line number, or None when unusable."""
    if isinstance(value, bool):
        return None  # bool is an int subclass; reject before the isinstance below
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            n = int(stripped)
            if n > 0:
                return n
    return None


def _first_line(params: dict[str, Any]) -> int | None:
    for key in _LINE_KEYS:
        line = _coerce_line(params.get(key))
        if line is not None:
            return line
    return None


def _first_path(params: dict[str, Any]) -> str | None:
    for key in _PATH_KEYS:
        value = params.get(key)
        if isinstance(value, str) and value and _is_absolute(value):
            return value
    return None


def _location(path: str, line: int | None) -> dict[str, Any]:
    entry: dict[str, Any] = {"path": path}
    if line is not None:
        entry["line"] = line
    return entry


def extract_tool_locations(
    tool_name: str,
    raw_params: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Derive ``ToolCallLocation`` entries from *raw_params* for a tool call.

    Returns an empty list when the tool has no discoverable file target
    (bash, network fetches) or when the params are malformed. The output is
    safe to place on the wire directly under ``session/update``'s
    ``locations`` key.

    Best-effort by design: a follow-along hint that lands on the wrong file
    is worse than none, so anything ambiguous — relative paths, unknown
    param shapes, non-string values — resolves to no location rather than a
    guess.
    """
    if not isinstance(raw_params, dict) or not raw_params:
        return []

    # Shell tools never carry a location by convention. Guarding on the tool
    # name would silently miss a Bash whose params happened to contain a
    # ``path`` (e.g. `cat /tmp/x`), which would follow the editor to a file
    # the agent isn't editing.
    if tool_name in _SHELL_TOOLS:
        return []

    path = _first_path(raw_params)
    if not path:
        # Some tools nest the target under a subfield (e.g. batch reads
        # carry a ``files: [...]`` list). Handle the common batch shapes so
        # a multi-file read still lets the editor follow the first file.
        files = raw_params.get("files")
        if isinstance(files, list):
            out: list[dict[str, Any]] = []
            for item in files:
                if isinstance(item, dict):
                    sub_path = _first_path(item)
                    if sub_path:
                        out.append(_location(sub_path, _first_line(item)))
                elif isinstance(item, str) and _is_absolute(item):
                    out.append({"path": item})
            return out
        return []

    return [_location(path, _first_line(raw_params))]


#: Tool names that never carry a follow-along target. Membership is
#: conservative: an unknown name falls through to the generic path-key scan
#: above, which is safe because that scan only returns absolute paths.
_SHELL_TOOLS: frozenset[str] = frozenset(
    {
        "execute_bash",
        "execute_pwsh",
        "control_bash_process",
        "Bash",
        "bash",
    }
)
