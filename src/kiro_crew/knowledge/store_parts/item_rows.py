"""Item rows and the deletes that remove items, a source, or a source's copy of them.

An item is written with its full-text row, updated under the write lock it reads the
old row in, and deleted with its locations, mentions and relations. A delete that
names the source losing its copy detaches an item another source still holds instead
of destroying it.

A function here whose first parameter is ``self`` is a ``KnowledgeStore`` method:
the class binds it under the same name in ``knowledge/store.py``, so it reads the
store's fields and calls its sibling methods through ``self``.

The ``KnowledgeStore`` fields this module uses. Writes: none. Reads: none; every query
goes through ``self.db``.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from kiro_crew.knowledge.store_parts.item_ownership import _DOC_STATE_TABLES

if TYPE_CHECKING:
    from kiro_crew.knowledge.store import KnowledgeStore


def find_doc_by_content_hash(
    self: KnowledgeStore, content_hash: str, exclude_source_id: str | None = None
) -> dict | None:
    """The first document already holding this exact content, or ``None``.

    Every chunk of a document carries the document's whole-text
    ``content_hash``, so a hit means this text is already in the Library.
    Uses ``idx_items_content_hash``.

    ``exclude_source_id`` skips a source, so re-ingesting a document into the
    source that already owns it is not mistaken for a duplicate -- that is a
    content replacement and must proceed.
    """
    if not content_hash:
        return None
    sql = (
        "SELECT i.source_id, s.source_type, s.name AS source_name "
        "FROM items i JOIN sources s ON s.id = i.source_id "
        "WHERE i.content_hash = ?"
    )
    params: list[str] = [content_hash]
    if exclude_source_id:
        sql += " AND i.source_id != ?"
        params.append(exclude_source_id)
    row = self.db.execute(sql + " LIMIT 1", tuple(params)).fetchone()
    return dict(row) if row else None


def add_item(
    self: KnowledgeStore,
    title,
    content,
    item_type,
    source_id=None,
    chunk_index=0,
    summary=None,
    tags=None,
    embedding=None,
    namespace="default",
    content_hash=None,
) -> str:
    item_id = str(uuid4())
    now = datetime.now().isoformat()
    tags_json = json.dumps(tags or [])
    self.db.execute("BEGIN IMMEDIATE")
    try:
        self.db.execute(
            "INSERT INTO items (id, title, content, item_type, source_id, chunk_index, namespace, summary, tags, embedding, content_hash, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item_id,
                title,
                content,
                item_type,
                source_id,
                chunk_index,
                namespace,
                summary,
                tags_json,
                embedding,
                content_hash,
                now,
                now,
            ),
        )
        # Sync FTS: get the rowid of the inserted item
        rowid = self.db.execute("SELECT rowid FROM items WHERE id = ?", (item_id,)).fetchone()[0]
        self._fts_index(rowid, title, content, tags_json)
        self.db.execute("COMMIT")
    except Exception:
        self.db.execute("ROLLBACK")
        raise
    return item_id


def get_item(self: KnowledgeStore, item_id):
    row = self.db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    return self._serialize_item(row) if row else None


def _serialize_item(row) -> dict:
    d = dict(row)
    raw = d.get("embedding")
    if isinstance(raw, bytes):
        d["embedding"] = base64.b64encode(raw).decode("ascii")
    return d


_ITEM_COLUMNS = {
    "title",
    "content",
    "item_type",
    "summary",
    "tags",
    "embedding",
    "status",
    "namespace",
    "updated_at",
}


def update_item(self: KnowledgeStore, item_id, **fields):
    if not fields:
        return
    fields["updated_at"] = datetime.now().isoformat()
    safe = {k: v for k, v in fields.items() if k in self._ITEM_COLUMNS}
    if not safe:
        return
    cols = ", ".join(f"{k} = ?" for k in safe)
    vals = [json.dumps(v) if isinstance(v, (list, dict)) else v for v in safe.values()]
    fts_fields = {"title", "content", "tags"} & set(fields)
    # The write lock comes first, BEFORE the old-row read, because the FTS
    # delete is built from what that read returns. Two concurrent PATCHes of
    # one item would otherwise both read the same old title, and the loser
    # would unindex terms the winner had already replaced -- leaving the item
    # searchable under a superseded title, with nothing that repairs it
    # (``ensure_fts_index_current`` re-indexes on a term-representation
    # version bump, never on content staleness). Holding the lock across the
    # read costs one indexed lookup by id, and it is the shape
    # ``merge_source_properties`` documents for the same reason.
    self.db.execute("BEGIN IMMEDIATE")
    try:
        old_row = None
        if fts_fields:
            old_row = self.db.execute(
                "SELECT rowid, title, content, tags FROM items WHERE id = ?", (item_id,)
            ).fetchone()
        self.db.execute(f"UPDATE items SET {cols} WHERE id = ?", (*vals, item_id))  # noqa: S608
        # Sync FTS: delete with OLD values, insert with NEW values
        if old_row:
            self._fts_unindex(
                old_row["rowid"], old_row["title"], old_row["content"], old_row["tags"]
            )
            new_row = self.db.execute(
                "SELECT title, content, tags FROM items WHERE id = ?", (item_id,)
            ).fetchone()
            self._fts_index(old_row["rowid"], new_row["title"], new_row["content"], new_row["tags"])
        self.db.execute("COMMIT")
    except Exception:
        self.db.execute("ROLLBACK")
        raise


def _delete_item_cascade(self: KnowledgeStore, item_id):
    """Delete item and its dependents without commit/graph reload (for batch use)."""
    row = self.db.execute(
        "SELECT rowid, title, content, tags FROM items WHERE id = ?", (item_id,)
    ).fetchone()
    if row:
        self._fts_unindex(row["rowid"], row["title"], row["content"], row["tags"])
    self.db.execute("DELETE FROM source_locations WHERE item_id = ?", (item_id,))
    self.db.execute("DELETE FROM mentions WHERE item_id = ?", (item_id,))
    self.db.execute("DELETE FROM entity_relations WHERE source_item_id = ?", (item_id,))
    self.db.execute("DELETE FROM items WHERE id = ?", (item_id,))


def delete_item(self: KnowledgeStore, item_id):
    self.db.execute("BEGIN IMMEDIATE")
    try:
        self._delete_item_cascade(item_id)
        self._prune_orphan_entities()
        self.db.execute("COMMIT")
    except Exception:
        self.db.execute("ROLLBACK")
        raise
    self._load_graph()


def delete_items_batch(
    self: KnowledgeStore, item_ids: list[str], owner_source_id: str | None = None
):
    """Delete multiple items in a single transaction with one graph reload.

    Pass *owner_source_id* when the caller means "this SOURCE's copy of these
    documents is gone" -- a folder file removed from disk, a replaced document, a
    collapsed duplicate. An item another source also holds is then DETACHED
    rather than destroyed: ownership moves to a surviving holder and only the
    calling source's location row is dropped. Without the argument the items are
    destroyed outright, which is correct only when the caller means the document
    itself is going.
    """
    if not item_ids:
        return
    self.db.execute("BEGIN IMMEDIATE")
    try:
        self.delete_items_batch_in_txn(item_ids, owner_source_id)
        self.db.execute("COMMIT")
    except Exception:
        self.db.execute("ROLLBACK")
        raise
    self._load_graph()


def delete_items_batch_in_txn(
    self: KnowledgeStore, item_ids: list[str], owner_source_id: str | None = None
):
    """The body of :meth:`delete_items_batch`, for a caller already in a write txn.

    Same semantics, minus the transaction and the graph reload, so a caller
    that must delete and then record something ATOMICALLY can put both inside
    one ``BEGIN IMMEDIATE`` -- otherwise the delete commits on its own and a
    concurrent writer can act on the gap. Such a caller owns two duties:
    commit the transaction, and call :meth:`reload_graph` afterwards, because
    the orphan sweep below drops entities the in-memory graph still holds.
    """
    for item_id in item_ids:
        if owner_source_id:
            others = self.sources_holding_item(item_id, exclude_source_id=owner_source_id)
            if others:
                self.reassign_item_source(item_id, others[0])
                self._adopt_reassigned_item(item_id, others[0])
                self.db.execute(
                    "DELETE FROM source_locations WHERE item_id = ? AND source_id = ?",
                    (item_id, owner_source_id),
                )
                continue
        self._delete_item_cascade(item_id)
    self._prune_orphan_entities()


def delete_source_cascade(self: KnowledgeStore, source_id):
    """Delete a source and all its items in a single transaction (batch SQL).

    No tombstone is written. Nothing re-creates a source behind the user, so
    recording the deletion would be a write nothing reads. The ``dismissed_auto_sources`` table
    is left in place unused rather than dropped, so no schema migration rides
    along with a feature removal.
    """
    self.db.execute("BEGIN IMMEDIATE")
    try:
        # A document reachable from another source SURVIVES this deletion. Its
        # ownership moves to one of those sources and only this source's location
        # row is dropped; the item, its text, embedding, FTS row and graph edges
        # are untouched. Only items this source solely holds are destroyed.
        owned = [
            r["id"]
            for r in self.db.execute(
                "SELECT id FROM items WHERE source_id = ?", (source_id,)
            ).fetchall()
        ]
        doomed: list[str] = []
        for item_id in owned:
            others = self.sources_holding_item(item_id, exclude_source_id=source_id)
            if others:
                self.reassign_item_source(item_id, others[0])
                # Same obligation as the item-level path: a recipient must never
                # own an item its own row does not name. The revive loop below
                # only reaches rows that DEFERRED to this source, and a row can
                # hold a location without ever carrying a marker -- the pre-ingest
                # gate writes exactly that shape -- so adoption belongs here too.
                self._adopt_reassigned_item(item_id, others[0])
            else:
                doomed.append(item_id)

        # This source stops being a location of everything it held, whether the
        # item survived under a new owner or is about to be deleted.
        self.db.execute("DELETE FROM source_locations WHERE source_id = ?", (source_id,))

        if doomed:
            q = ",".join("?" for _ in doomed)
            # FTS is external-content, so the old column values must be handed to
            # the 'delete' command BEFORE the rows go -- and only for the rows going.
            for row in self.db.execute(
                f"SELECT rowid, title, content, tags FROM items WHERE id IN ({q})",  # noqa: S608
                doomed,
            ).fetchall():
                self._fts_unindex(row["rowid"], row["title"], row["content"], row["tags"])
            self.db.execute(
                f"DELETE FROM source_locations WHERE item_id IN ({q})", doomed
            )  # noqa: S608
            self.db.execute(f"DELETE FROM mentions WHERE item_id IN ({q})", doomed)  # noqa: S608
            self.db.execute(
                f"DELETE FROM entity_relations WHERE source_item_id IN ({q})", doomed
            )  # noqa: S608
            self.db.execute(f"DELETE FROM items WHERE id IN ({q})", doomed)  # noqa: S608

        # Documents that deferred to this source need their marker cleared, or the
        # collapse outlives its reason and they stay stranded. But HOW they are
        # revived depends on whether the surviving copy just became theirs: the
        # reassignment above may have moved it into the very source that deferred
        # to this one. Marking such a row 'pending' would re-ingest a document the
        # source already owns and leave it holding two copies.
        for table, healthy in _DOC_STATE_TABLES:
            deferred = self.db.execute(
                f"SELECT source_id, content_hash, rowid FROM {table} "  # noqa: S608
                "WHERE merged_into_source_id = ?",
                (source_id,),
            ).fetchall()
            for row in deferred:
                adopted: list[str] = []
                if row["content_hash"]:
                    adopted = [
                        r["id"]
                        for r in self.db.execute(
                            "SELECT id FROM items WHERE source_id = ? AND content_hash = ?",
                            (row["source_id"], row["content_hash"]),
                        ).fetchall()
                    ]
                if adopted:
                    # The document is present and owned here now: adopt the items
                    # rather than re-ingesting. Its own items, so no foreign group.
                    # The status must be this table's own healthy value -- see
                    # _DOC_STATE_TABLES for why they are not interchangeable.
                    self.db.execute(
                        f"UPDATE {table} SET merged_into_source_id = NULL, "  # noqa: S608
                        "status = ?, item_ids = ? WHERE rowid = ?",
                        (healthy, json.dumps(adopted), row["rowid"]),
                    )
                elif table == "folder_file_state":
                    # Scan-driven: clearing the marker to 'pending' makes the next
                    # walk re-ingest the file, which is the whole point of reviving.
                    self.db.execute(
                        "UPDATE folder_file_state SET merged_into_source_id = NULL, "
                        "status = 'pending' WHERE rowid = ?",
                        (row["rowid"],),
                    )
                else:
                    # Push-driven with no scanner to revive it, and the content is
                    # genuinely gone -- so the row keeps its 'deduped' status and
                    # only loses the marker. Promoting it to 'active' while it owns
                    # nothing would make find_document_by_hash refuse a re-add of
                    # content the Library does not actually hold.
                    self.db.execute(
                        f"UPDATE {table} SET merged_into_source_id = NULL "  # noqa: S608
                        "WHERE rowid = ?",
                        (row["rowid"],),
                    )
        self.db.execute("DELETE FROM ingestion_jobs WHERE source_id = ?", (source_id,))
        self.db.execute("DELETE FROM folder_file_state WHERE source_id = ?", (source_id,))
        self.db.execute("DELETE FROM folder_include_targets WHERE source_id = ?", (source_id,))
        self.db.execute("DELETE FROM artifact_item_state WHERE source_id = ?", (source_id,))
        self.db.execute("DELETE FROM agent_item_state WHERE source_id = ?", (source_id,))
        self.db.execute("DELETE FROM agent_ingest_intent WHERE source_id = ?", (source_id,))
        self.db.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        self._prune_orphan_entities()
        self.db.execute("COMMIT")
    except Exception:
        self.db.execute("ROLLBACK")
        raise
    self._load_graph()
