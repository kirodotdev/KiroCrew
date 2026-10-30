"""The transcript row-index base, kept on disk and carried across first-line rewrites.

:class:`~kiro_crew.history_projection_reads.TranscriptReadProjection` pages a
transcript through a sparse row index whose base (``_TranscriptIndexBase``: device,
inode, a record boundary, a digest of the 4 KiB before it, the row count and the
checkpoints) lets a read resume from where the last scan stopped. In memory alone
that base is lost on every restart, and every metadata-line writer replaces the
file's inode, so either way the next page read framed the whole transcript from
byte 0. This module keeps the base in a sidecar file and moves it onto the new
inode a first-line rewrite produced.

What the sidecar is trusted for. Nothing on its own: the reader re-checks it against
the open file exactly as it re-checks the in-memory base (same device and inode, at
least as long as the boundary, the same digest before it), so a stale sidecar costs
one scan from byte 0. It sits beside the transcript it describes, with the same
permissions, and every row a page returns is decoded from the transcript itself; a
writer able to forge the sidecar can rewrite the transcript directly, so a forged
one can misplace that transcript's own rows and nothing more.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew.history_cache import _TranscriptIndexBase
from kiro_crew.history_projection import (
    _HISTORY_LOGGER,
    _TRANSCRIPT_INDEX_FINGERPRINT_BYTES,
    _TRANSCRIPT_PAGE_INDEX_MAX_CHECKPOINTS,
    _history_facade,
)

if TYPE_CHECKING:
    from kiro_crew.history import ConversationLog

#: Where a transcript's row-index base is persisted, under the transcripts' own
#: directory: a dot-directory, so no ``*.jsonl`` glob over that directory sees it.
PAGE_INDEX_DIR = ".page-index"
_VERSION = 1
#: A transcript shorter than this is cheap to frame from byte 0 and gets no sidecar;
#: past it, the base is persisted, and re-persisted once it has grown by this much,
#: so a new process resumes from at most this many unscanned bytes.
PERSIST_BYTES = 256 * 1024
#: Most sidecar paths whose last persisted extent this process remembers.
_PERSISTED_MAX = 4096
_persisted: "dict[str, tuple[int, int, int]]" = {}
_persisted_lock = threading.Lock()


def sidecar_path(path: Path) -> Path:
    """The sidecar file holding *path*'s persisted row-index base."""
    return path.parent / PAGE_INDEX_DIR / f"{path.name}.json"


def load(path: Path) -> _TranscriptIndexBase | None:
    """*path*'s persisted row-index base, or ``None`` when absent or not well formed.

    A shape check only; the caller re-checks the base against the open file (see the
    module docstring).
    """
    try:
        raw = json.loads(sidecar_path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        if not isinstance(raw, dict) or raw.get("v") != _VERSION:
            return None
        dev, ino, end, rows = raw["dev"], raw["ino"], raw["end"], raw["rows"]
        digest = bytes.fromhex(raw["digest"])
        points = raw["cps"]
        if not all(isinstance(v, int) and v >= 0 for v in (dev, ino, end, rows)):
            return None
        if len(digest) != 16 or not isinstance(points, list):
            return None
        if len(points) > _TRANSCRIPT_PAGE_INDEX_MAX_CHECKPOINTS:
            return None
        checkpoints: list[tuple[int, int]] = []
        for point in points:
            row, offset = point
            if not (isinstance(row, int) and isinstance(offset, int)):
                return None
            if not (0 <= row < rows and 0 <= offset < end):
                return None
            if checkpoints and (row <= checkpoints[-1][0] or offset <= checkpoints[-1][1]):
                return None
            checkpoints.append((row, offset))
        if rows and (not checkpoints or checkpoints[0][0] != 0):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return _TranscriptIndexBase(dev, ino, end, digest, rows, tuple(checkpoints))


def persist(path: Path, base: _TranscriptIndexBase, *, force: bool) -> None:
    """Write *base* to *path*'s sidecar when it is worth keeping. Fail-soft.

    Skipped for a transcript under :data:`PERSIST_BYTES`, and -- unless *force* --
    while this process last persisted the same file identity within that many bytes
    of the base's extent, so a page read on a growing transcript writes at most one
    small file per that much growth. A lost write costs the next process one longer
    tail scan, never a wrong page.
    """
    if base.scanned_end < PERSIST_BYTES:
        return
    marker = str(path)
    with _persisted_lock:
        seen = _persisted.get(marker)
    if (
        not force
        and seen is not None
        and seen[:2] == (base.dev, base.ino)
        and base.scanned_end - seen[2] < PERSIST_BYTES
    ):
        return
    payload = {
        "v": _VERSION,
        "dev": base.dev,
        "ino": base.ino,
        "end": base.scanned_end,
        "digest": base.digest.hex(),
        "rows": base.row_count,
        "cps": [list(point) for point in base.checkpoints],
    }
    sidecar = sidecar_path(path)
    try:
        sidecar.parent.mkdir(exist_ok=True)
        _history_facade().atomic_write(sidecar, json.dumps(payload), fsync=False)
    except OSError:
        _HISTORY_LOGGER.debug("history: could not persist the page index for %s", path)
        return
    with _persisted_lock:
        _persisted.pop(marker, None)
        _persisted[marker] = (base.dev, base.ino, base.scanned_end)
        while len(_persisted) > _PERSISTED_MAX:
            _persisted.pop(next(iter(_persisted)))


def forget(path: Path) -> None:
    """Remove *path*'s persisted base, for a transcript that was deleted. Fail-soft:
    a sidecar left behind is never trusted for another file, it only takes space."""
    with _persisted_lock:
        _persisted.pop(str(path), None)
    with contextlib.suppress(OSError):
        sidecar_path(path).unlink()


def carry_over_head_rewrite(
    log: "ConversationLog", key: str, before: os.stat_result, old_head: str, new_head: str
) -> None:
    """Move *key*'s row-index base onto the file a first-line rewrite just wrote.

    For the writers that replace ONLY the metadata line -- a metadata update, a
    consolidation marker, a cleared close -- by rewriting the whole file to a new
    inode. The saved base names the old inode, so without a carry the next page read
    frames the whole transcript from byte 0. Called under the writer's lock, after
    the write, with the old file's ``stat`` taken under the same lock before it was
    read.

    Carried only on proof, and otherwise left alone (the next read rebuilds):

    * Both first lines end in a newline and the bytes after them have the same length
      in both files. The writer re-wrote the decoded lines it read, so the body text
      is identical, and equal lengths rule out the one transformation a text read
      applies (newline translation only ever shortens).
    * The saved base -- in memory, or else the persisted one -- belongs to the old file (same device and inode, within its
      size), and every offset it holds lies past the old first line.
    * The bytes before the moved boundary in the NEW file hash to the saved digest,
      read from disk: a bounded read that proves both that the base matched the old
      bytes and that they moved by exactly the head's change.
    """
    if not old_head.endswith("\n") or not new_head.endswith("\n"):
        return
    path = log._path(key)
    base = log._page_index_base.get(key)
    if base is None or (base.dev, base.ino) != (before.st_dev, before.st_ino):
        # A writer in a process that never paged this file (a restart, then a
        # resume that clears the close) holds no base, but the sidecar may: it
        # names the old file and passes the same proof below.
        base = load(path)
    if base is None:
        return
    old_len = len(old_head.encode("utf-8"))
    new_len = len(new_head.encode("utf-8"))
    delta = new_len - old_len
    fingerprint = _TRANSCRIPT_INDEX_FINGERPRINT_BYTES
    if (base.dev, base.ino) != (before.st_dev, before.st_ino):
        return
    if base.scanned_end > before.st_size or base.scanned_end - fingerprint < old_len:
        return
    if any(offset < old_len for _row, offset in base.checkpoints):
        return
    try:
        with open(path, "rb") as handle:
            after = os.fstat(handle.fileno())
            if after.st_size - new_len != before.st_size - old_len:
                return
            end = base.scanned_end + delta
            handle.seek(end - fingerprint)
            window = handle.read(fingerprint)
    except OSError:
        return
    if len(window) != fingerprint:
        return
    if hashlib.blake2b(window, digest_size=16).digest() != base.digest:
        return
    carried = _TranscriptIndexBase(
        after.st_dev,
        after.st_ino,
        end,
        base.digest,
        base.row_count,
        tuple((row, offset + delta) for row, offset in base.checkpoints),
    )
    log._page_index_base[key] = carried
    persist(path, carried, force=True)
