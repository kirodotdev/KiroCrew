"""Read the SEL back: verify its chains, and serve recent events, newest first.

``verify_integrity`` walks every closed segment and then the live log; ``recent`` reads
backward from the tail and stops once a window or a limit is met. Both open rotated
segments through a read-side pin on ``security_events.d`` and a per-file funnel that
refuses a planted link, FIFO or swapped directory, and ``recent`` admits a segment only
once one of its records verifies under this log's key.

Each function here whose first parameter is ``self`` is a ``SecurityEventLog`` method:
the class binds it by name, so it reads and writes the log's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller. Module
names this code reads are patched through ``kiro_crew.sel``, which reaches every member
of the family that binds them (``kiro_crew._facade_mirror``).

Instance fields read: ``_path``, ``_segment_dir``.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, TYPE_CHECKING, Literal, NamedTuple, overload

from kiro_crew import platform_compat

if TYPE_CHECKING:
    from kiro_crew.sel import SecurityEventLog

#: The SEL's own logger: this code logs under ``kiro_crew.sel``.
logger = logging.getLogger("kiro_crew.sel")


# Tail-read chunk for recent(): the log is append-only, so the newest records
# live at the END. Reading backward in chunks keeps a bounded read bounded
# instead of loading the whole segment (the old recent() did
# read_text().splitlines(), i.e. a 4.09 GB allocation to print 20 lines).
_TAIL_CHUNK_BYTES = 256 * 1024


# Ceiling on bytes held while looking for a line boundary in the backward reader.
# A real SEL record is a few hundred bytes; anything past this has no newline in
# it, which means a truncated write or a file someone else placed there. Bounds
# the memory an attacker-influenced segment can make a reader allocate.
_MAX_LINE_BYTES = 4 * 1024 * 1024


@overload
def verify_integrity(self: SecurityEventLog) -> tuple[int, int]: ...


@overload
def verify_integrity(self: SecurityEventLog, *, detailed: Literal[True]) -> SelVerification: ...


def verify_integrity(
    self: SecurityEventLog, *, detailed: bool = False
) -> tuple[int, int] | SelVerification:
    """Verify the HMAC chains. Returns (total_entries, valid_entries).

    Every rotated segment (oldest first) is verified BEFORE the live log.
    Each file is an independent chain that starts from genesis, so a segment
    deleted by retention cannot make a surviving one look tampered — which is
    the property rotation had to preserve.

    There is deliberately no cross-file assertion. Forgery is caught per
    RECORD: a planted or rewritten segment cannot produce a valid HMAC because
    the key lives outside the log directory, so its entries are counted invalid
    here regardless of what any boundary record says. An earlier revision also
    checked a predecessor-hash claim carried in the rotation record; see
    :meth:`_rotation_event` for why that claim cannot be made true without
    serializing every append, and why a check that can fire on a benign cause
    is worse than no check.

    Segments are enumerated under a read-side directory pin: the
    segment dir that refused to pin (a planted link, or not a directory)
    contributes NOTHING here rather than being walked by name — which is
    what makes a swapped ``security_events.d`` fail closed instead of
    inflating ``total`` with another tree's files (a false tamper alarm).

    ``detailed=True`` adds the third outcome that refusal needs:
    ``history_verifiable=False`` with a ``reason`` when the
    directory refused to pin or was replaced mid-verification, because
    the rotated segments were not checked and "intact over the live log
    alone" must not be able to hide that. A directory that simply does
    not exist yet stays verifiable — a fresh install has no history to
    vouch for.
    """
    self.flush()  # ensure all queued events are on disk before verifying
    total = 0
    valid = 0
    pin, absent = _open_segment_dir(self._segment_dir)
    try:
        # Enumerate INSIDE the try: a mid-readdir I/O error must still
        # unwind through the finally that releases the pin, not strand
        # the descriptor on every failed request.
        segments = self._segments_oldest_first(pin=pin) if pin is not None else []
        for path in segments + [self._path]:
            if pin is None or pin.fd is None:
                # Pathname walks may see a segment retention-deleted
                # between enumeration and here; that is benign. An
                # fd-pinned walk must not re-touch the PATH at all — a
                # swap racing the recheck would skip real segments here
                # and the fd-relative open below handles absence itself.
                if not path.exists():
                    continue
            file_total, file_valid = self._verify_file(path, pin=pin)
            total += file_total
            valid += file_valid
    finally:
        if pin is not None:
            pin.close()
    if not detailed:
        return total, valid
    reason = ""
    if pin is None:
        if not absent:
            # absent is what the PIN itself observed, so a concurrent
            # repair removing a refused link cannot reclassify the
            # refusal as benign absence after the fact.
            reason = "segment directory refused to pin (planted link?)"
    elif not pin.matches(self._segment_dir):
        # matches() is lstat-based, so it still answers after close();
        # a mid-verification swap leaves the totals computed from the
        # pinned directory while the tree on disk is now a different one.
        reason = "segment directory was replaced during verification"
    return SelVerification(total=total, valid=valid, history_verifiable=not reason, reason=reason)


@overload
def _reader_handle(
    self: SecurityEventLog, path: Path, *, binary: Literal[True], pin: _SegmentDirPin | None = ...
) -> IO[bytes] | None: ...


@overload
def _reader_handle(
    self: SecurityEventLog, path: Path, *, binary: Literal[False], pin: _SegmentDirPin | None = ...
) -> IO[str] | None: ...


def _reader_handle(
    self: SecurityEventLog, path: Path, *, binary: bool, pin: _SegmentDirPin | None = None
) -> IO[str] | IO[bytes] | None:
    """Reader-side open policy, shared by both readers.

    The LIVE log opens with ordinary ``open()``: it is a fixed
    module-owned path, never an enumerated name, and its WRITER follows
    an operator's symlink (``O_CREAT | O_APPEND``), so a reader that
    refuses one turns the live log write-only — events keep landing
    while every read reports empty and ``verify_integrity`` counts a
    clean ``(0, 0)``. Rotated segments ARE enumerated, attacker-nameable
    entries, and take the descriptor-validating funnel
    (:func:`_open_segment`), which resolves them relative to *pin* when
    the read holds one — a segment dir swapped after the pin
    cannot redirect the open.
    """
    if path == self._path:
        try:
            if binary:
                return open(path, "rb")
            return open(path, encoding="utf-8")
        except OSError:
            logger.warning("SEL could not open the live log %s", path.name, exc_info=True)
            return None
    fd = _open_segment(path, pin=pin)
    if fd is None:
        return None
    if binary:
        return os.fdopen(fd, "rb")
    return os.fdopen(fd, encoding="utf-8")


def _verify_file(
    self: SecurityEventLog, path: Path, *, pin: _SegmentDirPin | None = None
) -> tuple[int, int]:
    """Verify one log file's chain. Returns (total, valid).

    Streamed line by line so a capped-but-large segment is never loaded whole.
    The handle comes from :meth:`_reader_handle`, so a symlink or FIFO
    planted under a segment name is refused here rather than followed; a
    refused file is not audit history and counts as (0, 0).
    """
    total = 0
    valid = 0
    prev_hash = ""
    handle = self._reader_handle(path, binary=False, pin=pin)
    if handle is None:
        return 0, 0
    with handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            total += 1
            try:
                data = json.loads(line)
                stored_hash = data.pop("entry_hash", "")
                if data.get("prev_hash", "") != prev_hash:
                    logger.warning("SEL chain break at entry %d of %s", total, path.name)
                    prev_hash = stored_hash
                    continue
                # Re-attach the hash: _record_signature_matches owns the one
                # payload/compare implementation both readers use.
                if self._record_signature_matches({**data, "entry_hash": stored_hash}):
                    valid += 1
                else:
                    logger.warning("SEL HMAC mismatch at entry %d of %s", total, path.name)
                prev_hash = stored_hash
            except (json.JSONDecodeError, Exception):
                logger.warning("SEL parse error at entry %d of %s", total, path.name)
    return total, valid


def recent(
    self: SecurityEventLog,
    limit: int = 100,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[dict]:
    """Return the most recent events, newest first.

    ``since`` / ``until`` bound the window (inclusive ``since``, exclusive
    ``until``) so a caller can ask "what happened in the last two hours"
    instead of guessing a count large enough to reach back that far.

    The read is bounded on BOTH ends. Files are scanned backward from the
    tail in chunks — never loaded whole, which is what made a large log
    impractical to query — and the walk stops at the first record older than
    ``since`` because the log is append-ordered. Rotated segments are only
    opened when the live log has not already satisfied the request.

    Records whose timestamp cannot be parsed are returned when no window was
    asked for, and skipped when one was: a record that cannot be placed in
    time cannot be asserted to fall inside the window.
    """
    self.flush()  # surface any queued-but-unwritten events
    result: list[dict] = []
    if limit <= 0:
        return result
    windowed = since is not None or until is not None
    # Newest first: the live log, then rotated segments newest to oldest.
    # Segments are discovered LAZILY (only if the live log has not already
    # satisfied the request), so the common tail read touches one file.
    # The segment dir is PINNED for the whole walk so a directory
    # swapped mid-read cannot redirect later opens; the early returns below
    # all unwind through the finally that releases the pin.
    pin, _absent = _open_segment_dir(self._segment_dir)
    try:
        for path in self._read_sources_newest_first(pin=pin):
            if pin is None or pin.fd is None:
                if not path.exists():
                    continue
            for line in self._iter_lines_backward(path, pin=pin):
                try:
                    data = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(data, dict):
                    continue
                if windowed:
                    ts = _parse_timestamp(data.get("timestamp", ""))
                    if ts is None:
                        continue
                    if until is not None and ts >= until:
                        continue
                    if since is not None and ts < since:
                        # Append-ordered log: everything from here back is older.
                        return result
                result.append(data)
                if len(result) >= limit:
                    return result
    finally:
        if pin is not None:
            pin.close()
    return result


def _read_sources_newest_first(
    self: SecurityEventLog, *, pin: _SegmentDirPin | None
) -> Iterator[Path]:
    """The live log, then ADMISSIBLE segments newest to oldest.

    A segment is only handed to the read path once one of its records
    verifies under our HMAC key. ``security_events.d`` is created by this
    release, and it was not on the sensitive-path floor before it, so an agent
    could have pre-created the directory and left a segment-shaped JSONL of
    its own choosing in it; without this gate the upgrade would then present
    those forged records to every reader as audit events. Forging is what the
    chain key exists to prevent and the key lives outside the log directory,
    so a planted segment cannot produce a single valid signature.

    The check is ONE record per segment (the last, reached through the same
    backward reader), not the whole file: full-segment verification would undo
    the bounded read this release exists to provide, and one valid signature
    is already unforgeable. ``verify_integrity`` deliberately does NOT filter
    this way — it reports a rejected segment's entries as invalid instead of
    hiding them, because an audit tool must surface tampering rather than
    quietly drop the evidence.

    A generator so segments are neither listed nor opened when the live log
    already answered the caller.

    *pin* is the caller's read-side directory pin, owned and
    released by the caller. ``None`` means the directory refused to pin —
    a planted link, or not a directory — and the response is to offer NO
    segment sources rather than fall back to a by-name walk, which is
    exactly what a swapped directory exploits.
    """
    yield self._path
    if pin is None:
        # _open_segment_dir already logged the refusal at the severity
        # the cause deserves (a planted link warns; a directory that
        # simply does not exist yet — a fresh install — stays quiet).
        logger.debug("SEL offering no segment sources: the segment dir did not pin")
        return
    for path in reversed(self._segments_oldest_first(pin=pin)):
        if self._segment_is_signed_by_us(path, pin=pin):
            yield path
        else:
            logger.warning(
                "SEL refusing to read %s as audit history: no record in it is "
                "signed with this log's key (planted before upgrade?)",
                path.name,
            )


def _segment_is_signed_by_us(
    self: SecurityEventLog, path: Path, *, pin: _SegmentDirPin | None = None
) -> bool:
    """Whether *path* holds at least one record signed with our HMAC key."""
    for line in self._iter_lines_backward(path, pin=pin):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and self._record_signature_matches(data):
            return True
    return False


def _iter_lines_backward(
    self: SecurityEventLog, path: Path, *, pin: _SegmentDirPin | None = None
) -> Iterator[str]:
    """Yield *path*'s non-empty lines from last to first, reading in chunks.

    Same backward scan as :meth:`_read_last_hash`: only the tail is touched
    unless the caller keeps consuming, so reading 20 entries out of a
    size-capped segment costs one chunk rather than the whole file.

    The pending buffer is CAPPED at ``_MAX_LINE_BYTES``. Held bytes are
    whatever has not yet been split into a complete line, so a file with no
    newline in it would otherwise accumulate to its full size in memory --
    and this reader is pointed at attacker-influenced input: a segment planted
    before this release (when the directory was not fenced) is read here both
    by the time-range read and by segment admission. A single planted
    gigabyte-long unterminated line would exhaust the process. Hitting the cap
    ends the scan, so a caller sees the lines it already got and admission
    simply fails to find a signed record -- which is the safe direction.

    The handle comes from :meth:`_reader_handle`, so a symlink or FIFO
    planted under a segment name yields nothing here instead of being
    followed (or, for a FIFO, blocking the reader inside ``open``); the
    live log itself opens ordinarily, matching its writer. A read holding
    a directory pin resolves segments relative to it.
    """
    handle = self._reader_handle(path, binary=True, pin=pin)
    if handle is None:
        return
    try:
        with handle as f:
            f.seek(0, 2)
            pos = f.tell()
            buf = b""
            while pos > 0:
                read_start = max(pos - _TAIL_CHUNK_BYTES, 0)
                f.seek(read_start)
                buf = f.read(pos - read_start) + buf
                pos = read_start
                parts = buf.split(b"\n")
                if pos > 0:
                    # First element may be incomplete — defer it until the
                    # next chunk supplies its start.
                    buf = parts[0]
                    complete = parts[1:]
                else:
                    buf = b""
                    complete = parts
                for raw in reversed(complete):
                    stripped = raw.strip()
                    if stripped:
                        yield stripped.decode("utf-8", errors="replace")
                if len(buf) > _MAX_LINE_BYTES:
                    logger.warning(
                        "SEL stopping the backward scan of %s: over %d bytes with no "
                        "line boundary (truncated or planted file?)",
                        path.name,
                        _MAX_LINE_BYTES,
                    )
                    return
    except OSError:
        logger.warning("SEL could not read %s", path, exc_info=True)


def _open_segment(path: Path, *, pin: _SegmentDirPin | None = None) -> int | None:
    """Open *path* read-only as a validated ROTATED SEGMENT, or ``None``.

    The dirent check in :meth:`SecurityEventLog._segments_oldest_first` judges
    what a directory entry WAS when scanned; this opener judges what the name
    IS at open time, closing the scan->open window in which a substitute can
    be planted (the read-side TOCTOU). Segments are the enumerated,
    attacker-nameable surface; the LIVE log deliberately does not come here —
    its writer follows an operator's symlink, so its readers must too
    (:meth:`SecurityEventLog._reader_handle` owns that split).

    With a *pin* the final DIRECTORY hop is pinned too: the open (and
    the identity check below) resolve RELATIVE to the pinned descriptor where
    the platform has directory descriptors, so a ``security_events.d`` swapped
    after the pin cannot redirect the open into another tree; where it does
    not, the pin's directory identity is revalidated against the path first,
    refusing a swapped parent mid-read. The per-file funnel below is otherwise
    unchanged.

    Three layers, each covering what the previous one cannot:

    - ``O_NOFOLLOW`` fails a planted symlink at the open itself where the
      platform has it (``ELOOP`` on Linux/macOS; some BSDs say ``EMLINK`` or
      ``EFTYPE``). ``O_NONBLOCK`` is load-bearing for a planted FIFO: without
      it the read-side ``os.open`` blocks until a writer appears, before any
      descriptor-level check is reachable; it has no effect on regular files.
      Both degrade to 0 via ``getattr`` on Windows, and ``O_BINARY`` keeps the
      descriptor out of Windows text mode there.
    - ``fstat`` on the DESCRIPTOR — which nothing can swap afterwards —
      requires a regular file, refusing FIFOs, directories, and device nodes
      on every platform.
    - An identity check makes the flag degradation safe: the opened file must
      be exactly the file *path* names right now (``lstat`` dev/ino equal to
      the descriptor's). A symlink's own ``lstat`` identity never equals its
      target's, so a symlink swap is refused even where ``O_NOFOLLOW`` does
      not exist, and a mid-swap mismatch fails closed. (On a filesystem that
      reports ``st_ino == 0`` for everything, the comparison degrades to the
      flag and type checks; such filesystems do not support symlinks.)

    A HARDLINK is deliberately not judged here: a hardlink IS the regular
    file its name points at, indistinguishable at the per-file level, and a
    link count above one is routinely legitimate (``rsync --link-dest`` and
    ``cp -al`` backups). Nor is a planted second name deduplicated anywhere
    else — every key such a decision could use is attacker-controlled, so a
    dedupe would let a planted alias DISPLACE the real segment (see the scan
    comment in :meth:`SecurityEventLog._segments_oldest_first`). An alias's
    bounded worst case is repeating already-signed records.

    Returns a descriptor at position 0, ready for ``os.fdopen``; ownership
    passes to the caller. A refusal warns in the same "planted link?" style as
    the dirent filter, so the two layers read consistently in the log.
    """
    rel_fd = pin.fd if pin is not None else None
    if pin is not None and rel_fd is None and not pin.matches(path.parent):
        # Identity pin (no directory descriptors on this platform): the parent
        # names a directory other than the one the read pinned, so the path may walk
        # through a swapped-in link. Fail closed.
        logger.warning(
            "SEL refusing audit file %s: the segment dir was replaced mid-read "
            "(planted link?); it is not audit history",
            path.name,
        )
        return None
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )
    try:
        if rel_fd is not None:
            fd = os.open(path.name, flags, dir_fd=rel_fd)
        else:
            fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno in (
            errno.ELOOP,
            getattr(errno, "EMLINK", -1),
            getattr(errno, "EFTYPE", -1),
        ):
            logger.warning(
                "SEL refusing audit file %s: it is a symlink (planted link?); "
                "it is not audit history",
                path.name,
            )
        elif exc.errno == errno.ENOENT:
            # Deleted by retention between the caller's exists() check and
            # this open — a normal race, not an incident.
            logger.debug("SEL audit file %s vanished before open", path.name)
        else:
            logger.warning("SEL could not open audit file %s", path.name, exc_info=True)
        return None
    try:
        opened = os.fstat(fd)
        if rel_fd is not None:
            named = os.stat(path.name, dir_fd=rel_fd, follow_symlinks=False)
        else:
            named = os.lstat(path)
    except OSError:
        os.close(fd)
        logger.warning("SEL could not stat opened audit file %s", path.name, exc_info=True)
        return None
    if not stat.S_ISREG(opened.st_mode):
        os.close(fd)
        logger.warning(
            "SEL ignoring non-regular audit file %s (planted link?); it is not audit history",
            path.name,
        )
        return None
    if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
        os.close(fd)
        logger.warning(
            "SEL refusing audit file %s: it is not the file its name points "
            "at (planted link?); it is not audit history",
            path.name,
        )
        return None
    return fd


#: Whether this platform can pin a directory BY DESCRIPTOR: ``O_DIRECTORY``
#: plus ``dir_fd``-relative open and no-follow stat. Where those hold,
#: fd-taking ``os.listdir`` holds too (same fdopendir capability), so it is
#: not probed separately. The POSIX branch of the read-side pin below is
#: built on this; Windows has none of it, so its pin carries identity
#: revalidation instead (see ``_SegmentDirPin``).
# supports_dir_fd holds FUNCTION OBJECTS — a string membership test is always
# False, which would silently strand every platform on the identity pin. And
# the no-follow lstat flavor is NOT a member even where it works: its
# dir_fd-relative spelling is stat(follow_symlinks=False), so THAT is what
# both the gate and the call sites use.
_PIN_BY_FD_SUPPORTED = (
    hasattr(os, "O_DIRECTORY") and os.open in os.supports_dir_fd and os.stat in os.supports_dir_fd
)


@dataclass
class _SegmentDirPin:
    """A read-side pin on the segment directory.

    ``fd`` is the strong form — an open directory descriptor nothing can swap
    afterwards — and every per-file OPEN held by the read resolves RELATIVE
    to it, immune to a later swap of the path. On every platform the pin also
    carries the directory's ``identity`` (the fd's ``fstat`` where there is
    one, the ``lstat`` otherwise), revalidated after enumeration and before
    each child open, so a directory swapped for a link — or for a different
    real directory, whose identity also differs — is refused mid-read. The
    residual is the gap between one revalidation and the next resolution
    through the name, which on fd platforms the descriptor-relative opens
    close and elsewhere the rotation-time repair (``_ensure_segment_dir``)
    bounds; the same ``st_ino == 0`` degradation note as
    :func:`_open_segment` applies.
    """

    fd: int | None
    identity: tuple[int, int]

    def close(self) -> None:
        """Release the descriptor, if this pin holds one."""
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def matches(self, path: Path) -> bool:
        """Whether *path* still names the pinned directory (False on any error)."""
        try:
            current = os.lstat(path)
        except OSError:
            return False
        return (current.st_dev, current.st_ino) == self.identity


def _open_segment_dir(path: Path) -> tuple[_SegmentDirPin | None, bool]:
    """Pin the segment DIRECTORY a read is about to walk.

    The directory-level analog of :func:`_open_segment`: that function pins
    the final component, this one pins the hop above it. Without it, a
    ``security_events.d`` replaced with a link (planted before this release,
    or swapped while a read is in flight) redirected enumeration AND every
    per-file open into another tree — whose segment-shaped regular files pass
    every per-file check, because they are regular files.

    Returns ``(pin, absent)``. ``pin is None`` is a REFUSAL, not a fallback:
    callers treat it as "no segments are readable" and fail closed, because
    walking the path anyway is exactly what a swapped directory exploits.
    *absent* is CONFIRMED absence (ENOENT) as the pin itself observed it —
    the one benign shape, a fresh install, which yields the same empty
    outcome an unpinned scan produces. A caller reporting on the
    read must keep THAT classification instead of re-stating the path: a
    concurrent repair can remove a refused link before anyone looks again,
    and the refusal would silently reclassify as absence.
    Judged, in the same three-layer spirit:

    - ``lstat`` + ``is_link_or_junction`` (junction-aware on Windows) refuses
      a linked directory outright, and ``S_ISDIR`` refuses a non-directory.
    - On a descriptor-capable platform the directory is then opened with
      ``O_DIRECTORY | O_NOFOLLOW | O_NONBLOCK``, so a link swapped in after
      the ``lstat`` fails the open itself.
    - The descriptor's ``fstat`` identity must equal what the name's ``lstat``
      said — the same mid-swap mismatch refusal as the per-file funnel.
    """
    try:
        named = os.lstat(path)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            # No segment dir yet: nothing was ever rotated, which the callers
            # already treat as "no segments".
            logger.debug("SEL segment dir %s does not exist yet", path)
            return None, True
        logger.warning("SEL could not stat the segment dir %s", path, exc_info=True)
        return None, False
    if platform_compat.is_link_or_junction(path):
        logger.warning(
            "SEL refusing the segment dir %s: it is a link (planted link?); "
            "it is not audit history",
            path,
        )
        return None, False
    if not stat.S_ISDIR(named.st_mode):
        logger.warning("SEL refusing the segment dir %s: it is not a directory", path)
        return None, False
    if not _PIN_BY_FD_SUPPORTED:
        return _SegmentDirPin(fd=None, identity=(named.st_dev, named.st_ino)), False
    # O_DIRECTORY types as POSIX-only; the capability constant above already
    # gated this branch to platforms that have it.
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, getattr(errno, "EMLINK", -1), getattr(errno, "EFTYPE", -1)):
            logger.warning(
                "SEL refusing the segment dir %s: it is a link (planted link?); "
                "it is not audit history",
                path,
            )
        elif exc.errno == errno.ENOENT:
            # Vanishing AFTER lstat already saw it is not fresh-install
            # absence — the directory WAS there, and its removal before the
            # pin landed is interference the refusal must surface.
            logger.warning(
                "SEL refusing the segment dir %s: it vanished between the lstat "
                "and the pin (planted link?)",
                path,
            )
            return None, False
        else:
            logger.warning("SEL could not pin the segment dir %s", path, exc_info=True)
        return None, False
    try:
        opened = os.fstat(fd)
    except OSError:
        os.close(fd)
        logger.warning("SEL could not stat the pinned segment dir %s", path, exc_info=True)
        return None, False
    if not stat.S_ISDIR(opened.st_mode):
        os.close(fd)
        logger.warning("SEL refusing the segment dir %s: it is not a directory", path)
        return None, False
    if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
        os.close(fd)
        logger.warning(
            "SEL refusing the segment dir %s: it is not the directory its name "
            "points at (planted link?); it is not audit history",
            path,
        )
        return None, False
    return _SegmentDirPin(fd=fd, identity=(opened.st_dev, opened.st_ino)), False


class SelVerification(NamedTuple):
    """``verify_integrity(detailed=True)``'s result.

    ``total``/``valid`` keep the plain two-number contract; the added pair
    states whether the audit HISTORY was verifiable at all. A segment
    directory that refused to pin (a planted link) or was replaced
    mid-verification leaves the rotated segments UNCHECKED, and a caller
    whose job is to surface tampering must not report that run as "intact"
    over the live log alone. A directory that simply does not exist yet
    stays verifiable — a fresh install has no history to vouch for.
    """

    total: int
    valid: int
    history_verifiable: bool
    reason: str


# Named by the path callers import them from: a repr, a pickle or a traceback names a
# class as ``module.qualname``, and that text names ``kiro_crew.sel``.
_SegmentDirPin.__module__ = "kiro_crew.sel"
SelVerification.__module__ = "kiro_crew.sel"


def _parse_timestamp(raw: object) -> datetime | None:
    """Parse a SEL record timestamp into an aware UTC datetime, or ``None``.

    Records are written with ``datetime.now(timezone.utc).isoformat()``, but a
    hand-edited or forwarded record may carry a ``Z`` suffix or no offset at all.
    ``fromisoformat`` only accepts ``Z`` from Python 3.11, and the package
    supports 3.10, so the suffix is normalized here. A naive timestamp is read as
    UTC — the only offset SEL ever writes — so it stays comparable with an aware
    window bound instead of raising.
    """
    if not isinstance(raw, str) or not raw:
        return None
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
