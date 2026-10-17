"""The maintenance sweeps: orphan sources and entities, and interrupted agent-ingest residue.

Both sweeps run off the boot path, inside the store's maintenance window, and delete
only on positive evidence: an orphan predicate re-checked under the write lock, or an
intent marker an interrupted agent ingest left behind.

A function here whose first parameter is ``self`` is a ``KnowledgeStore`` method:
the class binds it under the same name in ``knowledge/store.py``, so it reads the
store's fields and calls its sibling methods through ``self``.

The ``KnowledgeStore`` fields this module uses. Writes: none. Reads: ``_graph_loaded``,
``_graph_lock``.
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

    Formerly the tail of :meth:`_migrate`, so it ran inside ``__init__`` on
    the event-loop thread on every launch, before the socket bound. The
    body is data-scaled (every predicate is a full scan over ``sources``,
    ``items`` and the state tables) and takes SQLite's writer lock, so a
    large knowledge store stalled the gateway for long enough to trip the
    runtime's boot timeouts. ``start_dashboard`` now runs this from a
    worker thread AFTER the listener is accepting, via
    ``_kick_knowledge_orphan_reclaim``; the store is readable throughout
    (WAL readers do not wait on the writer).

    Running after the listener is up means a request can be ingesting
    concurrently: :meth:`add_source` commits the source row on its own, and
    the rows that protect it from the orphan predicate (its ingestion job,
    its items, its entities' mentions) are written by the caller's pipeline
    some time later. A sweep observing that half state would delete a
    source the user just added. The deferred worker therefore runs this
    inside :meth:`maintenance_window`, which waits for every
    :meth:`ingestion_in_flight` holder to finish and holds new ingestion
    off for the duration; this method itself sweeps every row it finds.

    A caller that looks up an existing source and ingests into it later
    holds ``pipeline.ingestion_in_flight()`` across the whole span, so the
    sweep waits for it rather than racing it.

    Thread-safe by the same rules as every other write path: the calling
    thread gets its own connection through :attr:`db`, and the sweep runs
    as a series of short ``BEGIN IMMEDIATE`` transactions (see the body). Because it may now run after a reader
    has materialised the graph, it refreshes the in-memory graph when one is
    loaded -- the constructor-time sweep never had to, since it always ran
    before the first load.
    """
    # Clean orphan sources (no items), entities (no mentions/relations), and stale relations
    #
    # Folder sources are EXCLUDED: a watched folder with zero discovered
    # files is legitimately empty, not orphaned. Deleting it here loses
    # user-set state -- notably a paused empty folder would be dropped on
    # restart and then re-created as active by auto-discovery, silently
    # un-pausing it. The row is user-registered configuration, not derived
    # data, so only its items are reclaimable. The agent-document and
    # artifact aggregate sources are containers of the same kind: each is
    # created empty ('active', no items, no state rows) the moment its
    # feature first needs it and filled by a later write, so an empty one
    # is a feature waiting for its first document, not garbage.
    orphan_pred = (
        "id NOT IN (SELECT DISTINCT source_id FROM items WHERE source_id IS NOT NULL) "
        "AND source_type NOT IN ('local_folder', 'obsidian_vault', 'quip', 'agent', 'artifact') "
        "AND id NOT IN (SELECT source_id FROM ingestion_jobs WHERE status IN ('pending', 'processing')) "
        # Only a source whose ingest has run to an end state is reclaimable.
        # Every other status is a claim on the row: 'pending' (the column
        # default a fresh add_source row carries until its background ingest
        # writes its gate holder and job row), 'pending_confirmation',
        # 'syncing', 'active' (a producible initial status for a source a
        # feature fills later) and 'paused' (user-set) all mean somebody
        # still intends to write under it. The allowlist is written by the
        # row's own INSERT or by the ingest that finished, so there is no
        # window in which a live source reads as an orphan.
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
    # The candidate list is read outside any transaction, and the deletes run
    # in chunks of short BEGIN IMMEDIATE transactions that re-check the
    # predicate under the lock. A knowledge write issued on the event loop
    # while the sweep runs waits for at most one chunk instead of the whole
    # data-scaled sweep, so the post-bind sweep cannot stall the loop for
    # the duration the pre-bind one did.
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
    # Only a graph somebody already materialised can be holding the entities
    # just dropped; an unloaded one is built fresh by its first reader. The
    # flag is read under ``_graph_lock`` so a first load racing this sweep
    # cannot slip between the check and the refresh: a load that holds the
    # lock finishes first and then reads as loaded (so it is refreshed), and
    # a load that arrives later reads the tables after the prune committed.
    with self._graph_lock:
        if self._graph_loaded:
            self._load_graph()


def _adopt_agent_sole_copy_residue(self: KnowledgeStore, source_id: str) -> set[str] | None:
    """Adopt crash residue that is a document's ONLY surviving content.

    Runs as the first step of :meth:`reclaim_agent_source_residue`, inside
    its drained maintenance window. A replace whose old-group delete
    committed but whose ownership-row write did not (a store written
    while those two were separate commits) leaves a slug whose
    row still names the DELETED previous group, beside the new version's
    unowned items and their intent marker. Reaping those items, as the
    marker alone would authorize, deletes the document's last copy. So for
    a slug whose row names a non-empty group of which NOTHING still exists
    (under any source or status), the unowned items of EVERY marker at that
    slug are written into its row instead, and those markers are retired.

    Nothing stored proves an attempt finished: a hard kill can land after
    any chunk commit, and no record says how many chunks the attempt meant
    to write. So adoption keeps the residue but claims no content for it.
    The row's ``content_hash`` is cleared rather than set to an attempt's
    hash, because that hash is what tells the next add of the same text
    "unchanged since last add" -- a truncated attempt adopted under it would
    never get its missing chunks written. And every attempt is kept, not
    only the newest, because the newest may be the truncated one and an
    older one the last complete version. The next add at the slug, of any
    content, replaces the whole adopted group.

    Left to the sweep, unchanged: a slug with no row (a first add that
    crashed -- nothing was replaced, so nothing is lost), and a row that
    owned nothing (a refused or deduped document whose content another row
    holds). A hash marked at more than one such slug cannot be attributed to
    one document; it is SPARED rather than adopted or reaped, and so is
    every other attempt at a slug holding one, so nothing there is deleted
    while its ownership is unclear -- a leaked duplicate is recoverable, a
    deleted sole copy is not.

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

    A HARD KILL during an agent-document ingest is still open: the item
    chunks and the ``agent_item_state`` ownership-row write are separate
    commits on an autocommit connection, so a process killed between the
    item commit and the row commit leaves committed items that no row names.
    The replacement path keys off that row, so the next add of the same
    document adds a second copy instead of replacing the first -- the
    duplicate this sweep repairs. Nothing else on the agent path reaps that
    residue, so it is permanent until this sweep removes it.

    REAPS ONLY ON POSITIVE EVIDENCE, NEVER BY ABSENCE OF A ROW. "No row
    names this item" is NOT proof of crash residue: ``_import_bundle_state``
    has several paths that leave a bundle's agent items unowned on purpose --
    a row skipped as ``ownership_row_key_held_locally``, an empty or
    over-claiming group, a bundle with no state tables -- and a single-item
    export/import round trip (``export_item`` writes no state row) does the
    same. A sweep keyed on absence would silently and unrecoverably delete
    every such imported item. So the ingest records an intent marker in
    ``agent_ingest_intent`` BEFORE it commits items and deletes it on
    finalize; a marker that survives into a drained maintenance window is
    the positive proof that an ingest started and never finished. This sweep
    deletes an unowned ``active`` agent item ONLY when its ``content_hash``
    matches an intent marker -- the hash the crashed ingest stamped on every
    chunk it wrote (``items.content_hash`` over the newline-normalized
    extracted text, the same value the ingest records on the marker). An
    imported item has no such marker for its hash, so it is never a
    candidate. The marker sits in its own table rather than on the ownership
    row, so it covers a crash while REPLACING a live group as well as a
    first add, and two interrupted attempts at one slug each keep their own
    evidence. A reaped attempt's marker is deleted in the same transaction.

    NEVER DELETES A DOCUMENT'S SOLE SURVIVING COPY: before any residue is
    chosen, :meth:`_adopt_agent_sole_copy_residue` adopts the residue of a
    slug whose recorded group is gone into that slug's row (and
    spares a hash it cannot attribute to one slug), so a replace interrupted
    after its old-group delete is repaired rather than emptied.

    SCOPED TO THE ``agent://`` SOURCE ALONE: only the agent path writes these
    markers, so the sweep cannot reach a folder- or artifact-backed item.

    An item another source also holds (a dedup co-location) is DETACHED, not
    destroyed: ``delete_items_batch_in_txn`` with ``owner_source_id`` moves
    ownership to a surviving holder and drops only the agent source's
    location row. Only an item no other source holds is removed outright.

    Runs off the boot path inside ``maintenance_window`` like
    :meth:`reclaim_orphans`: that waits for in-flight ingestion to drain and
    holds new ingestion off, so an item still mid-ingest -- its marker not
    yet replaced by the finalize hop -- is never read as residue. Returns the
    number of residue items removed.
    """
    agent_src = self.get_source_by_uri("agent://")
    if not agent_src:
        return 0
    source_id = agent_src["id"]

    def _owned_ids() -> set[str] | None:
        """Every item id a live state row names for this source.

        Delegates to :meth:`agent_owned_item_ids` -- the one place this
        derivation lives. Returns ``None`` ("cannot tell, do not sweep")
        when any row's ``item_ids`` is unreadable, in which case the whole
        sweep stands down for this source until the corruption is resolved
        (a stale item is recoverable; a wrongly-deleted one is not).
        """
        return self.agent_owned_item_ids(source_id)

    owned = _owned_ids()
    if owned is None:
        return 0

    def _ingesting_hashes() -> dict[str, str]:
        """Content hash -> EARLIEST ``started_at`` of a marker naming it.

        The ingest writes one of these before it commits items and deletes it
        on finalize, so a marker present in a drained maintenance window is
        positive proof that an ingest of exactly this content started and
        never finished. An item is residue ONLY if its own ``content_hash``
        is in this map AND it was created at or after the attempt began
        (``items.created_at >= started_at``); an imported item that merely
        lacks an ownership row has no marker, and a pre-existing or bundle
        item created BEFORE the crash is excluded by the timestamp even when
        it happens to share a hash. The marker lives in its own table, so it
        coexists with a live ownership row for the same slug -- a re-add that
        crashes while replacing a live group leaves its marker here just as a
        first add does. When two markers name one hash the EARLIEST start is
        kept, so neither crashed attempt's residue is excluded by the other.
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
        """Content hashes a HEALTHY ownership row holds AT A SLUG THAT HAS NO
        MARKER for that hash.

        A matching marker hash alone must not authorize deleting an item: a
        document deduped against another, then disturbed by a delete, can
        leave its ONLY copy unowned (``_adopt_reassigned_item`` refuses an
        ambiguous hash rather than cross-wire two groups), and a same-content
        crash marker would then name that surviving copy's hash. Deleting it
        on hash evidence is irreversible data loss. So if the content behind
        a hash is still held by a healthy ownership row at a DIFFERENT
        document -- one with no crash marker of its own for that hash -- the
        sweep leaves every unowned copy of that hash alone: the content is
        accounted for elsewhere and the hash is not safe residue evidence.
        A healthy row shields its hash whether it still NAMES live items or
        its group is EMPTY: a reassignment that refused ambiguous ownership
        leaves a ``deduped`` row with no items but a surviving sole copy, and
        that copy's hash must be shielded by the row's existence alone.

        The shield is UNCONDITIONAL on any healthy or ``deduped`` row's hash,
        with no exclusion for a hash that also carries a crash marker at the
        same slug. An earlier design excluded such a (slug, hash) pair so a
        genuine same-slug retry's orphan could be reaped, but that exclusion
        stripped protection from the ONE case where the "orphan" is actually
        the slug's sole surviving copy -- an irreversible deletion. Between
        leaking a duplicate (the exclusion's absence, self-healing: a later
        identical add dedups it) and deleting the last copy (the exclusion's
        presence), the fail-safe choice is to shield, so the exclusion is
        gone. Returns ``None`` -- "cannot tell, do not sweep" -- if any row's
        ``item_ids`` is unreadable, for the same fail-safe reason as
        :func:`_owned_ids`.
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
