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

if TYPE_CHECKING:
    from kiro_crew.knowledge.store import KnowledgeStore

logger = logging.getLogger("kiro_crew.knowledge.store")


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
# Two different quantities are both called a content hash, and they are not
# interchangeable:
#
# * ``folder_file_state.content_hash`` is sha256 over the file's RAW BYTES. It
#   answers "has this file changed on disk?" and is compared before any
#   extraction runs -- deriving it from extracted text would force an extraction
#   pass on every scan, which is the cost the mtime/hash gate exists to avoid.
#   The pre-ingest duplicate gate is in the same domain for the same reason: it
#   also runs before extraction.
# * ``items.content_hash`` is sha256 over the EXTRACTED TEXT.
#
# For .md/.txt the two coincide, so a cross-domain comparison appears to work.
# For anything the reader transforms -- PDF, DOCX, HTML -- they differ and the
# comparison silently matches nothing, which is how ownership bookkeeping came to
# be inert for exactly those documents. Folder rows therefore carry the text hash
# separately, in ``text_hash``; the aggregate tables already store a text hash in
# ``content_hash`` and need no second column.
_OWNERSHIP_HASH_COL: dict[str, str] = {
    "folder_file_state": "COALESCE(NULLIF(text_hash, ''), content_hash)",
    "artifact_item_state": "content_hash",
    "agent_item_state": "content_hash",
}
# Folder rows COALESCE so a legacy row -- written before ``text_hash`` existed, and
# deliberately never backfilled -- keeps behaving exactly as it does today: for the
# plaintext files whose two hashes are equal its ownership lookups still match, which
# they would stop doing if the new column were consulted alone. A rescan populates
# ``text_hash`` and the row becomes correct for transformed documents too.


def agent_owned_item_ids(self: KnowledgeStore, source_id: str) -> set[str] | None:
    """Every item id a live ``agent_item_state`` row names for a source.

    A row left by a refused write owns an empty group and names nothing,
    which is correct: its items were adopted by the winner's row and are
    named THERE. Returns ``None`` -- "cannot tell" -- if ANY row's
    ``item_ids`` is unreadable: the owned set would then be missing items
    that are genuinely owned, and a caller treating those as residue would
    delete owned content. A stale item is recoverable; a wrongly-deleted one
    is not, so every caller stands its sweep/cleanup down on ``None``.

    The single source of this derivation. The residue sweep and
    ``agent_source.clear_ingesting`` both read it from here rather than each
    spelling the same query, JSON parse and unreadable-group fail-safe.
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

    Reassignment moves ``items.source_id``, but a state row's ``item_ids`` is the
    only list its own delete path consults. A recipient that owns an item its row
    never names cannot delete it: removing that document drops an empty group and
    the content stays searchable, which is the strand this whole model exists to
    prevent. So the row that describes this content adopts the item.

    Matched on ``content_hash`` because that is what identifies the document
    independently of which source holds it. Appends rather than replaces, so a
    multi-item group (a chunked file) is not truncated to one, and clears any
    deferral marker: a row that owns an item is not deferring to anyone.

    A hash is only an identifier while it picks out ONE row. Two distinct
    documents in one source may legitimately hold identical text, and writing the
    item into both would put one physical item in two groups -- then removing
    either document deletes it and takes the other's indexed content with it. So
    an ambiguous hash adopts nothing: an un-adopted row leaves a stale claim,
    which is visible and recoverable, whereas a cross-wired group destroys
    content on the next delete.
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

    The counterpart to :meth:`_adopt_reassigned_item`. A source that lost a dedup
    holds no items for that document -- its state row is 'deduped' with an empty
    group -- yet it IS still a location of the winner's items, which is what keeps
    the document reachable if the winner goes away. When the losing copy is
    genuinely removed (its file deleted from that folder), the claim has to go too,
    or the source stays a candidate to inherit a document it does not have and the
    content resurfaces there as searchable text with no file behind it.

    Identified by ``content_hash`` because that is the only handle such a row has:
    it owns no item ids, and the winner's ids must never be written into it -- a
    row naming another source's items becomes a second document over one physical
    item set, which self-collapses and deletes the survivor.

    Only the location rows are removed. The items belong to the winner and are
    left untouched. Returns the number of claims dropped.

    Like adoption, this acts only when the hash picks out ONE document here. If a
    second document in this source holds identical text, the claim is shared and
    dropping it would strand that other document when the winner goes away. On
    ambiguity the claim is kept: a stale claim can resurface content that exists
    elsewhere, which is recoverable, while a released one destroys a document.
    ``exclude_folder_paths`` omits rows the current sweep will remove from the
    claimant count; those rows cannot back a surviving shared claim.
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

    A source that lost a dedup owns no items but IS a location of the winner's,
    and that claim is specific to the content it was made for. When the source's
    copy is EDITED, the claim becomes a claim on the wrong document: deleting the
    holder would then hand this source the superseded text, which stays searchable
    with nothing behind it.

    The rule lives here rather than at each ingest path because all three paths
    (folder file, artifact, agent document) can be edited and all three would
    otherwise have to re-derive it. Only fires when the row owned NOTHING -- a row
    with a live group replaces its own items through the normal delete-and-reingest
    path -- and only when the hash actually moved. Returns claims dropped.

    Two domains are in play and both are needed. *prev_hash*/*new_hash* decide
    whether the file CHANGED, which is a question about the bytes on disk. The
    detach then has to name a document in the ITEM domain, which is what
    *prev_text_hash* carries for folder rows. Callers whose ``content_hash`` is
    already a text hash (artifacts, agent documents) pass nothing and the
    fallback uses *prev_hash* unchanged. A legacy folder row with no text hash
    also falls back, and matches nothing exactly as it does today.
    """
    if prev_item_ids or not prev_hash or prev_hash == new_hash:
        return 0
    return self.detach_source_location_by_hash(source_id, prev_text_hash or prev_hash)


def surviving_group_in_txn(self: KnowledgeStore, table: str, source_id: str, key: str) -> list[str]:
    """Items a doc-state row already names and this source still owns.

    The caller must already hold a write transaction, and must not be on the
    event loop: this issues sync sqlite reads whose result is only meaningful
    under that lock.

    Exists because the terminal write for a document the pre-ingest gate
    REFUSED cannot predict its own group. The gate commits before returning,
    so a concurrent ``delete_source_cascade`` on the holder can land in
    between: it reassigns the surviving item to this source and
    :meth:`_adopt_reassigned_item` names it in this very row. Writing an empty
    group afterwards -- which "the gate refused, so this document owns
    nothing" predicts -- erases that, leaving the last copy owned by the
    source but named by no row: unreachable by the delete path, and
    undeletable.

    Row-scoped, never by content hash. Two documents in one source may
    legitimately hold identical text, so a hash-scoped read hands this row the
    OTHER document's items; both rows then name one physical item and deleting
    either destroys it. ``_adopt_reassigned_item`` refuses an ambiguous hash
    for that reason and this must not reintroduce it.

    Filtered to ids that still exist under this source, because the row is
    still carrying the group the gate just deleted. What survives is an
    adoption that landed here.

    An unreadable ``item_ids`` RAISES rather than reporting an empty group: the
    caller writes whatever comes back as the row's terminal state, so mapping
    corruption to "owns nothing" would overwrite a recoverable value and
    orphan every item it named.
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

    The reference count deletion consults: an item is destroyed only when this
    comes back empty. Joins ``sources`` so a location row left pointing at an
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


def reassign_item_source(self: KnowledgeStore, item_id: str, new_source_id: str) -> None:
    """Re-point which source OWNS *item_id*.

    ``update_item`` deliberately cannot do this -- ``source_id`` is absent from
    ``_ITEM_COLUMNS``, so an ordinary update silently drops it. Ownership moves
    only here, and only when the owning source is being deleted while another
    source still holds the document.
    """
    self.db.execute("UPDATE items SET source_id = ? WHERE id = ?", (new_source_id, item_id))
