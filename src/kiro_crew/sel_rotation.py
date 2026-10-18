"""Keep the SEL bounded: size rotation into closed segments, retention, and the age prune.

The live log is closed into ``security_events.d`` at ``_SEGMENT_MAX_BYTES`` under a
cross-process rotation lock, at most ``_SEGMENT_KEEP`` closed segments are kept, and
``prune`` drops whole segments and live-log records older than ``_RETENTION_DAYS``.
Whether the calling thread may rotate at all is the class's ``_may_rotate``.

Each function here whose first parameter is ``self`` is a ``SecurityEventLog`` method:
the class binds it by name, so it reads and writes the log's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller. Module
names this code reads are patched through ``kiro_crew.sel``, which reaches every member
of the family that binds them (``kiro_crew._facade_mirror``).

Instance fields written: ``_last_hash``.

Instance fields read: ``_lock``, ``_path``, ``_segment_dir``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import IO, TYPE_CHECKING

from kiro_crew import platform_compat
from kiro_crew.sel_reader import _parse_timestamp

if TYPE_CHECKING:
    from kiro_crew.sel import SecurityEventLog
    from kiro_crew.sel_reader import _SegmentDirPin

#: The SEL's own logger: this code logs under ``kiro_crew.sel``.
logger = logging.getLogger("kiro_crew.sel")


_RETENTION_DAYS = 365


# ── Size rotation ──
# The live log is closed (renamed into _SEGMENT_SUBDIR) once an append would
# push it past _SEGMENT_MAX_BYTES, and at most _SEGMENT_KEEP closed segments are
# retained, oldest deleted first. Without this the log grows without bound: a
# long-running install reached 4.09 GB, at which point the sanctioned reader is
# impractical and every append/read pays the size. The ceiling is
# _SEGMENT_MAX_BYTES * (_SEGMENT_KEEP + 1) -- ~256 MiB, roughly 500k events at
# the ~513 bytes/event measured on a real log. Age-based retention
# (_RETENTION_DAYS, swept by prune()) still applies on top;
# size rotation is what bounds the log BETWEEN those daily sweeps, which is the
# window that 4.09 GB accumulated in.
# Closed segment: security_events-<6-digit sequence>-<UTC stamp>.jsonl. The
# SEQUENCE, not the timestamp, orders segments: it is derived from the highest
# one still on disk plus one, so it keeps increasing across retention deletions
# and is immune to a clock step (an NTP correction that moved the wall clock
# backwards would otherwise mint a name sorting as the OLDEST segment, and
# retention would delete the newest log). The stamp is carried for humans
# reading the directory. Retention must never delete a merely prefix-matching
# file an operator parked here, so the pattern is matched in full.
_SEGMENT_SUBDIR = "security_events.d"


_SEGMENT_MAX_BYTES = 32 * 1024 * 1024


_SEGMENT_KEEP = 7


_SEGMENT_NAME_RE = re.compile(r"security_events-(?P<seq>\d{6,})-(?P<stamp>\d{8}T\d{6}Z)\.jsonl")


# Cross-process rotation mutex. It lives INSIDE the segment dir so it inherits
# that directory's sensitive-path fence: a lock file the agent could hold would
# let it suppress rotation and bring back the unbounded growth. Not a segment
# name, so _segments_oldest_first() never sees it and retention never deletes it.
_ROTATE_LOCK_FILE = ".rotate.lock"


# Ceiling on entries examined when enumerating segments. Rotation itself keeps at
# most _SEGMENT_KEEP + 1 files there, so a larger directory is someone else's
# doing; the walk runs on the append path (where a critical audit is written
# inline, sometimes on the event loop) and must not scale with a directory an
# agent filled before this release.
_SEGMENT_SCAN_CAP = 4096


# Ceiling on collision probes when minting a segment name. Each probe is a
# filesystem stat on the append path (where a critical audit is written inline,
# sometimes on the event loop), so a directory pre-filled with consecutive
# segment names must not be able to make rotation stat its way through it.
_SEGMENT_NAME_PROBES = 64


@contextmanager
def _rotation_window(self: SecurityEventLog) -> Iterator[None]:
    """Rotate if the live log is at the cap, holding the lock over the body.

    Caller holds ``_lock``. The body is the caller's chain + append.

    Below the cap -- the overwhelmingly common case -- this is a single
    ``stat`` and no lock at all, so a normal append pays nothing.

    At the cap it takes a CROSS-PROCESS lock, because ``_lock`` is a thread
    lock and SEVERAL PROCESSES share one data home (the gateway, the CLI,
    cron), all appending to this same file. They therefore reach the cap at
    the same moment, and two things go wrong unserialized:

    * both pick the same target name, and the second ``os.replace`` moves the
      freshly recreated live log onto the segment the first just closed,
      destroying it;
    * the loser keeps its pre-rotation chain tip and appends from it, so its
      first record in the new log names a ``prev_hash`` that is not in that
      file and ``security verify`` reports an untampered log as compromised.

    The acquire is NON-BLOCKING and the whole window is best-effort. A
    critical (``audit-or-deny``) event is written INLINE on its caller's
    thread, and some of those callers are event-loop coroutines, so a
    blocking acquire here could park the loop on another process's rotation —
    the ``no-blocking-call-on-event-loop`` hazard. Rotation is never worth
    that: it is deferrable (the next batch retries) while an audit write is
    not.

    Skipping on contention does NOT leave the loser appending from a stale
    tip, which is the reason the lock spans the append at all: the contended
    path re-checks the live log's identity and re-anchors the tip immediately
    before the caller chains (see :meth:`_reanchor_if_replaced`). A rotation
    that lands between that check and the append is the residual, a
    cross-process interleaving race -- closing THAT means holding a
    cross-process lock
    across every audit write, which is both the event-loop hazard above and
    a hot-path cost.

    Every failure -- an uncreatable/planted segment dir, a planted or
    unopenable lock file -- yields WITHOUT rotating so the audit record still
    lands.
    """
    if self._reanchor_if_replaced() < _SEGMENT_MAX_BYTES or not self._ensure_segment_dir():
        yield
        return
    lock_fh = self._open_rotation_lock()
    if lock_fh is None:
        yield
        return
    try:
        if not platform_compat.try_acquire_lock(lock_fh.fileno(), exclusive=True):
            # Another process is rotating right now. Do not wait (see above)
            # and do not trust the tip we anchored before it started.
            logger.debug("SEL rotation lock busy; deferring rotation to the next batch")
            self._reanchor_if_replaced()
            yield
            return
        try:
            # Elevate the chain-hold label for the duration of the heavy
            # step: a loop-side critical audit arriving now must fail
            # closed rather than join and block on ``_lock`` behind the
            # scan + rename + retention sweep.
            with self._chain_hold_relabel("rotation"):
                self._rotate_under_lock()
            yield
        finally:
            platform_compat.release_lock(lock_fh.fileno())
    finally:
        lock_fh.close()


def _open_rotation_lock(self: SecurityEventLog) -> IO[bytes] | None:
    """Open the rotation mutex, refusing a planted link. ``None`` if unusable.

    The path must never be followed through a symlink/junction: opening it
    CREATES the file and ``chmod``s it, so a link planted before this release
    (when ``security_events.d`` was not on the sensitive-path floor) would
    have rotation write a byte into, and relax the mode of, whatever the agent
    pointed it at. Same defense and same helpers as the segment dir and the
    SEL trust dir -- the link is removed, never its target -- and if the link
    cannot be removed we decline to rotate rather than open through it.
    """
    lock_path = self._segment_dir / _ROTATE_LOCK_FILE
    if platform_compat.is_link_or_junction(lock_path):
        logger.warning(
            "SEL rotation lock %s is a symlink/junction — removing the link "
            "(planted before upgrade?)",
            lock_path,
        )
        try:
            platform_compat.unlink_link_or_junction(lock_path)
        except OSError:
            logger.warning(
                "cannot remove linked SEL rotation lock %s; refusing to rotate "
                "rather than open through the link",
                lock_path,
                exc_info=True,
            )
            return None
    try:
        # "a+b": msvcrt.locking needs a writable fd. The file stays empty --
        # a byte-range lock covers byte 0 of a zero-length file, and writing
        # one here would fail with EACCES against a sibling that acquired
        # first, which declines a rotation for a lock that is working.
        lock_fh = open(lock_path, "a+b")
    except OSError:
        logger.warning("SEL rotation lock %s could not be opened", lock_path, exc_info=True)
        return None
    try:
        os.chmod(lock_path, 0o600)  # lockdown-ok: the rotation lock holds no data to expose
    except OSError:
        pass  # perms are hygiene here; the file holds no data
    return lock_fh


def _ensure_segment_dir(self: SecurityEventLog) -> bool:
    """Create the segment dir (owner-only), refusing a planted link.

    Returns whether the real directory is now usable. Rotation must NEVER
    write through a symlink/junction at this path: before this release
    ``security_events.d`` was not on the sensitive-path floor, so an agent
    could have pre-planted a link pointing somewhere it can read, and
    ``mkdir(exist_ok=True)`` succeeds on a link-to-directory. The segments
    would then land outside the fence, readable and rewritable by the audited
    agent — rotation itself becoming the way around it. Same defense, and the
    same helpers, as the SEL trust dir in
    :meth:`_load_or_create_hmac_key`; the link is removed, never its target.

    When the link cannot be removed (read-only dir) we REFUSE to rotate. The
    live log then keeps growing, which is the failure this release exists to
    bound — but an oversized log inside the fence beats a bounded one outside
    it.
    """
    if platform_compat.is_link_or_junction(self._segment_dir):
        logger.warning(
            "SEL segment dir %s is a symlink/junction — removing the link "
            "(planted before upgrade?) and creating a real directory",
            self._segment_dir,
        )
        try:
            platform_compat.unlink_link_or_junction(self._segment_dir)
        except OSError:
            logger.warning(
                "cannot remove linked SEL segment dir %s; refusing to rotate "
                "rather than write audit segments through the link",
                self._segment_dir,
                exc_info=True,
            )
            return False
    try:
        self._segment_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        logger.warning("SEL segment dir %s could not be created", self._segment_dir, exc_info=True)
        return False
    platform_compat.chmod_safe(self._segment_dir, 0o700)
    return True


def _rotate_under_lock(self: SecurityEventLog) -> None:
    """Perform one rotation. Caller holds ``_lock`` AND the rotation lock.

    The size is re-read here, not trusted from the check that opened the
    window: a sibling process may have rotated while we waited for the lock.

    The check reads the size ALREADY ON DISK, so a segment can overshoot the
    cap by at most the batch being appended (``_QUEUE_DRAIN_BATCH`` events,
    ~128 KiB against a 32 MiB cap). That keeps rotation one ``stat`` per
    batch instead of serializing every batch twice to measure it first.

    Chain semantics: the closed segment keeps its own complete chain and the
    new live log starts from genesis, so BOTH verify independently and
    retention deleting an old segment can never break a surviving one. The
    boundary is not lost — it is recorded as the new log's first entry, a
    ``sel_rotation`` event naming the closed segment and its size. It
    deliberately does NOT claim that segment's final ``entry_hash``: a hash
    captured here can be stale by the time the record is written (see
    :meth:`_rotation_event`), which would make verification report an
    untampered log as compromised. An investigator can therefore still walk
    segment to segment, and a segment that was deleted or swapped is visible
    as a rotation record whose named predecessor is absent or whose entries
    fail their own per-record HMACs.
    """
    size = self._live_size()
    if size < _SEGMENT_MAX_BYTES:
        # A sibling process rotated while we waited for the lock. Our cached
        # chain tip now lives in a closed segment, so appending from it would
        # chain this process's next batch off a record in a different file.
        # Re-anchor on the live log we are about to append to — safe to trust
        # because the caller holds the lock across that append.
        self._last_hash = self._read_last_hash()
        return
    target = self._next_segment_path()
    if target is None:
        return  # no free name; rotation deferred (see _next_segment_path)
    try:
        os.replace(self._path, target)
    except OSError:
        logger.warning("SEL rotation failed; continuing to append to %s", self._path, exc_info=True)
        return
    logger.info("SEL rotated %s -> %s (%d bytes)", self._path, target, size)
    # Anchor on the REPLACEMENT log -- normally absent, so genesis -- and write
    # the rotation record through the guarded append rather than assuming it
    # lands first. Another process can create and append to the replacement in
    # the gap after the rename (it sees a small file, so it never enters the
    # rotation window and never takes this lock). Writing a genesis record
    # blindly would then put TWO records claiming an empty prev_hash in the new
    # log and break its chain from the second line. The guard notices the file
    # already has content and the record chains off it instead, so the rotation
    # record is the first one we could place rather than necessarily line 1.
    self._reanchor_if_replaced()
    try:
        self._append_chained_locked([self._rotation_event(target, size)])
    except OSError:
        # The boundary record is lost but nothing is corrupt: the new log simply
        # starts without one. _append_chained_locked already rolled the tip back
        # to what it was, so the next batch chains off a record that is really
        # on disk.
        logger.warning("SEL rotation record could not be written", exc_info=True)
    self._enforce_segment_retention_locked()


def _next_segment_path(self: SecurityEventLog) -> Path | None:
    """An unused segment path for the log being closed now, or ``None``.

    The sequence continues from the highest segment still on disk, so it
    keeps rising even after retention has deleted earlier ones — a reused
    number would sort as the OLDEST segment and make the next retention
    sweep delete the log that was just closed.

    Collision probing is CAPPED. An existing name must never be overwritten
    (audit history is not silently replaced), but the probe is a filesystem
    ``stat`` per attempt and this runs on the append path, where a critical
    audit is written inline and sometimes on the event loop. A directory
    pre-filled with consecutive segment names could otherwise make rotation
    stat its way through all of them
    (``no-blocking-call-on-event-loop``). Returning ``None`` defers rotation,
    which leaves the log over budget rather than stalling a write.
    """
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    existing = self._segments_oldest_first()
    seq = (_segment_seq(existing[-1]) + 1) if existing else 1
    for _probe in range(_SEGMENT_NAME_PROBES):
        candidate = self._segment_dir / f"security_events-{seq:06d}-{stamp}.jsonl"
        if not candidate.exists():
            return candidate
        seq += 1
    logger.warning(
        "SEL could not find a free segment name within %d probes from %06d in %s; "
        "deferring rotation (names pre-planted, or a restored backup?)",
        _SEGMENT_NAME_PROBES,
        seq - _SEGMENT_NAME_PROBES,
        self._segment_dir,
    )
    return None


def _segments_oldest_first(
    self: SecurityEventLog, *, pin: _SegmentDirPin | None = None
) -> list[Path]:
    """Closed segments in rotation order (oldest first).

    Only files matching ``_SEGMENT_NAME_RE`` in full are returned, so an
    unrelated file an operator parked in the segment dir is never treated as
    rotation output (and so never deleted by retention). A candidate must
    ALSO be a real regular file: the name check is what protects an
    operator's file, the ``lstat`` check is the cheap first filter that
    stops a PLANTED link from being read as audit history. It judges the
    dirent at SCAN time only, so the readers re-validate at open time via
    :func:`_open_segment` — a link planted between scan and open is refused
    there rather than followed. A hardlink ALIAS is deliberately NOT
    deduplicated: every key such a decision could use is
    attacker-controlled here, so a dedupe lets a planted name DISPLACE the
    real segment (see the comment at the scan); an alias's worst case is
    repeating already-signed records, which admission gating bounds.

    The link case is reachable on an upgrade and matters on the read side,
    not the delete side: ``security_events.d`` was not on the sensitive-path
    floor before this release, so an agent could have pre-planted
    ``security_events-000001-<stamp>.jsonl`` as a symlink to any file it
    wanted surfaced -- and every reader here (``recent``, which backs the
    dashboard's SEL events endpoint, and ``verify_integrity``) would parse
    the LINKED file's lines and hand back whatever JSON objects it found as
    events. Retention unlinking such an entry would only remove the link,
    but the read is a real disclosure, so a non-regular entry is excluded.

    Ordering is by the name's numeric sequence — see ``_SEGMENT_NAME_RE``
    for why that rather than the timestamp or the mtime.

    Enumeration is BOUNDED at ``_SEGMENT_SCAN_CAP`` entries. Rotation keeps
    at most ``_SEGMENT_KEEP + 1`` files here, so any larger count is either an
    operator's dumping ground or entries planted before this release, and this
    walk runs on the append path — where a critical audit is written inline,
    sometimes on the event loop. An unbounded listing of a directory someone
    else filled would stall that caller
    (``no-blocking-call-on-event-loop``), so the scan stops at the cap and
    works with what it has: rotation then simply does not find the segments
    beyond it, which leaves the log over budget rather than blocking a write.

    *pin* is the read-side directory pin. The walk itself stays
    the same bounded, BY-NAME scan on every platform — the cap above is
    the memory bound, and materializing an unbounded listing first (as an
    fd-relative ``os.listdir`` would) would spend unbounded memory just to
    refuse it afterwards. The pin closes the redirect the name allows:
    after the walk, the path must still name the directory the read
    pinned (any swap — planted link, or a different real directory, whose
    identity also differs — fails closed to no segments), and on
    descriptor platforms the per-file opens that follow resolve RELATIVE
    to the pinned descriptor, so a swap after the revalidation still
    cannot redirect a read. Rotation calls this UNPINNED: it repairs the
    directory itself first (:meth:`_ensure_segment_dir`), and its callers
    are not read paths.
    """
    if pin is not None and pin.fd is not None:
        # fd-relative walk: a PATH swap can neither redirect nor empty
        # this scan. That closes the ABA shape — swap a decoy in DURING
        # the walk, restore the real directory before any recheck — in
        # which a by-name scan enumerates the decoy and then sees the
        # restored identity pass. os.scandir(fd) stays lazy, so the cap
        # below keeps bounding the walk on a legacy directory pre-filled
        # before the fence.
        try:
            scanner = os.scandir(pin.fd)
        except OSError:
            logger.warning("SEL could not enumerate the pinned segment dir", exc_info=True)
            return []
    else:
        try:
            scanner = os.scandir(self._segment_dir)
        except OSError:
            return []
    # A hardlink ALIAS is deliberately NOT handled here (or anywhere):
    # an alias IS the regular file its name points at, so telling it from
    # the original requires a cross-name decision — and every key
    # available for that decision (which name sorts first, which survives)
    # is attacker-controlled in this directory. A dedupe keyed on names
    # lets a planted alias DISPLACE the real segment and corrupt read
    # chronology, which is strictly worse than what an alias can do on its
    # own: duplicate already-signed records in bounded reads. That
    # duplication is admission-gated (_read_sources_newest_first requires
    # a record signed with the out-of-tree key) and cannot forge history.
    named: list[Path] = []
    examined = 0
    truncated = False
    with scanner:
        for entry in scanner:
            examined += 1
            if examined > _SEGMENT_SCAN_CAP:
                truncated = True
                break
            if not _SEGMENT_NAME_RE.fullmatch(entry.name):
                continue
            try:
                # follow_symlinks=False, so a symlink is judged as a symlink
                # rather than as whatever it points at.
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(entry_stat.st_mode):
                logger.warning(
                    "SEL ignoring non-regular entry %s in the segment dir "
                    "(planted link?); it is not audit history",
                    entry.name,
                )
                continue
            named.append(self._segment_dir / entry.name)
    if truncated:
        logger.warning(
            "SEL segment dir %s holds more than %d entries; scan stopped there. "
            "Rotation keeps at most %d, so the extra entries were not created by "
            "it — retention will not see past the cap until they are removed",
            self._segment_dir,
            _SEGMENT_SCAN_CAP,
            _SEGMENT_KEEP + 1,
        )
    if pin is not None and pin.fd is None and not pin.matches(self._segment_dir):
        # Identity pin (this platform has no directory descriptors): the
        # walk above was by NAME, so a directory swapped in mid-walk
        # would have been followed. The identity the read pinned and the
        # identity the path names NOW differ, so fail closed rather than
        # return entries from a tree the pin never covered.
        logger.warning(
            "SEL segment dir %s was replaced during enumeration (planted "
            "link?); its entries are not audit history",
            self._segment_dir,
        )
        return []
    return sorted(named, key=_segment_seq)


def _enforce_segment_retention_locked(self: SecurityEventLog) -> int:
    """Delete the oldest closed segments beyond ``_SEGMENT_KEEP``.

    Returns the number deleted. Caller holds ``_lock``.

    STOPS at the first segment it cannot unlink rather than skipping past it.
    Skipping would delete the NEXT-oldest instead — trading newer audit
    history for older history that is stuck occupying a retention slot, so a
    permission problem on one file would quietly eat the evidence behind it.
    Stopping leaves the count over budget until the obstruction clears, which
    is the safe direction, and the next rotation retries. Matches
    :meth:`_prune_segments_locked`, which already stops for the same reason.
    """
    segments = self._segments_oldest_first()
    excess = len(segments) - _SEGMENT_KEEP
    if excess <= 0:
        return 0
    deleted = 0
    for path in segments[:excess]:
        try:
            path.unlink()
        except OSError:
            logger.warning(
                "SEL segment retention could not delete %s; stopping this sweep "
                "rather than deleting newer segments behind it",
                path,
                exc_info=True,
            )
            break
        deleted += 1
    if deleted:
        logger.info(
            "SEL retention deleted %d closed segment(s) beyond the %d kept",
            deleted,
            _SEGMENT_KEEP,
        )
    return deleted


def prune(self: SecurityEventLog, keep_days: int = _RETENTION_DAYS) -> int:
    """Remove entries older than keep_days. Returns count removed.

    Streams the log line-by-line to bound memory usage, writes survivors
    to a temp file, then atomically replaces the original. The append lock
    is held across the whole read+replace critical section so a concurrent
    append cannot land in the old file after the read pass and be lost by
    the replace: appends either complete before the read (and are copied)
    or block until after the replace (and land in the new file). Both the
    cross-process chain lock and the thread lock are held, in that order —
    a writer in ANOTHER process is ordered only by the former, and taking it
    first is what keeps a cross-process wait off the event loop. Appends
    run on the background writer thread, so blocking them for the prune
    duration never touches the event loop.

    The chain lock orders sibling APPENDS; rotation by a sibling is ordered
    by the cross-process ROTATION lock (the same one rotation takes), which
    the live sweep additionally holds via :meth:`_prune_live_locked`. The
    read-then-replace below is the one window where an unserialized sibling
    is destructive rather than merely untidy: an append or rotation landing
    in the old file after the read pass would be dropped by the replace —
    the only path in this class that can lose already-persisted audit
    events. Unlike rotation, prune WAITS for the rotation lock instead of
    skipping. Rotation is deferrable and runs on the audit hot path; prune
    is a once-a-day sweep on the maintenance executor, never on the event
    loop, and skipping it would postpone retention for a whole day. When
    the lock cannot be taken at all the live sweep is skipped and reported,
    because doing it unserialized is what loses events.

    Rotated segments are aged out WHOLE rather than rewritten: a segment
    whose newest record is past the cutoff is deleted outright, which keeps
    every surviving segment's chain intact (rewriting one would orphan its
    first record's ``prev_hash``). Their entries are included in the
    returned count.
    """
    self.flush()  # don't rewrite the file out from under queued appends
    cutoff_dt = datetime.now(tz=timezone.utc) - timedelta(days=keep_days)

    removed = 0
    with self._chain_lock(kind="prune"), self._lock:
        removed += self._prune_segments_locked(cutoff_dt)
        if not self._path.exists():
            return removed
        lock_fh = self._open_rotation_lock() if self._ensure_segment_dir() else None
        if lock_fh is None:
            logger.warning(
                "SEL prune could not take the rotation lock; skipping the live-log "
                "sweep rather than risking a concurrent rotation's events"
            )
            return removed
        try:
            with platform_compat.file_lock(lock_fh.fileno(), exclusive=True):
                removed += self._prune_live_locked(cutoff_dt, keep_days)
        except OSError:
            logger.warning(
                "SEL prune could not serialize the live-log sweep; skipped",
                exc_info=True,
            )
        finally:
            lock_fh.close()
    return removed


def _prune_live_locked(self: SecurityEventLog, cutoff_dt: datetime, keep_days: int) -> int:
    """Rewrite the live log without its aged-out entries. Returns count removed.

    Caller holds ``_lock`` AND the cross-process rotation lock, so no sibling
    can rotate the file out from under the read-then-replace below.
    """
    cutoff_str = cutoff_dt.isoformat()
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=str(self._path.parent), prefix=".sel_prune_", suffix=".tmp"
    )
    live_removed = 0
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as tmp_f:
            with open(self._path, encoding="utf-8") as src_f:
                for raw_line in src_f:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        if data.get("timestamp", "") < cutoff_str:
                            live_removed += 1
                            continue
                    except json.JSONDecodeError:
                        live_removed += 1
                        continue
                    tmp_f.write(line)
                    tmp_f.write("\n")

        if live_removed:
            os.replace(tmp_path, self._path)
            # The replace gives the live log a NEW inode and a smaller size, so
            # re-anchor both the tip and the identity we compare against.
            # Leaving the identity stale would make the next append see a
            # foreign change and re-chain needlessly.
            self._reanchor_now()
            logger.info("SEL pruned %d entries older than %d days", live_removed, keep_days)
        else:
            os.unlink(tmp_path)
    except BaseException:
        # Clean up temp file on any failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return live_removed


def _prune_segments_locked(self: SecurityEventLog, cutoff: datetime) -> int:
    """Delete whole rotated segments whose NEWEST record predates *cutoff*.

    Returns the number of entries dropped with them. Caller holds ``_lock``.
    A segment is deleted only when its last record is past the cutoff, so a
    segment straddling the boundary is kept in full rather than rewritten:
    rewriting it would orphan its first record's ``prev_hash`` and turn a
    retention sweep into an apparent chain break. Best-effort — a segment
    that cannot be read or unlinked is logged and left alone.
    """
    removed = 0
    for path in self._segments_oldest_first():
        newest = self._newest_timestamp(path)
        if newest is None or newest >= cutoff:
            # Unreadable/undatable, or still within retention. Segments are
            # chronological, so the first keeper ends the sweep.
            break
        count = sum(1 for _ in self._iter_lines_backward(path))
        try:
            path.unlink()
        except OSError:
            logger.warning("SEL could not delete aged-out segment %s", path, exc_info=True)
            break
        removed += count
        logger.info("SEL deleted aged-out segment %s (%d entries)", path.name, count)
    return removed


def _newest_timestamp(self: SecurityEventLog, path: Path) -> datetime | None:
    """Timestamp of *path*'s newest parseable record, or ``None``."""
    for line in self._iter_lines_backward(path):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        parsed = _parse_timestamp(data.get("timestamp", ""))
        if parsed is not None:
            return parsed
    return None


@contextmanager
def _no_rotation() -> Iterator[None]:
    """A do-nothing stand-in for the rotation window (see ``_flush_batch``)."""
    yield


def _segment_seq(path: Path) -> int:
    """Rotation sequence encoded in a segment's name (0 if it has none).

    Sort key for :meth:`SecurityEventLog._segments_oldest_first`; callers filter
    non-segment names first, so the fallback only guards a caller that does not.
    """
    match = _SEGMENT_NAME_RE.fullmatch(path.name)
    return int(match.group("seq")) if match else 0
