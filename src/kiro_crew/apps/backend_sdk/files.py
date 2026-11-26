"""File helpers with the gateway's own refusals: the path gate, no-follow reads, atomic writes."""

from __future__ import annotations

import os
from pathlib import Path

from kiro_crew import atomic_write as _atomic_write
from kiro_crew import hooks, owner_only_files

#: Raised by :func:`read_bytes_nofollow` when the file is over its byte cap. The
#: gateway's own class, so ``except FileTooLargeError`` catches what it raises.
FileTooLargeError = hooks.FileTooLargeError


def canonical_file_path(path: str) -> str | None:
    """The canonical path of *path*, or ``None`` when the gateway's file path gate refuses it.

    ``kiro_crew.hooks.validate_file_path``, the gate the gateway's own file surfaces
    ask: ``None`` for an empty or unrepresentable path, a Windows UNC path outside
    the trusted roots (refused BEFORE any resolution, because resolving one is
    itself the outbound SMB probe), a Windows link the link screen refuses, and a
    path whose resolved target is on the sensitive-path list. Otherwise the
    ``realpath``, every link followed. Seam level 14 (``_EXPORT_LEVELS``).

    The answer judges a NAME, and what the name points at can change before it is
    opened. Read the bytes through :func:`read_bytes_nofollow` or a
    descriptor-pinned open, never a plain ``open`` of the result. Blocking (it
    resolves the path and asks the sensitive-path list): call it off the event loop.
    """
    # One positional argument: the gate's own default, so no other check is chosen.
    return hooks.validate_file_path(path)


def read_bytes_nofollow(
    path: str,
    *,
    within_root: str | None = None,
    max_bytes: int | None = None,
    within_root_is_canonical: bool = False,
) -> bytes | None:
    """The bytes of *path*, or ``None`` when the gateway's read gate refuses it.

    ``kiro_crew.hooks.safe_read_file_bytes_nolink``: the open refuses a link at
    the final component, then the opened descriptor is checked, so the inode that
    is validated is the inode that is read. ``None`` for a sensitive path, a
    hardlinked or non-regular file, a descriptor whose real path escapes
    *within_root* (or cannot be determined), or an unreadable file. Over
    *max_bytes* (the gateway's general cap when ``None``) it raises
    :data:`FileTooLargeError`. ``within_root_is_canonical`` keeps an
    already-resolved root literal, so replacing that directory with a link cannot
    redefine it.
    """
    return hooks.safe_read_file_bytes_nolink(
        path,
        within_root,
        max_bytes=max_bytes,
        within_root_is_canonical=within_root_is_canonical,
    )


def atomic_write(
    path: Path | str,
    content: str | bytes,
    *,
    fsync: bool = False,
    mode: int | None = None,
    newline: str | None = None,
    restrict_to_owner: bool = False,
) -> None:
    """Write *content* to *path* through a unique temp file and one rename.

    ``kiro_crew.atomic_write.atomic_write``: a reader sees the old file or the new
    one, never a torn write, and a failed write leaves no temp behind. *mode*
    sets the new file's permission bits; ``restrict_to_owner`` makes it owner-only
    on every platform and raises when that cannot be applied.
    """
    _atomic_write.atomic_write(
        path,
        content,
        fsync=fsync,
        mode=mode,
        newline=newline,
        restrict_to_owner=restrict_to_owner,
    )


def ensure_owner_only_dir(path: str | os.PathLike[str]) -> None:
    """``mkdir -p`` that is owner-only (``0700``) inside the data home.

    ``kiro_crew.owner_only_files.ensure_directory``: outside the data home it is a
    plain ``mkdir -p``, so a user's own directory never gets a mode it did not ask
    for.
    """
    owner_only_files.ensure_directory(path)
