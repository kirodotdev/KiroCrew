"""Listings behind the directory pickers ``/api/browse-dirs`` and ``/api/browse-files``."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.files import (
        _WIN_DRIVE_ROOT_RE,
        is_sensitive_path,
        platform_compat,
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
    pays an ``is_sensitive_path`` call that resolves several paths of its own. Run via
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
            if is_sensitive_path(os.path.realpath(entry.path)):
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
            # benign dir pointing at ~/.aws would otherwise pass through.
            if is_sensitive_path(os.path.realpath(entry.path)):
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
