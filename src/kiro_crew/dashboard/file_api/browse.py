"""Listings behind the directory pickers ``/api/browse-dirs`` and ``/api/browse-files``,
and the project picker's folder create, ``POST /api/browse-dirs/create``."""

from __future__ import annotations

import errno
import ntpath
import os
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.files import (
        _WIN_DRIVE_ROOT_RE,
        _PathProbeBusy,
        _probe_busy_response,
        _run_path_probe,
        _sel,
        is_sensitive_canonical_path,
        logger,
        pinned_fs,
        platform_compat,
        read_bounded_json,
        require_owner_dashboard_request,
    )


def _browse_entry_is_dir(entry) -> bool:
    # DirEntry.is_dir(follow_symlinks=True) stats the target, so one
    # unreadable child (a TCC-protected dir, a permission-denied entry)
    # raises instead of answering. Treat that as a non-dir so one bad
    # sibling never aborts the sort or the listing loop.
    try:
        return bool(entry.is_dir(follow_symlinks=True))
    except OSError:
        return False


def _browse_dirs_sync(base: str, skip: set[str]) -> list[dict]:
    """Walk *base* one level deep and return its visible subdirectories.

    Blocking, and unboundedly so: *base* is caller-chosen and defaults to ``$HOME``,
    so the scan is as large as that directory, and every surviving entry additionally
    pays a sensitive-path check against its ``realpath``. Run via
    ``asyncio.to_thread`` so one large directory cannot hold the sole event loop for
    the duration of the listing.
    """
    dirs: list[dict] = []
    try:
        for entry in sorted(os.scandir(base), key=lambda e: e.name.lower()):
            if not _browse_entry_is_dir(entry):
                continue
            if entry.name in skip:
                continue
            # Resolve symlinks before the sensitivity check — a symlink in
            # a benign dir pointing at ~/.aws would otherwise pass through.
            # The canonical gate matches inline on this worker thread, so a
            # large listing submits nothing to the ``mc-pathres`` pool.
            if is_sensitive_canonical_path(os.path.realpath(entry.path)):
                continue
            dirs.append({"name": entry.name, "path": entry.path})
    except PermissionError:
        pass
    return dirs


def _browse_files_sync(
    base: str, skip: set[str], show_hidden: bool = False
) -> tuple[list[dict], list[dict]]:
    """Walk *base* one level deep and return its ``(dirs, files)`` entries.

    The sibling of :func:`_browse_dirs_sync` and blocking for the same reasons, plus a
    ``stat`` per entry for the mtime the browser sorts on. Offloaded the same way.

    *show_hidden* (the Files panel's "Show hidden files" toggle) lists dot-files too.
    It changes nothing else: the skip set and the sensitive-path check still apply to
    every entry, so a credential file stays out of the listing either way.
    """
    dirs: list[dict] = []
    files: list[dict] = []
    try:
        # Sort: dirs before files, then alphabetical. The key must not raise
        # on an unreadable child: DirEntry.is_dir stats the target, so one
        # bad sibling would abort sorted() and empty the whole listing.
        for entry in sorted(
            os.scandir(base), key=lambda e: (not _browse_entry_is_dir(e), e.name.lower())
        ):
            # An entry that cannot even be classified is skipped, not fatal:
            # without this, one unreadable child aborts the loop and drops
            # every entry after it.
            try:
                is_dir = entry.is_dir(follow_symlinks=True)
                is_file = False if is_dir else entry.is_file(follow_symlinks=True)
            except OSError:
                continue
            # Dot-directories are listed (e.g. ``.worktrees``); dot-files stay hidden
            # unless the caller asked for hidden files.
            if entry.name.startswith(".") and not is_dir and not show_hidden:
                continue
            # Resolve symlinks before the sensitivity check — a symlink in a
            # benign dir pointing at ~/.aws would otherwise pass through. Same
            # inline canonical gate as the directory listing above.
            if is_sensitive_canonical_path(os.path.realpath(entry.path)):
                continue
            # Capture mtime so the activity-panel browser can offer a
            # sort-by-date option; fall back to 0 on a race (entry removed
            # mid-scan) so one unstattable entry never breaks the listing.
            try:
                mtime = int(entry.stat(follow_symlinks=True).st_mtime)
            except OSError:
                mtime = 0
            if is_dir:
                if entry.name not in skip:
                    dirs.append({"name": entry.name, "path": entry.path, "mtime": mtime})
            elif is_file:
                files.append({"name": entry.name, "path": entry.path, "mtime": mtime})
    except PermissionError:
        pass
    return dirs, files


def _is_windows_drive_root(path: str) -> bool:
    return platform_compat.IS_WINDOWS and _WIN_DRIVE_ROOT_RE.fullmatch(path) is not None


def _browse_drives_sync() -> list[dict[str, str]]:
    """Enumerate the mounted Windows drive roots, as browse-dirs rows.

    Blocking -- callers run it on the transfer pool, like the other listings.
    ``os.listdrives`` exists on every supported interpreter (``requires-python
    >= 3.12``); it is reached through ``getattr`` only because typeshed declares
    it under ``sys.platform == "win32"``, so a direct attribute fails mypy on
    the Linux CI runner. The caller has already refused non-Windows hosts.
    """
    roots = list(getattr(os, "listdrives")())
    return [{"name": r, "path": r} for r in roots]


def _browse_parent(base: str) -> str:
    """The Back target for *base*: its dirname, or ``""`` for a Windows drive root.

    Shared by ``/api/browse-dirs`` and ``/api/browse-files`` so both listings
    describe a drive root the same way. ``""`` is the caller's cue that the level
    above is the virtual drive list (``?drives=1``), not a directory; a consumer
    without a drive list (the folder panel) reads it as "top", exactly as it
    read the old ``C:\\`` == ``C:\\`` answer. A POSIX ``/`` keeps ``dirname``'s
    answer of ``/`` -- equal to itself, which every consumer already reads as
    "top".
    """
    if _is_windows_drive_root(base):
        return ""
    return os.path.dirname(base)


def _create_browse_dir_sync(parent: str, name: str) -> tuple[str, str]:
    """Make folder *name* inside directory *parent*: ``(outcome, path)``.

    Blocking (``realpath`` then ``mkdir``); the route runs it on the probe pool.
    The target is spelled the way the OS will create it (``abspath``: on Windows
    ``C:x`` leaves *parent* and a trailing dot is dropped) and must sit directly
    in *parent*. It then meets the sensitive-path floor before anything is
    created, so the picker cannot make a protected folder (``~/.aws`` in a home
    without one); a path inside a protected one is protected too, so this also
    refuses a protected *parent*. Outcomes: ``created``, ``sensitive``,
    ``exists``, ``not_found`` (the parent is gone or is not a directory),
    ``parent_changed``, ``permission_denied``, ``invalid_name`` and ``failed``.

    The checked *parent* is pinned before the create, the way uploads pin theirs,
    so an ancestor swapped for a link after the check cannot redirect the folder
    into a protected place: on POSIX the leaf is made relative to a descriptor
    walked with ``O_NOFOLLOW`` (``pinned_fs.pin_parent``); on Windows a handle
    without delete sharing locks the chain against renames while ``mkdir`` runs,
    once its final path is confirmed to be *parent*. A swap is ``parent_changed``.
    """
    base = os.path.realpath(os.path.expanduser(parent))
    target = os.path.abspath(os.path.join(base, name))
    if os.path.normcase(os.path.dirname(target)) != os.path.normcase(base):
        return "invalid_name", target
    if is_sensitive_canonical_path(target):
        return "sensitive", target
    guard_fd: int | None = None
    try:
        if pinned_fs.supports_pinned_walk() and os.mkdir in os.supports_dir_fd:
            guard_fd = pinned_fs.pin_parent(base, what="new folder's parent")
            os.mkdir(os.path.basename(target), dir_fd=guard_fd)
        elif platform_compat.IS_WINDOWS:
            guard_fd = platform_compat.pin_directory(base, links_only=True)
            pinned = pinned_fs.fd_real_path(guard_fd)
            if pinned is None or ntpath.normcase(ntpath.normpath(pinned)) != ntpath.normcase(
                ntpath.normpath(base)
            ):
                return "parent_changed", target
            os.mkdir(target)
        else:
            # Neither pin exists here (unreachable on Linux, macOS and Windows):
            # a by-name mkdir would be the unguarded swap window, so refuse.
            return "failed", target
    except (pinned_fs.PinnedPathRefusal, NotADirectoryError):
        # The POSIX walk and the Windows pin refuse a link and a non-directory alike;
        # a parent that is no directory at all is the plain "gone" case, anything
        # else was swapped.
        return ("parent_changed" if os.path.isdir(base) else "not_found"), target
    except FileExistsError:
        return "exists", target
    except FileNotFoundError:
        # With the parent still there it was the name: Windows reports an overlong
        # one as a missing path.
        return ("invalid_name" if os.path.isdir(base) else "not_found"), target
    except PermissionError:
        return "permission_denied", target
    except ValueError:  # a NUL or an unencodable character in the name
        return "invalid_name", target
    except OSError as exc:
        if exc.errno == errno.EROFS:
            return "permission_denied", target
        if exc.errno in (errno.ENAMETOOLONG, errno.EINVAL):
            return "invalid_name", target
        if exc.errno == errno.ELOOP:
            return "parent_changed", target
        logger.warning("browse_create_dir failed for %s: errno %s", target, exc.errno)
        return "failed", target
    finally:
        if guard_fd is not None:
            os.close(guard_fd)
    return "created", target


async def api_browse_create_dir(request: web.Request) -> web.Response:
    """POST /api/browse-dirs/create -- make one folder in the directory the picker shows.

    Body ``{"parent": <directory>, "name": <folder name>}``; answers 201
    ``{"path"}`` with the new folder's path so the picker can list it. Owner-only,
    checked before the body is read, like ``/api/file-write``: it writes to the
    gateway host's filesystem. *name* is one path component, so the route makes
    exactly one folder level. Every refusal carries a ``code`` for the picker.
    """
    owner_denied = await require_owner_dashboard_request(request, "browse_create_dir")
    if owner_denied is not None:
        return owner_denied
    caller = request.get("user", "dashboard")
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    parent, name = body.get("parent"), body.get("name")
    if not isinstance(parent, str) or not parent.strip():
        return web.json_response(
            {"error": "A parent directory is required", "code": "parent_required"}, status=400
        )
    if not isinstance(name, str) or name in ("", ".", "..") or "/" in name or "\\" in name:
        return web.json_response(
            {"error": "Not a valid folder name", "code": "invalid_name"}, status=400
        )
    try:
        outcome, target = await _run_path_probe(_create_browse_dir_sync, parent.strip(), name)
    except _PathProbeBusy:
        return _probe_busy_response(resource=parent, operation="browse_create_dir", caller=caller)
    if outcome == "created":
        _sel().log_api_access(
            caller=caller, operation="browse_create_dir", outcome="allowed", resources=target
        )
        return web.json_response({"path": target}, status=201)
    status, code, message = {
        "sensitive": (403, "access_denied", "Access denied"),
        "exists": (409, "already_exists", "A file or folder with that name already exists"),
        "not_found": (404, "not_found", "The parent directory no longer exists"),
        "parent_changed": (
            409,
            "parent_changed",
            "The parent directory changed while the folder was being created",
        ),
        "permission_denied": (403, "permission_denied", "No permission to create a folder here"),
        "invalid_name": (400, "invalid_name", "Not a valid folder name"),
        "failed": (500, "create_failed", "Could not create the folder"),
    }[outcome]
    _sel().log_api_access(
        caller=caller,
        operation="browse_create_dir",
        outcome="denied" if status < 500 else "failure",
        resources=target,
        error=code,
    )
    return web.json_response({"error": message, "code": code}, status=status)
