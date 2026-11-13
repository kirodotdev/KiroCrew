"""Which sources hold an item, and which document's state row owns it.

``source_locations`` records every source that holds an item; the per-document state
tables (``_DOC_STATE_TABLES``) record the item group each document owns. The methods
here keep the two in step when an item changes hands -- reassignment and adoption,
releasing a claim no copy in the source backs, reading a row's surviving group --
and read which items the agent aggregate's rows own.

A function here whose first parameter is ``self`` is a ``KnowledgeStore`` method:
the class binds it under the same name in ``knowledge/store.py``, so it reads the
store's fields and calls its sibling methods through ``self``.

The ``KnowledgeStore`` fields this module uses. Writes: none. Reads: none; every query
goes through ``self.db``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING, AbstractSet
from uuid import uuid4

from kiro_crew.knowledge.store_parts.source_rows import _ACCOUNT_BOUND_SOURCE_TYPE

if TYPE_CHECKING:
    from kiro_crew.knowledge.store import KnowledgeStore

logger = logging.getLogger("kiro_crew.knowledge.store")


class LiveSourceHoldsNoItems(ValueError):
    """An item would come to name an account-bound source.

    Raised by :meth:`KnowledgeStore.add_item` and
    :meth:`KnowledgeStore.add_source_location_in_txn` before the write. A
    ``bedrock_kb`` row is queried live and holds no items, and ``export_all``
    relies on that when it leaves the row out of a bundle: an item naming a
    source the bundle does not carry is refused by ``import_bundle`` as a
    malformed bundle, and the whole bundle rolls back.
    The rule lives here, with the writer, because every path that lands an
    item under a source funnels through these two methods -- the agent's
    ingest-text route, a folder or upload ingest, and the dedup path that
    attaches an existing item to a second holder -- and a source id is
    readable by any authenticated caller off the sources list.
    """


# Named by the path callers import it from: a traceback renders a raised class as
# ``module.qualname``, so this module's name must not reach it.
LiveSourceHoldsNoItems.__module__ = "kiro_crew.knowledge.store"


# The per-document state tables, each with the status that means "this row owns a
# live item group". The vocabularies genuinely differ and are NOT interchangeable:
# a folder file is scan-driven, so it moves pending -> done and can be re-walked,
# while an aggregate document is push-driven with no scanner to revive it and is
# either 'active' or 'deduped'. Writing 'done' to an aggregate row hides it from
# ``find_document_by_hash``, which matches on 'active' -- and an invisible row lets
# identical content in under a second uri as a duplicate.
_DOC_STATE_TABLES: tuple[tuple[str, str], ...] = (
    ("folder_file_state", "done"),
    ("artifact_item_state", "active"),
    ("agent_item_state", "active"),
)


# Which column identifies ONE document within a doc-state table. Ownership has to
# be derived per document, and the hash cannot do it: two documents in one source
# may legitimately hold identical text, so a hash-scoped read names one physical
# item into two groups and the first delete of either destroys it. An allowlist
# rather than a caller-supplied column name, because these identifiers are
# interpolated into SQL.
_DOC_STATE_KEY_COL: dict[str, str] = {
    "folder_file_state": "file_path",
    "artifact_item_state": "slug",
    "agent_item_state": "slug",
}


# Which column on each state table holds a hash in the SAME DOMAIN as
# ``items.content_hash``, for lookups that have to relate a state row to items.
#
# * ``folder_file_state.content_hash`` is sha256 over the file's RAW BYTES, so
#   the change gate can run before extraction.
# * ``items.content_hash`` is sha256 over the EXTRACTED TEXT.
#
# They coincide for .md/.txt but not for PDF, DOCX or HTML, where a
# cross-domain comparison silently matches nothing. Folder rows therefore carry
# the text hash in ``text_hash``; the aggregate tables already store a text
# hash in ``content_hash``.
_OWNERSHIP_HASH_COL: dict[str, str] = {
    "folder_file_state": "COALESCE(NULLIF(text_hash, ''), content_hash)",
    "artifact_item_state": "content_hash",
    "agent_item_state": "content_hash",
}
# Folder rows COALESCE so a row without ``text_hash`` (never backfilled) still
# matches for plaintext files; a rescan fills ``text_hash``.


def agent_owned_item_ids(self: KnowledgeStore, source_id: str) -> set[str] | None:
    """Every item id a live ``agent_item_state`` row names for a source.

    A row left by a refused write names nothing; the winner's row names its
    items. Returns ``None`` -- "cannot tell" -- if ANY row's ``item_ids`` is
    unreadable, and callers (the residue sweep,
    ``agent_source.clear_ingesting``) stand down: a stale item is
    recoverable, a wrongly-deleted one is not.
    """
    ids_owned: set[str] = set()
    for row in self.db.execute(
        "SELECT item_ids FROM agent_item_state WHERE source_id = ?", (source_id,)
    ).fetchall():
        raw = row["item_ids"]
        if raw in (None, ""):
            continue
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if isinstance(parsed, list):
            ids_owned.update(i for i in parsed if isinstance(i, str))
    return ids_owned


def _adopt_reassigned_item(self: KnowledgeStore, item_id: str, new_source_id: str) -> None:
    """Let the new owner's state row know it owns *item_id*.

    Reassignment moves ``items.source_id``, but a row's delete path consults
    only its own ``item_ids``; an item the row never names would stay
    searchable after the document is removed.

    Matched on ``content_hash``. Appends, so a chunked group is not
    truncated, and clears any deferral marker.

    An ambiguous hash (two documents in the source with identical text)
    adopts nothing: a stale claim is recoverable, while one item in two
    groups is destroyed by either document's delete.
    """
    row = self.db.execute("SELECT content_hash FROM items WHERE id = ?", (item_id,)).fetchone()
    content_hash = row["content_hash"] if row else None
    if not content_hash:
        return
    matches: list[tuple[str, str, object]] = []
    for table, healthy in _DOC_STATE_TABLES:
        hash_col = _OWNERSHIP_HASH_COL[table]
        for st in self.db.execute(
            f"SELECT rowid, item_ids FROM {table} "  # noqa: S608
            f"WHERE source_id = ? AND {hash_col} = ?",
            (new_source_id, content_hash),
        ).fetchall():
            matches.append((table, healthy, st))
    if len(matches) != 1:
        if matches:
            logger.warning(
                "Not adopting item into source %s: %d documents there share this "
                "content, so the hash does not say which one owns it",
                new_source_id,
                len(matches),
            )
        return
    table, healthy, st = matches[0]
    try:
        ids = json.loads(st["item_ids"] or "[]")
    except (TypeError, ValueError):
        ids = []
    if item_id not in ids:
        ids.append(item_id)
        self.db.execute(
            f"UPDATE {table} SET item_ids = ?, status = ?, "  # noqa: S608
            "merged_into_source_id = NULL WHERE rowid = ?",
            (json.dumps(ids), healthy, st["rowid"]),
        )


def detach_source_location_by_hash(
    self: KnowledgeStore,
    source_id: str,
    content_hash: str,
    *,
    exclude_folder_paths: AbstractSet[str] = frozenset(),
) -> int:
    """Drop this source's CLAIM on a document it has no copy of.

    The counterpart to :meth:`_adopt_reassigned_item`. A dedup loser holds no
    items ('deduped', empty group) but is still a location of the winner's.
    When its copy is removed, the claim must go too, or it could inherit the
    document and resurface text with no file behind it.

    Identified by ``content_hash``, the only handle such a row has; the
    winner's ids must never be written into it. Only location rows are
    removed. Returns the number of claims dropped.

    Acts only when the hash picks out ONE document here; on ambiguity the
    claim is kept (a stale claim is recoverable, a released shared one is
    not). ``exclude_folder_paths`` omits rows the current sweep will remove
    from the claimant count.
    """
    if not content_hash:
        return 0
    claimants = 0
    for table, _healthy in _DOC_STATE_TABLES:
        hash_col = _OWNERSHIP_HASH_COL[table]
        if table == "folder_file_state" and exclude_folder_paths:
            # Filtered here rather than as a NOT IN list: a sweep can remove more
            # files than SQLite accepts as bound parameters.
            rows = self.db.execute(
                f"SELECT file_path FROM {table} "  # noqa: S608
                f"WHERE source_id = ? AND {hash_col} = ?",
                (source_id, content_hash),
            ).fetchall()
            claimants += sum(1 for r in rows if r["file_path"] not in exclude_folder_paths)
            continue
        hit = self.db.execute(
            f"SELECT COUNT(*) AS n FROM {table} "  # noqa: S608
            f"WHERE source_id = ? AND {hash_col} = ?",
            (source_id, content_hash),
        ).fetchone()
        claimants += int(hit["n"] or 0) if hit else 0
    if claimants > 1:
        logger.warning(
            "Keeping source %s's claim: %d documents there share this content, so "
            "releasing it could strand one of them",
            source_id,
            claimants,
        )
        return 0
    cur = self.db.execute(
        "DELETE FROM source_locations WHERE source_id = ? AND item_id IN "
        "(SELECT id FROM items WHERE content_hash = ?)",
        (source_id, content_hash),
    )
    return cur.rowcount or 0


def release_stale_claim(
    self: KnowledgeStore,
    source_id: str,
    prev_hash: str | None,
    new_hash: str,
    prev_item_ids: list[str],
    prev_text_hash: str | None = None,
) -> int:
    """Release a claim made for content this source does not have.

    A dedup loser's claim is specific to its content; once its copy is EDITED,
    deleting the holder would hand it the superseded text. Shared by the
    folder, artifact and agent ingest paths. Fires only when the row owned
    NOTHING and the hash moved. Returns claims dropped.

    *prev_hash*/*new_hash* decide whether the bytes changed; the detach needs
    an ITEM-domain hash, which *prev_text_hash* carries for folder rows.
    Artifacts and agent documents already store a text hash and pass nothing.
    """
    if prev_item_ids or not prev_hash or prev_hash == new_hash:
        return 0
    return self.detach_source_location_by_hash(source_id, prev_text_hash or prev_hash)


def surviving_group_in_txn(self: KnowledgeStore, table: str, source_id: str, key: str) -> list[str]:
    """Items a doc-state row already names and this source still owns.

    The caller must already hold a write transaction, and must not be on the
    event loop: this issues sync sqlite reads whose result is only meaningful
    under that lock.

    A REFUSED document's terminal write cannot assume an empty group: between
    the gate's commit and this write, a ``delete_source_cascade`` on the
    holder may reassign the surviving item here and
    :meth:`_adopt_reassigned_item` name it in this row. An empty write would
    leave that last copy named by no row, and undeletable.

    Row-scoped, never by content hash, so another document with identical
    text never shares its items. Filtered to ids still under this source.

    An unreadable ``item_ids`` RAISES: the caller writes the result as the
    terminal state, so "owns nothing" would orphan every item it named.
    """
    key_col = _DOC_STATE_KEY_COL[table]
    row = self.db.execute(
        f"SELECT item_ids FROM {table} "  # noqa: S608
        f"WHERE source_id = ? AND {key_col} = ?",
        (source_id, key),
    ).fetchone()
    if not row:
        return []
    raw = row["item_ids"]
    if raw in (None, ""):
        return []
    try:
        ids = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"{table} item_ids unreadable for {key!r} in source {source_id} " f"({exc})"
        ) from exc
    if not isinstance(ids, list) or not ids:
        return []
    # Bounded by chunker.MAX_CHUNKS_PER_FILE, so the bind count cannot reach
    # SQLITE_MAX_VARIABLE_NUMBER.
    placeholders = ",".join("?" for _ in ids)
    return [
        r["id"]
        for r in self.db.execute(
            f"SELECT id FROM items WHERE id IN ({placeholders}) AND source_id = ?",  # noqa: S608,E501
            (*ids, source_id),
        ).fetchall()
    ]


def add_source_location(
    self: KnowledgeStore, item_id, source_id, chunk_range=None, section_title=None, anchor=None
):
    """Record that *source_id* holds *item_id*, at an optional position within it.

    ``OR IGNORE`` against ``UNIQUE (item_id, source_id)``: a document reachable
    from two sources has one row per source, and re-attaching a pair that already
    exists is a no-op. Attaching a second source is what keeps the item alive when
    the first is deleted -- see ``sources_holding_item``.
    """
    self.add_source_location_in_txn(
        item_id, source_id, chunk_range=chunk_range, section_title=section_title, anchor=anchor
    )
    self.db.commit()


def add_source_location_in_txn(
    self: KnowledgeStore, item_id, source_id, chunk_range=None, section_title=None, anchor=None
):
    """:meth:`add_source_location` without the commit, for a caller in a write txn.

    The connection runs in autocommit mode, so ``db.commit()`` inside an
    explicit ``BEGIN IMMEDIATE`` would END that transaction early and hand a
    concurrent writer the very gap the caller took the lock to close.
    """
    # A location is the other way an item comes to name a source: the
    # dedup path attaches an existing item to the ingesting source here
    # instead of inserting a copy.
    self._refuse_live_source(source_id)
    lid = str(uuid4())
    now = datetime.now().isoformat()
    self.db.execute(
        "INSERT OR IGNORE INTO source_locations "
        "(id, item_id, source_id, chunk_range, section_title, anchor, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (lid, item_id, source_id, chunk_range, section_title, anchor, now),
    )


def sources_holding_item(
    self: KnowledgeStore, item_id: str, exclude_source_id: str | None = None
) -> list[str]:
    """Ids of EXISTING sources that hold *item_id*, optionally excluding one.

    The holder count dedup reads. The deletion paths consult
    :meth:`_reassignment_candidates` instead, which narrows this to the holders
    that may take ownership; an item is destroyed only when that comes back
    empty. Joins ``sources`` so a location row left pointing at an
    already-deleted source cannot keep a dead item alive.
    """
    sql = (
        "SELECT sl.source_id FROM source_locations sl "
        "JOIN sources s ON s.id = sl.source_id WHERE sl.item_id = ?"
    )
    params: list[str] = [item_id]
    if exclude_source_id:
        sql += " AND sl.source_id != ?"
        params.append(exclude_source_id)
    return [r["source_id"] for r in self.db.execute(sql, params).fetchall()]


def _refuse_live_source(self: KnowledgeStore, source_id) -> None:
    """Raise :class:`LiveSourceHoldsNoItems` if *source_id* names an
    account-bound source; a ``None`` source and an unknown id pass (the
    latter is the foreign key's business). One primary-key read."""
    if source_id is None:
        return
    row = self.db.execute("SELECT source_type FROM sources WHERE id = ?", (source_id,)).fetchone()
    if row is not None and row["source_type"] == _ACCOUNT_BOUND_SOURCE_TYPE:
        raise LiveSourceHoldsNoItems(
            f"source {source_id} is a live-retrieval source and holds no items"
        )


def _reassignment_candidates(
    self: KnowledgeStore, item_id: str, losing_source_id: str
) -> list[str]:
    """The holders of *item_id* that may take ownership when *losing_source_id*
    is deleted: :meth:`sources_holding_item` minus the account-bound types.

    A live-retrieval source never holds items (:meth:`_refuse_live_source`
    guards every writer), so a location row naming one under a local item can
    only come from a direct write to ``knowledge.db``. Reassigning the item to
    that row's source would file a local document under a source that
    :meth:`export_all` leaves out of every bundle, along with everything that
    names it -- the document would survive in the live store and vanish from
    the backup. An item whose only other holders are account-bound therefore
    goes with its owner, as if no other holder existed; the planted location
    row goes with it.
    """
    rows = self.db.execute(
        "SELECT sl.source_id FROM source_locations sl "
        "JOIN sources s ON s.id = sl.source_id "
        "WHERE sl.item_id = ? AND sl.source_id != ? AND s.source_type <> ?",
        (item_id, losing_source_id, _ACCOUNT_BOUND_SOURCE_TYPE),
    ).fetchall()
    return [r["source_id"] for r in rows]


def reassign_item_source(self: KnowledgeStore, item_id: str, new_source_id: str) -> None:
    """Re-point which source OWNS *item_id*.

    ``update_item`` deliberately cannot do this -- ``source_id`` is absent from
    ``_ITEM_COLUMNS``, so an ordinary update silently drops it. Ownership moves
    only here, and only when the owning source is being deleted while another
    source still holds the document.
    """
    self.db.execute("UPDATE items SET source_id = ? WHERE id = ?", (new_source_id, item_id))
