"""Bound a pod log read to one start, for backends whose log files append.

launchd's ``StandardOutPath``/``StandardErrorPath`` and the Windows wrapper's
``>>`` redirects both append every boot to the same two files, so a tail read
after a failed start would also return an earlier start's lines. A mark taken
just before the start records each file's size; reading past it returns only
what the current start wrote.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path


def mark(paths: Iterable[Path]) -> dict[str, int]:
    """Each file's current size, keyed by file name; a missing file marks at 0."""
    sizes: dict[str, int] = {}
    for path in paths:
        try:
            sizes[path.name] = path.stat().st_size
        except OSError:
            sizes[path.name] = 0
    return sizes


def read_since(path: Path, offset: int) -> bytes:
    """Bytes appended to *path* past *offset*; the whole file if it shrank.

    A file smaller than its mark was replaced after the mark was taken, so all
    of it belongs to the current start.
    """
    with path.open("rb") as fh:
        if fh.seek(0, os.SEEK_END) >= offset:
            fh.seek(offset)
        else:
            fh.seek(0)
        return fh.read()
