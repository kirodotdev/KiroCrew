"""Descriptor identity: whether the descriptor a reader holds is still the regular
file its validated name admitted, including the macOS case alias, the hardlink
sibling witness and kernel-pathname containment.

Composed onto ``kiro_crew.hooks``; see :mod:`kiro_crew.hook_runtime`.
"""

from __future__ import annotations

import os
import stat as _stat
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.hooks import (
        _fd_real_path,
        is_sensitive_path,
        pinned_fs,
    )


def _validated_name_holds(fd: int, path: str) -> bool:
    """Whether the validated name *path* holds the inode *fd* was read from.

    Walks *path* through a pinned parent without following a link at any
    component, then compares against the descriptor we READ: a witness for the
    cases where the kernel's own name for the descriptor legitimately differs
    from the validated spelling.
    """
    if not pinned_fs.supports_pinned_walk():
        return False
    try:
        witness = pinned_fs.open_in_pinned_parent(
            os.path.dirname(path),
            os.path.basename(path),
            flags=os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
            mode=0o600,
            what="validated file",
            refusal=OSError,
        )
        try:
            return os.path.samestat(os.fstat(fd), os.fstat(witness))
        finally:
            os.close(witness)
    except OSError:
        return False


def _darwin_case_alias_matches(fd: int, path: str, opened_path: str) -> bool:
    """Prove a case-only spelling difference without following a swapped link.

    Case folding selects candidates, never authorizes them: case-sensitive
    volumes can hold distinct inodes at those names. Walk the validated name
    without resolving it again, then compare against the descriptor we READ.
    """
    if sys.platform != "darwin" or path.casefold() != opened_path.casefold():
        return False
    return _validated_name_holds(fd, path)


def _hardlink_alias_matches(fd: int, path: str, opened_path: str) -> bool:
    """Prove the kernel named a SIBLING link of the inode, not a swapped file.

    An inode with ``st_nlink > 1`` has several names, and the kernel's answer
    for a descriptor is whichever one it picks: macOS ``F_GETPATH`` (and
    Windows ``GetFinalPathNameByHandleW``) can return the other link even when
    the file was opened by *path* -- about one read in a hundred on an idle
    host, more under load. A name difference alone is therefore not a swap for
    a hardlinked inode; the witness walk of *path* decides. A single-link inode
    keeps the strict name comparison.
    """
    if os.path.normcase(os.path.normpath(opened_path)) == os.path.normcase(path):
        return False
    try:
        if os.fstat(fd).st_nlink <= 1:
            return False
    except OSError:
        return False
    return _validated_name_holds(fd, path)


def _opened_file_matches_validated_path(fd: int, path: str) -> bool:
    """Check the opened regular file against its validated, symlink-free name."""
    if not _stat.S_ISREG(os.fstat(fd).st_mode):
        return False
    opened_path = _fd_real_path(fd)
    if opened_path is None:
        return False
    matches = os.path.normcase(os.path.normpath(opened_path)) == os.path.normcase(path)
    if not matches:
        matches = _darwin_case_alias_matches(
            fd, path, os.path.normpath(opened_path)
        ) or _hardlink_alias_matches(fd, path, opened_path)
    return matches and not is_sensitive_path(opened_path)


def _opened_path_within_root(
    opened_path: str, within_root: str, *, root_is_canonical: bool = False
) -> bool:
    """Compare kernel spellings on macOS without case-folding containment."""
    root_real = within_root if root_is_canonical else os.path.realpath(within_root)
    try:
        if os.path.commonpath([opened_path, root_real]) == root_real:
            return True
    except ValueError:
        return False
    if sys.platform != "darwin" or not pinned_fs.supports_pinned_walk():
        return False
    # realpath can preserve an APFS alias. Pin that resolved root without
    # following links, then compare kernel paths, never a folded prefix.
    root_fd = pinned_fs.pin_parent(root_real, what="read root", refusal=OSError)
    try:
        root_witness = _fd_real_path(root_fd)
        return (
            root_witness is not None
            and os.path.commonpath([opened_path, root_witness]) == root_witness
        )
    finally:
        os.close(root_fd)
