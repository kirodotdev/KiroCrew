"""Reclaim staging files and lock sentinels left behind in the transcript directory.

Several transcript writers stage new content as a sibling of its target and then
rename it into place. Two of the staging names land directly in the sessions
directory: ``tmp<8>.tmp`` from a ``tempfile.mkstemp(dir=..., suffix=".tmp")``
with no prefix (the metadata-line edit in ``history_projection`` and the by-name
``atomic_write`` path), and ``.kc-incoming-<8>`` from the session-store moves
(``session_storage_parts.file_moves``). Each writer unlinks its temp on an
exception, but a process killed between the stage and the rename runs no handler,
so the temp, often a full copy of a transcript, stays on disk with nothing that
ever reclaims it.

:func:`sweep_session_staging_temps` removes such files, under three gates:

* the NAME is exactly one of the two staging shapes above, so a transcript, an
  archive or any other file is never a candidate;
* it is a regular file, judged by ``lstat`` through a pinned directory, so a link
  is never followed and nothing outside the directory is reached;
* it has not been modified for :data:`STAGING_TEMP_MIN_AGE_SECS`. A staging file
  lives for the milliseconds between its create and its rename, so one this old
  belongs to no writer in this or any other process.

The same pass prunes ``<stem>.jsonl.lock`` sentinels. ``history_locks`` creates one
per physical transcript stem as the cross-process ``flock`` sidecar and never
removes it, so a sentinel outlives its session (a departed sub-agent, a deleted or
archived chat). A sentinel is removed only when every one of these holds:

* the name is ``<stem>.jsonl.lock`` and no ``<stem>.jsonl`` transcript sits beside
  it, checked again while the lock is held;
* it is a regular file by ``lstat`` through the pinned directory, and the
  descriptor opened on it (``O_NOFOLLOW``) is the same inode;
* it has not been modified for :data:`STAGING_TEMP_MIN_AGE_SECS`;
* this sweep takes its exclusive ``flock`` without waiting, so no process holds it,
  and on POSIX the name is unlinked WHILE that ``flock`` is held. A writer that
  opened the old inode first re-checks the name after its own acquire and re-opens
  (``history_locks._try_acquire_named_sidecar``), so two holders never coexist. On
  Windows the descriptor is closed before the unlink, and an unlink of a name any
  process has open fails there, so a holder is never raced.

Only direct children of the sessions directory are examined.
"""

from __future__ import annotations

import logging
import os
import re
import stat
import time
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.config.paths import data_home
from kiro_crew.history import SESSIONS_DIR_NAME

logger = logging.getLogger(__name__)

#: How long a staging file or lock sentinel must sit unmodified before it is
#: treated as orphaned. Far above any real stage-to-rename window, which is a single
#: write and fsync.
STAGING_TEMP_MIN_AGE_SECS = 60 * 60

#: ``tempfile.mkstemp`` with its default ``tmp`` prefix and a ``.tmp`` suffix: eight
#: characters from its name alphabet in between.
_DEFAULT_PREFIX_TEMP_RE = re.compile(r"tmp[a-z0-9_]{8}\.tmp")

#: ``tempfile.mkstemp(prefix=INCOMING_PREFIX)``: the session-store move staging name.
_INCOMING_TEMP_RE = re.compile(r"\.kc-incoming-[a-z0-9_]{8}")

#: The transcript ``flock`` sidecar suffix ``history_locks._lock_path`` appends.
_LOCK_SUFFIX = ".lock"
_TRANSCRIPT_SUFFIX = ".jsonl"


def is_staging_temp_name(name: str) -> bool:
    """Whether *name* is one of the two staging shapes this sweep may remove."""
    return bool(_DEFAULT_PREFIX_TEMP_RE.fullmatch(name) or _INCOMING_TEMP_RE.fullmatch(name))


def transcript_of_lock_name(name: str) -> str | None:
    """The transcript name a ``<stem>.jsonl.lock`` sentinel guards, else None."""
    if not name.endswith(_TRANSCRIPT_SUFFIX + _LOCK_SUFFIX):
        return None
    transcript = name[: -len(_LOCK_SUFFIX)]
    if transcript == _TRANSCRIPT_SUFFIX:
        return None
    return transcript


def sweep_session_staging_temps(
    sessions_dir: Path | None = None, *, now: float | None = None
) -> int:
    """Remove orphaned staging files and stale lock sentinels under the sessions dir.

    Returns how many were removed. Fail-open: a directory that is missing or not a
    real directory, and an entry that cannot be read or removed, are left alone.
    """
    directory = sessions_dir if sessions_dir is not None else data_home() / SESSIONS_DIR_NAME
    cutoff = (time.time() if now is None else now) - STAGING_TEMP_MIN_AGE_SECS
    try:
        pinned = platform_compat.PinnedDirectory(
            platform_compat.pin_directory(directory), str(directory)
        )
    except OSError:
        return 0
    removed = 0
    with pinned:
        try:
            names = pinned.names()
        except OSError:
            return 0
        present = set(names)
        for name in names:
            transcript = transcript_of_lock_name(name)
            if transcript is not None:
                if transcript in present:
                    continue
                try:
                    if _prune_stale_lock(pinned, name, transcript, cutoff):
                        removed += 1
                except OSError:
                    logger.debug("could not prune lock sentinel %s", name, exc_info=True)
                continue
            if not is_staging_temp_name(name):
                continue
            try:
                if pinned.is_link(name):
                    continue
                info = pinned._lstat(name)
                if info is None or not stat.S_ISREG(info.st_mode):
                    continue
                if info.st_mtime >= cutoff:
                    continue
                pinned.unlink(name)
                removed += 1
            except OSError:
                logger.debug("could not sweep staging file %s", name, exc_info=True)
    return removed


def _open_no_follow(pinned: platform_compat.PinnedDirectory, name: str) -> int:
    """Open *name* in *pinned* read-write without creating it or following a link."""
    if platform_compat.IS_POSIX:
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        return os.open(name, flags, dir_fd=pinned._fd)
    return os.open(os.path.join(pinned.path, name), os.O_RDWR | getattr(os, "O_BINARY", 0))


def _prune_stale_lock(
    pinned: platform_compat.PinnedDirectory, name: str, transcript: str, cutoff: float
) -> bool:
    """Remove the lock sentinel *name* when it is old, unheld and its transcript is gone."""
    if pinned.is_link(name):
        return False
    info = pinned._lstat(name)
    if info is None or not stat.S_ISREG(info.st_mode) or info.st_mtime >= cutoff:
        return False
    fd = _open_no_follow(pinned, name)
    closed = False
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            info.st_dev,
            info.st_ino,
        ):
            return False
        if not platform_compat.try_acquire_lock(fd, exclusive=True):
            return False  # held: a live writer owns it
        try:
            # Re-checked under the lock: a writer that created the transcript and
            # released between the listing and this acquire has made it live again.
            if pinned._lstat(transcript) is not None:
                return False
            if platform_compat.IS_POSIX:
                now_named = pinned._lstat(name)
                if now_named is None or (now_named.st_dev, now_named.st_ino) != (
                    opened.st_dev,
                    opened.st_ino,
                ):
                    return False
                pinned.unlink(name)
                return True
        finally:
            platform_compat.release_lock(fd)
        os.close(fd)
        closed = True
        pinned.unlink(name)
        return True
    finally:
        if not closed:
            os.close(fd)
