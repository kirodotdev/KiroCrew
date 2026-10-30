"""Where a folded prefix ends on disk, checked again in a few stats and one bounded read.

A fold that resumes -- from a savepoint after a restart, or from a bundle cached in this
process -- stands on state folded from bytes it does not read again. Something has to say
those bytes are still the bytes. The store's two answers are each incomplete on their own:

* the cut counter (``CrewLog.cuts``) is O(1), but only covers the store's OWN rewrites, and
  a unit created before the counter existed has none;
* a digest of the whole prefix covers any change, but costs a walk of every record the
  fold consumed, on every resume -- which is the O(entries) cost this module removes.

A :class:`PrefixExtent` is the third answer, and it is cheap because it does not prove the
prefix is unchanged byte for byte. It proves the FILES are the ones the fold read, in
place, and only appended to since:

* every OLDER segment still has the name, size and mtime it had (only the newest segment is
  ever appended to, so an older one whose size or mtime moved was rewritten);
* the segment the prefix ends in is the same file (device and inode), is not shorter than
  the prefix, and either is untouched (same size and mtime) or only GREW;
* the last :data:`EXTENT_TAIL_BYTES` of the prefix still hash to what they did, and still end
  a record. The store's own repairs (a torn tail closed, a failed append removed) land at the
  end of the file, which is where this window sits.

Two strengths, chosen by the caller (:func:`extent_holds`). UNTOUCHED -- every segment
the same file at the same size and mtime -- has no blind spot, and is the only one a unit
with no cut counter is admitted on: any change, an append included, sends it to the full
digest once, after which the extent is re-taken. GROWN -- the end segment only grew, its
last window unchanged -- cannot see an outside edit further back followed by an append,
so it is used only beside a cut count, which covers the store's own rewrites. ``False``
from :func:`extent_holds` is always "not proven", never "damaged".

Everything here is decode-free except one record parse in :func:`extent_at`, and never
raises.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Mapping
from typing import Any, Final, NamedTuple

from kiro_crew.crew_log.schema import MAX_ENTRY_BYTES
from kiro_crew.crew_log.store import CrewLog, ResumePoint, log_exception_text, segment_paths
from kiro_crew.jsonl_util import bounded_raw_records_with_offsets

logger = logging.getLogger(__name__)

#: How much of a prefix's END is re-read and re-hashed when the stats alone cannot settle
#: it (the segment grew). Bounded, so the check costs the same on any log size.
EXTENT_TAIL_BYTES: Final[int] = 16 * 1024


class PrefixExtent(NamedTuple):
    """Where a folded prefix ends on disk, and the file facts that pin it there."""

    #: The segment holding the prefix's last record.
    segment: str
    #: The byte offset just past that record.
    end: int
    dev: int
    ino: int
    #: The segment's own size and mtime when the extent was taken.
    size: int
    mtime_ns: int
    #: Every OLDER segment as ``(name, size, mtime_ns, dev, ino)``, oldest first.
    sealed: "tuple[tuple[str, int, int, int, int], ...]"
    #: Start of the hashed window; it ends at :attr:`end`.
    tail_start: int
    tail_sha: str


def extent_at(handle: CrewLog, point: "ResumePoint | None") -> "PrefixExtent | None":
    """The extent of *handle*'s prefix through the record *point* names, or ``None``.

    *point* is a position a walk of this log handed out (``ReadCursor.at``). The record at
    its offset is framed and its ``seq`` compared, so a point that does not name that
    record here gives no extent rather than a wrong one.
    """
    if point is None or not isinstance(point.offset, int) or point.offset <= 0:
        return None
    try:
        paths = segment_paths(handle.kind, handle.id)
        names = [path.name for path in paths]
        if point.segment not in names:
            return None
        index = names.index(point.segment)
        sealed = []
        for path in paths[:index]:
            stat = path.stat()
            sealed.append((path.name, stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino))
        path = paths[index]
        with open(path, "rb") as source:
            stat = os.fstat(source.fileno())
            source.seek(point.offset)
            framed = next(
                iter(
                    bounded_raw_records_with_offsets(
                        source, path, cap=MAX_ENTRY_BYTES, label="crew log"
                    )
                ),
                None,
            )
            if framed is None:
                return None
            start, end, raw = framed
            if start != point.offset or raw is None or not raw.endswith(b"\n"):
                return None
            try:
                parsed = json.loads(raw)
            except ValueError:
                return None
            if not isinstance(parsed, dict) or parsed.get("seq") != point.seq:
                return None
            tail_start = max(0, end - EXTENT_TAIL_BYTES)
            source.seek(tail_start)
            tail = source.read(end - tail_start)
        if len(tail) != end - tail_start:
            return None
        return PrefixExtent(
            segment=path.name,
            end=end,
            dev=stat.st_dev,
            ino=stat.st_ino,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            sealed=tuple(sealed),
            tail_start=tail_start,
            tail_sha=hashlib.sha256(tail).hexdigest(),
        )
    except Exception:
        log_exception_text(
            logger, logging.DEBUG, "crew log prefix extent for %s could not be read", handle.id
        )
        return None


def extent_holds(handle: CrewLog, extent: "PrefixExtent | None", *, grown_ok: bool = False) -> bool:
    """Whether *handle*'s log still holds the prefix *extent* pins, in O(1) reads.

    ``grown_ok=False`` (the default) proves the files are UNTOUCHED: every segment the
    same file (device and inode) at the same size and mtime. That is a complete answer
    with no blind spot, and it is what a unit with no cut counter is admitted on: any
    change at all, an append included, sends it back to the full digest.

    ``grown_ok=True`` also accepts an end segment that only grew, with the last
    :data:`EXTENT_TAIL_BYTES` of the prefix unchanged. That is a check on the FILES, for
    a caller whose prefix is already vouched for by the cut counter; it cannot see an
    outside edit further back followed by an append, and is never used on its own.

    ``None`` is never proven.
    """
    if extent is None:
        return False
    try:
        paths = segment_paths(handle.kind, handle.id)
        names = [path.name for path in paths]
        if extent.segment not in names:
            return False
        index = names.index(extent.segment)
        if len(extent.sealed) != index:
            return False
        for path, (name, size, mtime_ns, dev, ino) in zip(paths[:index], extent.sealed):
            stat = path.stat()
            if (path.name, stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino) != (
                name,
                size,
                mtime_ns,
                dev,
                ino,
            ):
                return False
        with open(paths[index], "rb") as source:
            stat = os.fstat(source.fileno())
            if (stat.st_dev, stat.st_ino) != (extent.dev, extent.ino):
                return False
            if stat.st_size < extent.end or stat.st_size < extent.size:
                return False
            if not grown_ok and (stat.st_size, stat.st_mtime_ns) != (extent.size, extent.mtime_ns):
                return False
            if stat.st_size == extent.size and stat.st_mtime_ns != extent.mtime_ns:
                # Same size, written since: an in-place rewrite, not an append.
                return False
            source.seek(extent.tail_start)
            tail = source.read(extent.end - extent.tail_start)
        if len(tail) != extent.end - extent.tail_start or not tail.endswith(b"\n"):
            return False
        return hashlib.sha256(tail).hexdigest() == extent.tail_sha
    except Exception:
        return False


def extent_mapping(extent: PrefixExtent) -> dict[str, Any]:
    """*extent* as the JSON a savepoint's witness stores."""
    return {
        "segment": extent.segment,
        "end": extent.end,
        "dev": extent.dev,
        "ino": extent.ino,
        "size": extent.size,
        "mtime_ns": extent.mtime_ns,
        "sealed": [list(item) for item in extent.sealed],
        "tail_start": extent.tail_start,
        "tail_sha": extent.tail_sha,
    }


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _count(value: Any) -> "int | None":
    return value if _is_count(value) else None


def _is_sha(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def extent_from(raw: Any) -> "PrefixExtent | None":
    """A stored extent mapping as a :class:`PrefixExtent`, or ``None`` if malformed."""
    if not isinstance(raw, Mapping):
        return None
    sealed_raw = raw.get("sealed")
    if not isinstance(sealed_raw, list):
        return None
    sealed: list[tuple[str, int, int, int, int]] = []
    for item in sealed_raw:
        if (
            not isinstance(item, list)
            or len(item) != 5
            or not isinstance(item[0], str)
            or not all(_is_count(value) for value in item[1:])
        ):
            return None
        sealed.append((item[0], item[1], item[2], item[3], item[4]))
    segment = raw.get("segment")
    counts = [
        _count(raw.get(key)) for key in ("end", "dev", "ino", "size", "mtime_ns", "tail_start")
    ]
    tail_sha = raw.get("tail_sha")
    if not isinstance(segment, str) or not isinstance(tail_sha, str) or not _is_sha(tail_sha):
        return None
    end, dev, ino, size, mtime_ns, tail_start = counts
    if (
        end is None
        or dev is None
        or ino is None
        or size is None
        or mtime_ns is None
        or tail_start is None
        or tail_start > end
        or end - tail_start > EXTENT_TAIL_BYTES
    ):
        return None
    return PrefixExtent(
        segment=segment,
        end=end,
        dev=dev,
        ino=ino,
        size=size,
        mtime_ns=mtime_ns,
        sealed=tuple(sealed),
        tail_start=tail_start,
        tail_sha=tail_sha,
    )


def resume_point_from(raw: Any) -> "ResumePoint | None":
    """A stored resume mapping as a :class:`ResumePoint`, or ``None`` if malformed.

    Shape only: whether the point still applies is the store's question, asked by
    ``CrewLog.iter_from`` against the live file before it seeks anywhere.
    """
    if not isinstance(raw, Mapping):
        return None
    segment = raw.get("segment")
    offset = _count(raw.get("offset"))
    seq = _count(raw.get("seq"))
    cuts = raw.get("cuts")
    if not isinstance(segment, str) or offset is None or seq is None:
        return None
    if cuts is not None and not _is_count(cuts):
        return None
    return ResumePoint(segment=segment, offset=offset, seq=seq, cuts=cuts)


def resume_mapping(point: ResumePoint) -> dict[str, Any]:
    """*point* as the JSON a savepoint's witness stores."""
    return {"segment": point.segment, "offset": point.offset, "seq": point.seq, "cuts": point.cuts}
