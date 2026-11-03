"""Listings behind the directory pickers ``/api/browse-dirs`` and ``/api/browse-files``,
and the picker's folder create, ``POST /api/browse-dirs/create``."""

from __future__ import annotations

import errno
import os
import unicodedata
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.files import (
        _CREATE_DIR_NAME_MAX_BYTES,
        _WIN_DRIVE_ROOT_RE,
        _WIN_INVALID_DIR_NAME_CHARS,
        _WIN_RESERVED_DIR_NAMES,
        _PathProbeBusy,
        _probe_busy_response,
        _resolve_search_root,
        _run_path_probe,
        _sel,
        is_sensitive_canonical_path,
        is_sensitive_path,
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


def _browse_files_sync(base: str, skip: set[str]) -> tuple[list[dict], list[dict]]:
    """Walk *base* one level deep and return its ``(dirs, files)`` entries.

    The sibling of :func:`_browse_dirs_sync` and blocking for the same reasons, plus a
    ``stat`` per entry for the mtime the browser sorts on. Offloaded the same way.
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
            # Dot-directories are listed (e.g. ``.worktrees``); dot-files stay hidden.
            if entry.name.startswith(".") and not is_dir:
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


def _browse_dir_name_is_valid(name: str) -> bool:
    """Whether *name* is one plain folder name the picker's create route accepts.

    Pure string checks, no filesystem access, so it runs on the loop. One
    component only: a separator, ``.`` or ``..`` would let the name climb out
    of the folder the user is looking at, and the route creates exactly one
    directory. Surrounding whitespace is refused rather than stripped, because
    a name the server rewrote is not the name the user typed; the picker trims
    before it sends. Control characters (Unicode category ``Cc``: C0, DEL and
    C1) are refused on every platform: a folder name holding a newline or an
    escape is a display and shell hazard with no legitimate use here. Windows adds its own reserved characters,
    device names and the trailing dot or space it silently drops.
    """
    if not name or name in (".", "..") or name != name.strip():
        return False
    try:
        if len(name.encode("utf-8")) > _CREATE_DIR_NAME_MAX_BYTES:
            return False
    except UnicodeEncodeError:
        return False
    if any(ch in "/\\" or unicodedata.category(ch) == "Cc" for ch in name):
        return False
    if platform_compat.IS_WINDOWS:
        if any(ch in _WIN_INVALID_DIR_NAME_CHARS for ch in name) or name.endswith("."):
            return False
        if name.split(".", 1)[0].upper() in _WIN_RESERVED_DIR_NAMES:
            return False
    return True


def _create_browse_dir_sync(parent: str, name: str) -> tuple[str, str]:
    """Create folder *name* in the canonical directory *parent*: ``(outcome, path)``.

    Blocking; callers run it on the probe pool. *parent* is the ``realpath``
    the route already resolved and checked, so its chain is pinned as recorded
    (:func:`pinned_fs.pin_parent`, ``O_NOFOLLOW`` per component) and the
    directory is created relative to that descriptor, the way ``/api/file-write``
    reaches its target: a component swapped for a link since the check is
    refused instead of followed. Where the platform cannot pin (Windows), the
    create is by name after the same checks.

    The new path is judged against the sensitive-path floor before anything is
    created, so the picker cannot make a credential directory (``~/.ssh`` in an
    empty home, say) that the listing would then hide. Mode ``0o777`` under the
    process umask: this is an ordinary project folder, not private product state.

    Outcomes: ``created``, ``sensitive``, ``exists``, ``parent_changed`` (the
    pin refused a swapped component), ``not_found`` (the parent went away),
    ``permission_denied`` and ``failed``.
    """
    target = os.path.join(parent, name)
    if is_sensitive_canonical_path(target):
        return "sensitive", target
    try:
        if pinned_fs.supports_pinned_walk():
            parent_fd = pinned_fs.pin_parent(parent, what="new folder's parent")
            try:
                os.mkdir(name, 0o777, dir_fd=parent_fd)
            finally:
                os.close(parent_fd)
        else:
            os.mkdir(target, 0o777)
    except pinned_fs.PinnedPathRefusal:
        return "parent_changed", target
    except FileExistsError:
        return "exists", target
    except FileNotFoundError:
        return "not_found", target
    except PermissionError:
        return "permission_denied", target
    except OSError as exc:
        if exc.errno == errno.EROFS:
            return "permission_denied", target
        logger.warning("browse_create_dir failed for %s: errno %s", target, exc.errno)
        return "failed", target
    return "created", target


async def api_browse_create_dir(request: web.Request) -> web.Response:
    """POST /api/browse-dirs/create -- make one folder in the directory the picker shows.

    Body ``{"parent": <directory>, "name": <folder name>}``. Answers 201
    ``{"path", "parent"}`` with the new folder's path, so the picker can list
    it straight away. Owner-only, checked before the body is read, like
    ``/api/file-write``: this route writes to the gateway host's filesystem.
    ``parent`` resolves exactly as ``/api/browse-dirs?path=`` does and meets the
    same sensitive-path refusal; an empty one is refused rather than read as
    ``$HOME``, because a create must name where it lands. Every refusal carries
    a ``code`` the picker maps to its own copy.
    """
    owner_denied = await require_owner_dashboard_request(request, "browse_create_dir")
    if owner_denied is not None:
        return owner_denied
    caller = request.get("user", "dashboard")
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    raw_parent = body.get("parent")
    name = body.get("name")
    if not isinstance(raw_parent, str) or not raw_parent.strip():
        return web.json_response(
            {"error": "A parent directory is required", "code": "parent_required"}, status=400
        )
    # Its own code, ahead of the general check: the picker's field caps UTF-16 units,
    # so a long name in a multi-byte script passes it and lands here, and "special
    # characters" would send the user hunting for a character that is not there.
    # ``surrogatepass`` so a lone surrogate is still counted, then refused below.
    if (
        isinstance(name, str)
        and len(name.encode("utf-8", "surrogatepass")) > _CREATE_DIR_NAME_MAX_BYTES
    ):
        return web.json_response(
            {"error": "The folder name is too long", "code": "name_too_long"}, status=400
        )
    if not isinstance(name, str) or not _browse_dir_name_is_valid(name):
        return web.json_response(
            {"error": "Not a valid folder name", "code": "invalid_name"}, status=400
        )
    raw_parent = raw_parent.strip()
    try:
        parent, parent_is_dir = await _run_path_probe(_resolve_search_root, raw_parent)
    except _PathProbeBusy:
        return _probe_busy_response(
            resource=raw_parent, operation="browse_create_dir", caller=caller
        )
    if not parent_is_dir:
        return web.json_response(
            {"error": "Not a directory", "code": "not_a_directory", "path": parent}, status=400
        )
    if is_sensitive_path(parent):
        _sel().log_api_access(
            caller=caller,
            operation="browse_create_dir",
            outcome="denied",
            resources=parent,
            error="sensitive path",
        )
        return web.json_response({"error": "Access denied", "code": "access_denied"}, status=403)
    try:
        outcome, target = await _run_path_probe(_create_browse_dir_sync, parent, name)
    except _PathProbeBusy:
        return _probe_busy_response(resource=parent, operation="browse_create_dir", caller=caller)
    if outcome == "created":
        _sel().log_api_access(
            caller=caller, operation="browse_create_dir", outcome="allowed", resources=target
        )
        return web.json_response({"path": target, "parent": parent}, status=201)
    refusals = {
        "sensitive": (403, "access_denied", "Access denied"),
        "exists": (409, "already_exists", "A file or folder with that name already exists"),
        "parent_changed": (409, "parent_changed", "The parent directory changed; list it again"),
        "not_found": (404, "not_found", "The parent directory no longer exists"),
        "permission_denied": (403, "permission_denied", "No permission to create a folder here"),
        "failed": (500, "create_failed", "Could not create the folder"),
    }
    status, code, message = refusals[outcome]
    _sel().log_api_access(
        caller=caller,
        operation="browse_create_dir",
        outcome="denied" if status < 500 else "failure",
        resources=target,
        error=code,
    )
    return web.json_response({"error": message, "code": code}, status=status)
