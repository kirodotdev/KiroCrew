"""An in-memory index of the rows :meth:`ConversationLog.list_sessions` returns.

``list_sessions`` stats every transcript in the session directory and reads the head
of each one its per-file caches miss. Those caches are LRUs bounded at
``_METADATA_CACHE_MAX`` entries and a listing walks the whole directory in order, so
past that many transcripts every entry is evicted before it is reused and each
listing re-reads every untitled head. The dashboard asks for the listing on every
history refresh.

:class:`SessionListingIndex` keeps one row per transcript, read by the same per-file
code the scan uses (``SessionCatalogProjection._listing_row``). A directory that
holds more than :data:`MAX_ROWS` transcripts, or whose rows hold more than
:data:`MAX_RETAINED_BYTES`, gets no rows: the index logs one warning, each listing is
the direct scan, and a build is tried again at most every :data:`RECONCILE_SECS`. The
index keeps a running total of its rows' charged bytes, each row's serialized JSON
length plus a fixed per-row overhead, moved on every insert, replace and drop, so the
check costs one comparison. Rows are never clamped: the
index lists the scan's rows or none. Four signals keep the rows current:

* Writes through :class:`ConversationLog` in this process. Its writers call
  ``_invalidate_cache``, which records the sanitized stem in
  ``ConversationLog._dir_bumped_stems`` with the directory's write epoch. Each
  listing re-reads only the stems bumped since it last looked, then drops the records
  every index over the directory has replayed. A bumped stem the index does not hold,
  whose file exists, is a file this process created: the listing then re-reads the
  directory's names, because only the directory order places the new row. The
  journal's cap drops the oldest records and raises the directory's floor
  (``_dir_bump_floors``); an index that had not replayed up to the floor re-reads
  every transcript on its next listing.
* The directory's stamp, for a file created, removed or renamed in the directory with
  no journal record: the trash's move and restore, an import, another process. Each
  listing stats the directory once. When its identity or mtime moved since the last
  names pass, or its mtime is under :data:`_DIR_STAMP_SETTLE_NS` old (a change in the
  same timestamp tick leaves the stamp as it was), the listing re-reads the
  directory's names before it answers. A names pass reads only the files the index
  has no row for.
* A stat reconcile, at most every :data:`RECONCILE_SECS`, for a file's content changed
  with no journal record: another process's append, or a file replaced under a name
  the index holds. A listing that finds one due hands it to the maintenance pool and
  answers from the rows it holds. The reconcile stats every transcript outside the
  index lock, re-reads only files whose stamp moved, and swaps the result in under the
  lock, keeping any row a listing re-read meanwhile. Such a change therefore shows on
  the first listing after the reconcile that follows it.
* A read that failed with an I/O error is retried on every listing, as the scan
  retries it.

A ``scandir`` error other than a missing directory keeps the rows, the order and the
memoized listing; only a missing directory empties the index.

The listing is the scan's dedup and sort over the rows, in the directory order of the
last pass, so ties resolve as the scan resolves them. It is memoized until a row or
the order changes, and each call returns copies of the rows.

The first build reads every transcript's head, seconds at tens of thousands of files.
The gateway runs :meth:`SessionListingIndex.warm` on a worker thread once it is
serving, so the first listing finds the index built. A listing that arrives during
that build waits on the index lock, as it waits on any other pass.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import weakref
from collections.abc import Callable
from typing import TYPE_CHECKING

from kiro_crew.history_files import CacheIdentity, _safe_key
from kiro_crew.history_search import _jsonl_stem

if TYPE_CHECKING:
    from kiro_crew.history import ConversationLog

logger = logging.getLogger(__name__)

#: Least time between two stat reconciles, in seconds: the longest a write by another
#: process goes unseen, plus one listing and the reconcile's own run.
RECONCILE_SECS = 5.0

#: Most transcripts a directory may hold for the index to keep rows for it. It bounds
#: the row count only; :data:`MAX_RETAINED_BYTES` bounds what the rows hold.
MAX_ROWS = 100_000

#: Most bytes the index's rows may hold, as :func:`_retained_bytes` charges them:
#: compact JSON, each non-ASCII string's allocation and index overhead. A typical
#: row is charged about 4.3 KB, so this budget binds first, at about 62,000 rows.
MAX_RETAINED_BYTES = 256 * 2**20

#: Bytes one entry keeps besides its row's charge, the stem and canonical
#: key a filename bounds included. Measured once with ``sys.getsizeof`` over a built
#: index of 10,000 typical rows, titled and untitled: the deep size of ``_entries``,
#: ``_order``, ``_safe_of``, ``_by_safe`` and the snapshot, each object counted once,
#: less the JSON length of its row, came to about 1,370 bytes per
#: row; rounded up.
_ROW_OVERHEAD = 1400

#: Least age, by the wall clock, of the directory's last change for its stamp to be
#: trusted. A file created in the same timestamp tick as the stamp's read leaves the
#: stamp unchanged. FAT keeps a 2 s tick and HFS+ a 1 s tick; ext4's follows the
#: kernel's coarse clock, a few milliseconds.
_DIR_STAMP_SETTLE_NS = 2 * 10**9


def _wall_ns() -> int:
    """The wall clock :data:`_DIR_STAMP_SETTLE_NS` is measured on.

    Read through the module's ``time`` at each call, so a test that replaces that
    name moves this clock too.
    """
    return time.time_ns()


#: The directory's ``(st_dev, st_ino, st_mtime_ns)``. Creating, removing or renaming
#: an entry moves the mtime on every platform; ctime is left out because on Windows it
#: is the creation time.
_DirStamp = tuple[int, int, int]

#: Per transcript directory, the indexes whose replay position holds back the
#: drop of journal records. Guarded by ``ConversationLog._cache_gens_guard``.
_READERS: dict[str, weakref.WeakSet[SessionListingIndex]] = {}

#: Transcripts a warm build reads between two calls of its ``should_stop``.
_STOP_CHECK_EVERY = 256


class _OverCeiling(Exception):
    """The directory is over :data:`MAX_ROWS` transcripts or :data:`MAX_RETAINED_BYTES`."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Stopped(Exception):
    """A warm build's ``should_stop`` answered True."""


#: CPython 3.12 allocation bounds per JSON token; non-ASCII strings add their full size separately.
_DICT_BYTES = 184
_LIST_BYTES = 88
_ITEM_BYTES = 32
_QUOTE_BYTES = 25


def _non_ascii_string_bytes(value: object) -> int:
    """Return the allocated size of every non-ASCII string inside *value*."""
    total = 0
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if not item.isascii():
                total += sys.getsizeof(item)
        elif isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
    return total


def _retained_bytes(row: dict | None) -> int:
    """The bytes one entry is charged, at least what *row* holds in memory.

    Counts every key, nested value and container. Compact JSON and token bounds
    cover ASCII strings and containers. Non-ASCII strings add their allocated size
    because one wide code point can widen the complete string.
    """
    text = json.dumps(row, default=str, separators=(",", ":"))
    return (
        _ROW_OVERHEAD
        + len(text)
        + _non_ascii_string_bytes(row)
        + _DICT_BYTES * text.count("{")
        + _LIST_BYTES * text.count("[")
        + _ITEM_BYTES * text.count(",")
        + _QUOTE_BYTES * text.count('"')
    )


def _over_budget(held: int) -> str:
    return f"its rows hold {held} bytes, over the {MAX_RETAINED_BYTES}-byte budget"


def _log_warm(outcome: str, transcripts: int, started: float) -> None:
    """The one line :meth:`SessionListingIndex.warm` logs; *started* is a monotonic time."""
    logger.info(
        "Session listing index warm %s: %d transcripts in %.2f s",
        outcome,
        transcripts,
        time.monotonic() - started,
    )


def _submit_to_maintenance_pool(job: Callable[[], None]) -> None:
    from kiro_crew.executors import maintenance_executor

    maintenance_executor().submit(job)


class _Entry:
    """One transcript's listing state, as one read left it."""

    __slots__ = ("status", "row", "volatile", "stamp", "canon", "has_messages", "size")

    def __init__(
        self,
        status: str,
        row: dict | None,
        volatile: bool,
        stamp: CacheIdentity | None,
        canon: str,
        has_messages: bool | None,
        size: int,
    ) -> None:
        self.status = status
        self.row = row
        self.volatile = volatile
        #: The cache identity of the stat the row was read after.
        self.stamp = stamp
        self.canon = canon
        #: ``log.has_messages`` for this revision, when the row's head read settled it.
        self.has_messages = has_messages
        #: :func:`_retained_bytes` of this entry.
        self.size = size


class SessionListingIndex:
    """Listing rows for one :class:`ConversationLog`, refreshed incrementally."""

    def __init__(
        self,
        log: "ConversationLog",
        *,
        clock: Callable[[], float] = time.monotonic,
        submit: Callable[[Callable[[], None]], None] = _submit_to_maintenance_pool,
    ) -> None:
        self._log = log
        self._clock = clock
        self._submit = submit
        self._base = os.fspath(log._dir)
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}
        #: File stems in the directory order of the last pass. A stem a re-read found
        #: gone stays here until the next pass; the snapshot skips it.
        self._order: list[str] = []
        #: File stem -> sanitized stem (the form ``_dir_bumped_stems`` records).
        self._safe_of: dict[str, str] = {}
        #: Sanitized stem -> file stems.
        self._by_safe: dict[str, list[str]] = {}
        #: Stems whose last read failed; every listing re-reads them.
        self._retry: set[str] = set()
        self._snapshot: list[dict] | None = None
        self._built = False
        #: The sum of ``size`` over ``_entries``.
        self._bytes = 0
        #: The last pass went over MAX_ROWS or MAX_RETAINED_BYTES; never set while built.
        self._over = False
        self._warned = False
        #: The journal dropped a record this index had not replayed.
        self._rebuild_due = False
        #: The directory write epoch the last bump replay read up to.
        self._epoch = 0
        self._names_due = False
        #: The directory stamp read before the scan of the last build or names pass;
        #: None when that stamp was unsettled or unreadable.
        self._dir_seen: _DirStamp | None = None
        #: Moves on every change a pass makes, so a reconcile can tell that the order
        #: or the membership changed while it ran.
        self._passes = 0
        self._last_reconcile = 0.0
        self._reconciling = False
        #: Stems a listing re-read or dropped while a reconcile ran, else None.
        self._touched: set[str] | None = None

    # ------------------------------------------------------------------ reads

    def list_sessions(self) -> list[dict]:
        """What ``log.list_sessions()`` returns, as copies of the indexed rows.

        A built index stats the directory once per call and reads no transcript
        unless a signal in the module docstring says one changed. Over
        :data:`MAX_ROWS` transcripts or :data:`MAX_RETAINED_BYTES` it is
        ``log.list_sessions()`` itself.
        """
        with self._lock:
            if self._built:
                # Before the journal replay and any scan: a change landing after this
                # stat moves the stamp the next listing compares.
                stamp = self._dir_stamp()
                self._catch_up()
                if self._rebuild_due:
                    self._build(stamp)
                else:
                    for stem in list(self._retry):
                        self._reread(stem)
                    if stamp is None or stamp != self._dir_seen:
                        self._names_due = True
                    if self._names_due:
                        self._names_pass(stamp)
            elif not self._over or self._clock() - self._last_reconcile >= RECONCILE_SECS:
                self._build(self._dir_stamp())
            self._within_budget()
            over = self._over
            now = self._clock()
            due = (
                self._built
                and not self._reconciling
                and now - self._last_reconcile >= RECONCILE_SECS
            )
            if due:
                self._reconciling = True
                self._last_reconcile = now
            if self._snapshot is None:
                self._snapshot = self._build_snapshot()
            rows = [dict(row) for row in self._snapshot]
        if over:
            return self._log.list_sessions()
        if due:
            try:
                self._submit(self._run_reconcile)
            except Exception:  # noqa: BLE001 - a pool shutting down refuses work
                logger.debug("Session listing reconcile not scheduled", exc_info=True)
                with self._lock:
                    self._reconciling = False
        return rows

    def has_messages(self, key: str, *, identity: CacheIdentity | None = None) -> bool | None:
        """Return the indexed ``log.has_messages(key)`` answer, else None.

        ``identity`` is the current file identity from the caller. When supplied,
        it must match the entry's identity. Writes through :class:`ConversationLog`
        in this process since the last listing are replayed first. A caller that
        needs a current answer supplies the identity and falls back to the log's
        stamp-validated memo when this method returns None.
        """
        if not self._built:
            # Unlocked: a build in progress holds the lock for its whole pass.
            return None
        with self._lock:
            if not self._built:
                return None
            self._catch_up()
            if self._rebuild_due or not self._within_budget():
                return None
            entry = self._entries.get(key)
            if entry is None or (identity is not None and entry.stamp != identity):
                return None
            return entry.has_messages

    def warm(self, should_stop: Callable[[], bool] | None = None) -> bool:
        """Build the index ahead of the first listing; True when it is built.

        Returns False at once when a listing holds the index lock: that listing builds
        the index or finds it built. *should_stop* is called before each
        :data:`_STOP_CHECK_EVERY` transcripts; when it answers True the build stops and
        the index stays unbuilt. A build that stops or raises commits nothing: the
        rows, the build state and the journal replay position stay as they were, so
        the next listing builds the index. Logs one line when it returns.
        """
        started = time.monotonic()
        if not self._lock.acquire(blocking=False):
            _log_warm("superseded by a listing", 0, started)
            return False
        try:
            if self._built:
                _log_warm("superseded by a listing", 0, started)
                return True
            self._build(self._dir_stamp(), should_stop)
            if self._built:
                _log_warm("built", len(self._entries), started)
            else:
                _log_warm("not built", 0, started)
            return self._built
        finally:
            self._lock.release()

    # ------------------------------------------------------------- reconcile

    def reconcile(self) -> bool:
        """Stat every transcript and re-read those whose stamp moved.

        The stats and reads run outside the index lock; the swap runs under it.
        Returns False when the index is not built or the directory could not be read,
        in which case the index keeps what it holds, and when the directory holds more
        than :data:`MAX_ROWS` transcripts or its rows more than
        :data:`MAX_RETAINED_BYTES`, in which case it drops every row.
        """
        with self._lock:
            if not self._built:
                return False
            known = dict(self._entries)
            passes = self._passes
            self._touched = set()
        try:
            order, fresh = self._stat_pass(known)
        except _OverCeiling as over:
            with self._lock:
                self._touched = None
                self._stop(over.reason)
            return False
        except OSError:
            logger.debug("Session listing reconcile could not read %s", self._base, exc_info=True)
            with self._lock:
                self._touched = None
            return False
        with self._lock:
            touched, self._touched = self._touched or set(), None
            if self._passes != passes:
                # A pass replaced the order or the rows meanwhile, from a newer read
                # of the directory: keep its order and members, take only re-reads.
                for stem, new in fresh.items():
                    old = known.get(stem)
                    if old is not None and stem not in touched and self._entries.get(stem) is old:
                        if new.status == "gone":
                            self._drop(stem)
                        else:
                            self._install(stem, new, old)
                return self._within_budget()
            entries: dict[str, _Entry] = {}
            for stem in order:
                if stem in touched:
                    # A listing re-read this row after the snapshot: its read is newer.
                    current = self._entries.get(stem)
                    if current is not None:
                        entries[stem] = current
                    continue
                entry = fresh.get(stem) or known.get(stem)
                if entry is not None and entry.status != "gone":
                    entries[stem] = entry
            for stem in touched:
                current = self._entries.get(stem)
                if current is not None and stem not in entries:
                    # Written here after the scan read the directory without it.
                    entries[stem] = current
                    self._names_due = True
            self._reset(entries, order)
            return self._within_budget()

    def _run_reconcile(self) -> None:
        try:
            self.reconcile()
        except Exception:  # noqa: BLE001 - a failed pass leaves the rows as they were
            logger.warning("Session listing reconcile failed", exc_info=True)
            with self._lock:
                self._touched = None
        finally:
            with self._lock:
                self._reconciling = False

    # ---------------------------------------------------------------- passes

    def _scan(self) -> "list[os.DirEntry[str]]":
        """The directory's transcript entries in directory order; none when it is gone.

        Any other ``OSError`` propagates, so the caller keeps the rows it holds. More
        than :data:`MAX_ROWS` entries raise :class:`_OverCeiling` before any row read.
        """
        try:
            with os.scandir(self._base) as it:
                found = [e for e in it if os.path.normcase(e.name).endswith(".jsonl")]
        except FileNotFoundError:
            return []
        if len(found) > MAX_ROWS:
            raise _OverCeiling(f"{len(found)} transcripts, over the {MAX_ROWS}-row ceiling")
        return found

    def _stat_pass(
        self, known: dict[str, _Entry], should_stop: Callable[[], bool] | None = None
    ) -> tuple[list[str], dict[str, _Entry]]:
        """The directory order, and a fresh entry for each file *known* does not match.

        A row whose stamp still matches costs one stat and no read; a link that is
        still a link costs nothing. Fresh entries that alone hold more than
        :data:`MAX_RETAINED_BYTES` raise :class:`_OverCeiling` before the next read.
        *should_stop* is called before each :data:`_STOP_CHECK_EVERY` entries, and
        :class:`_Stopped` is raised when it answers True.
        """
        order: list[str] = []
        fresh: dict[str, _Entry] = {}
        fresh_bytes = 0
        for i, de in enumerate(self._scan()):
            if should_stop is not None and not i % _STOP_CHECK_EVERY and should_stop():
                raise _Stopped
            stem = _jsonl_stem(de.name)
            order.append(stem)
            old = known.get(stem)
            if old is not None and not old.volatile:
                link = de.is_symlink()
                if link and old.status == "link":
                    continue
                if not link and old.status == "row":
                    try:
                        # The listing row reads the same stat back from the entry.
                        st = os.stat(de.path) if os.name == "nt" else de.stat()
                    except OSError:
                        st = None
                    if st is not None and self._log._cache_identity(st) == old.stamp:
                        continue
            entry = fresh[stem] = self._make_entry(de.path, stem, de)
            fresh_bytes += entry.size
            if fresh_bytes > MAX_RETAINED_BYTES:
                raise _OverCeiling(_over_budget(fresh_bytes))
        return order, fresh

    def _dir_stamp(self) -> _DirStamp | None:
        """The directory's stamp, or None when the stat failed or the stamp is unsettled.

        None never equals a stamp a pass recorded, so the next listing re-reads the
        names.
        """
        try:
            st = os.stat(self._base)
        except OSError:
            return None
        if _wall_ns() - st.st_mtime_ns < _DIR_STAMP_SETTLE_NS:
            return None
        return (st.st_dev, st.st_ino, st.st_mtime_ns)

    def _build(
        self, stamp: _DirStamp | None, should_stop: Callable[[], bool] | None = None
    ) -> None:
        """Read every transcript; *stamp* is the directory stamp read before the scan.

        A failed directory read, a stop and a raise leave the rows, whether the index
        is built and the journal replay position as they were, so the next listing
        tries again.
        """
        now = self._clock()
        # Every bump up to this epoch is covered by the reads below. The replay
        # position moves to it only once the pass is committed.
        epoch = self._journal_epoch()
        try:
            order, fresh = self._stat_pass({}, should_stop)
        except _OverCeiling as over:
            self._stop(over.reason)
            return
        except _Stopped:
            return
        except OSError:
            logger.debug("Session listing index could not read %s", self._base, exc_info=True)
            return
        self._replayed(epoch)
        self._built = True
        self._over = False
        self._rebuild_due = False
        self._names_due = False
        self._dir_seen = stamp
        self._last_reconcile = now
        self._reset({s: e for s, e in fresh.items() if e.status != "gone"}, order)

    def _within_budget(self) -> bool:
        """False, after dropping every row, when the rows hold over :data:`MAX_RETAINED_BYTES`.

        Under the index lock.
        """
        if self._built and self._bytes > MAX_RETAINED_BYTES:
            self._stop(_over_budget(self._bytes))
            return False
        return True

    def _stop(self, reason: str) -> None:
        """Drop every row: the directory is over a bound, as *reason* says.

        Listings then serve the direct scan, and the next build is tried at most
        :data:`RECONCILE_SECS` later. Under the index lock.
        """
        if not self._warned:
            self._warned = True
            logger.warning(
                "Session listing index off for %s: %s; each listing scans the directory",
                self._base,
                reason,
            )
        self._built = False
        self._over = True
        self._rebuild_due = False
        self._names_due = False
        self._dir_seen = None
        self._last_reconcile = self._clock()
        self._passes += 1
        self._snapshot = None
        self._entries = {}
        self._bytes = 0
        self._order = []
        self._safe_of = {}
        self._by_safe = {}
        self._retry = set()
        owner = type(self._log)
        with owner._cache_gens_guard:
            readers = _READERS.get(self._base)
            if readers is not None:
                readers.discard(self)
                if not readers:
                    del _READERS[self._base]

    def _names_pass(self, stamp: _DirStamp | None) -> None:
        """Re-read the directory's names: place new files, drop removed ones.

        *stamp* is the directory stamp read before the scan.
        """
        try:
            scanned = self._scan()
        except _OverCeiling as over:
            self._stop(over.reason)
            return
        except OSError:
            # Kept due, so the next listing tries again.
            return
        self._names_due = False
        self._dir_seen = stamp
        entries: dict[str, _Entry] = {}
        order: list[str] = []
        for de in scanned:
            stem = _jsonl_stem(de.name)
            order.append(stem)
            entry = self._entries.get(stem) or self._make_entry(de.path, stem, de)
            if entry.status != "gone":
                entries[stem] = entry
        self._reset(entries, order)

    def _take_bumped(self) -> list[str] | None:
        """The sanitized stems this process bumped since the last call.

        None when the journal's cap dropped a record this index had not replayed, so
        the stems that moved are not known. Registers this index as a reader of the
        directory's journal, then drops the records every registered reader replayed.
        """
        owner = type(self._log)
        with owner._cache_gens_guard:
            _READERS.setdefault(self._base, weakref.WeakSet()).add(self)
            epoch = owner._dir_write_epochs.get(self._base, 0)
            if epoch == self._epoch:
                return []
            bumped = owner._dir_bumped_stems.get(self._base)
            moved: list[str] = []
            if bumped:
                for stem in reversed(bumped):
                    if bumped[stem] <= self._epoch:
                        break
                    moved.append(stem)
            stems = None if owner._dir_bump_floors.get(self._base, 0) > self._epoch else moved
            self._epoch = epoch
            self._drop_replayed()
        return stems

    def _journal_epoch(self) -> int:
        """The directory's write epoch now; registers this index as a journal reader.

        The replay position stays where it is, so the records after it are kept until
        :meth:`_replayed` moves it.
        """
        owner = type(self._log)
        with owner._cache_gens_guard:
            _READERS.setdefault(self._base, weakref.WeakSet()).add(self)
            return owner._dir_write_epochs.get(self._base, 0)

    def _replayed(self, epoch: int) -> None:
        """Move the replay position to *epoch*, then drop what every reader replayed."""
        with type(self._log)._cache_gens_guard:
            self._epoch = epoch
            self._drop_replayed()

    def _drop_replayed(self) -> None:
        """Drop the journal records every reader of the directory replayed.

        Under ``ConversationLog._cache_gens_guard``.
        """
        bumped = type(self._log)._dir_bumped_stems.get(self._base)
        readers = _READERS.get(self._base)
        if bumped and readers:
            low = min(reader._epoch for reader in readers)
            while bumped and next(iter(bumped.values())) <= low:
                bumped.popitem(last=False)

    def _catch_up(self) -> None:
        """Re-read every row this process wrote since the last call.

        When the journal cannot say which rows moved, mark a rebuild due instead.
        """
        bumped = self._take_bumped()
        if bumped is None or self._rebuild_due:
            self._rebuild_due = True
            return
        for safe in bumped:
            stems = self._by_safe.get(safe)
            if not stems:
                # A spelling with no file is one of the key's other identities.
                if os.path.lexists(os.path.join(self._base, f"{safe}.jsonl")):
                    self._names_due = True
                continue
            for stem in stems:
                if stem in self._entries:
                    self._reread(stem)
                else:
                    # Dropped by an earlier re-read and written again: its place in
                    # the directory order is only known from a names pass.
                    self._names_due = True

    # ------------------------------------------------------------------ rows

    def _make_entry(self, path: str, stem: str, de: "os.DirEntry[str] | None") -> _Entry:
        read = self._log._catalog_projection._listing_row(path, stem, de)
        canon = self._log._canonical_key(stem)
        return _Entry(
            read.status,
            read.row,
            read.volatile,
            None if read.stat is None else self._log._cache_identity(read.stat),
            canon,
            read.has_messages,
            _retained_bytes(read.row),
        )

    def _reread(self, stem: str) -> None:
        old = self._entries.get(stem)
        if old is None:
            return
        entry = self._make_entry(os.path.join(self._base, f"{stem}.jsonl"), stem, None)
        if entry.status == "gone":
            self._drop(stem)
            return
        if old.status == "link" and entry.status == "row":
            # A regular file where an alias was: renamed in, so its order moved.
            self._names_due = True
        self._install(stem, entry, old)

    def _install(self, stem: str, entry: _Entry, old: _Entry) -> None:
        """Replace *stem*'s entry *old*, which ``_entries`` holds, with *entry*."""
        self._entries[stem] = entry
        self._bytes += entry.size - old.size
        if self._touched is not None:
            self._touched.add(stem)
        if entry.volatile or entry.status == "unreadable":
            self._retry.add(stem)
        else:
            self._retry.discard(stem)
        if entry.row != old.row or entry.status != old.status:
            self._snapshot = None
        else:
            # The snapshot holds old.row, so keep that one object and charge it once.
            entry.row = old.row

    def _drop(self, stem: str) -> None:
        self._bytes -= self._entries.pop(stem).size
        if self._touched is not None:
            self._touched.add(stem)
        self._retry.discard(stem)
        self._snapshot = None

    def _reset(self, entries: dict[str, _Entry], order: list[str]) -> None:
        old = self._entries
        if (
            order == self._order
            and len(entries) == len(old)
            and all(old.get(k) is e for k, e in entries.items())
        ):
            return
        self._passes += 1
        self._snapshot = None
        self._entries = entries
        self._bytes = sum(e.size for e in entries.values())
        self._order = order
        self._retry = {k for k, e in entries.items() if e.volatile or e.status == "unreadable"}
        known = self._safe_of
        safe_of = {stem: known.get(stem) or _safe_key(stem) for stem in order}
        by_safe: dict[str, list[str]] = {}
        for stem, safe in safe_of.items():
            by_safe.setdefault(safe, []).append(stem)
        self._safe_of = safe_of
        self._by_safe = by_safe

    def _build_snapshot(self) -> list[dict]:
        """The scan's dedup and sort, over the indexed rows in directory order."""
        by_canon: dict[str, dict] = {}
        for stem in self._order:
            entry = self._entries.get(stem)
            if entry is None or entry.row is None:
                continue
            meta = entry.row
            existing = by_canon.get(entry.canon)
            if existing is None or meta["modified"] >= existing["modified"]:
                by_canon[entry.canon] = meta
        sessions = list(by_canon.values())
        sessions.sort(key=lambda s: s.get("modified", 0), reverse=True)
        return sessions


__all__ = ["RECONCILE_SECS", "SessionListingIndex"]
