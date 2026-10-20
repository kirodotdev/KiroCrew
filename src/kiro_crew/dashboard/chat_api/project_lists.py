"""The recent and favourite project lists."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_handlers import (
        _MAX_RECENT_PROJECTS,
        _favorite_projects_lock,
        atomic_write,
        config_dir,
        deny_non_dashboard_caller,
        is_sensitive_path,
        read_bounded_json,
        sel,
    )


def _recent_projects_path() -> Path:
    return config_dir() / "recent_projects.json"


def _save_recent_project(path: str) -> None:
    """Prepend path to recent projects list (deduped, capped)."""

    fp = _recent_projects_path()
    fp.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = json.loads(fp.read_text(encoding="utf-8")) if fp.is_file() else []
    except (json.JSONDecodeError, OSError):
        existing = []
    if not isinstance(existing, list):
        existing = []
    existing = [p for p in existing if p != path]
    existing.insert(0, path)
    existing = existing[:_MAX_RECENT_PROJECTS]
    fd, tmp = tempfile.mkstemp(dir=fp.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_fh:
            tmp_fh.write(json.dumps(existing))
        os.replace(tmp, fp)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


async def api_recent_projects(request: web.Request) -> web.Response:
    """GET /api/recent-projects — list recently used project directories."""

    def _read_recent_projects() -> list[str]:
        fp = _recent_projects_path()
        try:
            dirs = json.loads(fp.read_text(encoding="utf-8")) if fp.is_file() else []
        except Exception:
            dirs = []
        if not isinstance(dirs, list):
            dirs = []
        return [
            d for d in dirs if isinstance(d, str) and os.path.isdir(d) and not is_sensitive_path(d)
        ]

    dirs = await asyncio.to_thread(_read_recent_projects)
    sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="recent_projects",
        outcome="allowed",
        resources=f"count={len(dirs)}",
    )
    return web.json_response({"dirs": dirs})


def _favorite_projects_path() -> Path:
    return config_dir() / "favorite_projects.json"


class FavoritesUnreadable(Exception):
    """`favorite_projects.json` exists but is not a JSON list we can read."""


def _load_favorite_projects(*, strict: bool = False) -> list[str]:
    """The stored favourites, de-duplicated, with NO filesystem probing.

    This is the list the write paths edit, so it keeps entries whose directory is
    currently unreachable: a favourite on a detached volume or a not-yet-cloned
    worktree must survive a round trip through add or remove rather than being
    deleted as a side effect of someone else's write. `_read_favorite_projects`
    applies the display filter instead.

    An absent file is an empty list. A file that exists but cannot be read or is not
    a JSON list (a hand edit with a typo, a permission change) reads as empty for
    DISPLAY, but `strict=True` raises `FavoritesUnreadable` instead: the write paths
    pass it, because writing back an empty snapshot would silently replace the
    user's whole list, which unlike the recents window cannot be rebuilt.
    """

    fp = _favorite_projects_path()
    if not fp.is_file():
        return []
    try:
        stored = json.loads(fp.read_text(encoding="utf-8"))
    except Exception as exc:
        if strict:
            raise FavoritesUnreadable(str(exc)) from exc
        return []
    if not isinstance(stored, list):
        if strict:
            raise FavoritesUnreadable("not a JSON list")
        return []
    out: list[str] = []
    for entry in stored:
        if isinstance(entry, str) and entry and entry not in out:
            out.append(entry)
    return out


def _read_favorite_projects() -> list[str]:
    """Favourites for display: existing, non-sensitive directories, stored order kept.

    Insertion order is the answer, not recency or the alphabet -- the list is one the
    user curates by hand, so a favourite stays where they put it. The sensitive-path
    filter matches the recents read: a path that became sensitive after it was
    favourited is withheld from the response, and removing it stays possible because
    the delete path reads `_load_favorite_projects` instead of this.

    The file is loaded under `_favorite_projects_lock`, the lock the write transaction
    holds. On Windows a rename cannot replace a file another handle has open, so a
    read open while an add or remove renamed its new list into place failed that
    write with `PermissionError` and lost its change. The filesystem probes run after
    the lock is released: they can stall on a network mount and must not hold up a
    write.
    """

    with _favorite_projects_lock:
        stored = _load_favorite_projects()
    return [d for d in stored if os.path.isdir(d) and not is_sensitive_path(d)]


def _write_favorite_projects(paths: list[str]) -> None:
    """Replace the favourites file atomically.

    Through the shared `atomic_write`: a unique temp, the temp removed on any exit,
    and a rename that retries the Windows sharing-violation window a handle from
    outside this process (an indexer, an AV scanner) can open on either path. The
    explicit owner-only mode is the one the hand-rolled `mkstemp` write always
    published, kept wherever the file lives.
    """

    atomic_write(_favorite_projects_path(), json.dumps(paths), mode=0o600)


def _probe_favorite_candidate(path: str) -> tuple[str, bool, bool]:
    """Resolve `path` and answer (resolved, is a directory, is sensitive).

    One function so the add handler can hand every filesystem probe to a worker thread
    in a single call: `realpath` walks the symlink chain and the sensitivity check
    resolves too, and on a stalled network mount either would freeze the event loop.
    """

    resolved = os.path.realpath(os.path.expanduser(path))
    return resolved, os.path.isdir(resolved), is_sensitive_path(resolved)


def _add_favorite_project(path: str) -> list[str]:
    """Append `path` to the favourites; returns the display list.

    Appends rather than prepends so the order the user built stays put. Already
    favourited is a no-op success -- the button is a toggle whose "on" state can be
    requested twice (two tabs, a double click) and neither caller should see an error
    for a list that already says what they asked it to say.
    """

    with _favorite_projects_lock:
        stored = _load_favorite_projects(strict=True)
        if path not in stored:
            stored.append(path)
            _write_favorite_projects(stored)
    return _read_favorite_projects()


def _remove_favorite_project(path: str) -> list[str]:
    """Drop `path` from the favourites; returns the display list.

    Both the path as given and its resolved form are dropped. The add path stores the
    resolved form, but an entry can sit in the file unresolved -- a hand-edited file, or
    a spelling whose symlink chain has since moved -- and a remove that matched only one
    spelling would leave a row the user cannot get rid of. Missing is a no-op.
    """

    resolved = os.path.realpath(os.path.expanduser(path))
    with _favorite_projects_lock:
        stored = _load_favorite_projects(strict=True)
        kept = [p for p in stored if p != path and p != resolved]
        if len(kept) != len(stored):
            _write_favorite_projects(kept)
    return _read_favorite_projects()


def _favorites_unreadable_response() -> web.Response:
    """409 for a write refused because the stored list could not be read.

    Refused rather than written: the write would replace a list the user may have
    spent months curating with a single entry. The message names the file so the
    user can repair it, and nothing on disk is touched.
    """

    return web.json_response(
        {
            "error": (
                f"{_favorite_projects_path().name} could not be read, so it was left "
                "untouched. Fix or remove it, then try again."
            ),
            "code": "favorites_unreadable",
        },
        status=409,
    )


async def api_favorite_projects(request: web.Request) -> web.Response:
    """GET /api/favorite-projects — list the favourited project directories."""

    dirs = await asyncio.to_thread(_read_favorite_projects)
    sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="favorite_projects",
        outcome="allowed",
        resources=f"count={len(dirs)}",
    )
    return web.json_response({"dirs": dirs})


async def api_favorite_project_add(request: web.Request) -> web.Response:
    """POST /api/favorite-projects — favourite one project directory.

    Validated the way setting a slot's project is (`api_chat_slot_project`): a real
    directory, resolved, and not a sensitive path. Storing the resolved form is what
    lets the picker's star compare a row against this list by string.

    Owner-gated, unlike the GET beside it: the favourites file is the owner's own
    config, so an installed app whose manifest covers this path may read the list but
    must not edit it. The gate goes BEFORE the body is read and before any filesystem
    probe, so a refused caller gets no directory-existence oracle out of it.
    """

    denied = deny_non_dashboard_caller(request, "favorite_project_add")
    if denied is not None:
        return denied
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    path = body.get("path", "")
    if not isinstance(path, str):
        return web.json_response(
            {"error": "path must be a string", "code": "path_not_a_string"}, status=400
        )
    path = path.strip()
    if not path:
        return web.json_response({"error": "path is required", "code": "path_required"}, status=400)
    path, is_dir, sensitive = await asyncio.to_thread(_probe_favorite_candidate, path)
    if not is_dir:
        return web.json_response(
            {"error": "Not a directory", "code": "not_a_directory"}, status=400
        )
    if sensitive:
        sel().log_api_access(
            caller=request.get("user", "dashboard"),
            operation="favorite_project_add",
            outcome="denied",
            resources=f"path={path}",
            error="sensitive path",
        )
        return web.json_response({"error": "Access denied", "code": "denied"}, status=403)
    try:
        dirs = await asyncio.to_thread(_add_favorite_project, path)
    except FavoritesUnreadable as exc:
        sel().log_api_access(
            caller=request.get("user", "dashboard"),
            operation="favorite_project_add",
            outcome="denied",
            resources=f"path={path}",
            error=f"favorites file unreadable: {exc}",
        )
        return _favorites_unreadable_response()
    sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="favorite_project_add",
        outcome="allowed",
        resources=f"path={path} count={len(dirs)}",
    )
    return web.json_response({"dirs": dirs})


async def api_favorite_project_remove(request: web.Request) -> web.Response:
    """DELETE /api/favorite-projects?path=… — un-favourite one project directory.

    The path comes from the query string, not a body, because a DELETE body is not
    carried reliably by every client. None of the add path's filesystem checks run
    here: a favourite whose directory was deleted, or which became sensitive after it
    was stored, is exactly the row a user needs to be able to remove, and demanding it
    still be a listable directory would strand it.

    Owner-gated for the same reason as the add above.
    """

    denied = deny_non_dashboard_caller(request, "favorite_project_remove")
    if denied is not None:
        return denied
    path = (request.query.get("path") or "").strip()
    if not path:
        return web.json_response({"error": "path is required", "code": "path_required"}, status=400)
    try:
        dirs = await asyncio.to_thread(_remove_favorite_project, path)
    except FavoritesUnreadable as exc:
        sel().log_api_access(
            caller=request.get("user", "dashboard"),
            operation="favorite_project_remove",
            outcome="denied",
            resources=f"path={path}",
            error=f"favorites file unreadable: {exc}",
        )
        return _favorites_unreadable_response()
    sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="favorite_project_remove",
        outcome="allowed",
        resources=f"path={path} count={len(dirs)}",
    )
    return web.json_response({"dirs": dirs})
