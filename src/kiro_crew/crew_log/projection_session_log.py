"""Opening one session's crew log, and stating which log a fold was read from."""

from __future__ import annotations

from pathlib import Path

from kiro_crew.crew_log.schema import KIND_SESSION
from kiro_crew.crew_log.store import CrewLog, segment_paths, unit_header_created_at


def open_session_log(session_id: str) -> CrewLog | None:
    """This session's crew log opened for READING, or ``None`` when it has none.

    Never repairs. Repair appends closers and takes write ownership, which
    belongs to the gateway resuming the session, and a read path that claimed it
    would refuse whenever the live writer holds it -- turning "show me this
    session" into an error for exactly the sessions that are running.
    """
    if not CrewLog.exists(KIND_SESSION, session_id):
        return None
    return CrewLog.open(KIND_SESSION, session_id)


def _log_identity(handle: CrewLog) -> tuple[str | None, int | None, int | None]:
    """The crew log file's creation identity, size and mtime from stat calls alone.

    A reuse (:func:`fold_session` ``since=``) folds new bytes onto a cached
    checkpoint only when the file it folds now is the SAME one the checkpoint
    came from. The identity combines three signals so no single one has to be
    unique on its own: the header's ``created_at`` (stamped once at create, so a
    recreated log under the same id gets a fresh value), and the file's device
    and inode (which differ when a freed inode is NOT reused, and, combined with
    ``created_at``, make a same-millisecond recreation onto a recycled inode the
    only colliding case -- itself near-impossible). This catches what the seq
    guard cannot: a recreated log that has already grown PAST the cached seq.
    ``None`` is "unknown identity" and never matches, so a header without the
    field or a stat failure falls back to the safe full rebuild. Size and mtime
    together also prevent a same-size in-place rewrite from taking the unchanged
    fast path. A successful stat still returns both when the header lacks
    ``created_at``.

    BOTH identity signals are read from the file on disk on every call, and
    neither comes from *handle*'s own parsed header. That header was parsed when
    the handle was opened, so it keeps answering for the file that existed then --
    which would leave this comparing device and inode alone across exactly the
    recreation it exists to catch, and a just-freed inode is commonly handed
    straight back.

    The size and mtime cover EVERY segment the walk would read, not only the
    newest one that ``handle.path`` names: size is the sum and mtime the newest
    across the segment set, with the count folded into the sum so a whole
    segment appearing or vanishing (rotation, retention) can never cancel out
    against another's growth. The fingerprint must cover exactly what the walk
    consumes -- a fingerprint narrower than the walk is how each round of this
    guard's history got the same finding back in a new spelling -- and it stays
    stat-only, because reading file contents per poll is the cost the fast path
    exists to avoid. A segment vanishing between the listing and its stat is a
    file set in motion, and reads as "unknown": the fold then walks, which is
    the safe answer to a moving target.
    """
    return _unit_log_identity(handle.kind, handle.id, handle.path)


def _unit_log_identity(
    kind: str, unit_id: str, path: Path | None = None
) -> tuple[str | None, int | None, int | None]:
    """:func:`_log_identity` for a unit named by kind and id, without opening it.

    *path* is the segment whose device and inode name the file; it defaults to
    the newest segment. Opening a handle settles a torn tail, which is a write, so
    a reader that only asks "is this still the same log" uses this instead.
    """
    created_at = unit_header_created_at(kind, unit_id)
    try:
        segments = segment_paths(kind, unit_id)
        if path is None:
            if not segments:
                return None, None, None
            path = segments[-1]
        stat = path.stat()
        total_size = 0
        newest_mtime = 0
        for segment in segments:
            seg_stat = segment.stat()
            total_size += seg_stat.st_size
            newest_mtime = max(newest_mtime, seg_stat.st_mtime_ns)
        # The count rides in the size so "one segment of N bytes" and "two
        # segments of N bytes total" cannot fingerprint alike even at one stat's
        # granularity, and an empty listing stays distinct from an unstatable one.
        total_size = total_size + (len(segments) << 48)
    except OSError:
        return None, None, None
    if created_at is None:
        return None, total_size, newest_mtime
    return f"{created_at}:{stat.st_dev}:{stat.st_ino}", total_size, newest_mtime


def log_origin(handle: CrewLog) -> str | None:
    """The crew log file's creation identity for *handle*, or ``None``.

    The identity half of :func:`_log_identity` -- see there for what the value
    means and why it is read from disk on every call.

    Public because the on-disk savepoints (:mod:`kiro_crew.crew_log.checkpoint`)
    record this same value and must compare it the same way. Two spellings of "is
    this the same log" would be free to disagree, and the one that said yes too
    often would fold a retired file's state onto a live one's bytes.
    """
    return _log_identity(handle)[0]
