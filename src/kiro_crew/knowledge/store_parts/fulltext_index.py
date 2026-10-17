"""The full-text index: ``items_fts`` writes, its term representation and keyword search.

Every row write into ``items_fts`` goes through ``_fts_index`` / ``_fts_unindex``, in
the representation the index holds (CJK-segmented once ``PRAGMA user_version``
reaches ``FTS_INDEX_VERSION``); the rebuild's ``'delete-all'`` reset is the one other
write. ``ensure_fts_index_current`` migrates a legacy index on the first read, and
``search_items_fts`` is the store's own keyword search.

A function here whose first parameter is ``self`` is a ``KnowledgeStore`` method:
the class binds it under the same name in ``knowledge/store.py``, so it reads the
store's fields and calls its sibling methods through ``self``.

The ``KnowledgeStore`` fields this module uses. Writes: ``_fts_index_current``,
``_fts_segmented``. Reads: ``_fts_lock``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from kiro_crew._sqlite_compat import fts5_cjk_match_groups, fts5_segment_for_index, sqlite3

if TYPE_CHECKING:
    from kiro_crew.knowledge.store import KnowledgeStore

logger = logging.getLogger("kiro_crew.knowledge.store")


# Bumped when the *term representation* stored in ``items_fts`` changes, which a
# schema probe cannot detect: the CREATE statement is identical either way, only
# the text handed to the index differs. Version 1 segments CJK characters
# (fts5_segment_for_index) so a word inside a spaceless run is addressable.
FTS_INDEX_VERSION = 1


_FTS_REBUILD_BATCH = 500


def ensure_fts_index_current(self: KnowledgeStore) -> None:
    """Rebuild ``items_fts`` if it holds a stale term representation.

    Called first by the gateway's post-bind knowledge maintenance task
    (``_kick_knowledge_orphan_reclaim``, on a worker thread), and by each of
    the three FTS readers before it matches -- :meth:`search_items_fts`,
    ``HybridRetriever._keyword_search``, and the dashboard's entity-items
    lookup -- as the fallback when that task could not take the writer lock.
    Deliberately NOT called
    from ``__init__``: the constructor runs on the gateway's boot path, and a
    rebuild is proportional to corpus size, so migrating there would stall
    the gateway at boot for a large legacy library. Every caller runs on a
    worker thread, so the one-time cost lands after the listener binds.

    Steady state is a single boolean check. The first caller takes the lock
    and does the work; concurrent readers wait rather than each starting
    their own rebuild.

    **A migration that cannot get the writer lock is not a search failure.**
    The rebuild opens ``BEGIN IMMEDIATE``, so a concurrent long import can
    hold the lock past ``busy_timeout`` and raise ``OperationalError``.
    Every caller is a reader whose own query already degrades to "no keyword
    hits" on that error, and letting it escape from here instead turns a
    transient lock into an HTTP 500 -- one that the dashboard's entity lookup
    does not even guard. So a lock failure leaves the index at its old
    representation and returns: the read that follows finds legacy terms,
    which costs the CJK recall this PR restores for that one request and
    nothing else, and ``_fts_index_current`` stays False so the next reader
    retries. Only lock/contention errors are absorbed -- a corrupt database
    raises ``DatabaseError``, which is not caught here.
    """
    if self._fts_index_current:
        return
    with self._fts_lock:
        if self._fts_index_current:
            return
        try:
            self._migrate_fts_index()
        except sqlite3.OperationalError:
            logger.warning(
                "knowledge: FTS index migration could not take the writer lock; "
                "serving the legacy index for now and retrying on the next read",
                exc_info=True,
            )
            return
        self._fts_index_current = True


def _migrate_fts_index(self: KnowledgeStore) -> None:
    """Re-index ``items_fts`` when its stored term representation is stale.

    Gated on ``PRAGMA user_version`` rather than a schema probe, because the
    ``CREATE VIRTUAL TABLE`` text is identical before and after: what changed
    is the text handed to the index, which SQLite does not record anywhere.
    ``user_version`` is otherwise unused by this database.

    Ordering matters. The version is bumped only after the whole rebuild
    commits, so a crash or a kill part-way through leaves the marker at its
    old value and the next open starts over. A partially rebuilt index is
    therefore always transient, never a resting state.
    """
    version = self.db.execute("PRAGMA user_version").fetchone()[0]
    if version >= FTS_INDEX_VERSION:
        self._fts_segmented = True
        return
    rows = self.db.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    if rows:
        logger.info(
            "knowledge: re-indexing %d item(s) for FTS index format v%d (CJK-segmented terms)",
            rows,
            FTS_INDEX_VERSION,
        )
    # IMMEDIATE so the writer lock is held for the whole rebuild: that is what
    # stops a concurrent writer from reading the old representation and then
    # writing terms the migrated index cannot match.
    self.db.execute("BEGIN IMMEDIATE")
    try:
        # 'delete-all' is the documented reset for an external-content table:
        # it drops the index without touching `items`, which holds the data.
        self.db.execute("INSERT INTO items_fts (items_fts) VALUES ('delete-all')")
        # Declared before the re-insert, not after: the rows below are written
        # through _fts_index, which asks _fts_terms_segmented what to write,
        # and PRAGMA user_version is still the old value until this
        # transaction commits.
        self._fts_segmented = True
        last = 0
        while True:
            batch = self.db.execute(
                "SELECT rowid, title, content, tags FROM items "
                "WHERE rowid > ? ORDER BY rowid LIMIT ?",
                (last, self._FTS_REBUILD_BATCH),
            ).fetchall()
            if not batch:
                break
            for row in batch:
                self._fts_index(row["rowid"], row["title"], row["content"], row["tags"])
                last = row["rowid"]
        self.db.execute(f"PRAGMA user_version = {FTS_INDEX_VERSION:d}")
        self.db.execute("COMMIT")
    except Exception:
        self.db.execute("ROLLBACK")
        # The index is back to whatever it held before, so the declaration
        # has to go back to unknown rather than to False -- another process
        # may have migrated the same database meanwhile.
        self._fts_segmented = None
        raise


def _fts_terms_segmented(self: KnowledgeStore) -> bool:
    """Whether ``items_fts`` currently holds CJK-segmented terms.

    A writer must use the representation the index already holds, because
    FTS5's ``'delete'`` subtracts the exact terms it is handed: handing
    segmented terms to a not-yet-migrated index raises
    ``DatabaseError: database disk image is malformed``. A legacy database
    has legitimate writers before any reader can migrate it -- the orphan
    reclaim in ``_migrate`` runs inside the constructor, and the startup
    watcher sweep can update or delete an item before the first search --
    so "segment unconditionally" is not available.

    **Serialized by SQLite's writer lock, not by a Python lock.** Every
    caller reads this from inside a ``BEGIN IMMEDIATE`` transaction, and the
    rebuild flips it from inside one too. SQLite admits one writer at a time,
    so a reader of this value already excludes the only thing that can change
    it -- across processes as well as threads, which a Python lock could not
    do. Taking a Python lock here instead would invert against SQLite's:
    a writer holding SQLite's lock would wait on Python's while the
    rebuilding reader holds Python's and waits on SQLite's.

    A True answer is latched, since ``user_version`` only ever increases, so
    the steady state costs nothing. A False answer is deliberately NOT
    cached: another process (an MCP tool server on the same database) may
    migrate it at any time, and a cached False would have this process keep
    writing raw terms into a migrated index.
    """
    if self._fts_segmented:
        return True
    version = self.db.execute("PRAGMA user_version").fetchone()[0]
    if version >= FTS_INDEX_VERSION:
        self._fts_segmented = True
    return bool(self._fts_segmented)


def _fts_terms(self: KnowledgeStore, title, content, tags) -> tuple[str, str, str]:
    """The three column values as this database's index represents them."""
    values = (title or "", content or "", tags or "")
    if not self._fts_terms_segmented():
        return values
    return (
        fts5_segment_for_index(values[0]),
        fts5_segment_for_index(values[1]),
        fts5_segment_for_index(values[2]),
    )


def _fts_index(self: KnowledgeStore, rowid, title, content, tags) -> None:
    """Index one item's row in the representation the index holds.

    The single write path into ``items_fts``. Centralised because the
    representation is not a per-call-site choice: an index built from
    segmented text and probed with un-segmented text does not match, and the
    reverse raises.

    Holds no Python lock, by design -- see ``_fts_terms_segmented``. Callers
    must already own SQLite's writer lock (``BEGIN IMMEDIATE``).
    """
    self.db.execute(
        "INSERT INTO items_fts (rowid, title, content, tags) VALUES (?, ?, ?, ?)",
        (rowid, *self._fts_terms(title, content, tags)),
    )


def _fts_unindex(self: KnowledgeStore, rowid, title, content, tags) -> None:
    """Remove one item's row from ``items_fts``.

    FTS5's ``'delete'`` command subtracts the terms it is given, so it has to
    be given the same text that was indexed. Passing the wrong
    representation either leaves the original terms in the index -- which
    keeps serving deleted or superseded content as live hits, and which
    ``'integrity-check'`` does not flag -- or raises
    ``database disk image is malformed`` outright.

    Holds no Python lock, by design -- see ``_fts_terms_segmented``. Callers
    must already own SQLite's writer lock (``BEGIN IMMEDIATE``).
    """
    self.db.execute(
        "INSERT INTO items_fts (items_fts, rowid, title, content, tags) "
        "VALUES ('delete', ?, ?, ?, ?)",
        (rowid, *self._fts_terms(title, content, tags)),
    )


def search_items_fts(self: KnowledgeStore, query, limit=10, offset=0) -> list:
    self.ensure_fts_index_current()
    safe = self._sanitize_fts5(query)
    if not safe:
        return []
    try:
        rows = self.db.execute(
            "SELECT i.*, fts.rank FROM items_fts fts "
            "JOIN items i ON i.rowid = fts.rowid "
            "WHERE items_fts MATCH ? ORDER BY fts.rank LIMIT ? OFFSET ?",
            (safe, limit, offset),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [self._serialize_item(r) for r in rows]


def _sanitize_fts5(query: str) -> str:
    """Escape user input for FTS5 MATCH, ANDing the query's tokens.

    Tokens stay individually quoted so the user's input can never contribute
    FTS5 operators. CJK runs expand to their adjacent-character phrases
    (``fts5_cjk_match_groups``) because a spaceless run is one whitespace
    token but several words; non-CJK input is unchanged. The join is AND:
    this is the store's direct-search surface, where every typed word is
    taken as deliberate.
    """
    return " AND ".join(fts5_cjk_match_groups(query))
