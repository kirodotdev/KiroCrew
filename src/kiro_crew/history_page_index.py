"""The transcript row-index base, kept on disk and found again after a rewrite.

:class:`~kiro_crew.history_projection_reads.TranscriptReadProjection` pages a
transcript through a sparse row index whose base (``_TranscriptIndexBase``: the
first line's length, a record boundary, a digest of the body around it, the row
count and the checkpoints) lets a read resume from where the last scan stopped.
In memory alone that base is lost on every restart, so this module keeps it in a
sidecar file beside the transcript.

The base is identified by CONTENT, not by inode. A copy of the data home, a restore
from backup, and every atomic rewrite give the transcript a new inode, while the
dashboard's full save and the metadata writers rewrite the same rows behind a new
first line. :func:`relocate` therefore re-checks a base against the open file with
every offset moved by the first line's change in length, and accepts it only when
the digest of the body windows still matches there.

What the sidecar is trusted for. Nothing on its own: a stale or forged base costs
one scan from byte 0, because the digest is re-read from the open file before any
offset is used. It sits beside the transcript it describes, with the same
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
from typing import IO

from kiro_crew.history_cache import _TranscriptIndexBase
from kiro_crew.history_projection import (
    _HISTORY_LOGGER,
    _TRANSCRIPT_INDEX_FINGERPRINT_BYTES,
    _TRANSCRIPT_PAGE_INDEX_MAX_CHECKPOINTS,
    _history_facade,
)

#: Where a transcript's row-index base is persisted, under the transcripts' own
#: directory: a dot-directory, so no ``*.jsonl`` glob over that directory sees it.
PAGE_INDEX_DIR = ".page-index"
_VERSION = 2
#: A transcript shorter than this is cheap to frame from byte 0 and gets no sidecar;
#: past it, the base is persisted, and re-persisted once it has grown by this much,
#: so a new process resumes from at most this many unscanned bytes.
PERSIST_BYTES = 256 * 1024
#: Longest first line :func:`relocate` reads to learn the head's length. A metadata
#: line is a few KiB; a longer first line is not one, and the base is refused.
HEAD_MAX_BYTES = 1 << 20
#: Bytes after each checkpoint that :func:`body_digest` covers. A row's first
#: bytes carry its role and timestamp, so a checkpoint that moved onto another
#: row, or into the middle of one, reads different bytes here.
CHECKPOINT_WINDOW_BYTES = 64
#: Most sidecar paths whose last persisted base this process remembers.
_PERSISTED_MAX = 4096
_persisted: "dict[str, _TranscriptIndexBase]" = {}
_persisted_lock = threading.Lock()


def sidecar_path(path: Path) -> Path:
    """The sidecar file holding *path*'s persisted row-index base."""
    return path.parent / PAGE_INDEX_DIR / f"{path.name}.json"


def _body_start(head_len: int, checkpoints: tuple[tuple[int, int], ...]) -> int:
    """Where the digested body begins: after the first line, unless that line is a row.

    A legacy transcript whose first line is a message has it indexed as row 0, so
    that line is body and must be covered by the digest too.
    """
    if checkpoints and checkpoints[0][1] < head_len:
        return 0
    return head_len


def body_digest(
    handle: IO[bytes],
    head_len: int,
    scanned_end: int,
    checkpoints: tuple[tuple[int, int], ...],
) -> bytes | None:
    """Digest of the body windows a base is checked by, read from *handle*.

    Windows of up to ``_TRANSCRIPT_INDEX_FINGERPRINT_BYTES`` at the start of the
    body, at the last checkpoint and just before *scanned_end*, plus a short
    window around EVERY checkpoint (the byte before it and
    :data:`CHECKPOINT_WINDOW_BYTES` after). The per-checkpoint windows are what
    pin each interior offset: a rewrite whose row-length changes cancel out
    before the end still moves the rows between them, so some checkpoint's
    window then reads other bytes. ``None`` when the file is shorter than the
    windows need.
    """
    span = _TRANSCRIPT_INDEX_FINGERPRINT_BYTES
    start = _body_start(head_len, checkpoints)
    if scanned_end <= start:
        return None
    last_cp = checkpoints[-1][1] if checkpoints else start
    windows = [
        (start, min(start + span, scanned_end)),
        (last_cp, min(last_cp + span, scanned_end)),
        (max(start, scanned_end - span), scanned_end),
    ]
    windows.extend(
        (max(start, offset - 1), min(offset + CHECKPOINT_WINDOW_BYTES, scanned_end))
        for _row, offset in checkpoints
    )
    digest = hashlib.blake2b(digest_size=16)
    for lo, hi in windows:
        handle.seek(lo)
        window = handle.read(hi - lo)
        if len(window) != hi - lo:
            return None
        digest.update(b"%d:%d:" % (lo - start, hi - lo))
        digest.update(window)
    return digest.digest()


def head_length(handle: IO[bytes]) -> int | None:
    """Length of *handle*'s first line, or ``None`` when it has no complete one."""
    handle.seek(0)
    head = handle.readline(HEAD_MAX_BYTES + 1)
    if not head.endswith(b"\n") or len(head) > HEAD_MAX_BYTES:
        return None
    return len(head)


def relocate(handle: IO[bytes], base: _TranscriptIndexBase) -> _TranscriptIndexBase | None:
    """*base* moved onto the open file, or ``None`` when the file does not hold its rows.

    Every offset moves by the first line's change in length. The moved base is
    accepted only when the file reaches its boundary and the body windows hash to
    the saved digest at their moved offsets.
    """
    if base.scanned_end <= 0:
        return None
    new_head = head_length(handle)
    if new_head is None:
        return None
    shift = new_head - base.head_len
    if shift and _body_start(base.head_len, base.checkpoints) == 0:
        return None  # the first line is row 0, so a change to it changed the body
    end = base.scanned_end + shift
    if os.fstat(handle.fileno()).st_size < end:
        return None
    checkpoints = tuple((row, offset + shift) for row, offset in base.checkpoints)
    if body_digest(handle, new_head, end, checkpoints) != base.digest:
        return None
    if not shift:
        return base
    return _TranscriptIndexBase(new_head, end, base.digest, base.row_count, checkpoints)


def load(path: Path) -> _TranscriptIndexBase | None:
    """*path*'s persisted row-index base, or ``None`` when absent or not well formed.

    A shape check only; the caller re-checks the base against the open file with
    :func:`relocate` (see the module docstring). A version-1 sidecar, keyed on the
    inode, is ignored and replaced by the next scan.
    """
    try:
        raw = json.loads(sidecar_path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        if not isinstance(raw, dict) or raw.get("v") != _VERSION:
            return None
        head, end, rows = raw["head"], raw["end"], raw["rows"]
        digest = bytes.fromhex(raw["digest"])
        points = raw["cps"]
        if not all(isinstance(v, int) and v >= 0 for v in (head, end, rows)):
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
    return _TranscriptIndexBase(head, end, digest, rows, tuple(checkpoints))


def _extends(seen: _TranscriptIndexBase, base: _TranscriptIndexBase) -> bool:
    """True when *base* is *seen* plus rows scanned after it, on the same first line."""
    return (
        seen.head_len == base.head_len
        and seen.row_count <= base.row_count
        and base.checkpoints[: len(seen.checkpoints)] == seen.checkpoints
    )


def persist(path: Path, base: _TranscriptIndexBase, *, force: bool) -> None:
    """Write *base* to *path*'s sidecar when it is worth keeping. Fail-soft.

    Skipped for a transcript under :data:`PERSIST_BYTES`, and -- unless *force* --
    while this process last persisted a base that *base* extends by less than that
    many bytes, so a page read on a growing transcript writes at most one small file
    per that much growth. A base for rewritten content does not extend the last one,
    so it is always written. A lost write costs the next process one longer tail
    scan, never a wrong page.
    """
    if base.scanned_end < PERSIST_BYTES:
        return
    marker = str(path)
    with _persisted_lock:
        seen = _persisted.get(marker)
    if (
        not force
        and seen is not None
        and _extends(seen, base)
        and base.scanned_end - seen.scanned_end < PERSIST_BYTES
    ):
        return
    payload = {
        "v": _VERSION,
        "head": base.head_len,
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
        _persisted[marker] = base
        while len(_persisted) > _PERSISTED_MAX:
            _persisted.pop(next(iter(_persisted)))


def forget(path: Path) -> None:
    """Remove *path*'s persisted base, for a transcript that was deleted. Fail-soft:
    a sidecar left behind is never trusted for another file, it only takes space."""
    with _persisted_lock:
        _persisted.pop(str(path), None)
    with contextlib.suppress(OSError):
        sidecar_path(path).unlink()
