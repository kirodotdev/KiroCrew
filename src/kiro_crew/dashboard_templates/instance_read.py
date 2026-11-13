"""The opens a member dashboard reader makes without the page lock, never following a link.

A sibling of :mod:`kiro_crew.dashboard_templates.instance`, which owns the store and
calls these from its readers; kept out of the store module for its size alone. It
imports nothing from the store, so the store's own names (the refusal type, the lock
file name) arrive as arguments.
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

from kiro_crew import pinned_fs
from kiro_crew.atomic_write import refuse_linked_parent

__all__ = ["LinkedLevel", "open_page_dir", "refuse_linked_for_read", "without_links"]


class LinkedLevel(FileNotFoundError):
    """A level of a member's page folder is a link. A ``FileNotFoundError``, so a reader
    answers "not set" exactly as it does for a page nobody adopted."""


def _is_link(name: str, dir_fd: int) -> bool:
    try:
        return stat.S_ISLNK(os.lstat(name, dir_fd=dir_fd).st_mode)
    except OSError:
        return False


def open_page_dir(home: Path, levels: tuple[str, ...], refusal: type[Exception]) -> int:
    """Open an EXISTING member page folder one ``O_NOFOLLOW`` level at a time.

    *levels* are the folder names below *home*. Each level is opened relative to the
    one before it, so a level swapped for a link afterwards cannot move where the next
    one lands, and a reader never makes a level. An absent level raises
    :class:`FileNotFoundError`; a level that is a link raises :class:`LinkedLevel`. A
    level that is anything else the open refuses (a file where a folder belongs) keeps
    its own error, so the store is reported unreadable rather than unadopted. The
    caller closes the descriptor it gets.
    """
    first, *rest = levels
    try:
        fd = pinned_fs.open_dir_pinned(home / first, what="dashboard pages", refusal=refusal)
    except refusal:
        if stat.S_ISLNK(os.lstat(home / first).st_mode):
            raise LinkedLevel("no dashboard page here") from None
        raise
    for name in rest:
        try:
            child = os.open(name, pinned_fs.dir_flags(), dir_fd=fd)
        except OSError as exc:
            linked = exc.errno in (errno.ELOOP, errno.ENOTDIR) and _is_link(name, fd)
            os.close(fd)
            if linked:
                raise LinkedLevel("no dashboard page here") from None
            raise
        os.close(fd)
        fd = child
    return fd


def refuse_linked_for_read(directory: Path, lock_file: str, leaf: str | None = None) -> None:
    """The ``lstat`` walk for a reader where the platform cannot pin.

    Covers *directory*'s levels and, when *leaf* names a file in it, the file itself.
    """
    try:
        refuse_linked_parent(directory / lock_file)
    except OSError:
        raise LinkedLevel("no dashboard page here") from None
    if leaf is not None and (directory / leaf).is_symlink():
        raise LinkedLevel("no dashboard page here")


def without_links(directory: Path | int, names: list[str]) -> list[str]:
    """*names* minus the entries of *directory* (a path, or a held descriptor) that are links."""
    kept: list[str] = []
    for name in names:
        try:
            if isinstance(directory, int):
                mode = os.lstat(name, dir_fd=directory).st_mode
            else:
                mode = os.lstat(directory / name).st_mode
        except FileNotFoundError:
            continue
        if not stat.S_ISLNK(mode):
            kept.append(name)
    return kept
