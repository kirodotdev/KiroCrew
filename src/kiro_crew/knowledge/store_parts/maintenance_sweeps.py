"""The maintenance sweeps: orphan sources and entities, and interrupted agent-ingest residue.

Both sweeps run off the boot path, inside the store's maintenance window, and delete
only on positive evidence: an orphan predicate re-checked under the write lock, or an
intent marker an interrupted agent ingest left behind.

A function here whose first parameter is ``self`` is a ``KnowledgeStore`` method:
the class binds it under the same name in ``knowledge/store.py``, so it reads the
store's fields and calls its sibling methods through ``self``.

The ``KnowledgeStore`` fields this module uses. Writes: none. Reads: none.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime
from typing import TYPE_CHECKING

from kiro_crew.knowledge.store_parts.item_ownership import (
    _DOC_STATE_KEY_COL,
    _DOC_STATE_TABLES,
    _OWNERSHIP_HASH_COL,
)

if TYPE_CHECKING:
    from kiro_crew.knowledge.store import KnowledgeStore

logger = logging.getLogger("kiro_crew.knowledge.store")


#: Orphan sources deleted per writer transaction by :meth:`KnowledgeStore.reclaim_orphans`.
_RECLAIM_CHUNK = 200


def reclaim_orphans(self: KnowledgeStore) -> None:
    """Delete orphan sources, entities and stale relations -- off the boot path.

    The body is data-scaled and takes SQLite's writer lock, so
    ``start_dashboard`` runs it on a worker after the listener is up (via
    ``_kick_knowledge_orphan_reclaim``); WAL readers are not blocked.

    A request may be ingesting meanwhile, and :meth:`add_source` commits the
    source row before the rows that protect it from the orphan predicate. The
    caller therefore runs this inside :meth:`maintenance_window`, and a caller
    that ingests into an existing source later holds
    ``pipeline.ingestion_in_flight()`` across the span.

    Deletes run as short ``BEGIN IMMEDIATE`` transactions. A graph a reader
    already loaded is marked stale afterwards.
    """
    # Folder sources are EXCLUDED: an empty watched folder is user
    # configuration, and reaping a paused one would let auto-discovery
    # re-create it active. The agent and artifact aggregate sources are created
    # empty before their first document. bedrock_kb never has items (it is
    # queried at search time), so without the exemption every restart would
    # delete it.
    orphan_pred = (
        "id NOT IN (SELECT DISTINCT source_id FROM items WHERE source_id IS NOT NULL) "
        "AND source_type NOT IN ('local_folder', 'obsidian_vault', 'quip', 'agent', 'artifact', 'bedrock_kb') "
        "AND id NOT IN (SELECT source_id FROM ingestion_jobs WHERE status IN ('pending', 'processing')) "
        # Only a source whose ingest reached an end state is reclaimable. Every
        # other status ('pending', 'pending_confirmation', 'syncing', 'active',
        # 'paused') means somebody still intends to write under it.
        "AND COALESCE(sync_status, '') IN ('synced', 'error', 'missing') "
        "AND id NOT IN (SELECT DISTINCT source_id FROM folder_file_state) "
        "AND id NOT IN (SELECT DISTINCT source_id FROM artifact_item_state) "
        "AND id NOT IN (SELECT DISTINCT source_id FROM agent_item_state) "
        # A source can hold documents it does not OWN: after a duplicate
        # collapse it is a location of the surviving copy. Reaping it here
        # would delete the very rows that record co-ownership, on every
        # gateway start, and the document would stop being reachable from it.
        "AND id NOT IN (SELECT DISTINCT source_id FROM source_locations)"
    )
    # Candidates are read outside a transaction; deletes run in short chunks
    # that re-check the predicate under the lock, so a concurrent write waits
    # for at most one chunk.
    orphan_ids = [
        row[0] for row in self.db.execute(f"SELECT id FROM sources WHERE {orphan_pred}").fetchall()
    ]
    for offset in range(0, len(orphan_ids), _RECLAIM_CHUNK):
        chunk = orphan_ids[offset : offset + _RECLAIM_CHUNK]
        marks = ",".join("?" * len(chunk))
        still_orphan = f"SELECT id FROM sources WHERE id IN ({marks}) AND {orphan_pred}"
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute(
                f"DELETE FROM source_locations WHERE source_id IN ({still_orphan})", chunk
            )
            self.db.execute(
                f"DELETE FROM ingestion_jobs WHERE source_id IN ({still_orphan})", chunk
            )
            self.db.execute(f"DELETE FROM sources WHERE id IN ({still_orphan})", chunk)
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
    self.db.execute("BEGIN IMMEDIATE")
    try:
        self.db.execute(
            "DELETE FROM entity_relations WHERE source_id NOT IN (SELECT id FROM entities) OR target_id NOT IN (SELECT id FROM entities)"
        )
        self._prune_orphan_entities()
        self.db.execute("COMMIT")
    except Exception:
        self.db.execute("ROLLBACK")
        raise
    # A graph somebody already materialised may hold the entities just
    # dropped; drop it so its next reader rebuilds from the pruned tables.
    self.mark_graph_stale()


def _adopt_agent_sole_copy_residue(self: KnowledgeStore, source_id: str) -> set[str] | None:
    """Adopt crash residue that is a document's ONLY surviving content.

    First step of :meth:`reclaim_agent_source_residue`, inside its drained
    window. A replace whose old-group delete committed but whose
    ownership-row write did not leaves a row naming the DELETED group beside
    the new version's unowned items and intent marker; reaping those would
    delete the last copy. So for a slug whose row names a non-empty group of
    which nothing still exists, the unowned items of every marker at that
    slug are written into its row and the markers retired.

    No attempt is proven complete, so the row's ``content_hash`` is cleared
    (a matching hash would make the next add skip missing chunks) and every
    attempt is kept, not only the newest. The next add at the slug replaces
    the whole adopted group.

    Left to the sweep: a slug with no row (nothing was replaced) and a row
    that owned nothing. A hash marked at more than one candidate slug is
    SPARED, with every other attempt at that slug: a leaked duplicate is
    recoverable, a deleted sole copy is not.

    Returns the spared hashes for the sweep to leave alone, or ``None`` --
    "cannot tell, do not sweep" -- when an ownership group is unreadable.
    """
    spared: set[str] = set()
    self.db.execute("BEGIN IMMEDIATE")
    try:
        owned = self.agent_owned_item_ids(source_id)
        if owned is None:
            self.db.execute("COMMIT")
            return None
        markers: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for r in self.db.execute(
            "SELECT slug, content_hash, started_at FROM agent_ingest_intent WHERE source_id = ?",
            (source_id,),
        ).fetchall():
            if not r["content_hash"]:
                continue
            markers[r["slug"]].append((r["content_hash"], r["started_at"] or ""))
        # Pass 1: the slugs whose recorded group is GONE -- not one of its
        # ids exists any more, under any source or status. An item a user
        # set non-active, or one a delete detached to another source, still
        # exists, so its row is not a candidate and keeps its claim.
        candidates: list[str] = []
        for slug in markers:
            row = self.db.execute(
                "SELECT item_ids FROM agent_item_state WHERE source_id = ? AND slug = ?",
                (source_id, slug),
            ).fetchone()
            if not row or row["item_ids"] in (None, ""):
                continue
            try:
                group = json.loads(row["item_ids"])
            except (TypeError, ValueError):
                self.db.execute("COMMIT")
                return None
            group = [i for i in group if isinstance(i, str)] if isinstance(group, list) else []
            if not group:
                continue
            gone = True
            for off in range(0, len(group), _RECLAIM_CHUNK):
                batch = group[off : off + _RECLAIM_CHUNK]
                if self.db.execute(
                    f"SELECT 1 FROM items "  # noqa: S608
                    f"WHERE id IN ({','.join('?' * len(batch))}) LIMIT 1",
                    batch,
                ).fetchone():
                    gone = False
                    break
            if gone:
                candidates.append(slug)
        # A hash is ambiguous only between CANDIDATE slugs: a marker at a
        # slug whose group is live is that slug's ordinary residue.
        candidate_slugs_by_hash: dict[str, int] = defaultdict(int)
        for slug in candidates:
            for content_hash in {h for h, _ in markers[slug]}:
                candidate_slugs_by_hash[content_hash] += 1
        adopted_total = 0
        for slug in candidates:
            # Every attempt's residue, newest attempt first. None of them can
            # be proven complete, so none is chosen over another: the newest
            # may be a truncated edit and an older one the last full version.
            attempts: list[tuple[str, list[str]]] = []
            for content_hash, started in sorted(markers[slug], key=lambda m: m[1], reverse=True):
                ids = [
                    r["id"]
                    for r in self.db.execute(
                        "SELECT id, created_at FROM items "
                        "WHERE source_id = ? AND status = 'active' "
                        "AND content_hash = ? ORDER BY chunk_index, created_at",
                        (source_id, content_hash),
                    ).fetchall()
                    if r["id"] not in owned and (r["created_at"] or "") >= started
                ]
                if ids:
                    attempts.append((content_hash, ids))
            if not attempts:
                continue
            if any(candidate_slugs_by_hash[h] > 1 for h, _ in attempts):
                # One of them cannot be attributed to this document alone:
                # adopt nothing here and reap nothing here either, so no
                # attempt at this slug is lost while ownership is unclear.
                spared.update(h for h, _ in attempts)
                continue
            group = [i for _, ids in attempts for i in ids]
            # ``content_hash`` is cleared, not set to an attempt's hash: the
            # add path reads a matching hash as "unchanged since last add",
            # and this group is not proven to hold any attempt in full.
            self.db.execute(
                "UPDATE agent_item_state SET content_hash = NULL, "
                "item_ids = ?, status = 'active', updated_at = ? "
                "WHERE source_id = ? AND slug = ?",
                (json.dumps(group), datetime.now().isoformat(), source_id, slug),
            )
            for content_hash, _ids in attempts:
                self.db.execute(
                    "DELETE FROM agent_ingest_intent "
                    "WHERE source_id = ? AND slug = ? AND content_hash = ?",
                    (source_id, slug, content_hash),
                )
            owned.update(group)
            adopted_total += len(group)
        self.db.execute("COMMIT")
    except Exception:
        self.db.execute("ROLLBACK")
        raise
    if adopted_total:
        logger.info(
            "agent residue sweep adopted %d item(s) that were a replaced "
            "document's only surviving copy",
            adopted_total,
        )
    return spared


def reclaim_agent_source_residue(self: KnowledgeStore) -> int:
    """Delete items an interrupted agent-document ingest provably left behind.

    The item chunks and the ``agent_item_state`` row are separate commits, so
    a hard kill between them leaves items no row names, and the next add of
    the same document duplicates them. Nothing else reaps that residue.

    REAPS ONLY ON POSITIVE EVIDENCE, never by absence of a row: several
    ``_import_bundle_state`` paths and ``export_item`` round trips leave
    agent items unowned on purpose. The ingest writes a marker in
    ``agent_ingest_intent`` before committing items and deletes it on
    finalize; an unowned ``active`` item is deleted only when its
    ``content_hash`` matches a marker that survived into a drained window.
    Markers live in their own table, so they cover a crash while replacing a
    live group and keep two attempts at one slug apart. A reaped attempt's
    marker is deleted in the same transaction.

    NEVER DELETES A SOLE SURVIVING COPY:
    :meth:`_adopt_agent_sole_copy_residue` runs first.

    Scoped to the ``agent://`` source alone. An item another source also
    holds is DETACHED (``delete_items_batch_in_txn`` with
    ``owner_source_id``), not destroyed.

    Runs inside ``maintenance_window`` like :meth:`reclaim_orphans`, so a
    mid-ingest item is never read as residue. Returns the number removed.
    """
    agent_src = self.get_source_by_uri("agent://")
    if not agent_src:
        return 0
    source_id = agent_src["id"]

    def _owned_ids() -> set[str] | None:
        """Every item id a live state row names for this source.

        Returns ``None`` ("cannot tell, do not sweep") when any row's
        ``item_ids`` is unreadable: a stale item is recoverable, a
        wrongly-deleted one is not.
        """
        return self.agent_owned_item_ids(source_id)

    owned = _owned_ids()
    if owned is None:
        return 0

    def _ingesting_hashes() -> dict[str, str]:
        """Content hash -> EARLIEST ``started_at`` of a marker naming it.

        An item is residue only if its ``content_hash`` is here AND
        ``items.created_at >= started_at``, so an item created before the
        crash is excluded even when it shares a hash. The earliest start is
        kept so neither of two crashed attempts excludes the other.
        """
        out: dict[str, str] = {}
        for row in self.db.execute(
            "SELECT content_hash, started_at FROM agent_ingest_intent WHERE source_id = ?",
            (source_id,),
        ).fetchall():
            h = row["content_hash"]
            if not h:
                continue
            started = row["started_at"] or ""
            if h not in out or started < out[h]:
                out[h] = started
        return out

    def _live_owned_hashes() -> set[str] | None:
        """Content hashes any HEALTHY or ``deduped`` ownership row holds, marker or not.

        A marker hash alone must not authorize a delete: a deduped document
        disturbed by a delete can leave its ONLY copy unowned
        (``_adopt_reassigned_item`` refuses an ambiguous hash), and a
        same-content crash marker would then name it. So any hash a healthy
        or ``deduped`` row holds, even with an empty group, shields every
        unowned copy of that hash, including at a slug that also has a
        marker for it: a leaked duplicate self-heals on the next identical
        add, a deleted last copy does not. Returns ``None`` -- "cannot tell,
        do not sweep" -- if any row's ``item_ids`` is unreadable.
        """
        # item id -> content_hash, for every item a healthy row names.
        protect_ids: set[str] = set()
        # Hashes a healthy row CLAIMS directly even though its item group is
        # empty. A ``deduped`` row left by a reassignment that could not
        # establish ownership (``_adopt_reassigned_item`` refuses an
        # ambiguous hash) names no items, so it would contribute nothing to
        # ``protect_ids`` and give the hash no shield -- yet its surviving
        # copy is the ONLY one, and a same-content crash marker names that
        # copy's hash, so the sweep would delete it irreversibly. The row's
        # existence attests the content is accounted for, so shield its hash
        # directly.
        claimed: set[str] = set()
        for table, healthy in _DOC_STATE_TABLES:
            key_col = _DOC_STATE_KEY_COL[table]
            hash_col = _OWNERSHIP_HASH_COL[table]
            for row in self.db.execute(
                f"SELECT {key_col} AS k, {hash_col} AS h, "  # noqa: S608
                f"item_ids FROM {table} WHERE status = ?",
                (healthy,),
            ).fetchall():
                raw = row["item_ids"]
                if raw in (None, ""):
                    # Empty group: the row still claims its content hash, so
                    # the sweep must not delete an unowned copy of it.
                    if row["h"]:
                        claimed.add(row["h"])
                    continue
                try:
                    parsed = json.loads(raw)
                except (TypeError, ValueError):
                    return None
                if isinstance(parsed, list):
                    if not parsed and row["h"]:
                        # A row whose JSON group is an empty list claims its
                        # hash for the same reason as a NULL/'' group.
                        claimed.add(row["h"])
                    protect_ids.update(i for i in parsed if isinstance(i, str))
        # ``deduped`` agent rows are NOT ``active`` so the loop above skips
        # them, but a ``deduped`` row is exactly the reassignment-refusal case:
        # it owns no items yet records that this slug's content is accounted
        # for by the winner elsewhere. Its ``content_hash`` must shield that
        # content so the sweep never deletes an unowned sole copy of it on
        # bare marker evidence.
        for row in self.db.execute(
            "SELECT content_hash AS h FROM agent_item_state "
            "WHERE source_id = ? AND status = 'deduped'",
            (source_id,),
        ).fetchall():
            if row["h"]:
                claimed.add(row["h"])
        if not protect_ids:
            return set(claimed)
        live: set[str] = set(claimed)
        ids = list(protect_ids)
        for off in range(0, len(ids), _RECLAIM_CHUNK):
            batch = ids[off : off + _RECLAIM_CHUNK]
            live.update(
                r["content_hash"]
                for r in self.db.execute(
                    f"SELECT content_hash FROM items "  # noqa: S608
                    f"WHERE id IN ({','.join('?' * len(batch))})",
                    batch,
                ).fetchall()
                if r["content_hash"]
            )
        return live

    ingesting = _ingesting_hashes()
    if not ingesting:
        return 0
    # Sole-copy adoption runs BEFORE any residue is chosen. It may
    # rewrite an ownership row, so ownership and the marker map are re-read
    # once it has committed.
    spared = self._adopt_agent_sole_copy_residue(source_id)
    if spared is None:
        return 0
    owned = _owned_ids()
    if owned is None:
        return 0
    ingesting = _ingesting_hashes()
    if not ingesting:
        return 0
    live_owned = _live_owned_hashes()
    if live_owned is None:
        return 0
    live_owned |= spared
    # Active items under the agent source that no row owns, whose content
    # hash a stale ``ingesting`` marker names, AND which were created at or
    # after that marker's attempt began -- the positive, attempt-scoped
    # evidence of an interrupted ingest. The ``created_at`` gate is what makes
    # a bare hash match safe: a pre-existing or bundle-imported item that
    # merely shares a hash was created BEFORE the crash marker, so it is never
    # mistaken for this attempt's residue and is never deleted. A hash a
    # healthy ownership row still names anywhere is also EXCLUDED: that content
    # is live-held. Read outside the transaction; each delete chunk re-derives
    # ownership, the marker map and the live-owned set under the writer lock so
    # a row or marker written between the read and the delete still protects
    # its items.
    residue = [
        row["id"]
        for row in self.db.execute(
            "SELECT id, content_hash, created_at FROM items "
            "WHERE source_id = ? AND status = 'active'",
            (source_id,),
        ).fetchall()
        if row["id"] not in owned
        and row["content_hash"] in ingesting
        and row["content_hash"] not in live_owned
        and (row["created_at"] or "") >= ingesting[row["content_hash"]]
    ]
    if not residue:
        return 0
    removed = 0
    acted_hashes: set[str] = set()
    for offset in range(0, len(residue), _RECLAIM_CHUNK):
        chunk = residue[offset : offset + _RECLAIM_CHUNK]
        self.db.execute("BEGIN IMMEDIATE")
        try:
            # Re-read ownership under the lock: an ingest that committed its
            # row after the candidate read above must not have its items
            # swept. A row written in the gap names them now, so they drop
            # out of ``still_residue`` and survive. An unreadable group that
            # appeared in the gap stands the sweep down for the same
            # fail-safe reason as the initial read.
            owned_now = _owned_ids()
            if owned_now is None:
                self.db.execute("COMMIT")
                break
            # Re-read the intent markers too: an ingest that finalized in the
            # gap deletes its marker, so its hash drops out of this set and
            # its items stop being proven residue -- they survive. Only items
            # whose hash STILL has a marker remain candidates.
            ingesting_now = _ingesting_hashes()
            # And re-read the live-owned hashes: a group adopted or re-added
            # in the gap now holds this content, so an unowned copy of its
            # hash stops being safe residue evidence and survives.
            live_now = _live_owned_hashes()
            if live_now is None:
                self.db.execute("COMMIT")
                break
            live_now |= spared
            # And the item must still exist, still belong to this source,
            # still be ACTIVE, and still carry a hash a marker names. The
            # status filter closes the window where a concurrent writer
            # flipped the item out of ``active`` between the candidate read
            # and this lock: the sweep deletes only rows it re-confirms are
            # live residue, never one another path has already retired.
            present_now = {
                r["id"]: (r["content_hash"], r["created_at"] or "")
                for r in self.db.execute(
                    f"SELECT id, content_hash, created_at FROM items "  # noqa: S608
                    f"WHERE source_id = ? AND status = 'active' "
                    f"AND id IN ({','.join('?' * len(chunk))})",
                    (source_id, *chunk),
                ).fetchall()
            }
            still_residue = [
                i
                for i in chunk
                if i in present_now
                and i not in owned_now
                and present_now[i][0] in ingesting_now
                and present_now[i][0] not in live_now
                and present_now[i][1] >= ingesting_now[present_now[i][0]]
            ]
            if still_residue:
                # owner_source_id => an item another source co-holds is
                # detached to that holder, only a truly single-held item is
                # destroyed. Same semantics the live delete paths use.
                self.delete_items_batch_in_txn(still_residue, owner_source_id=source_id)
                removed += len(still_residue)
                acted_hashes.update(present_now[i][0] for i in still_residue)
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
    # Retire markers ONCE, after every chunk, and ONLY for a hash that has no
    # unowned ATTEMPT-CREATED active item left under this source. A document's
    # chunks all share one hash and a single interrupted ingest can exceed one
    # delete chunk, so retiring a hash's marker mid-loop (the moment any batch
    # touched it) would strand the rest of that document's chunks: the next
    # sweep's marker map would then omit them and they would survive forever
    # as duplicates. Deferring retirement and gating it on "no unowned
    # same-hash item created at/after the attempt remains" keeps the evidence
    # alive until the last orphan of that content is reaped, and clears it as
    # soon as it is spent -- a pre-existing or bundle item that merely shares
    # the hash (created before the marker) is not this attempt's residue and
    # must not pin the marker open.
    if acted_hashes:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            owned_final = _owned_ids()
            started_by_hash = _ingesting_hashes()
            for h in acted_hashes:
                started = started_by_hash.get(h)
                if started is None:
                    # The marker is already gone (a finalize cleared it in the
                    # gap); nothing to retire.
                    continue
                remaining = [
                    r["id"]
                    for r in self.db.execute(
                        "SELECT id, created_at FROM items "
                        "WHERE source_id = ? AND status = 'active' "
                        "AND content_hash = ?",
                        (source_id, h),
                    ).fetchall()
                    if (owned_final is None or r["id"] not in owned_final)
                    and (r["created_at"] or "") >= started
                ]
                if not remaining:
                    self.db.execute(
                        "DELETE FROM agent_ingest_intent "
                        "WHERE source_id = ? AND content_hash = ?",
                        (source_id, h),
                    )
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
    if removed:
        self.reload_graph()
        logger.info(
            "knowledge: reclaimed %d orphaned item(s) in the agent aggregate "
            "source left by an interrupted ingest",
            removed,
        )
    return removed
