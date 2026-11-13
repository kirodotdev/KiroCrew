"""Read a data file into the page cache, front to back, ahead of its first query.

SQLite reads a table through its B-tree, a page at a time and out of file order,
so a store queried right after boot pays one random read per page: on a copy of
a real 435 MB knowledge library one full read of the item vectors took 14.2 s.
Reading the same file in order took 1.3 s, and the query after it 0.1 s.
``prefetch`` does that ordered read on a background thread. (``posix_fadvise``
WILLNEED was measured too and changed nothing on that file, so it is not used.)

The read runs in a short-lived child process, never in this one. The files are
live SQLite databases this process already has open, and on POSIX closing ANY
descriptor on a file drops every ``fcntl`` lock the process holds on it --
including the locks SQLite's own connection holds. A rollback-journal database
whose writer loses its RESERVED lock that way lets another process commit into
the same pages, and the file is left corrupt. A child's descriptors carry none
of this process's locks, so its close takes nothing away.

A hint only: it never changes what a query returns and never raises. The pages
it brings in are ordinary page cache, reclaimed under memory pressure like any
other; no process memory grows. A file over ``MAX_BYTES`` is left alone.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from pathlib import Path
from typing import Iterable

from kiro_crew import platform_compat

logger = logging.getLogger(__name__)

#: Larger files are not read ahead: the ordered read is ~1.3 s per 435 MB here,
#: and past this size it would compete with boot for longer than it saves.
MAX_BYTES = 2 * 1024**3
_CHUNK = 4 * 1024 * 1024

#: Bound on the child's whole read. At the measured 1.3 s per 435 MB a file at
#: ``MAX_BYTES`` reads in about 6 s; a child still reading long after that is on a
#: stalled disk, and the hint is not worth waiting for.
_READ_TIMEOUT_SECS = 120

#: The child's program: read each path to the end, skip one it cannot read.
_CHILD_SOURCE = (
    "import sys\n"
    "size = int(sys.argv[1])\n"
    "for name in sys.argv[2:]:\n"
    "    try:\n"
    "        with open(name, 'rb', buffering=0) as handle:\n"
    "            while handle.read(size):\n"
    "                pass\n"
    "    except OSError:\n"
    "        pass\n"
)


def _candidates(path: Path) -> list[str]:
    found: list[str] = []
    for candidate in (path, Path(f"{path}-wal")):
        try:
            if candidate.is_file() and candidate.stat().st_size <= MAX_BYTES:
                found.append(str(candidate))
        except OSError:
            logger.debug("page-cache prefetch skipped %s", candidate, exc_info=True)
    return found


def _read_through(path: Path) -> None:
    files = _candidates(path)
    if not files:
        return
    argv = platform_compat.isolated_python_argv("-I", "-c", _CHILD_SOURCE, str(_CHUNK), *files)
    try:
        subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_READ_TIMEOUT_SECS,
            check=False,
            creationflags=platform_compat._SUBPROCESS_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        logger.debug("page-cache prefetch skipped %s", path, exc_info=True)


def read_now(paths: Iterable[os.PathLike | str | None]) -> None:
    """Read *paths* (and their ``-wal`` files) in order, waiting for the read. Never raises."""
    for path in (Path(p) for p in paths if p):
        _read_through(path)


def prefetch(paths: Iterable[os.PathLike | str | None], *, name: str = "kc-page-prefetch") -> None:
    """Read *paths* (and their ``-wal`` files) into the page cache on a daemon thread.

    Returns at once. ``None`` and missing paths are skipped.
    """
    todo = [Path(p) for p in paths if p]
    if not todo:
        return

    def _run() -> None:
        for path in todo:
            _read_through(path)

    try:
        threading.Thread(target=_run, name=name, daemon=True).start()
    except RuntimeError:  # interpreter shutting down
        pass
