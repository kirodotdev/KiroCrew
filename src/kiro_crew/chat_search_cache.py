"""One chat's history search cache: a SQLite FTS5 file of what each message holds.

The chat's history search window (``dashboard/chat_history_search.py``) answers
from this cache (``dashboard/chat_history_cached.py``), so a search does not decode
and redact the transcript again on every keystroke, and its per-kind counts cover
the whole chat.

What a chat's cache holds is decided by the caller: :meth:`ChatSearchCache.update`
takes an ``entry_of(msg, index)`` that turns a transcript row into one ``rows``
record (a tuple in :data:`COLUMNS` order) or None. The search window's builder
stores the message's text already redacted, the text the results show, so search
matches the redacted text shown in results and nothing else is kept.

When a cache is built. Only after a chat is searched, never for a chat nobody
searched: most chats (worker sessions) are never searched, and indexing them all
would cost disk and CPU for nothing. The first search of a chat in this process
(:meth:`ChatSearchCache.holds` is False, whether a file is on disk or not) is
answered by the paged scan, and only then does the caller start the update in a
worker thread; later searches read the cache. Updates run one at a time across
the process (``_BUILD_LOCK``), reading ``BUILD_PAGE_ROWS`` rows per page; each page
is committed with the count of rows it covers, so a build a write interrupts keeps
what it read.

How a cache stays current. Each update probes the chain's revision. The revision
the process last brought the cache to answers at once (:meth:`ChatSearchCache.current`
checks only that, so a search can run it without waiting for the build thread).
Otherwise the cache checks
the rows it already holds against the transcript by content, not by inode: the
dashboard's save replaces the file on every turn, so an inode or mtime test would
rebuild every chat after every turn. Row 0, the last ``VERIFY_ROWS`` rows it
covers and every row the chat may still edit in place (:func:`editable`) are
compared by digest. A changed row 0, a changed oldest row of that window, a different chain of files (a size rotation), fewer rows than before (a
rewind or a compaction), another format version or other redaction rules
(``history_index.loaded_rules_marker``) rebuild the cache; a changed row inside the
window or a changed watched row is read again; rows past it are appended. The
window stays at 256 rows: a turn often writes more rows than that (tool rows
included), and rows of the turn in progress are edited in place.

Where it lives. ``<sessions>/.search-cache/<stem>.db``, one SQLite file per chat,
owner-only from creation like the session search index, in WAL mode with
``secure_delete`` on. All files together stay under ``DISK_CAP_BYTES``: the least
recently searched go first (a search touches its file's mtime at most once every
``TOUCH_EVERY_SECS``). A chat whose file alone passes the cap after any commit
of an update (a new page, re-read rows or a reset) is removed and not built again in the process (:class:`CacheTooLarge`): its
search stays the paged scan. A chat's file is removed when the chat is deleted or
moved to trash, or when archive retention expires its segments
(``history_archive_text.remove_expired_segments``: a cache built before a size
rotation still holds the rows that moved into them), always by :func:`drop` and
before the source is unlinked, together with every cache whose chain of
transcripts reads it. Closing a chat keeps its file, since a closed chat can be
reopened and searched; the disk cap bounds the folder. No build starts while the
chat's delete is in flight, nor on a chain that reads a chat moved to the trash
(``drop(..., trashed=True)``) until it is restored (:meth:`ChatSearchCache.untrash`).

Deletes and builds. A file is written in place, page by page. A drop bumps the
drop generation of each stem it names before it sweeps the files whose stored
chain reads one; it never waits for a build. A build copies its chain's
generations when it starts, and after every commit and once more before it marks
the cache current it removes its own file (:class:`CacheDropped`) if one changed.
A rebuild unmarks the cache before its reset commit, and a search reads a file
only while it is marked (:meth:`ChatSearchCache.reading`), so no search reads a
partial table. A build that starts after a drop and before the transcript is gone
is refused up front, by ``delete_in_flight`` or by the trashed stems. Building into
a second file and renaming it into place was not taken: renaming an open WAL
database can corrupt it, and that design still needs these checks.

Connections. One connection per chat file, shared by the threads that use it and
held only under that file's lock, so a drop can close it before the file is
unlinked (a per-thread connection could not be closed from the dropping thread).
At most ``OPEN_MAX`` stay open; the least recently used idle one is closed.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any, NamedTuple

from kiro_crew._sqlite_compat import fts5_available, sqlite3
from kiro_crew.history_files import _safe_key
from kiro_crew.history_index import loaded_rules_marker
from kiro_crew.history_projection import TranscriptRevisionChanged
from kiro_crew.owner_only_files import (
    SQLITE_SIDECAR_SUFFIXES,
    mkdirs_owner_only,
    prepare_owner_only_sqlite,
)
from kiro_crew.sqlite_wal import enable_wal

logger = logging.getLogger(__name__)

#: The cache folder inside the sessions directory.
CACHE_DIR = ".search-cache"
SUFFIX = ".db"
#: Most bytes all chats' cache files (with their sidecars) may hold on disk together.
DISK_CAP_BYTES = 200 * 1024 * 1024
#: The newest rows of a cache checked against the transcript before it is reused.
VERIFY_ROWS = 256
#: Rows decoded per page while a cache is built or extended.
BUILD_PAGE_ROWS = 1000
#: Times an update starts over from a fresh probe when a write moves the transcript.
UPDATE_ATTEMPTS = 6
#: Seconds between two touches of one cache file's mtime (its last-search time).
TOUCH_EVERY_SECS = 60.0
#: Chat files kept open at once.
OPEN_MAX = 8
#: Bump when the stored shape changes; a file of another version is rebuilt.
FORMAT_VERSION = 2
#: The steer states a row can still leave for ``superseded`` (``chat_delivery``).
_OPEN_STEER_STATES = frozenset({"written", "requeued"})
#: Watched rows at most this far apart are read in one page.
_WATCH_SPAN_ROWS = 64

#: The ``rows`` columns, in the order ``entry_of`` returns them. ``body`` is the
#: case-folded text a query matches (what ``rows_fts`` indexes); ``msg`` is 0 for a
#: row that is no message and is kept only for its ``steer`` / ``succ`` ids.
COLUMNS = (
    "idx",
    "at",
    "kinds",
    "body",
    "shown",
    "static_hit",
    "html_files",
    "art",
    "steer",
    "succ",
    "nonempty",
    "msg",
    "mid",
    "ts",
)
_INSERT = f"INSERT INTO rows({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})"
_BUSY_TIMEOUT_MS = 10_000
_CONNECT_TIMEOUT_SECS = 30

_BUILD_LOCK = threading.Lock()
EntryOf = Callable[[dict, int], "tuple | None"]


class CacheDropped(RuntimeError):
    """The chat's cache was removed while it was read or written."""


class CacheTooLarge(CacheDropped):
    """The chat's cache alone passed ``DISK_CAP_BYTES``, so it was removed and is
    not built again in this process."""


class Snapshot(NamedTuple):
    """A cache brought up to one transcript revision: what a search reads."""

    key: str
    #: The drop generation the cache was built under; a later drop voids it.
    gen: int
    #: Rows of the transcript the cache covers, ``[0, total)``.
    total: int


def row_digest(msg: Any) -> str:
    """A short digest of one decoded row, the identity a cache checks rows by."""
    raw = json.dumps(msg, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.blake2b(raw.encode("utf-8", "surrogatepass"), digest_size=12).hexdigest()


class _State(NamedTuple):
    key: str
    chain: list[str]
    total: int
    head: str
    tail: dict[int, str]
    #: ``{index: digest}`` of the rows the chat may still edit in place.
    watch: dict[int, str]


def editable(msg: Any) -> bool:
    """Whether the chat may edit row *msg* in place after newer rows were written.

    Two edits reach rows of any age: switching a reply's regenerated variant
    (``chat_regenerate``, the newest reply that has variants, however far back)
    and marking a requeued steer superseded once its successor lands
    (``superseded_steer``). Every other in-place edit is to the turn being
    written, which the verify window covers.
    shortcut: a new kind of in-place edit to old rows must be named here.
    """
    if not isinstance(msg, dict):
        return False
    if msg.get("variants"):
        return True
    meta = msg.get("meta")
    return (
        isinstance(meta, dict)
        and meta.get("steer") is True
        and meta.get("steerState") in _OPEN_STEER_STATES
    )


class _Handle:
    """One chat file and the one connection to it, used only under ``lock``."""

    __slots__ = ("path", "lock", "conn")

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.conn: Any = None

    def open(self) -> Any:
        if self.conn is None:
            # Owner-only from creation: the cache holds a copy of the chat's text.
            mkdirs_owner_only(self.path.parent)
            prepare_owner_only_sqlite(self.path)
            conn = sqlite3.connect(
                str(self.path),
                timeout=_CONNECT_TIMEOUT_SECS,
                isolation_level=None,
                check_same_thread=False,
            )
            try:
                conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
                enable_wal(conn, timeout_secs=_BUSY_TIMEOUT_MS / 1000)
                # A re-read row's old text must not survive in a freed page.
                conn.execute("PRAGMA secure_delete=ON")
                _init_schema(conn)
            except BaseException:
                conn.close()
                raise
            self.conn = conn
        return self.conn

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            finally:
                self.conn = None


def _create_tables(conn: Any) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS rows("
        " idx INTEGER PRIMARY KEY, at REAL, kinds INTEGER NOT NULL, body TEXT,"
        " shown TEXT, static_hit TEXT, html_files INTEGER NOT NULL, art TEXT,"
        " steer TEXT, succ TEXT, nonempty INTEGER NOT NULL, msg INTEGER NOT NULL,"
        " mid TEXT, ts TEXT)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS rows_succ ON rows(succ) WHERE succ IS NOT NULL")
    # The text is folded already, so the tokenizer matches it as stored.
    # ``detail=none``: only which rows hold a trigram, a third of the file
    # ``full`` takes; FTS5 then refuses phrase queries, so a term is looked up
    # as its trigrams ANDed and tested as a substring in SQL. ``columnsize=0``:
    # nothing ranks, so no per-row token counts.
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS rows_fts USING fts5("
        "body, content='rows', content_rowid='idx', tokenize='trigram case_sensitive 1',"
        " detail=none, columnsize=0)"
    )
    conn.execute("CREATE TABLE IF NOT EXISTS meta(name TEXT PRIMARY KEY, value TEXT NOT NULL)")


def _init_schema(conn: Any) -> None:
    """Create the tables; drop them first when they hold another version or rules.

    A file that already holds this version and these rules is not written, so
    opening it leaves its mtime (its last-search time, see :meth:`ChatSearchCache.evict`)
    alone."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    rules = loaded_rules_marker()
    stored = None
    if version:
        with contextlib.suppress(sqlite3.OperationalError):
            row = conn.execute("SELECT value FROM meta WHERE name='rules'").fetchone()
            stored = row[0] if row else None
    if version and (version != FORMAT_VERSION or stored != rules):
        for table in ("rows_fts", "rows", "meta"):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        version = 0
        stored = None
    _create_tables(conn)
    if stored != rules:
        conn.execute("INSERT OR REPLACE INTO meta(name, value) VALUES ('rules', ?)", (rules,))
    if not version:
        conn.execute(f"PRAGMA user_version={FORMAT_VERSION}")


def _read_state(conn: Any) -> _State | None:
    meta = dict(conn.execute("SELECT name, value FROM meta").fetchall())
    if "total" not in meta:
        return None
    try:
        return _State(
            meta["key"],
            list(json.loads(meta["chain"])),
            int(meta["total"]),
            meta["head"],
            {int(i): str(d) for i, d in json.loads(meta["tail"])},
            {int(i): str(d) for i, d in json.loads(meta["watch"])},
        )
    except (KeyError, TypeError, ValueError):
        return None


def _read_state_read_only(path: Path) -> _State | None:
    """:func:`_read_state` of the file at *path* through a read-only connection."""
    conn = sqlite3.connect(
        f"{path.absolute().as_uri()}?mode=ro",
        uri=True,
        timeout=_CONNECT_TIMEOUT_SECS,
        isolation_level=None,
    )
    try:
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        return _read_state(conn)
    finally:
        conn.close()


def _write_state(conn: Any, state: _State) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO meta(name, value) VALUES (?, ?)",
        [
            ("key", state.key),
            ("chain", json.dumps(state.chain)),
            ("total", str(state.total)),
            ("head", state.head),
            ("tail", json.dumps(sorted(state.tail.items()))),
            ("watch", json.dumps(sorted(state.watch.items()))),
        ],
    )


def _insert(conn: Any, entries: list[tuple]) -> None:
    conn.executemany(_INSERT, entries)
    conn.executemany(
        "INSERT INTO rows_fts(rowid, body) VALUES (?, ?)",
        [(e[0], e[3]) for e in entries if e[3] is not None],
    )


def _delete(conn: Any, indexes: list[int]) -> None:
    for index in indexes:
        conn.execute(
            "INSERT INTO rows_fts(rows_fts, rowid, body)"
            " SELECT 'delete', idx, body FROM rows WHERE idx=? AND body IS NOT NULL",
            (index,),
        )
        conn.execute("DELETE FROM rows WHERE idx=?", (index,))


def _read_rows(log: Any, key: str, indexes: list[int], revision: Any) -> Iterator[tuple[int, Any]]:
    """``(index, row)`` for each of the sorted *indexes*, rows close together read
    in one page."""
    at = 0
    while at < len(indexes):
        first = indexes[at]
        end = at
        while end + 1 < len(indexes) and indexes[end + 1] - first < _WATCH_SPAN_ROWS:
            end += 1
        last = indexes[end]
        page = log.read_messages_chained_page(
            key, limit=last + 1 - first, before=last + 1, expected_revision=revision
        )
        if page.next_before != first or len(page.messages) != last + 1 - first:
            raise TranscriptRevisionChanged(key)
        for index in indexes[at : end + 1]:
            yield index, page.messages[index - first]
        at = end + 1


def _entries(rows: list, start: int, entry_of: EntryOf) -> list[tuple]:
    out = []
    for offset, msg in enumerate(rows):
        entry = entry_of(msg, start + offset) if isinstance(msg, dict) else None
        if entry is not None:
            out.append(entry)
    return out


class ChatSearchCache:
    """Every chat's search cache under one sessions directory."""

    def __init__(self, sessions_dir: Path) -> None:
        self.dir = Path(sessions_dir) / CACHE_DIR
        self._lock = threading.Lock()
        self._open: OrderedDict[str, _Handle] = OrderedDict()
        #: Per stem, how many times its file was dropped or the stem was named by
        #: :meth:`drop`: a build or read that started under an older count stops
        #: instead of writing the file back.
        self._gen: dict[str, int] = {}
        #: Per key, ``(revision, gen, rules, chain stems)`` the cache was last
        #: brought to. Only a key held here is read (:meth:`reading`).
        self._current: dict[str, tuple] = {}
        #: Stems moved to the trash (``drop(..., trashed=True)``), until
        #: :meth:`untrash`: no build starts on a chain that reads one, since the
        #: move drops the cache before it moves the transcript out. In memory only:
        #: a restart forgets them, which is safe, because a moved transcript is
        #: outside the sessions directory, where no build reads.
        self._trashed: set[str] = set()
        self._touched: dict[str, float] = {}
        #: Chats whose cache alone passed the disk cap: never built again here.
        self._too_large: set[str] = set()

    def path(self, key: str) -> Path:
        return self.dir / f"{_safe_key(key)}{SUFFIX}"

    # ----------------------------------------------------------- handles

    def _chain_dropped(self, gens: dict[str, int], chain: Iterable[str]) -> bool:
        """Whether a stem of *chain* was dropped since its gen in *gens*. Caller
        holds ``_lock``."""
        return any(self._gen.get(s, 0) != gens.get(s, 0) for s in chain)

    @contextlib.contextmanager
    def _use(self, key: str, gen: int) -> Iterator[Any]:
        """The open connection of *key*'s file, held alone for the block.

        Raises :class:`CacheDropped` when the file was dropped since *gen*.
        """
        stem = _safe_key(key)
        idle: list[_Handle] = []
        with self._lock:
            if self._gen.get(stem, 0) != gen:
                raise CacheDropped(key)
            handle = self._open.get(stem)
            if handle is None:
                handle = self._open[stem] = _Handle(self.dir / f"{stem}{SUFFIX}")
            self._open.move_to_end(stem)
            for other_stem, other in list(self._open.items())[: max(0, len(self._open) - OPEN_MAX)]:
                if other.lock.acquire(blocking=False):
                    del self._open[other_stem]
                    idle.append(other)
        for other in idle:
            try:
                other.close()
            finally:
                other.lock.release()
        with handle.lock:
            # A drop between the check above and this lock closed the handle.
            with self._lock:
                if self._gen.get(stem, 0) != gen:
                    raise CacheDropped(key)
            yield handle.open()

    @contextlib.contextmanager
    def reading(self, snap: Snapshot) -> Iterator[Any]:
        """The connection a search of *snap* reads through; see :meth:`_use`.

        Raises :class:`CacheDropped` when the cache is not current: a rebuild
        empties the table in place, and unmarks it first (:meth:`_update_once`).
        Checked under the file's lock, so a rebuild's reset waits for a search
        that got in first.
        """
        with self._use(snap.key, snap.gen) as conn:
            with self._lock:
                if snap.key not in self._current:
                    raise CacheDropped(snap.key)
            yield conn

    def holds(self, key: str) -> bool:
        """Whether this process brought *key*'s cache up to date at least once
        since it was last dropped. No IO."""
        stem = _safe_key(key)
        with self._lock:
            held = self._current.get(key)
            return held is not None and held[1] == self._gen.get(stem, 0)

    def touch(self, key: str) -> None:
        """Mark *key*'s cache as searched now, at most once a ``TOUCH_EVERY_SECS``."""
        stem = _safe_key(key)
        now = time.time()
        with self._lock:
            if now - self._touched.get(stem, 0.0) < TOUCH_EVERY_SECS:
                return
            self._touched[stem] = now
        with contextlib.suppress(OSError):
            os.utime(self.dir / f"{stem}{SUFFIX}")

    def too_large(self, key: str) -> bool:
        """Whether *key*'s cache passed the disk cap alone in this process, so no
        update builds it again (:class:`CacheTooLarge`). No IO."""
        with self._lock:
            return key in self._too_large

    # ------------------------------------------------------------- update

    def current(self, log: Any, key: str) -> Snapshot | None:
        """*key*'s cache as it is, when it already matches the transcript; else None.

        One probe of the chain and no build: it never waits for the build thread,
        so a search of an up-to-date chat is not queued behind another chat's
        build. Blocking IO (the probe), off the loop.
        """
        return self._matching(key, log.read_messages_chained_page(key, limit=0))[0]

    def _matching(self, key: str, probe: Any) -> tuple[Snapshot | None, int]:
        """``(snapshot, gen)``: the snapshot when the cache held for *key* matches
        *probe*'s revision under the current drop generation and rules, else None."""
        with self._lock:
            gen = self._gen.get(_safe_key(key), 0)
            held = self._current.get(key)
        if held is None or held[:3] != (probe.revision, gen, loaded_rules_marker()):
            return None, gen
        return Snapshot(key, gen, probe.total), gen

    def update(
        self, log: Any, key: str, entry_of: EntryOf, pause: Callable[[], None] | None = None
    ) -> Snapshot:
        """*key*'s cache brought up to the transcript; blocking IO, off the loop.

        Builds it on first use, appends rows written since, rebuilds it when the
        rows it holds changed (see the module docstring); a cache that already
        matches answers as :meth:`current` does. Raises
        ``TranscriptRevisionChanged`` when writes keep moving the transcript
        through ``UPDATE_ATTEMPTS`` probes, :class:`CacheTooLarge` for a chat
        :meth:`too_large`, :class:`CacheDropped` when the cache is
        dropped meanwhile, and ``OSError`` / ``sqlite3.Error`` when it cannot be
        read or written. *pause*, when given, runs before each page is read, so
        the caller can hold the build while other reads need the time.
        """
        probe = log.read_messages_chained_page(key, limit=0)
        ready, gen = self._matching(key, probe)
        if ready is not None:
            return ready
        if self.too_large(key):
            raise CacheTooLarge(key)
        stem = _safe_key(key)
        rules = loaded_rules_marker()
        with _BUILD_LOCK:
            # Copied before the delete-in-flight and trash checks below: a delete
            # or a move to trash they miss bumps one of these gens afterwards.
            keys = {key, *(item[0] for item in probe.revision)}
            with self._lock:
                gens = {s: self._gen.get(s, 0) for s in (_safe_key(k) for k in keys)}
                trashed = not self._trashed.isdisjoint(gens)
            # A build that starts during a delete or a move to trash, after its
            # drop and before the transcript is unlinked or moved, would read what
            # the delete or the move removes, and no later drop bumps a gen:
            # refused until the delete is done, or the chat is restored.
            in_flight = getattr(log, "delete_in_flight", None)
            if trashed or (in_flight is not None and any(in_flight(k) for k in keys)):
                raise CacheDropped(key)
            for attempt in range(1, UPDATE_ATTEMPTS + 1):
                try:
                    revision, total = self._update_once(log, key, gen, gens, entry_of, pause)
                    break
                except TranscriptRevisionChanged:
                    if attempt == UPDATE_ATTEMPTS:
                        raise
                    logger.debug("search cache: %s moved while it was read; again", key)
            chain = {stem, *(_safe_key(item[0]) for item in revision)}
            with self._lock:
                if self._gen.get(stem, 0) != gen:
                    raise CacheDropped(key)
                dropped = self._chain_dropped(gens, chain)
                if not dropped:
                    self._current[key] = (revision, gen, rules, frozenset(chain))
                    self._touched[stem] = time.time()
            if dropped:
                self._remove(stem)
                raise CacheDropped(key)
        self.evict(keep=self.path(key))
        return Snapshot(key, gen, total)

    def _update_once(
        self,
        log: Any,
        key: str,
        gen: int,
        gens: dict[str, int],
        entry_of: EntryOf,
        pause: Callable[[], None] | None,
    ) -> tuple[Any, int]:
        """One pass of :meth:`update`. Each commit (the reset, every :meth:`_verify`
        and every :meth:`_append`) is followed by :meth:`_after_commit`: a page
        committed with a dropped transcript's rows is readable at once, so the
        check follows the commit, never precedes it."""
        probe = log.read_messages_chained_page(key, limit=0)
        revision, total = probe.revision, probe.total
        chain = [item[0] for item in revision]
        with self._use(key, gen) as conn:
            state = _read_state(conn)
        if state is not None and (state.key != key or state.chain != chain or total < state.total):
            state = None
        if state is not None and state.total:
            state = self._verify(log, key, gen, gens, state, revision, entry_of)
        if state is None:
            # The reset empties the table in place: no earlier snapshot may read it.
            stem = _safe_key(key)
            with self._lock:
                for k in [k for k in self._current if _safe_key(k) == stem]:
                    del self._current[k]
            state = _State(key, chain, 0, "", {}, {})
            with self._use(key, gen) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("INSERT INTO rows_fts(rows_fts) VALUES ('delete-all')")
                conn.execute("DELETE FROM rows")
                _write_state(conn, state)
            self._after_commit(key, gens, state.chain)
        while state.total < total:
            if pause is not None:
                pause()
            end = min(total, state.total + BUILD_PAGE_ROWS)
            page = log.read_messages_chained_page(
                key, limit=end - state.total, before=end, expected_revision=revision
            )
            state = self._append(key, gen, gens, state, page.next_before, page.messages, entry_of)
        return revision, total

    def _after_commit(self, key: str, gens: dict[str, int], chain: Iterable[str]) -> None:
        """The step after every commit of a build or update. A stem of *key*'s chain
        dropped since *gens* removes the file (:class:`CacheDropped`). A chat whose
        file alone passes ``DISK_CAP_BYTES`` is removed and never cached again in
        the process (:class:`CacheTooLarge`), so its search stays the paged scan."""
        stem = _safe_key(key)
        with self._lock:
            hit = self._chain_dropped(gens, {stem, *(_safe_key(k) for k in chain)})
        if hit:
            self._remove(stem)
            raise CacheDropped(key)
        if self._disk_size(stem) > DISK_CAP_BYTES:
            self._remove(stem)
            with self._lock:
                self._too_large.add(key)
            raise CacheTooLarge(key)

    def _verify(
        self,
        log: Any,
        key: str,
        gen: int,
        gens: dict[str, int],
        state: _State,
        revision: Any,
        entry_of: EntryOf,
    ) -> _State | None:
        """Check *state*'s rows against the transcript; re-read changed rows.

        The rows checked are row 0, the newest ``VERIFY_ROWS`` and every watched
        row (:func:`editable`). None when the cache must be rebuilt: row 0 or the
        oldest window row changed, which a rewrite that moved rows causes.
        """
        low = max(0, state.total - VERIFY_ROWS)
        page = log.read_messages_chained_page(
            key, limit=state.total - low, before=state.total, expected_revision=revision
        )
        rows = page.messages
        if page.next_before != low or len(rows) != state.total - low:
            return None
        if low > 0:
            first = log.read_messages_chained_page(
                key, limit=1, before=1, expected_revision=revision
            )
            if not first.messages or row_digest(first.messages[0]) != state.head:
                return None
        elif row_digest(rows[0]) != state.head:
            return None
        digests = [row_digest(msg) for msg in rows]
        if state.tail.get(low) != digests[0]:
            return None
        changed = [
            (low + offset, msg)
            for offset, msg in enumerate(rows)
            if state.tail.get(low + offset) != digests[offset]
        ]
        older = sorted(i for i in state.watch if i < low)
        for index, msg in _read_rows(log, key, older, revision):
            if row_digest(msg) != state.watch[index]:
                changed.append((index, msg))
        if not changed:
            return state
        tail = dict(state.tail)
        watch = dict(state.watch)
        entries = []
        for index, msg in changed:
            digest = row_digest(msg)
            if index >= low:
                tail[index] = digest
            if editable(msg):
                watch[index] = digest
            else:
                watch.pop(index, None)
            entry = entry_of(msg, index) if isinstance(msg, dict) else None
            if entry is not None:
                entries.append(entry)
        state = state._replace(tail=tail, watch=watch)
        with self._use(key, gen) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            _delete(conn, [index for index, _msg in changed])
            _insert(conn, entries)
            _write_state(conn, state)
        self._after_commit(key, gens, state.chain)
        return state

    def _append(
        self,
        key: str,
        gen: int,
        gens: dict[str, int],
        state: _State,
        start: int,
        rows: list,
        entry_of: EntryOf,
    ) -> _State:
        entries = _entries(rows, start, entry_of)
        end = start + len(rows)
        floor = end - VERIFY_ROWS
        tail = {i: d for i, d in state.tail.items() if i >= floor}
        for offset in range(max(0, floor - start), len(rows)):
            tail[start + offset] = row_digest(rows[offset])
        head = row_digest(rows[0]) if start == 0 and rows else state.head
        watch = dict(state.watch)
        for offset, msg in enumerate(rows):
            if editable(msg):
                watch[start + offset] = tail.get(start + offset) or row_digest(msg)
        state = state._replace(total=end, head=head, tail=tail, watch=watch)
        with self._use(key, gen) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            _insert(conn, entries)
            _write_state(conn, state)
        self._after_commit(key, gens, state.chain)
        return state

    # ------------------------------------------------------------ removal

    def _remove(self, stem: str) -> bool:
        """Close and unlink one chat's file and its sidecars. False when a file
        that exists could not be removed."""
        with self._lock:
            self._gen[stem] = self._gen.get(stem, 0) + 1
            handle = self._open.pop(stem, None)
            self._touched.pop(stem, None)
            for key in [k for k in self._current if _safe_key(k) == stem]:
                del self._current[key]
        if handle is not None:
            with handle.lock:
                handle.close()
        ok = True
        base = self.dir / f"{stem}{SUFFIX}"
        for path in (base, *(Path(f"{base}{s}") for s in SQLITE_SIDECAR_SUFFIXES)):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                logger.warning("search cache: could not remove %s", path, exc_info=True)
                ok = False
        return ok

    def drop(self, names: Iterable[str], *, trashed: bool = False) -> bool:
        """Remove the caches of *names* (every spelling of one chat), and every
        cache whose chain of transcripts reads one of them: a tab's later
        transcript holds its predecessors' rows. False on a file that exists and
        could not be removed, or a cache directory that could not be listed.

        *trashed*: the chats are moving to the trash, so no build reads them
        again until :meth:`untrash` (the move to trash runs this before it moves
        the transcripts, and a build that starts in between sees no later gen
        bump to stop it).

        The gens of *names* are bumped first, the files swept second, and never
        waits for a build (``_BUILD_LOCK``). A build commits a page first and
        compares its gens second (:meth:`_after_commit`), so a build that committed
        before the bump is in the files the sweep finds (its chain is stored), and
        one that commits after it voids itself."""
        stems = {_safe_key(name) for name in names}
        with self._lock:
            for stem in stems:
                self._gen[stem] = self._gen.get(stem, 0) + 1
            if trashed:
                self._trashed |= stems
        holders = self._holders(stems)
        ok = holders is not None
        for stem in stems | (holders or set()):
            ok = self._remove(stem) and ok
        return ok

    def untrash(self, names: Iterable[str]) -> None:
        """Let *names* be cached again: they are back from the trash, or their
        move to trash left them in place. No IO."""
        stems = {_safe_key(name) for name in names}
        with self._lock:
            self._trashed -= stems

    def _holders(self, stems: set[str]) -> set[str] | None:
        """The other cache files whose stored chain names a transcript in *stems*;
        None when the cache directory cannot be listed. A file whose chain cannot
        be read counts (removing a cache loses nothing); one with no chain yet holds
        no rows, since a build stores its chain before its first page."""
        try:
            names = os.listdir(self.dir)
        except FileNotFoundError:
            return set()
        except OSError:
            logger.warning("search cache: could not list %s", self.dir, exc_info=True)
            return None
        found = set()
        for name in names:
            stem = name[: -len(SUFFIX)]
            if not name.endswith(SUFFIX) or stem in stems:
                continue
            chain = self._stored_chain(stem)
            if chain is None or any(_safe_key(k) in stems for k in chain):
                found.add(stem)
        return found

    def _stored_chain(self, stem: str) -> list[str] | None:
        """The chain of transcript keys *stem*'s cache was read from; ``[]`` when it
        holds no chain yet, None when it cannot be read.

        An open file is read through its one connection (a second one to the same
        file would drop this process's SQLite locks on it when closed). Any other
        is read through a read-only connection made under its handle's lock, so no
        connection of this process opens it meanwhile: no schema check, no write,
        no checkpoint, so its mtime (its last-search time) does not move."""
        with self._lock:
            handle = self._open.get(stem)
            if handle is None:
                handle = self._open[stem] = _Handle(self.dir / f"{stem}{SUFFIX}")
        try:
            with handle.lock:
                if handle.conn is not None:
                    state = _read_state(handle.conn)
                else:
                    state = _read_state_read_only(handle.path)
        except (OSError, sqlite3.Error):
            return None
        return state.chain if state is not None else []

    def _disk_size(self, stem: str) -> int:
        """Bytes one chat's file and its sidecars hold on disk now."""
        base = self.dir / f"{stem}{SUFFIX}"
        size = 0
        for path in (base, *(Path(f"{base}{s}") for s in SQLITE_SIDECAR_SUFFIXES)):
            with contextlib.suppress(OSError):
                size += path.stat().st_size
        return size

    def evict(self, *, keep: Path | None = None, cap: int | None = None) -> int:
        """Remove the least recently searched cache files until all fit the cap.

        The file at *keep* stays. Sizes include each file's sidecars. Returns the
        bytes left on disk.
        """
        cap = DISK_CAP_BYTES if cap is None else cap
        files: list[tuple[float, int, Path]] = []
        try:
            with os.scandir(self.dir) as listing:
                names = {item.name: item for item in listing}
        except OSError:
            return 0
        for name, item in names.items():
            if not name.endswith(SUFFIX):
                continue
            with contextlib.suppress(OSError):
                st = item.stat(follow_symlinks=False)
                size = st.st_size
                for suffix in SQLITE_SIDECAR_SUFFIXES:
                    side = names.get(name + suffix)
                    if side is not None:
                        with contextlib.suppress(OSError):
                            size += side.stat(follow_symlinks=False).st_size
                files.append((st.st_mtime, size, Path(item.path)))
        used = sum(size for _mtime, size, _path in files)
        for _mtime, size, path in sorted(files):
            if used <= cap:
                break
            if keep is not None and path == keep:
                continue
            if self._remove(path.name[: -len(SUFFIX)]):
                used -= size
        return used

    def close_all(self) -> None:
        """Close every open connection (files stay)."""
        with self._lock:
            handles = list(self._open.values())
            self._open.clear()
            self._current.clear()
        for handle in handles:
            with handle.lock:
                handle.close()


_stores: dict[str, ChatSearchCache] = {}
_stores_lock = threading.Lock()


def available() -> bool:
    """Whether this SQLite can hold a cache (FTS5 with the trigram tokenizer)."""
    return fts5_available()


def store_for(sessions_dir: Path) -> ChatSearchCache:
    """The one cache store of *sessions_dir* in this process."""
    marker = os.path.abspath(os.fspath(sessions_dir))
    with _stores_lock:
        store = _stores.get(marker)
        if store is None:
            store = _stores[marker] = ChatSearchCache(Path(marker))
        return store


def drop(sessions_dir: Path, names: Iterable[str], *, trashed: bool = False) -> bool:
    """Remove the search caches of *names* under *sessions_dir*; see :meth:`ChatSearchCache.drop`."""
    return store_for(sessions_dir).drop(names, trashed=trashed)


def untrash(sessions_dir: Path, names: Iterable[str]) -> None:
    """Let *names* under *sessions_dir* be cached again; see :meth:`ChatSearchCache.untrash`."""
    store_for(sessions_dir).untrash(names)


def reset_stores() -> None:
    """Close and forget every store of this process (the files stay)."""
    with _stores_lock:
        stores = list(_stores.values())
        _stores.clear()
    for store in stores:
        store.close_all()


__all__ = [
    "CACHE_DIR",
    "COLUMNS",
    "DISK_CAP_BYTES",
    "VERIFY_ROWS",
    "CacheDropped",
    "CacheTooLarge",
    "ChatSearchCache",
    "Snapshot",
    "available",
    "drop",
    "reset_stores",
    "row_digest",
    "store_for",
    "untrash",
]
