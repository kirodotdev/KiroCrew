"""Serving several chats' folders from one shared backend.

A shared backend runs in one fixed folder, so a server that reads relative paths
or asks for the chat's roots would read the wrong project. This module makes
such a server shareable instead of keeping a copy per chat:

* the gateway knows each chat's folder (``record_session_folder``, fed by the
  Crew-authored claim frame, never the stub's register frame);
* a ``tools/call`` has its relative folder arguments made absolute against the
  CALLER's folder, and a missing required one (``repo_path``) filled with it
  (:func:`scope_arguments`);
* ``roots/list`` is answered by the gateway with every chat folder the backend
  has served (``backend_caller_scope._attached_union``).

All chats belong to one local user, so an absolute path into another chat's
folder is NOT refused: this is correctness, not isolation. A tool this module
cannot scope (a free-form command or URL argument, or no schema) is
"unconfinable": its calls pass through untouched and the gateway does not answer
its server's ``roots/list``, so that server stays one copy per chat.
"""

from __future__ import annotations

import logging
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger("kiro_crew.mcp_gateway.backend")

#: Argument names that hold a filesystem path (compared lower-cased). A schema
#: key outside this set is never rewritten. ``repo`` and ``repository`` are not
#: here: on hosting servers (GitHub, GitLab) they name a repository.
PATH_KEYS = frozenset(
    {
        "path",
        "paths",
        "file",
        "files",
        "filepath",
        "file_path",
        "filename",
        "source",
        "sources",
        "destination",
        "dest",
        "dir",
        "directory",
        "folder",
        "cwd",
        "root",
        "repo_path",
        "working_dir",
        "workdir",
        "project_path",
        "db_path",
        "output_dir",
    }
)

#: A required argument with one of these names that the caller left out is
#: filled with the caller's folder (git's ``repo_path``).
FILL_KEYS = frozenset(
    {"repo_path", "cwd", "working_dir", "workdir", "directory", "dir", "project_path"}
)

#: Arguments that run something or name a remote: a tool with one cannot be
#: scoped by rewriting paths, so its server stays per chat.
UNCONFINABLE_KEYS = frozenset(
    {
        "command",
        "cmd",
        "script",
        "shell",
        "exec",
        "executable",
        "args",
        "argv",
        "url",
        "uri",
        "href",
        "endpoint",
    }
)

_URI_FORMATS = frozenset({"uri", "uri-reference", "iri", "iri-reference"})
_MAX_DEPTH = 6
_MAX_SESSIONS = 512
_MAX_PATH_LEN = 4096
#: Longest session key the folder table keeps. A longer one is refused, never
#: cut: a cut key could name another session.
_MAX_SESSION_KEY_LEN = 1024


@dataclass(frozen=True)
class ToolScope:
    """How one tool's arguments are scoped. ``confinable`` False means "cannot be"."""

    confinable: bool
    fill: tuple[str, ...] = ()


def _walk_schema(schema: Any, depth: int = 0) -> Iterable[tuple[str, dict[str, Any]]]:
    """Every ``(property name, property schema)`` under *schema*, nested ones too."""
    if not isinstance(schema, dict) or depth > _MAX_DEPTH:
        return
    props = schema.get("properties")
    if isinstance(props, dict):
        for name, sub in props.items():
            if isinstance(name, str) and isinstance(sub, dict):
                yield name, sub
                yield from _walk_schema(sub, depth + 1)
    yield from _walk_schema(schema.get("items"), depth + 1)
    for combiner in ("anyOf", "oneOf", "allOf"):
        branches = schema.get(combiner)
        if isinstance(branches, list):
            for branch in branches:
                yield from _walk_schema(branch, depth + 1)


def scan_tool(tool: Any) -> ToolScope:
    """Decide how *tool* (one ``tools/list`` entry) is scoped. No schema is unconfinable."""
    if not isinstance(tool, dict):
        return ToolScope(False)
    schema = tool.get("inputSchema")
    if not isinstance(schema, dict) or "$ref" in schema:
        return ToolScope(False)
    for name, sub in _walk_schema(schema):
        key = name.lower()
        if key in UNCONFINABLE_KEYS or "$ref" in sub:
            return ToolScope(False)
        if str(sub.get("format", "")).lower() in _URI_FORMATS and key not in PATH_KEYS:
            return ToolScope(False)
    required = schema.get("required")
    fill = tuple(
        sorted(
            {r for r in required if isinstance(r, str) and r.lower() in FILL_KEYS}
            if isinstance(required, list)
            else ()
        )
    )
    return ToolScope(True, fill)


def scan_tools(tools: Any) -> dict[str, ToolScope]:
    """Scope of every named tool in a ``tools/list`` result's ``tools`` array."""
    out: dict[str, ToolScope] = {}
    for tool in tools if isinstance(tools, list) else ():
        name = tool.get("name") if isinstance(tool, dict) else None
        if isinstance(name, str) and name:
            out[name] = scan_tool(tool)
    return out


def tools_confinable(tools: Any) -> Optional[bool]:
    """Whether EVERY tool can be scoped; ``None`` when the list is unreadable or empty."""
    scopes = scan_tools(tools)
    if not scopes or len(scopes) != sum(1 for t in tools if isinstance(t, dict) and t.get("name")):
        return None
    return all(s.confinable for s in scopes.values())


# ── session folders ──

_FOLDERS: "OrderedDict[str, str]" = OrderedDict()
_LOCK = threading.Lock()
#: How many session folders the table has dropped at its bound (oldest first).
_EVICTED = [0]


def _valid_folder(folder: Any) -> str:
    if not isinstance(folder, str) or not folder or len(folder) > _MAX_PATH_LEN or "\x00" in folder:
        return ""
    return folder if os.path.isabs(folder) else ""


def record_session_folder(session_key: str, folder: Any) -> bool:
    """Remember *session_key*'s folder. Only an absolute path is kept; returns whether it was."""
    clean = _valid_folder(folder)
    if not isinstance(session_key, str) or not session_key or not clean:
        return False
    if len(session_key) > _MAX_SESSION_KEY_LEN:
        return False
    with _LOCK:
        _FOLDERS.pop(session_key, None)
        _FOLDERS[session_key] = os.path.normpath(clean)
        while len(_FOLDERS) > _MAX_SESSIONS:
            _FOLDERS.popitem(last=False)
            _EVICTED[0] += 1
            if _EVICTED[0] == 1:
                logger.warning(
                    "folder scope: over %d sessions; the oldest session's folder is forgotten",
                    _MAX_SESSIONS,
                )
    return True


def session_folder(session_key: str) -> str:
    """The folder recorded for *session_key*, or ``""`` when none is known."""
    with _LOCK:
        return _FOLDERS.get(session_key, "")


def forget_session_folders() -> None:
    """Drop every record (tests)."""
    with _LOCK:
        _FOLDERS.clear()
        _EVICTED[0] = 0


def shared_crew_off() -> bool:
    """Whether Shared Crew is off in this running gateway; unreadable reads as on.

    The owning gateway sends it on its owner-port announce: with Shared Crew
    off every chat belongs to the one local user, and the daemon may serve
    several chats' folders as one shared folder server's roots.
    """
    try:
        from kiro_crew.shared_crew.preview import preview_enabled

        return not preview_enabled()
    except Exception:  # noqa: BLE001 - unknown ownership must not share folders
        logger.debug("folder scope: Shared Crew state unreadable; reading as on", exc_info=True)
        return False


def file_uri(folder: str) -> str:
    """The ``file://`` URI MCP roots carry for an absolute *folder*."""
    return Path(folder).as_uri()


# ── argument rewriting ──


def _absolute(value: str, folder: str) -> str:
    if not value or "\x00" in value or len(value) > _MAX_PATH_LEN:
        return value
    if os.path.isabs(value) or value.startswith("~"):
        return value
    return os.path.normpath(os.path.join(folder, value))


def _rewrite(node: Any, folder: str, keys: frozenset[str], depth: int) -> Any:
    if depth > _MAX_DEPTH:
        return node
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            lowered = key.lower() if isinstance(key, str) else ""
            if lowered in keys and isinstance(value, str):
                value = _absolute(value, folder)
            elif (
                lowered in keys
                and isinstance(value, list)
                and all(isinstance(v, str) for v in value)
            ):
                value = [_absolute(v, folder) for v in value]
            elif isinstance(value, (dict, list)):
                value = _rewrite(value, folder, keys, depth + 1)
            out[key] = value
        return out
    if isinstance(node, list):
        return [_rewrite(v, folder, keys, depth + 1) for v in node]
    return node


def scope_arguments(
    arguments: Any, scope: Optional[ToolScope], folder: str, *, folder_user: bool
) -> Any:
    """*arguments* scoped to the caller's *folder*; unchanged when nothing applies.

    A relative folder argument (``repo_path``, ``cwd``) is made absolute against
    *folder*, and a required one the caller left out is filled with it: the
    backend's own directory is a Crew-private one, so these never meant it.
    Every other path argument is rewritten only for a *folder_user* (a server
    that asked for the chat's roots): a server launched on a folder of its own
    resolves relative paths against that folder, and rewriting them would break it.

    Nothing is refused. An unknown or unconfinable tool, an unknown folder, or
    arguments that are not an object pass through as they are today.
    """
    if scope is None or not scope.confinable or not folder:
        return arguments
    if arguments is None and scope.fill:
        arguments = {}
    if not isinstance(arguments, dict):
        return arguments
    out = _rewrite(arguments, folder, PATH_KEYS if folder_user else FILL_KEYS, 0)
    present = {k.lower() for k in out if isinstance(k, str)}
    for key in scope.fill:
        if key.lower() not in present:
            out[key] = folder
    return out
