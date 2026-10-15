"""Chain a batch onto the SEL's live log, and keep the chain tip anchored to that file.

Each batch is stamped with the HMAC chain and appended only after the opened file is
checked by descriptor against the one the tip was read from; a log another process
rotated or appended to is re-anchored and the batch chained again, and an append that
cannot be made with a valid link raises ``SelChainContention``. The tip is recovered
from the last complete record, skipping a truncated tail.

Each function here whose first parameter is ``self`` is a ``SecurityEventLog`` method:
the class binds it by name, so it reads and writes the log's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller. Module
names this code reads are patched through ``kiro_crew.sel``, which reaches every member
of the family that binds them (``kiro_crew._facade_mirror``).

Instance fields written: ``_last_hash``, ``_live_seen``.

Instance fields read: ``_path``.
"""

from __future__ import annotations

import errno
import json
import logging
import os
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew.sel_chain_lock import _on_event_loop

if TYPE_CHECKING:
    from kiro_crew.sel import SecurityEvent, SecurityEventLog

#: The SEL's own logger: this code logs under ``kiro_crew.sel``.
logger = logging.getLogger("kiro_crew.sel")


class _ChainTipBeyondBound(OSError):
    """The chain tip lies past a bounded tail read.

    An OSError subclass so the append path's existing fail-closed handling
    covers it, but a distinct type so _read_last_hash's fail-soft ``except
    OSError`` (which exists for genuine read errors and returns "") cannot
    swallow it and hand back a genesis tip.
    """


# Re-chain attempts when a concurrent rotation replaces the live log between
# chaining and appending. Each collision is a lost microsecond race; after this
# many the record is written unguarded, because a missing audit record is worse
# than one suspect chain link.
_APPEND_RETRIES = 3


class _LiveLogReplaced(Exception):
    """The live log was swapped by another process between chaining and appending.

    Internal control flow only: raised by ``_append_lines_locked`` BEFORE anything
    is written and handled by ``_append_chained_locked``, which re-anchors and
    chains the batch again. Never escapes the module.
    """


class SelChainContention(OSError):
    """Repeated foreign writes prevented a correctly-chained append.

    Deliberately an ``OSError``: this class already has one answer for an audit
    that cannot be written, and it is not "write it wrong". A ``critical=True``
    caller re-raises and refuses the action it was about to audit (audit-or-deny),
    and the background writer drops the batch with a warning exactly as it does
    for a full disk. Raising the same TYPE those paths already handle keeps that
    behaviour without a new branch at either site.

    Reaching this needs a foreign write to land between our chaining and our open
    on every one of ``_APPEND_RETRIES + 1`` attempts.
    """


# Named by the path callers import them from: a traceback, or a warning logged with
# ``exc_info``, renders each raised class as ``module.qualname``, and that text names
# ``kiro_crew.sel``.
_ChainTipBeyondBound.__module__ = "kiro_crew.sel"
_LiveLogReplaced.__module__ = "kiro_crew.sel"
SelChainContention.__module__ = "kiro_crew.sel"


def _live_log_moved_on(previous: tuple[int, int, int], current: tuple[int, int, int]) -> bool:
    """Whether the live log has moved off the file+position *previous* describes.

    Both arguments are ``(st_dev, st_ino, st_size)``. ONE predicate serves both the
    pre-append check and the post-open guard, because they are asking the same
    question and answering it differently is how a stale tip survives a collision:
    a detector that fires on growth paired with a re-anchor that only reacts to a
    shrink leaves the retry writing the very ``prev_hash`` the collision proved
    wrong.

    Two independent signals:

    * a DIFFERENT inode -- the file was replaced (a rotation). Skipped when either
      inode is 0, which some Windows filesystems report instead of a usable index.
    * a DIFFERENT size -- somebody appended, so the file ends somewhere other than
      where we chained from. This also covers the case no inode comparison can see: a
      rotator anchored on an ABSENT replacement has no inode, and only the size
      reveals that another process already put a record there.

    Our OWN writes never trip this. The anchor is refreshed from the fd's
    post-write ``fstat`` on every append, so a difference means somebody else.
    """
    prev_dev, prev_ino, prev_size = previous
    cur_dev, cur_ino, cur_size = current
    if prev_ino and cur_ino and (prev_ino, prev_dev) != (cur_ino, cur_dev):
        return True
    return prev_size != cur_size


def _identity_changed(expect: tuple[int, int, int], actual: os.stat_result) -> bool:
    """Whether an opened fd has diverged from the file+position *expect* describes.

    Thin adapter over :func:`_live_log_moved_on` so the guard and the pre-append
    check cannot drift apart.
    """
    return _live_log_moved_on(expect, (actual.st_dev, actual.st_ino, actual.st_size))


def _append_chained_locked(self: SecurityEventLog, events: list[SecurityEvent]) -> None:
    """Chain *events* onto the live log, re-chaining if it moves under us.

    Caller holds the cross-process chain lock AND ``_lock``. The tip is
    rolled back on any failure so a record never chains off a hash that was
    not persisted.

    The retry survives under the chain lock because the lock orders sibling
    APPENDS, not everything that can move the file: this instance's cached
    tip is stale when a sibling appended before we took the lock, and a
    sibling's ROTATION — serialized by the rotation lock, not this one —
    can rename the file between our chaining and our open. In both cases
    the append VALIDATES the file it opened and re-chains — see
    :meth:`_append_lines_locked`.

    EVERY attempt is validated, including the last. When the retries are
    exhausted the batch is NOT written; :class:`SelChainContention` is raised.

    An earlier revision wrote the final attempt unguarded, reasoning that a
    missing audit record is worse than one suspect chain link. That was wrong
    twice over. A knowingly-broken link does not read as "one imperfect
    record" -- it makes ``security verify`` report the log as COMPROMISED, and
    an investigator cannot tell that false signal apart from real tampering.
    This PR already deleted the rotation record's predecessor-hash claim on
    exactly that principle (a check that can fire on a benign cause is worse
    than no check); writing a bad link is the same mistake with the sign
    flipped. And it invented a third behaviour: this class already answers an
    unwritable audit by DENYING the action (``critical=True``, audit-or-deny)
    or by dropping the batch with a warning (best-effort) -- never by writing
    something it knows to be corrupt.

    Raising an ``OSError`` subclass is what routes it correctly with no new
    plumbing: a critical caller re-raises and refuses the action it was about
    to audit, and the background writer treats it exactly as it already treats
    a full disk.
    """
    for attempt in range(_APPEND_RETRIES + 1):
        prev_last_hash = self._last_hash
        lines = self._chain_locked(events)
        try:
            self._append_lines_locked(lines, expect=self._live_seen)
            return
        except _LiveLogReplaced:
            # The guard proved the file moved on between our chaining and our
            # open, and the records were never written. Roll the tip back, then
            # re-anchor UNCONDITIONALLY -- the question the predicate would ask
            # has already been answered by the collision itself.
            self._last_hash = prev_last_hash
            logger.info(
                "SEL live log moved on mid-append; re-chaining this batch (attempt %d of %d)",
                attempt + 1,
                _APPEND_RETRIES + 1,
            )
            self._reanchor_now()
        except OSError:
            self._last_hash = prev_last_hash  # nothing persisted — roll back
            raise
    raise SelChainContention(
        f"could not append {len(events)} SEL event(s) with a valid chain link "
        f"after {_APPEND_RETRIES + 1} attempts: another process kept writing to "
        "the live log between chaining and appending"
    )


def _chain_locked(self: SecurityEventLog, events: list[SecurityEvent]) -> list[str]:
    """Stamp the HMAC chain onto *events* and return their JSONL lines.

    Advances ``_last_hash`` per event. Caller holds ``_lock`` and is
    responsible for rolling the tip back if the write does not land.
    """
    lines: list[str] = []
    for event in events:
        event.prev_hash = self._last_hash
        event.entry_hash = self._compute_hash(event)
        lines.append(json.dumps(asdict(event)) + "\n")
        self._last_hash = event.entry_hash
    return lines


def _append_lines_locked(
    self: SecurityEventLog, lines: list[str], *, expect: tuple[int, int, int] | None = None
) -> None:
    """Append already-chained JSONL *lines* to the live log. Caller holds ``_lock``.

    ``expect`` is the identity of the file whose tip *lines* were chained from.
    When given, the file actually opened is validated BY FD before anything is
    written, and :class:`_LiveLogReplaced` is raised if it is a different file.

    This is what makes a lock-free append safe against a concurrent rotation,
    and why the check is on the FD rather than on the path. Two outcomes, both
    correct:

    * the rename has NOT happened yet -- we hold the old inode, the identity
      matches, we write, and the winner's rename carries our record into the
      segment still correctly chained off the tip we used;
    * the rename HAS happened -- ``O_CREAT`` gave us a brand-new file with a
      different inode, we notice before writing, and the caller re-anchors and
      re-chains instead of leaving a record whose ``prev_hash`` lives in
      another file.

    A path ``stat`` cannot do this: whatever it observed may be replaced before
    the ``open`` that follows it.
    """
    # Use os.open with explicit 0o600 mode to prevent other users
    # from reading the security audit log.
    fd = os.open(
        self._path,
        os.O_CREAT | os.O_APPEND | os.O_WRONLY,
        0o600,
    )
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        actual = os.fstat(f.fileno())
        if expect is not None and _identity_changed(expect, actual):
            raise _LiveLogReplaced("live log was replaced between chaining and appending")
        # If a crash mid-append left a truncated tail line WITHOUT a
        # trailing newline, writing directly (O_APPEND) would glue
        # this record onto the corrupt fragment, forming a single
        # unparseable line. _read_last_hash() recovers the correct
        # prev_hash from the last complete record, but that glued
        # line stays unreadable by verify_integrity — so the new
        # event, though correctly chained, is orphaned from every
        # parseable record. Insert a newline boundary first so the
        # new record starts on a fresh, parseable line. We do NOT
        # truncate the corrupt fragment: the SEL log is append-only
        # forensic evidence, and the fragment is preserved as its
        # own (skipped) line.
        if self._ends_without_newline():
            f.write("\n")
        f.write("".join(lines))
        f.flush()
        # Record what we wrote to from the FD, not the path: if a rotation
        # renames the path immediately after this write, the file we actually
        # appended to is the one we must remember, so the NEXT append's guard
        # can see that it has moved on.
        written = os.fstat(f.fileno())
    # Ensure permissions are correct even if file pre-existed with
    # wrong mode (e.g. created by an older version). POSIX repair only,
    # deliberately still NOT ``platform_compat.restrict_to_owner``: on POSIX
    # that helper IS this exact call, so a swap would add only the Windows
    # owner-only DACL — and this append path can run inline on a caller's
    # thread that may be the asyncio event loop (the ``critical=True``
    # audit-or-deny write, and the fallback taken when the writer thread
    # cannot start, see ``_may_rotate``), where a DACL write to a UNC or
    # mapped-drive path costs an unbounded SMB round-trip. Adopting the
    # helper here therefore means first deciding what a non-local volume
    # gets, the way ``write_config_atomically`` gates its own lockdown on
    # ``windows_acl.volume_is_local``; until that is settled the log keeps
    # whatever DACL it inherits on Windows.
    try:
        os.chmod(self._path, 0o600)  # lockdown-ok: unbounded SMB round-trip on the loop
    except OSError:
        logger.warning(
            "Failed to enforce 0o600 permissions on SEL audit log %s", self._path, exc_info=True
        )
    self._live_seen = (written.st_dev, written.st_ino, written.st_size)


def _live_size(self: SecurityEventLog) -> int:
    """Size of the live log on disk, or 0 when it is absent/unreadable."""
    try:
        return self._path.stat().st_size
    except OSError:
        return 0


def _live_identity(self: SecurityEventLog) -> tuple[int, int, int]:
    """``(st_dev, st_ino, st_size)`` of the live log, zeros when absent."""
    try:
        st = self._path.stat()
    except OSError:
        return (0, 0, 0)
    return (st.st_dev, st.st_ino, st.st_size)


def _probe_live(self: SecurityEventLog) -> tuple[int, int, int] | None:
    """:meth:`_live_identity`, but ``None`` when a failed stat leaves it unknown."""
    try:
        st = self._path.stat()
    except OSError as exc:
        return (0, 0, 0) if exc.errno == errno.ENOENT else None
    return (st.st_dev, st.st_ino, st.st_size)


def _reanchor_if_replaced(self: SecurityEventLog) -> int:
    """Refresh the cached chain tip if the live log was replaced. Returns its size.

    Caller holds ``_lock``. This is the ONE stat the rotation check already
    needed, so detecting a foreign rotation is free.

    Why it is needed: several processes share one data home and append to this
    file, each caching its own tip in ``_last_hash`` and never re-reading it.
    When ANOTHER process rotates, this process's next append usually does not
    see the cap at all (the live log is small and fresh), so it never enters
    the rotation window — and it would chain its record off a tip that now
    lives inside the closed segment. Unlike the pre-existing interleaving
    race, which needs two appends to actually collide, that break is
    GUARANTEED for every process after every foreign rotation, so rotation
    must not leave it standing.

    Detection needs no lock and no extra syscall. The live log is append-only,
    so it can never shrink: a smaller size than we last wrote means the file
    was replaced. ``st_ino`` catches the same event where the filesystem
    reports it (a fresh file gets a new inode), and is skipped when either
    side reports 0 — some Windows filesystems do not supply a file index.

    The residual after this is a rotation landing
    between this stat and our append. Closing THAT means holding a
    cross-process lock across every audit write, a hot-path cost,
    so it stays measured rather than paid for here.
    """
    identity = self._live_identity()
    previous = self._live_seen
    self._live_seen = identity
    if previous is not None and _live_log_moved_on(previous, identity):
        logger.info(
            "SEL live log moved on since our last write (another process "
            "rotated or appended); re-reading the chain tip before appending"
        )
        # Bounded on the event-loop thread for the same reason as
        # _reanchor_now: only an already-corrupt multi-kilobyte tail needs
        # more than one tail chunk, and scanning it under a loop-side
        # critical audit would stall every session the loop serves.
        # Exhausting the bound raises (an OSError), failing the audit
        # closed instead of chaining from a guessed tip.
        self._last_hash = self._read_last_hash(max_chunks=1 if _on_event_loop() else None)
    return identity[2]


def _reanchor_now(self: SecurityEventLog) -> None:
    """Re-read the chain tip and the live log's identity, unconditionally.

    For the collision path, where the append guard has ALREADY proved the file
    moved on. There is nothing left to decide there, so this must not route
    through :meth:`_reanchor_if_replaced`'s predicate: an earlier revision did,
    and because that predicate reacted only to a shrink while the guard fired
    on growth too, a foreign APPEND was detected and then not corrected -- the
    retry re-chained from the same tip the collision had just invalidated and
    wrote it. Both now share one predicate, and this path skips it entirely.
    """
    self._live_seen = self._live_identity()
    # Bounded on the event-loop thread: a normal log yields the tip from a
    # single tail read, so only an already-corrupt multi-kilobyte tail could
    # walk further, and an unbounded backward scan there would stall chat and
    # the heartbeat for every session the loop serves. Exhausting the bound
    # raises (_ChainTipBeyondBound is an OSError), so a critical audit fails
    # closed instead of chaining from a guessed tip; callers off the loop
    # recover as far back as needed.
    self._last_hash = self._read_last_hash(max_chunks=1 if _on_event_loop() else None)


def _ends_without_newline(self: SecurityEventLog) -> bool:
    """True if the log's final byte is not a newline.

    A trailing byte other than ``\\n`` means the previous append was
    truncated (crash / partial write) and left an unterminated fragment.
    The writer uses this to insert a newline boundary before the next
    record so the new line stays independently parseable (see
    _flush_batch). Fail-soft: on any read error assume no separator is
    needed rather than crashing the writer.
    """
    try:
        with open(self._path, "rb") as f:
            f.seek(0, 2)
            if f.tell() == 0:
                return False
            f.seek(-1, 2)
            return f.read(1) != b"\n"
    except OSError:
        return False


def _read_last_hash(
    self: SecurityEventLog, path: Path | None = None, *, max_chunks: int | None = None
) -> str:
    """Return the entry_hash of the last COMPLETE record, or "" if none.

    Reads the live log by default; *path* names a closed segment instead
    (rotation reads the tip of the file it just renamed, which is immutable
    and therefore race-free).

    A crash mid-append can leave a truncated/partial final line. The old
    implementation wrapped the parse in a blanket ``except: return ""``, so
    one corrupt tail line silently restarted the HMAC chain from genesis —
    severing the tamper-evidence link and masking exactly the corruption
    the chain exists to detect. Instead we scan backward and skip an
    unparseable trailing line to chain from the last COMPLETE valid record;
    we only return "" when the log is genuinely empty/absent or contains no
    parseable record at all (nothing to chain from). Skipped corrupt tail
    lines are logged so the integrity concern is surfaced, not hidden.
    """
    target = path or self._path
    if not target.exists():
        return ""
    try:
        with open(target, "rb") as f:
            f.seek(0, 2)
            pos = f.tell()
            if pos == 0:
                return ""
            # Scan backward in chunks. ``buf`` holds bytes not yet split
            # into complete lines; while pos > 0 its first split element is
            # a possibly-partial line whose start lies in an earlier chunk,
            # so we hold it back until more is read (or we reach the file
            # start). This lets us walk past a truncated tail line to find
            # the last complete record.
            buf = b""
            skipped_corrupt = False
            chunks_read = 0
            while pos > 0:
                # ``max_chunks`` bounds how far back the scan may walk. The
                # append path passes 1 on the event-loop thread: a normal log
                # yields the tip from a single tail read, so only an already
                # corrupt multi-kilobyte tail could walk further, and doing
                # that on the loop would stall chat and the heartbeat.
                # Exhausting the bound raises rather than returning a guessed
                # tip, so the caller fails closed instead of chaining from the
                # wrong record. Callers off the loop pass None and recover as
                # far back as needed.
                if max_chunks is not None and chunks_read >= max_chunks:
                    raise _ChainTipBeyondBound(
                        "SEL chain tip lies beyond the bounded tail read "
                        "(corrupt tail); refusing to scan further here"
                    )
                read_start = max(pos - 4096, 0)
                f.seek(read_start)
                buf = f.read(pos - read_start) + buf
                pos = read_start
                chunks_read += 1
                parts = buf.split(b"\n")
                if pos > 0:
                    # First element may be incomplete — defer it.
                    buf = parts[0]
                    complete = parts[1:]
                else:
                    # Reached the file start: every element is complete.
                    buf = b""
                    complete = parts
                for line in reversed(complete):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except ValueError:
                        # Truncated/corrupt line — skip backward to the last
                        # complete record rather than resetting the chain to
                        # genesis. Flag it so the corruption is not hidden.
                        skipped_corrupt = True
                        logger.warning(
                            "SEL: skipping unparseable audit-log line while "
                            "resolving chain tip in %s; chaining from the last "
                            "complete record instead of resetting to genesis",
                            target,
                        )
                        continue
                    if not isinstance(data, dict):
                        # Parseable JSON but not a record object — treat as
                        # corrupt and keep scanning backward.
                        skipped_corrupt = True
                        logger.warning(
                            "SEL: skipping non-object audit-log line while "
                            "resolving chain tip in %s",
                            target,
                        )
                        continue
                    if skipped_corrupt:
                        logger.warning(
                            "SEL: recovered chain tip from an earlier complete "
                            "record after a corrupt/truncated tail in %s",
                            target,
                        )
                    return data.get("entry_hash", "")
        # No parseable record anywhere in the file — nothing to chain from.
        return ""
    except _ChainTipBeyondBound:
        # Not a read failure: the bound was deliberately hit. Propagate so
        # the caller fails closed rather than chaining from genesis.
        raise
    except OSError:
        logger.warning("SEL: failed to read chain tip from %s", target, exc_info=True)
        return ""
