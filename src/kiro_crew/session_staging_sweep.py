"""Reclaim staging files a killed writer left in the transcript directory.

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
  archive, a lock sentinel or any other file is never a candidate;
* it is a regular file, judged by ``lstat`` through a pinned directory, so a link
  is never followed and nothing outside the directory is reached;
* it has not been modified for :data:`STAGING_TEMP_MIN_AGE_SECS`. A staging file
  lives for the milliseconds between its create and its rename, so one this old
  belongs to no writer in this or any other process.

Only direct children of the sessions directory are examined. The ``*.jsonl.lock``
advisory-lock sentinels are not touched.
"""

from __future__ import annotations

import logging
import re
import stat
import time
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.config.paths import data_home
from kiro_crew.history import SESSIONS_DIR_NAME

logger = logging.getLogger(__name__)

#: How long a staging file must sit unmodified before it is treated as orphaned.
#: Far above any real stage-to-rename window, which is a single write and fsync.
STAGING_TEMP_MIN_AGE_SECS = 60 * 60

#: ``tempfile.mkstemp`` with its default ``tmp`` prefix and a ``.tmp`` suffix: eight
#: characters from its name alphabet in between.
_DEFAULT_PREFIX_TEMP_RE = re.compile(r"tmp[a-z0-9_]{8}\.tmp")

#: ``tempfile.mkstemp(prefix=INCOMING_PREFIX)``: the session-store move staging name.
_INCOMING_TEMP_RE = re.compile(r"\.kc-incoming-[a-z0-9_]{8}")


def is_staging_temp_name(name: str) -> bool:
    """Whether *name* is one of the two staging shapes this sweep may remove."""
    return bool(_DEFAULT_PREFIX_TEMP_RE.fullmatch(name) or _INCOMING_TEMP_RE.fullmatch(name))


def sweep_session_staging_temps(
    sessions_dir: Path | None = None, *, now: float | None = None
) -> int:
    """Remove orphaned staging files directly under the sessions directory.

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
        for name in names:
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
