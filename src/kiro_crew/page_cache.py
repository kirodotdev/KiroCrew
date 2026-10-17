"""Read a data file into the page cache, front to back, ahead of its first query.

SQLite reads a table through its B-tree, a page at a time and out of file order,
so a store queried right after boot pays one random read per page: on a copy of
a real 435 MB knowledge library one full read of the item vectors took 14.2 s.
Reading the same file in order took 1.3 s, and the query after it 0.1 s.
``prefetch`` does that ordered read on a background thread. (``posix_fadvise``
WILLNEED was measured too and changed nothing on that file, so it is not used.)

A hint only: it never changes what a query returns and never raises. The pages
it brings in are ordinary page cache, reclaimed under memory pressure like any
other; no process memory grows. A file over ``MAX_BYTES`` is left alone.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

#: Larger files are not read ahead: the ordered read is ~1.3 s per 435 MB here,
#: and past this size it would compete with boot for longer than it saves.
MAX_BYTES = 2 * 1024**3
_CHUNK = 4 * 1024 * 1024


def _read_through(path: Path) -> None:
    for candidate in (path, Path(f"{path}-wal")):
        try:
            if not candidate.is_file() or candidate.stat().st_size > MAX_BYTES:
                continue
            with open(candidate, "rb", buffering=0) as handle:
                while handle.read(_CHUNK):
                    pass
        except OSError:
            logger.debug("page-cache prefetch skipped %s", candidate, exc_info=True)


def read_now(paths: Iterable[os.PathLike | str | None]) -> None:
    """Read *paths* (and their ``-wal`` files) in order on the calling thread. Never raises."""
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
