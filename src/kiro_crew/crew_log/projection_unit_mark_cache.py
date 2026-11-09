"""An older unit's slot-fold mark, settled by two stats while its files have not moved.

A warm slot read compares every unit's mark (:func:`projection_slot_memo._unit_mark`)
with the one its cell was folded at. The full mark opens the log, resolves its path,
reads the header twice, lists the segments and reads ``.cuts``: about 240 file system
calls per unit per read. Every value in it comes from the unit's directory and segment
files, so this module keeps each older unit's mark beside one ``lstat`` of the directory
and of each segment, and hands the mark back while those stamps hold. Any difference, a
missing file or a link reads the full mark again.
"""

from __future__ import annotations

import os
import stat
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from typing import Any, Final, NamedTuple, Protocol, TypeVar

from kiro_crew.crew_log.schema import KIND_SESSION
from kiro_crew.crew_log.store import crew_log_dir, segment_paths

#: A unit whose directory or segments changed this recently is never cached: a second
#: change inside the same timestamp tick could leave every stat below unchanged. Two
#: seconds covers the coarsest common filesystem clock (FAT, and 1 s on ext3 / HFS+).
#: An older unit that has sat this long, which is every older unit of a live slot, is
#: then settled by stats alone.
_MARK_RACY_NS: int = 2_000_000_000

#: How many units' cached marks are held across every home and slot, oldest dropped
#: first. shortcut: a count, not bytes; an entry is a few hundred bytes. Raise it if a
#: host's slots together hold more units than this and misses show up in profiles.
_MARK_CACHE_LIMIT: Final[int] = 4096


class _Mark(Protocol):
    """What this cache needs of a mark: whether its identity is known."""

    @property
    def origin(self) -> "str | None": ...


_M = TypeVar("_M", bound=_Mark)


class _CachedMark(NamedTuple):
    """A unit's mark, and the stats of its directory and segments taken BEFORE it.

    ``stamps`` comes first so a change landing while the mark is read makes the stamps
    stale rather than the mark: the next read then sees a difference and reads again.
    """

    directory: str
    segments: "tuple[str, ...]"
    stamps: "tuple[tuple[int, ...], ...]"
    mark: Any


_mark_cache: "OrderedDict[tuple[str, str], _CachedMark]" = OrderedDict()
_mark_cache_guard = threading.Lock()


def _forget_unit_marks() -> None:
    """Drop every cached mark, so the next read of each unit reads its full mark."""
    with _mark_cache_guard:
        _mark_cache.clear()


def _stamp(info: os.stat_result) -> "tuple[int, ...]":
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _reparse_tag(info: os.stat_result) -> int:
    """The Windows reparse tag ``lstat`` reports (a junction, a link), or 0 elsewhere."""
    tag: int = getattr(info, "st_reparse_tag", 0)
    return tag


def _segment_stamps(
    directory: str, segments: "Sequence[str]"
) -> "tuple[tuple[int, ...], ...] | None":
    """One ``lstat`` of *directory* and of each segment, or ``None`` if any is wrong.

    ``lstat``, so a directory or segment swapped for a link never matches: the answer
    is about the name, which is what :func:`resolved_within` checks on the full path.
    A Windows junction ``lstat``s as a directory, so any reparse point is refused too.
    The directory's mtime moves when a segment or ``.cuts`` is created, renamed or
    removed (rotation, retention, recreate); a segment's size, mtime and ctime move
    when its bytes do.
    """
    try:
        top = os.lstat(directory)
        if not stat.S_ISDIR(top.st_mode) or _reparse_tag(top):
            return None
        stamps = [_stamp(top)]
        for segment in segments:
            info = os.lstat(segment)
            if not stat.S_ISREG(info.st_mode) or _reparse_tag(info):
                return None
            stamps.append(_stamp(info))
    except OSError:
        return None
    return tuple(stamps)


def _read_and_cache_mark(home: str, unit_id: str, read: "Callable[[str], _M]") -> _M:
    """``read(unit_id)``, with stats taken first and cached beside it when settled."""
    stamps: "tuple[tuple[int, ...], ...] | None" = None
    directory = ""
    segments: "tuple[str, ...]" = ()
    try:
        directory = str(crew_log_dir(KIND_SESSION, unit_id))
        # The directory is stamped BEFORE it is listed, so a segment created after the
        # listing still moves the directory's mtime away from the cached one.
        before = _segment_stamps(directory, ())
        segments = tuple(str(path) for path in segment_paths(KIND_SESSION, unit_id))
        after = _segment_stamps(directory, segments)
        if before is not None and after is not None and segments and after[0] == before[0]:
            stamps = after
    except Exception:
        stamps = None
    mark = read(unit_id)
    settled = stamps is not None and mark.origin is not None
    if settled and stamps is not None:
        newest = max(max(stamp[3], stamp[4]) for stamp in stamps)
        settled = newest < time.time_ns() - _MARK_RACY_NS
    with _mark_cache_guard:
        if not settled or stamps is None:
            _mark_cache.pop((home, unit_id), None)
            return mark
        _mark_cache[(home, unit_id)] = _CachedMark(directory, segments, stamps, mark)
        _mark_cache.move_to_end((home, unit_id))
        while len(_mark_cache) > _MARK_CACHE_LIMIT:
            _mark_cache.popitem(last=False)
    return mark


def _older_unit_mark(home: str, unit_id: str, read: "Callable[[str], _M]") -> _M:
    """An older unit's mark from two stats when nothing about its files has moved.

    *read* is the full mark. A single-segment unit whose directory and segment stat the
    same as when its mark was read costs two ``lstat``; anything else calls *read*.
    """
    with _mark_cache_guard:
        held = _mark_cache.get((home, unit_id))
    if held is not None and _segment_stamps(held.directory, held.segments) == held.stamps:
        cached: _M = held.mark
        return cached
    return _read_and_cache_mark(home, unit_id, read)
