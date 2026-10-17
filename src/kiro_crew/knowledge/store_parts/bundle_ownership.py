"""Which of a bundle's ownership rows an import restores, and which of its items it withholds.

A bundle's per-document state rows claim items on a local subsystem's behalf, so an
import restores only rows that cannot be read as an order to delete, grants each row
only the items this import wrote, and withholds the items of a document this store
already holds under the same key. Every ``IN`` list sized by the bundle is chunked
under ``_MAX_SQL_PARAMS``.

A function here whose first parameter is ``self`` is a ``KnowledgeStore`` method:
the class binds it under the same name in ``knowledge/store.py``, so it reads the
store's fields and calls its sibling methods through ``self``.

The ``KnowledgeStore`` fields this module uses. Writes: none. Reads: none; every query
goes through ``self.db``.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kiro_crew.knowledge.store_parts.bundle_transfer import KnowledgeBundleError, _bundle_item_group
from kiro_crew.knowledge.store_parts.item_ownership import (
    _DOC_STATE_KEY_COL,
    _DOC_STATE_TABLES,
    _OWNERSHIP_HASH_COL,
)

if TYPE_CHECKING:
    from kiro_crew.knowledge.store import KnowledgeStore


#: Tables whose row may be restored from a bundle. A state row is a claim that a LOCAL
#: subsystem owns this document, and importing one makes that claim on the subsystem's
#: behalf without its knowledge -- so wherever that subsystem REAPS BY ABSENCE, the row
#: is not a stale marker but an order to delete the items the import just brought. Two
#: of the three do reap, which is why only one is listed:
#:
#: * ``folder_file_state`` -- ``FolderWatcher._do_scan`` step 4 walks every state row and
#:   deletes the ones whose path its walk did not yield, WITHOUT consulting ``status``.
#:   What the walk yields depends on the MAPPED source's own live filters (extension
#:   allowlist, ``min_file_bytes``, ``ignore_patterns``, ``confine_to_root``, and a root
#:   ``.kiroignore`` re-read every sweep), so a receiving store that filters the same
#:   folder more narrowly than the sender deletes the arriving documents.
#: * ``artifact_item_state`` -- ``reconcile_artifacts`` runs on every
#:   ``ArtifactKnowledgeSync.start``, takes ``known.keys() - live``, and removes each
#:   provably absent slug's group. A bundle imported on a host whose artifact store does
#:   not hold those slugs (any second machine, any restore) therefore loses them on the
#:   next start. ``_known_kinds`` reads every row regardless of ``status``, so no status
#:   value hides the row from that pass.
#: * ``agent_item_state`` -- nothing reaps it. ``agent_source.remove_document`` runs only
#:   when a caller explicitly deletes that document; there is no reconcile pass, no
#:   inventory comparison, and no absence test anywhere on the agent path.
#:
#: A row left out costs its items their ownership, which is the state a bundle carrying
#: no state tables already leaves them in, and the owning subsystem re-derives its row
#: the next time it sees the document.
_BUNDLE_STATE_RESTORED_TABLES = frozenset({"agent_item_state"})


def _bundle_state_restores(table: str) -> bool:
    """Whether a row of *table* may be restored onto this host."""
    return table in _BUNDLE_STATE_RESTORED_TABLES


def _bundle_state_text(field: str, value: object) -> str | None:
    """A state row's text column, refused as a typed error if it cannot be bound.

    ``isinstance(value, str)`` is not enough: a lone surrogate is a ``str`` that
    SQLite cannot encode, and the ``UnicodeEncodeError`` it raises at bind time sits
    outside every arm the import endpoint catches, so it surfaces as a 500 instead of
    the malformed-bundle 400. Same gate the properties and signature columns use, for
    the same reason -- these strings reach a bind too.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise KnowledgeBundleError(f"'{field}' must be a string or null")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise KnowledgeBundleError(f"'{field}' must be valid UTF-8 text") from None
    return value


#: Conservative ceiling on bound parameters in one statement -- the same one
#: :mod:`kiro_crew.vector_memory` keeps, for the same reason: sqlite's own limit is
#: 32,766 on the bundled build but only 999 on a host still on a pre-3.32 library, and
#: there is no cheap way to read it per runtime. Every ``IN`` list below whose length
#: comes from a BUNDLE is chunked under it. A bundle names as many sources as it likes
#: and one state row's group runs to ``_MAX_ITEM_GROUP_MEMBERS``, both past 999, and the
#: failure is an ``OperationalError`` raised mid-transaction: the whole import rolls back
#: and the endpoint answers 500 where it should have succeeded.
_MAX_SQL_PARAMS = 400


def _sql_param_chunks(values: set[str] | list[str], reserve: int = 0) -> Iterator[tuple[str, ...]]:
    """*values* in bind-sized tuples, one ``IN`` list each; empty input yields nothing.

    *reserve* is how many binds that statement spends on its OWN fixed parameters -- a
    ``WHERE source_id = ? AND id IN (...)`` reserves one -- so the whole statement, not
    just the list, stays inside :data:`_MAX_SQL_PARAMS`.
    """
    width = max(1, _MAX_SQL_PARAMS - reserve)
    ordered = list(values)
    for start in range(0, len(ordered), width):
        yield tuple(ordered[start : start + width])


#: Columns each state table carries through a bundle, beyond its source, its key
#: and its item group. Everything else is either derived on import -- the live
#: status, which the surviving group defines -- or local bookkeeping that does not
#: travel: retry counters, error text, and the dedup winner a losing row points at
#: all describe the EXPORTING store's own progress.
_BUNDLE_STATE_CARRIED_COLS: dict[str, tuple[str, ...]] = {
    "folder_file_state": ("content_hash", "text_hash", "last_seen"),
    "artifact_item_state": ("content_hash", "updated_at", "name", "kind"),
    "agent_item_state": ("content_hash", "updated_at", "name", "source_uri"),
}


#: Columns written to a fixed value instead of being carried.
#:
#: Carried columns that are NOT NULL in the schema. A bundle omitting one gets the
#: import's own clock, so a row that otherwise qualifies is not refused over a
#: missing timestamp.
_BUNDLE_STATE_REQUIRED_COLS = frozenset({"last_seen", "updated_at"})


def _bundle_blocked_items(
    self: KnowledgeStore, bundle: dict, source_id_map: dict[str, str]
) -> tuple[set[str], list[dict[str, str]]]:
    """Item ids a bundle may not bring in, because this store already holds the
    document that would own them.

    Two documents cannot share one ``(source, key)``: that pair IS a document's
    identity within a source. When a live local row already holds the pair -- the
    same folder watched on two machines, the same artifact slug on both -- the
    bundle's copy is not a document here, and importing its items anyway would
    leave them permanently unowned: nothing claims them for de-duplication, the
    Sources UI shows no group for them, and the text answers searches alongside
    the local copy after every later edit of it.

    Only that reason blocks an item, because only that reason says the content
    does not belong here. Every other refusal in :meth:`_import_bundle_state`
    rejects a ROW as an untrustworthy statement about items that are themselves
    legitimate -- an unparsable group, one whose ids another row already owns, one
    the bundle files under a different source -- and those items arrive unowned
    exactly as they do from a bundle with no state tables at all. An over-claiming
    group is not among them: the ids the import wrote are owned and the rest are
    merely named in the account, so nothing it brought is left unowned.

    A row whose group overlaps another bundle row's group blocks nothing, and
    neither does an id whose own bundle item is filed under a DIFFERENT source.
    The bundle does not agree about who owns such an item, so treating one row's
    collision as a verdict on it would drop another document's valid content --
    the store's own writer can produce that shape, when a reassigned item leaves
    one row naming an item its new owner's row never names. Those ids arrive
    unowned instead, like every other item a malformed ownership row describes.
    """
    # Where each item the bundle ships will actually be filed. An id the colliding
    # row names but the bundle files elsewhere is not this document's to withhold.
    item_source: dict[str, str | None] = {}
    for item in bundle.get("items", []):
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            raw = item.get("source_id")
            mapped = source_id_map.get(raw) if isinstance(raw, str) else None
            item_source[item["id"]] = mapped
    groups: list[set[str]] = []
    colliding: list[tuple[str, str, str, set[str], set[str]]] = []
    for table in _DOC_STATE_KEY_COL:
        # Every table, including the ones whose rows are never restored, because
        # withholding is not reserving items for a claim someone goes on to make.
        # It is the answer to "this store already holds that document": a folder
        # file at the same path, or the same artifact slug, IS that document here,
        # so importing its items again leaves identical text answering searches
        # beside the local copy with nothing owning it and no pass that removes it.
        # Arriving unowned is the acceptable outcome for a document this store
        # LACKS; for one it already holds, not arriving is.
        key_col = _DOC_STATE_KEY_COL[table]
        for row in bundle.get(table, []):
            if not isinstance(row, dict):
                continue
            raw_source = row.get("source_id")
            if not isinstance(raw_source, str):
                continue
            target_source = source_id_map.get(raw_source)
            # Validated here because this is the FIRST bind a state key reaches.
            key = _bundle_state_text(f"{table}.{key_col}", row.get(key_col))
            if target_source is None or not key:
                continue
            group = set(_bundle_item_group(row.get("item_ids")))
            if not group:
                continue
            groups.append(group)
            held = self.db.execute(
                f"SELECT item_ids FROM {table} "  # noqa: S608
                f"WHERE source_id = ? AND {key_col} = ?",
                (target_source, key),
            ).fetchone()
            if held is not None and self._state_row_owns_items(held["item_ids"], target_source):
                colliding.append(
                    (table, target_source, key, group, set(_bundle_item_group(held["item_ids"])))
                )
    seen: Counter[str] = Counter()
    for group in groups:
        seen.update(group)
    shared = {item_id for item_id, count in seen.items() if count > 1}
    blocked: set[str] = set()
    withheld: list[dict[str, str]] = []
    for table, target_source, key, group, local_group in colliding:
        # A collision is only a DIFFERENT document holding the key. When the local
        # row's own group overlaps the bundle's, this is that same document coming
        # back, and withholding its group destroys exactly what the bundle is for:
        # a per-chunk delete leaves the group naming the deleted id while a
        # surviving sibling keeps the row live, so the whole group -- the deleted
        # chunk included -- would be held back, silently, with no exit but deleting
        # the rest of the document first. Its live ids are skipped by ``INSERT OR
        # IGNORE`` and its absent ids arrive, to be merged into that row's group by
        # the accepting pass.
        if group & local_group:
            continue
        document = {
            item_id
            for item_id in group
            if item_id not in shared and item_source.get(item_id) == target_source
        }
        if not document:
            continue
        blocked |= document
        withheld.append(
            {
                "reason": "document_key_held_locally",
                "table": table,
                "source_id": target_source,
                "key": key,
                "items": str(len(document)),
            }
        )
    # An id a local row claims must not be handed to an import, with one exception
    # for the document the bundle is restoring; see :meth:`_contested_claims`. An id
    # that already exists here is outside it either way: ``INSERT OR IGNORE`` leaves
    # the local row alone and the inserted-items rule already refuses to hand it to
    # an imported claim.
    contested, stale_file = self._contested_claims(bundle, source_id_map, set(item_source))
    _present, absent = self._items_exist(contested)
    for item_id in sorted(absent - blocked):
        if item_id in stale_file:
            # This id's only claimant is the restoring document's own
            # ``folder_file_state`` row whose file is gone -- a deletion the next
            # folder scan will run, not a live foreign owner. Saying "another
            # document claims it" is wrong and offers no remedy; name the actual
            # one: let the folder rescan reap the stale row, then re-import.
            withheld.append({"reason": "folder_file_rescan_then_reimport", "item_id": item_id})
        else:
            withheld.append({"reason": "item_id_claimed_by_another_document", "item_id": item_id})
    return blocked | absent, withheld


def _backing_object_present(table: str, key: str) -> bool:
    """Whether *key*'s backing object still exists, for the one table it is testable on.

    The claimed-id exemption in :meth:`KnowledgeStore._contested_claims` forgives a
    document meeting its OWN local row -- but only safely while the backing object that
    row names still exists. A row whose object is already gone and not yet reconciled
    away still holds the key, and exempting its claim lets a bundle import make the ids
    live over content the next reap then deletes: the silent-restore-then-delete loss
    this guards against.

    *key* MUST be a LOCAL doc-state row's recorded key, never a bundle-authored string.
    For ``folder_file_state`` this reaches the filesystem (``Path.is_file``), and a
    bundle-supplied path would let an uploaded bundle name any host path -- a UNC path
    leaks NTLM over SMB on Windows, a stalled mount holds the write lock. The sole
    caller stats ``row["row_key"]`` from the local table for exactly this reason.

    Only ``folder_file_state`` can answer here. Its key IS a filesystem path, so a
    regular file at it on disk is a direct, bounded, in-transaction test. ``is_file``
    rather than ``exists``: the folder reap keys on what ``FolderWatcher._walk`` yields,
    which is FILES, so a directory (or a broken symlink) sitting at the path is not a
    backing object the walk would ever re-ingest -- the two agree only on files. The
    other tables keep the exemption unconditionally (returning ``True``): nothing reaps
    ``agent_item_state`` by absence, and ``artifact_item_state`` existence lives in the
    artifact store the reconcile pass holds and this transaction cannot reach, where
    withholding is the rejected variant that loses a per-chunk-deleted document for good.

    A path that cannot be stat'd (permission, a path too long, a broken mount) is
    treated as present: the same conservatism :func:`_artifact_is_really_gone` uses,
    because a stale group is recoverable on the next reconcile while a withheld-away
    document is not.
    """
    if table != "folder_file_state":
        return True
    try:
        return Path(key).is_file()
    except OSError:
        return True


def _contested_claims(
    self: KnowledgeStore, bundle: dict, source_id_map: dict[str, str], shipped: set[str]
) -> tuple[set[str], set[str]]:
    """Shipped ids a local row claims that this import must not create, and the
    subset of them contested ONLY because a restoring folder row's backing file is
    gone (so the remedy is a rescan then a re-import, not "another document owns it").

    The rule and its one exception: an id a local row claims must not be handed to an
    import, UNLESS the claiming row is a document this bundle is restoring. Both
    halves are load-bearing and they fail in opposite directions. The exception is
    keyed on the bundle's own row at the SAME ``(table, source, key)``, which is what
    makes it "this document meeting itself" rather than a blanket amnesty -- so it
    covers every table the bundle states, not only the one whose rows get written. A
    folder file or artifact slug this host STILL HOLDS owns its chunk correctly, and
    no pass re-derives one withheld here: the scan reaps only a row whose path the
    walk missed and skips a ``done`` row whose mtime and hash did not change. The
    exemption is therefore gated on the backing object still existing
    (``_backing_object_present``): a ``folder_file_state`` row whose file is gone is a
    deletion order the next scan will run, not a document to restore, so it stays
    contested rather than vouching for ids that reap would then delete.

    Without the rule, a stale group belonging to an UNRELATED document captures the
    item: the claim is read before the item loop, so the id does not exist yet, the
    insert creates it, the claim goes live over content it never owned, and the
    bundle's own row is skipped as claimed -- leaving the item held only by that row,
    so deleting that unrelated document destroys imported content.

    Without the exception, restoring a document meets its own row. ``_delete_item_
    cascade`` removes an item and leaves the doc-state group naming it, which is the
    documented normal shape, so re-importing an export would meet the deleter's own
    row and withhold the very item the bundle is the only copy of. That holds whether
    the row owns anything live or not: a per-chunk delete leaves a surviving sibling,
    and the row is then live AND the document being restored at once. The accepting
    pass repoints or merges that row, so withholding there contradicts it instead of
    guarding it.
    """
    restored_groups: dict[tuple[str, str, str], set[str]] = {}
    stated: set[str] = set()
    for table, key_col in _DOC_STATE_KEY_COL.items():
        rows = bundle.get(table, [])
        if not isinstance(rows, list) or not rows:
            continue
        stated.add(table)
        # Collected for EVERY table the bundle states, including the ones no restore
        # writes. The exemption is keyed on the bundle's own row at this same
        # ``(table, source, key)``, so what it forgives is this document meeting
        # itself -- and that is just as true of a folder file or an artifact slug this
        # host still has. `_delete_item_cascade` never prunes a group, by design, so a
        # per-chunk delete leaves the row naming the chunk while the file or slug is
        # still here; withholding the id there loses it for good, because the folder
        # scan reaps only a row whose path the walk MISSED and skips a `done` row
        # whose mtime and hash are unchanged, so nothing re-ingests the chunk.
        #
        # This loop reads only the BUNDLE's own rows, so it does no filesystem access:
        # the key here is bundle-authored, and stat-ing it would let an uploaded bundle
        # name any host path (a UNC path leaks NTLM over SMB on Windows, a stalled
        # mount holds the write lock) before a single local row is consulted. The
        # existence test the opposite race needs is applied in the SECOND loop below,
        # against the LOCAL row's recorded key, so only a path this host already wrote
        # is ever stat'd.
        for row in rows:
            if not isinstance(row, dict):
                continue
            raw = row.get("source_id")
            target = source_id_map.get(raw) if isinstance(raw, str) else None
            key = row.get(key_col)
            group = set(_bundle_item_group(row.get("item_ids")))
            if target and isinstance(key, str) and key and group:
                restored_groups.setdefault((table, target, key), set()).update(group)
    contested: set[str] = set()
    stale_file: set[str] = set()
    other: set[str] = set()
    sources = set(source_id_map.values())
    if not sources or not shipped:
        return contested, stale_file
    for table, key_col in _DOC_STATE_KEY_COL.items():
        # A bundle that ships no row for a table states no ownership through it, so
        # its items cannot be judged against that table's local claims at all. The
        # single-item export endpoint and every pre-existing bundle file are exactly
        # that shape, and judging them here withholds the item a delete removed with
        # no exit: no exemption can exist when nothing was shipped to exempt it.
        if table not in stated:
            continue
        # Chunked: the source count is untrusted bundle input, so one ``IN`` list over
        # all of them can pass the bind ceiling and abort the import (:data:`_MAX_SQL_PARAMS`).
        for params in _sql_param_chunks(sources):
            placeholders = ", ".join("?" * len(params))
            for row in self.db.execute(
                f"SELECT source_id, {key_col} AS row_key, item_ids "  # noqa: S608
                f"FROM {table} WHERE source_id IN ({placeholders})",
                params,
            ):
                overlap = set(_bundle_item_group(row["item_ids"])) & shipped
                if not overlap:
                    continue
                # Exempt the IDS the bundle's own row for this key names, not the key.
                # A marker row carries the key with an EMPTY group -- a full export
                # ships those verbatim -- so keying the exemption on the pair alone
                # lets such a row vouch for ids it never claimed, handing them to
                # whatever unrelated local row holds a stale claim on them.
                #
                # The exemption is further gated on the LOCAL row's backing object
                # still existing (``_backing_object_present``), the one slice of the
                # owning subsystem's existence answer reachable in this transaction:
                # a ``folder_file_state`` key is a filesystem path, so the file's
                # presence on disk is a direct, bounded test, and the folder reap keys
                # on that same path. A local row whose file is already GONE -- deleted
                # and not yet reconciled away -- is a deletion order the next scan runs,
                # not a document to restore: exempting it would let the import make the
                # ids live over content that reap then deletes, silent loss of exactly
                # the document the user imported the bundle to restore. The key stat'd
                # here is ``row["row_key"]`` from the LOCAL table, never a bundle string,
                # so no uploaded path is ever touched. The other two tables keep the
                # exemption unconditionally (``_backing_object_present`` returns True):
                # nothing reaps ``agent_item_state`` by absence, and
                # ``artifact_item_state`` existence lives in the artifact store the
                # ``reconcile_artifacts`` pass holds and this transaction cannot reach,
                # so withholding there is the rejected variant that loses a
                # per-chunk-deleted document for good.
                exempt = restored_groups.get((table, row["source_id"], row["row_key"]), set())
                # An id contested ONLY because this own-row's backing file is gone is
                # a different story than one a foreign row claims: the user's remedy
                # is to let the folder rescan reap the stale row, then re-import. We
                # record which ids fell to the file gate so :meth:`_bundle_blocked_
                # items` can report that remedy instead of the misleading "another
                # document claims it". An id a NON-exempt (foreign) row also contests
                # stays in the generic bucket -- the rescan would not free it -- so
                # the reported stale-file set is net of ``other`` below.
                file_gate_denied = bool(exempt) and not _backing_object_present(
                    table, row["row_key"]
                )
                if file_gate_denied:
                    exempt = set()
                newly = overlap - exempt
                if file_gate_denied:
                    stale_file |= newly
                else:
                    other |= newly
                contested |= newly
    return contested, stale_file - other


def _items_exist(self: KnowledgeStore, item_ids: set[str]) -> tuple[set[str], set[str]]:
    """(*item_ids* this store holds, the rest), read in bounded chunks.

    Chunked because the caller's set is bundle-sized rather than group-sized, and a
    single ``IN`` list long enough to cover it can exceed SQLite's parameter limit.
    """
    present: set[str] = set()
    for chunk in _sql_param_chunks(item_ids):
        placeholders = ", ".join("?" * len(chunk))
        present.update(
            row["id"]
            for row in self.db.execute(
                f"SELECT id FROM items WHERE id IN ({placeholders})", chunk  # noqa: S608
            )
        )
    return present, item_ids - present


def _import_bundle_state(
    self: KnowledgeStore,
    bundle: dict,
    source_id_map: dict[str, str],
    inserted_items: set[str],
    now: str,
) -> tuple[int, list[dict[str, str]]]:
    """Restore per-document ownership rows for the items this bundle brought.

    Runs INSIDE :meth:`import_bundle`'s transaction and AFTER the item loop,
    because the test a row has to pass is which items the import actually wrote.

    A row owns the items THIS import inserted for it, and nothing else. Ownership
    means "these items, all of them", so an id the bundle ships that ALREADY
    existed here is left out: ``INSERT OR IGNORE`` skipped it, the row in the store
    is local content, and naming it would hand a foreign document the authority to
    replace or delete something this store owns. Unowned local content -- residue
    from an interrupted ingest -- is the case that makes that load-bearing rather
    than theoretical.

    What the import DID write is a different statement, and refusing it is what
    strands content. A second bundle for a document already here brings exactly
    that shape: the first restore put part of the group down, so the second's row
    names ids it did not insert alongside ids it did. Dropping the whole row leaves
    the new chunks searchable and owned by nothing -- ``agent_item_state`` is the
    one table a bundle restores and no pass reaps it, so the document's own delete
    takes the ids its row names and walks past the rest. The arriving ids therefore
    join the live local row's group WHEN that row's own group overlaps the bundle's,
    which is what says the two are one document coming back. This pass tests that
    itself rather than inheriting it from the blocking pass, which cannot supply it:
    an id two bundle rows both name is shared, so it is never blocked, and it arrives
    with a DISJOINT live row sitting on its key. Merging there would file it under an
    unrelated local document whose own delete would then destroy it, so such a row is
    skipped and its items arrive unowned, reported as ``ownership_row_key_held_locally``.
    A merge extends that row's group and touches nothing else, because its hash,
    name and status describe this store's copy and the bundle can be older than it.

    A row with nothing left to own is dropped rather than repaired, and its items
    arrive unowned: an empty group is a de-duplication claim or a scan marker,
    both of which describe the exporting store's progress and are re-derived
    here by the next ingest of that document.

    The status written is the table's own live value rather than the bundle's:
    the surviving group is what makes a row live, and the vocabularies differ
    per table (see :data:`_DOC_STATE_TABLES`).

    A local row for the same document keeps its identity while it OWNS live items,
    and only its group grows. One with an empty or stale group is holding a marker,
    not ownership, so the imported row takes its place and its claim on another
    source's items is released with it.

    Returns the number of rows restored.
    """
    # Ids already spoken for, so no item ends up in two groups. One item in two
    # groups is content-destroying rather than untidy: the next document-level
    # delete hands ``delete_items_batch`` an item whose only other holder is a
    # state row it does not consult, finds nothing else holding it, and removes
    # it -- leaving the second row naming deleted content. The module already
    # refuses this shape elsewhere, in ``_adopt_reassigned_item`` and
    # ``detach_source_location_by_hash``, rather than putting one item in two
    # groups. Seeded from the local rows of the sources this bundle touches, then
    # grown as rows are accepted, so overlap WITHIN one bundle is caught too.
    claimed_ids = self._claimed_item_ids(set(source_id_map.values()))
    restored = 0
    dropped: list[dict[str, str]] = []
    for table, live_status in _DOC_STATE_TABLES:
        if not _bundle_state_restores(table):
            continue
        key_col = _DOC_STATE_KEY_COL[table]
        carried = _BUNDLE_STATE_CARRIED_COLS[table]
        for row in bundle.get(table, []):
            if not isinstance(row, dict):
                continue
            raw_source = row.get("source_id")
            # A non-string source id would be an unhashable dictionary key.
            target_source = source_id_map.get(raw_source) if isinstance(raw_source, str) else None
            key = _bundle_state_text(f"{table}.{key_col}", row.get(key_col))
            if not key:
                # An empty or non-text key names no document a caller could look for,
                # so this skip stays silent -- the one unowned-arrival path with no
                # account entry. A withheld entry is keyed on the document key, and
                # there is none here to report; the same reasoning keeps a non-text
                # ``source_id`` silent above.
                continue
            group = _bundle_item_group(row.get("item_ids"))
            if target_source is None:
                # A text source id the bundle never declares resolves to no source
                # here, so the row is skipped and the items it names arrive unowned.
                # The account names the row in the bundle's own words, as every other
                # unowned-arrival path does; the handler redacts these fields by name.
                stranded = sorted(set(group) & inserted_items)
                if isinstance(raw_source, str) and stranded:
                    dropped.append(
                        {
                            "reason": "ownership_row_source_undeclared",
                            "table": table,
                            "source_id": raw_source,
                            "key": key,
                            "items": ",".join(stranded),
                        }
                    )
                continue
            if not group:
                continue
            existing = self.db.execute(
                f"SELECT {_OWNERSHIP_HASH_COL[table]} AS owned_hash, item_ids "  # noqa: S608
                f"FROM {table} WHERE source_id = ? AND {key_col} = ?",
                (target_source, key),
            ).fetchone()
            local_group = _bundle_item_group(existing["item_ids"]) if existing is not None else []
            live = existing is not None and self._state_row_owns_items(
                existing["item_ids"], target_source
            )
            if live and not set(local_group) & set(group):
                # A live local row sharing NO id with the bundle's group is a
                # DIFFERENT document holding this key, and the blocking pass does not
                # keep the two apart on its own: an id two bundle rows both name is
                # shared, so it is never blocked, and it reaches here with that
                # disjoint row in place. Merging would put the arriving id into this
                # store's unrelated document, whose own delete then takes content the
                # import brought -- with no pass that reaps the row or reports it.
                # Skip the row instead: its items arrive unowned, the outcome every
                # other untrustworthy ownership row already gets, and the account
                # names the key that held them back.
                stranded = sorted(set(group) & inserted_items)
                if stranded:
                    dropped.append(
                        {
                            "reason": "ownership_row_key_held_locally",
                            "table": table,
                            "source_id": target_source,
                            "key": key,
                            "items": ",".join(stranded),
                        }
                    )
                continue
            # A live local row whose group OVERLAPS the bundle's is THIS document
            # already here, so its group is the base the arriving ids join and its
            # own ids are already owned rather than missing.
            merging = live
            base = local_group if merging else []
            # What this import actually wrote for this row, and what the row names
            # that nothing here hands it. Ownership covers the first; the second is
            # local content or absent content, and claiming either would let a
            # foreign document replace or delete something this store owns.
            arriving = [item for item in group if item in inserted_items and item not in base]
            absent = sorted(set(group) - inserted_items - set(base))
            if absent:
                # An export this store writes prunes the group to what it carries, so
                # this is a bundle built elsewhere, or a second bundle whose ids a
                # previous restore already put here under another document. Those ids
                # stay out of the group -- partial ownership would name items another
                # document holds -- and the caller is told which document and which
                # ids, because their content is here unowned and nothing else says so.
                dropped.append(
                    {
                        "reason": "ownership_row_names_absent_items",
                        "table": table,
                        "source_id": target_source,
                        "key": key,
                        "items": ",".join(absent),
                    }
                )
            if not arriving:
                # Nothing this import wrote, so there is no ownership to record: the
                # row would either restate the base or claim local content.
                continue
            # An id another document already owns is never taken, and the base is not
            # "another document" -- it is the row being extended. The whole row is
            # refused rather than partially owned, so the items this import wrote for
            # it arrive unowned; name them, as every other unowned-arrival path does,
            # since nothing else reports this one. The id another row claims is never
            # blocked (the store's own writer produces it when a reassigned item
            # leaves one row naming an item its new owner's row never names), so the
            # count would otherwise read whole while the content arrived owned by
            # nothing.
            if set(arriving) & (claimed_ids - set(base)):
                stranded = sorted(set(group) & inserted_items)
                if stranded:
                    dropped.append(
                        {
                            "reason": "ownership_row_item_claimed_elsewhere",
                            "table": table,
                            "source_id": target_source,
                            "key": key,
                            "items": ",".join(stranded),
                        }
                    )
                continue
            # The ids were inserted by this import, so they exist -- but under the
            # source the ITEM named, which is not necessarily the one this row names.
            # A row may only own items filed under itself, and one id filed elsewhere
            # makes the whole row an untrustworthy statement about ownership rather
            # than a partial one: this store's own export cannot produce that shape.
            # Chunked because the group runs to ``_MAX_ITEM_GROUP_MEMBERS``, past the
            # bind ceiling on a pre-3.32 host (:data:`_MAX_SQL_PARAMS`).
            held: set[str] = set()
            for chunk in _sql_param_chunks(arriving, reserve=1):
                placeholders = ", ".join("?" * len(chunk))
                held.update(
                    r["id"]
                    for r in self.db.execute(
                        "SELECT id FROM items WHERE source_id = ? "  # noqa: S608
                        f"AND id IN ({placeholders})",
                        (target_source, *chunk),
                    ).fetchall()
                )
            if held != set(arriving):
                # One id this import wrote landed under the source the ITEM named, not
                # the one this row names, which makes the row an inconsistent
                # statement about ownership rather than a partial one, so the whole
                # row is refused. The items it named that this import wrote still
                # arrive -- owned by nothing -- so name them, as the neighbouring
                # refusals do; nothing else reports this one.
                stranded = sorted(set(group) & inserted_items)
                if stranded:
                    dropped.append(
                        {
                            "reason": "ownership_row_item_source_mismatch",
                            "table": table,
                            "source_id": target_source,
                            "key": key,
                            "items": ",".join(stranded),
                        }
                    )
                continue
            if merging:
                # Extend the live row's group and touch nothing else. Its hash, name
                # and status describe THIS store's copy of the document, and a bundle
                # reaching this branch can be older than that copy -- repointing the
                # identity columns at it would leave the row describing a revision
                # this store does not hold.
                self.db.execute(
                    f"UPDATE {table} SET item_ids = ? "  # noqa: S608
                    f"WHERE source_id = ? AND {key_col} = ?",
                    (json.dumps([*base, *arriving]), target_source, key),
                )
                claimed_ids.update(arriving)
                restored += 1
                continue
            if existing is not None:
                # An empty or stale group is not ownership. It is a de-duplication
                # marker or a scan marker, and nothing else will ever name the items
                # this bundle brought, so the imported row takes the key. The
                # marker's claim on another source's items goes with it: leaving the
                # claim behind under a hash no row names any more means a later
                # deletion of the holder reassigns an item here and finds nothing to
                # adopt it into.
                self.detach_source_location_by_hash(target_source, existing["owned_hash"] or "")
            columns = ("source_id", key_col, "item_ids", "status", *carried)
            values: list[Any] = [target_source, key, json.dumps(arriving), live_status]
            for col in carried:
                value = row.get(col)
                if col in _BUNDLE_STATE_REQUIRED_COLS and not isinstance(value, str):
                    value = now
                values.append(_bundle_state_text(f"{table}.{col}", value))
            self.db.execute(
                f"INSERT OR REPLACE INTO {table} ({', '.join(columns)}) "  # noqa: S608
                f"VALUES ({', '.join('?' * len(columns))})",
                values,
            )
            claimed_ids.update(group)
            restored += 1
    return restored, dropped


def _claimed_item_ids(self: KnowledgeStore, source_ids: set[str]) -> set[str]:
    """Every item id an existing state row of *source_ids* already owns.

    Scoped to the sources a bundle resolves to, because ownership is per-source
    and that keeps the read bounded on a large library.
    """
    claimed: set[str] = set()
    if not source_ids:
        return claimed
    for table in _DOC_STATE_KEY_COL:
        # Chunked: the caller's set is one entry per source the BUNDLE resolves to,
        # which nothing caps (:data:`_MAX_SQL_PARAMS`).
        for params in _sql_param_chunks(source_ids):
            placeholders = ", ".join("?" * len(params))
            for row in self.db.execute(
                f"SELECT item_ids FROM {table} "  # noqa: S608
                f"WHERE source_id IN ({placeholders})",
                params,
            ):
                claimed.update(_bundle_item_group(row["item_ids"]))
    return claimed


def _state_row_owns_items(self: KnowledgeStore, raw: object, source_id: str) -> bool:
    """Whether a state row of *source_id* still names an item filed under it.

    An empty group is not ownership: a document that lost a de-duplication keeps a
    marker row with no group, and every pending or failed scan row carries one too.
    A group naming only items that have since been deleted is the same thing with
    more history behind it.

    Existence alone is not ownership either, which is why *source_id* is matched.
    ``_adopt_reassigned_item`` leaves a stale claim by design when a content hash is
    ambiguous, so a row can name items that ``delete_source_cascade`` reassigned to
    a different source. Those items are live and the row describing them is not this
    one, so reading the claim as ownership withholds the document a bundle carries
    and loses its content silently -- which is the same rule the accepting pass
    applies to the group a bundle ships.
    """
    group = _bundle_item_group(raw)
    if not group:
        return False
    # Chunked: the group runs to ``_MAX_ITEM_GROUP_MEMBERS``, past the bind ceiling on
    # a pre-3.32 host (:data:`_MAX_SQL_PARAMS`). One live id anywhere is ownership, so
    # the first chunk that finds one answers.
    for chunk in _sql_param_chunks(group, reserve=1):
        placeholders = ", ".join("?" * len(chunk))
        if (
            self.db.execute(
                f"SELECT 1 FROM items WHERE source_id = ? "  # noqa: S608
                f"AND id IN ({placeholders}) LIMIT 1",
                (source_id, *chunk),
            ).fetchone()
            is not None
        ):
            return True
    return False
