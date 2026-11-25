"""``POST /api/mcp/project-file``: add one server to a chat folder's own MCP file.

The apply step of an ``mcp.server`` card with ``target`` ``project``
(:mod:`kiro_crew.change_card_mcp_server`). An agent's file-edit tool is refused on a
project's MCP files (:mod:`kiro_crew.project_mcp_files`), so the person's Apply on
that card is how one changes. The route writes only the file the card named:
``<folder>/.kiro/settings/mcp.json`` (the card raises it for kiro-cli and KAS, for
an agent that sets ``includeMcpJson``) or ``<folder>/.mcp.json``.

Owner-only, like every mutating MCP route, and only as a card's Apply: a request
the card hook did not admit as a step of a confirmed card is refused with
``403 card_required``, so the folder is always the one the gateway wrote into the
card. Body::

    {"folder": "/abs/chat/folder", "file": ".mcp.json", "name": "weather",
     "spec": {"command": "npx", "args": ["-y", "weather-mcp"]}}

Answers ``200 {"written": "<path>"}``, ``409 already_exists`` for a name the file
already has, ``409 not_editable`` for a file that is not a JSON object with an
object ``mcpServers`` or is reached through a link, ``400`` for a body the card
could not have sent, and
``404 folder_not_found`` when the folder is gone. Nothing else in the file changes;
the write is an atomic replace, so a reader sees the old file or the new one.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import threading
from typing import Any

from aiohttp import web

from kiro_crew.project_mcp_files import KIRO_PROJECT_MCP_FILE, PROJECT_MCP_FILES
from kiro_crew.sel import sel

#: Serializes every read-check-replace, so two Applies on one file both land.
#: shortcut: one lock for all project files; per-file locks if Applies ever queue.
_WRITE_LOCK = threading.Lock()

#: Opens a project file without following a final link or waiting on a FIFO.
_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_BINARY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)


#: The largest project MCP file the card reads or rewrites; a hand-written one is a
#: few KiB. A larger one is shown as not editable rather than read into memory.
_MAX_FILE_BYTES = 1024 * 1024


def _open_regular(path: str, *, dir_fd: int | None = None) -> int:
    """A descriptor for *path* (relative to *dir_fd* when given), a regular file.

    Raises ``FileNotFoundError``. Opened non-blocking and checked on the open
    descriptor, so a FIFO an agent planted is refused at once
    (``bounded_read.NotRegularFileError``, an ``OSError``) instead of blocking; so is
    a file over the cap. The caller owns the descriptor.
    """
    from kiro_crew.bounded_read import NotRegularFileError

    fd = os.open(path, _OPEN_FLAGS, dir_fd=dir_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_FILE_BYTES:
            raise NotRegularFileError(f"{path} is not a regular file under the size cap")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read_open(fd: int, path: str) -> str:
    """The UTF-8 text an :func:`_open_regular` descriptor holds, up to the cap."""
    from kiro_crew.bounded_read import NotRegularFileError, bounded_read_fd

    # Bounded again while reading: the file can grow after the fstat.
    data = bounded_read_fd(fd, _MAX_FILE_BYTES + 1)
    if len(data) > _MAX_FILE_BYTES:
        raise NotRegularFileError(f"{path} grew past the size cap")
    return data.decode("utf-8")


def _read_regular_text(path: str) -> str:
    """The UTF-8 text of *path*, a regular file. Raises ``FileNotFoundError``."""
    fd = _open_regular(path)
    try:
        return _read_open(fd, path)
    finally:
        os.close(fd)


#: Outcomes of :func:`write_project_server`.
WRITTEN = "written"
ALREADY_EXISTS = "already_exists"
NOT_EDITABLE = "not_editable"
FOLDER_NOT_FOUND = "folder_not_found"


def read_project_servers(path: str) -> dict[str, Any]:
    """What a card shows about *path*: ``exists``, ``corrupt`` and the server names.

    Blocking. A missing file is an empty one. A file that is not a JSON object
    whose ``mcpServers`` (when present) is an object is ``corrupt``: an add would
    have to rewrite what the person wrote, so it is refused instead. So is a file
    reached through a link (:func:`_crosses_a_link`), which the route refuses too.
    """
    folder, file = _split_project_path(path)
    if folder and _crosses_a_link(folder, file):
        return {"exists": True, "corrupt": True, "servers": []}
    try:
        text = _read_regular_text(path)
    except FileNotFoundError:
        return {"exists": False, "corrupt": False, "servers": []}
    except (OSError, UnicodeDecodeError):
        return {"exists": True, "corrupt": True, "servers": []}
    servers = _servers_of(text)
    if servers is None:
        return {"exists": True, "corrupt": True, "servers": []}
    return {"exists": True, "corrupt": False, "servers": sorted(servers)}


def _split_project_path(path: str) -> tuple[str, str]:
    """``(folder, file)`` for a project MCP file path, or ``("", "")``."""
    for file in PROJECT_MCP_FILES:
        suffix = os.sep + file
        if path.endswith(suffix):
            return path[: -len(suffix)], file
    return "", ""


def _parsed(text: str) -> dict[str, Any] | None:
    """The file as an object with an object ``mcpServers``, or None.

    An empty file is an empty object. One leading UTF-8 byte-order mark is
    accepted, as the session's own settings reader accepts it; comments are not,
    because the rewrite would drop them.
    """
    from kiro_crew.user_json import strip_utf8_bom

    body = strip_utf8_bom(text)
    try:
        data = json.loads(body) if body.strip() else {}
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
        return None
    return data


def _servers_of(text: str) -> dict[str, Any] | None:
    data = _parsed(text)
    return None if data is None else data.get("mcpServers", {})


def write_project_server(folder: str, file: str, name: str, spec: dict[str, Any]) -> str:
    """Add *name* to ``<folder>/<file>`` and return the outcome. Blocking.

    The file keeps every other key and server as written. ``.kiro/settings`` is
    created when missing, the way kiro-cli's own ``/mcp add --scope workspace``
    creates it. A link anywhere below *folder* (``.kiro``, ``settings`` or the file
    itself) is refused as :data:`NOT_EDITABLE`: the card named a file in the chat's
    folder, and a link an agent's shell planted could otherwise turn this write
    into one the person never saw.
    """
    with _WRITE_LOCK:
        return _write_locked(folder, file, name, spec)


def _write_locked(folder: str, file: str, name: str, spec: dict[str, Any]) -> str:
    """The read-check-replace, every folder on the way opened without following a link.

    ``.kiro`` and ``settings`` are reached (and created) through
    :func:`kiro_crew.pinned_fs.open_pinned_descendant_dir` below the folder's
    resolved path. On POSIX each is opened by descriptor with ``O_NOFOLLOW`` and the
    file is read and replaced through the last one, so a link an agent's shell
    plants after :func:`_crosses_a_link` looked is refused instead of followed.
    Where a folder cannot be opened by descriptor (Windows) the walk refuses a
    planted link or junction by ``lstat`` and the file is addressed by name.
    """
    from kiro_crew.atomic_write import pinned_parent_replace_supported
    from kiro_crew.pinned_fs import PinnedPathRefusal, open_pinned_descendant_dir

    if not os.path.isdir(folder):
        return FOLDER_NOT_FOUND
    if _crosses_a_link(folder, file):
        return NOT_EDITABLE
    if file == KIRO_PROJECT_MCP_FILE:
        from kiro_crew.mcp_utils import kiro_oauth_wire_entry

        # kiro-cli reads ``oauthScopes`` and ``oauth.clientId``; Crew's own spelling
        # of those hints would be ignored there.
        spec = kiro_oauth_wire_entry(spec, store_entry=None, server=name)
    *dirs, leaf = file.split(os.sep)
    path = os.path.join(folder, file)
    try:
        with open_pinned_descendant_dir(
            os.path.realpath(folder), dirs, what="project MCP file", create=True
        ) as parent:
            dir_fd = parent if parent is not None and pinned_parent_replace_supported() else None
            # Pinned: the descriptor the file was checked and read through is the
            # one its mode bits and ACLs are copied from, never a second open.
            src_fd = None
            try:
                try:
                    if dir_fd is not None:
                        src_fd = _open_regular(leaf, dir_fd=dir_fd)
                        text = _read_open(src_fd, leaf)
                    else:
                        text = _read_regular_text(path)
                    existed = True
                except FileNotFoundError:
                    text, existed = "", False
                except (OSError, UnicodeDecodeError):
                    return NOT_EDITABLE
                content = _added(text, name, spec)
                if content in (NOT_EDITABLE, ALREADY_EXISTS):
                    return content
                _replace(path, content, existed=existed, dir_fd=dir_fd, src_fd=src_fd)
            finally:
                if src_fd is not None:
                    os.close(src_fd)
    except PinnedPathRefusal:
        return NOT_EDITABLE
    return WRITTEN


def _added(text: str, name: str, spec: dict[str, Any]) -> str:
    """The file's new text with *name* added, or the outcome that refuses the add."""
    data = _parsed(text)
    if data is None:
        return NOT_EDITABLE
    if name in data.get("mcpServers", {}):
        return ALREADY_EXISTS
    data.setdefault("mcpServers", {})[name] = spec
    return json.dumps(data, indent=2) + "\n"


def _replace(
    path: str, content: str, *, existed: bool, dir_fd: int | None, src_fd: int | None
) -> None:
    """Publish *content* at *path*, keeping an existing file's mode bits and ACLs.

    An entry's env can carry a credential, so a replacement never takes the
    umask's wider default over the bits the person set on the file. With *dir_fd*
    (the pinned folder that holds the file) the new file is staged and renamed
    through it, and *src_fd* is the caller's open descriptor of the old one. By
    name (no *dir_fd*), the old file's access-control source is opened here.
    """
    from kiro_crew.atomic_write import atomic_write, open_access_control_source

    owned = src_fd is None and existed and dir_fd is None
    if owned:
        src_fd = open_access_control_source(path)
    try:
        mode = None
        if existed:
            info = os.fstat(src_fd) if src_fd is not None else os.stat(path)
            mode = stat.S_IMODE(info.st_mode)
        atomic_write(
            path,
            content,
            fsync=True,
            mode=mode,
            preserve_access_control_from=src_fd,
            parent_dir_fd=dir_fd,
            preserve_windows_dacl=existed and dir_fd is None,
        )
    finally:
        if owned and src_fd is not None:
            os.close(src_fd)


def _crosses_a_link(folder: str, file: str) -> bool:
    """Whether any existing component of *file* below *folder* is a link or junction.

    Checked by name first on every platform; on POSIX :func:`_write_locked` then
    refuses a link planted after this check.
    """
    from kiro_crew.platform_compat import is_link_or_junction

    current = folder
    for part in file.split(os.sep):
        current = os.path.join(current, part)
        try:
            os.lstat(current)
        except FileNotFoundError:
            return False
        except OSError:
            return True
        if is_link_or_junction(current):
            return True
    return False


def _deny(status: int, code: str, message: str) -> web.Response:
    return web.json_response({"error": message, "code": code}, status=status)


async def api_mcp_project_file(request: web.Request) -> web.Response:
    """POST /api/mcp/project-file -- the project add a card's Apply sends."""
    # Body-scope imports, like the sibling MCP routes: these modules reach back into
    # handler modules, so a module-level import would close a cycle.
    from kiro_crew.dashboard.handlers._shared import (
        read_bounded_json,
        require_owner_dashboard_request,
    )
    from kiro_crew.dashboard.handlers.change_cards import is_admitted_card_step
    from kiro_crew.dashboard.handlers.mcp import _is_valid_mcp_name
    from kiro_crew.dashboard.handlers.mcp_custom import _clean_spec, _validate_spec

    denied = await require_owner_dashboard_request(request, "mcp_project_file")
    if denied is not None:
        return denied
    # Only the Apply of a card: the card's folder came from the asking chat's live
    # session, and the card hook refused any body that differs from it, so no
    # caller picks the folder.
    if not is_admitted_card_step(request):
        sel().log_api_access(
            caller=str(request.get("user") or "unknown"),
            operation="mcp_project_file",
            outcome="denied",
            source="dashboard",
            resources="not_a_card_step",
        )
        return _deny(
            403, "card_required", "add a server to a project file from its card in the chat"
        )
    # The shared 64 KB ceiling: one server entry is far smaller.
    body, error = await read_bounded_json(request)
    if error is not None:
        return error
    assert body is not None
    folder, file, name, spec = (body.get(k) for k in ("folder", "file", "name", "spec"))
    if not isinstance(folder, str) or not os.path.isabs(folder) or "\x00" in folder:
        return _deny(400, "invalid_folder", "'folder' must be an absolute path")
    if file not in PROJECT_MCP_FILES:
        return _deny(400, "invalid_file", "'file' must be .kiro/settings/mcp.json or .mcp.json")
    if not isinstance(name, str) or not _is_valid_mcp_name(name):
        return _deny(400, "invalid_name", "invalid MCP server name")
    if not isinstance(spec, dict):
        return _deny(400, "invalid_spec", "'spec' must be an object")
    err = _validate_spec(spec)
    if err:
        return _deny(400, "invalid_spec", f"server '{name}': {err}")
    folder = os.path.normpath(folder)
    try:
        outcome = await asyncio.to_thread(
            write_project_server, folder, file, name, _clean_spec(spec)
        )
    except OSError as exc:
        outcome = "error"
        detail = exc.strerror or type(exc).__name__
    else:
        detail = ""
    path = os.path.join(folder, file)
    sel().log_api_access(
        caller="dashboard",
        operation="mcp_project_file",
        outcome={WRITTEN: "ok", "error": "error"}.get(outcome, "denied"),
        source="dashboard",
        resources=f"{name} -> {path}",
    )
    if outcome == WRITTEN:
        return web.json_response({"written": path})
    if outcome == ALREADY_EXISTS:
        return _deny(409, ALREADY_EXISTS, f"'{name}' is already in {path}")
    if outcome == NOT_EDITABLE:
        return _deny(409, NOT_EDITABLE, f"{path} is not a JSON object with mcpServers")
    if outcome == FOLDER_NOT_FOUND:
        return _deny(404, FOLDER_NOT_FOUND, f"{folder} is not a folder")
    return _deny(500, "write_failed", f"could not write {path}: {detail}")
