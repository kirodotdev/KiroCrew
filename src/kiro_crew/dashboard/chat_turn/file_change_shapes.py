"""Pre-write file snapshots for the write tools of harnesses other than kiro-cli.

``_snapshot_write_target`` reads kiro-cli's argument shapes (``command`` plus
``path``/``text``/``oldStr``/``newStr``). The other harnesses name their files
under other keys, so their edits recorded no snapshot and their replies carried
no file-change chip. The shapes read here are the live ones:

* opencode ``write`` ``{filePath, content}`` and ``edit``
  ``{filePath, oldString, newString, replaceAll}``;
* pi ``write`` ``{path, content}`` and ``edit`` ``{path, edits}``;
* claude ``Write`` ``{file_path, content}``, ``Edit``
  ``{file_path, old_string, new_string, replace_all}`` and ``MultiEdit``
  ``{file_path, edits}``; deepseek's ``write`` sends claude's ``Write`` shape;
* Copilot CLI ``apply_patch``, whose ``rawInput`` is the patch text itself.

Each of these frames arrives before the call's permission request, so the write
has not run and the before is read from disk (``_disk_before_snapshot``). A path
the call spells relative to the session's working directory is joined to it;
without an absolute working directory such a path names no file Crew can pin and
is skipped. ``validate_file_path`` refuses sensitive paths exactly as it does for
kiro-cli's writes.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import validate_file_path
    from kiro_crew.dashboard.chat_turn.file_changes import (
        _MAX_SNAPSHOT_PATH_CHARS,
        _disk_before_snapshot,
        _snapshot_write_target,
    )

#: (path key, every key the tool sends). A frame is read as one of these writes
#: when its keys, reserved ``__`` arguments (the tool purpose) aside, are a subset of one shape
#: and include a key beyond the path: a path alone is also how a read starts
#: (claude streams ``{file_path}`` first for both its Read and its Edit).
_HARNESS_WRITE_SHAPES: tuple[tuple[str, frozenset[str]], ...] = (
    ("filePath", frozenset({"filePath", "content"})),
    ("filePath", frozenset({"filePath", "oldString", "newString", "replaceAll"})),
    ("path", frozenset({"path", "content"})),
    ("path", frozenset({"path", "edits"})),
    ("file_path", frozenset({"file_path", "content"})),
    ("file_path", frozenset({"file_path", "old_string", "new_string", "replace_all"})),
    ("file_path", frozenset({"file_path", "edits"})),
)

_PATCH_FILE_HEADERS = ("*** Add File: ", "*** Update File: ", "*** Delete File: ", "*** Move to: ")


def _harness_write_path(raw_params: dict) -> str | None:
    """The file a non-kiro write tool's arguments name, or None for any other shape."""
    keys = {key for key in raw_params if not (isinstance(key, str) and key.startswith("__"))}
    for path_key, shape in _HARNESS_WRITE_SHAPES:
        if path_key in keys and len(keys) > 1 and keys <= shape:
            path = raw_params[path_key]
            return path if isinstance(path, str) else None
    return None


def _patch_file_paths(patch: str) -> list[str]:
    """The files an apply_patch envelope adds, updates, deletes or moves to, in order."""
    if not patch.startswith("*** Begin Patch"):
        return []
    paths: list[str] = []
    for line in patch.split("\n"):
        line = line.removesuffix("\r")
        header = next((h for h in _PATCH_FILE_HEADERS if line.startswith(h)), None)
        if header is None:
            continue
        path = line[len(header) :].strip()
        if path and path not in paths:
            paths.append(path)
    return paths


def _anchored(path: str, cwd: str) -> str | None:
    """*path* as an absolute path, joined to *cwd* when relative, else None."""
    if not path or "\x00" in path:
        return None
    if not os.path.isabs(path):
        if not (cwd and os.path.isabs(cwd)):
            return None
        path = os.path.join(cwd, path)
    return path if len(path) <= _MAX_SNAPSHOT_PATH_CHARS else None


def _client_cwd(client: Any) -> str:
    """The session working directory *client* reports, ``""`` when it names none."""
    cwd = getattr(client, "cwd", "")
    return cwd if isinstance(cwd, str) else ""


def _snapshot_tool_writes(event: Any, cwd: str = "") -> list[dict[str, Any]]:
    """Every pre-write snapshot one tool-call event yields, for ``_record_turn_snapshot``.

    kiro-cli's shapes go to ``_snapshot_write_target`` first and, when it takes
    the event, nothing else is read, so its behaviour is unchanged. Otherwise a
    non-kiro write shape (the module docstring) is snapshotted from disk. An MCP
    call is never read as a builtin write, and a patch whose display text was
    redacted is skipped, because its paths may differ from the ones written.
    """
    raw_params = getattr(event, "raw_tool_params", None)
    snapshot = _snapshot_write_target(
        raw_params,
        diff_old_text=getattr(event, "diff_old_text", None),
        diff_path=getattr(event, "diff_path", "") or "",
    )
    if snapshot:
        return [snapshot]
    if getattr(event, "mcp_server_name", ""):
        return []
    if isinstance(raw_params, dict):
        named = _harness_write_path(raw_params)
        paths = [named] if named else []
    elif (
        raw_params is None
        and getattr(event, "tool_kind", "") == "edit"
        and not getattr(event, "tool_input_redacted", False)
        and isinstance(getattr(event, "tool_input", None), str)
    ):
        paths = _patch_file_paths(event.tool_input)
    else:
        paths = []
    snapshots: list[dict[str, Any]] = []
    for path in paths:
        anchored = _anchored(path, cwd)
        if anchored is None:
            continue
        canonical = validate_file_path(anchored)
        if canonical is not None:
            snapshots.append(_disk_before_snapshot(anchored, canonical))
    return snapshots
