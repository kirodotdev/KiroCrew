"""Residue sweep for the agent aggregate source.

An agent-document add commits its item chunks, the previous group's deletion and
its ``agent_item_state`` ownership row as three separate autocommit transactions.
A plain cancellation is already covered -- the row is written from inside the
ingest's uncancellable finalize hop -- but a HARD KILL between the item commit
and the row commit still leaves committed items that no state row names. Nothing
on the agent path reaps by absence, so that residue is permanent: the
replacement path keys off the missing row, so the next add of the same document
stores a second copy instead of replacing the first.

``KnowledgeStore.reclaim_agent_source_residue`` removes it, but ONLY on positive
evidence of an interrupted ingest, never by absence of a row. "No row names this
item" is not proof of crash residue: ``_import_bundle_state`` leaves a bundle's
agent items unowned on several legitimate paths (a row skipped as
``ownership_row_key_held_locally``, an empty or over-claiming group, a bundle
with no state tables), and a single-item export/import round trip does the same.
So the ingest records an intent marker in ``agent_ingest_intent`` (carrying the
content hash it stamps on every chunk) BEFORE it commits items and deletes that
marker on finalize; the sweep deletes an unowned item ONLY when an intent marker
names its hash. The marker is a separate table, not a status on the ownership
row, so it coexists with a live row -- a crash while REPLACING a group leaves a
marker exactly as a first add does. These tests pin both halves: real crash
residue (items + a stranded intent marker) is removed, and legitimately unowned
content -- a bundle-imported item with no marker, folder/artifact sources -- is
never touched.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.knowledge.agent_source import (
    add_agent_document,
    document_slug,
    ensure_agent_source,
    get_state,
)
from kiro_crew.knowledge.ingestion import IngestionPipeline
from kiro_crew.knowledge.readers import FileReader
from kiro_crew.knowledge.store import KnowledgeStore

URI = "https://example.invalid/doc"
OTHER_URI = "https://example.invalid/other"


def _one_chunk(text, **kw):
    return [
        {"content": text, "chunk_index": 0, "section_title": None, "line_start": 0, "line_end": 0}
    ]


@pytest.fixture()
def kstore(tmp_path):
    s = KnowledgeStore(str(tmp_path / "knowledge.db"))
    yield s
    s._close_all_for_tests()


@pytest.fixture()
def pipeline(kstore):
    extractor = MagicMock()
    extractor._pool = None
    extractor.extract_batch = AsyncMock(
        side_effect=lambda contents: [
            {"category": "document", "summary": "s", "entities": []} for _ in contents
        ]
    )
    chunker = MagicMock()
    for m in ("chunk", "chunk_markdown", "chunk_code", "chunk_slides"):
        getattr(chunker, m).side_effect = _one_chunk
    return IngestionPipeline(
        store=kstore,
        extractor=extractor,
        chunker=chunker,
        reader=FileReader(),
        embedder=None,
        dedup_enabled=False,
    )


def _live_ids(store, source_id):
    return {
        r["id"]
        for r in store.db.execute(
            "SELECT id FROM items WHERE source_id = ?", (source_id,)
        ).fetchall()
    }


def _bodies(store, source_id, needle):
    return [
        r["content"]
        for r in store.db.execute(
            "SELECT content FROM items WHERE source_id = ?", (source_id,)
        ).fetchall()
        if needle in r["content"]
    ]


def _simulate_hard_kill(store, source_id, slug):
    """Model a process killed between the item commit and the finalize hop.

    The real sequence is: record the intent marker in ``agent_ingest_intent``,
    commit the item chunks, then (never reached) write the ``active`` ownership
    row and delete the marker. A hard kill before the finalize hop therefore
    leaves the committed items, NO ownership row, AND a stranded intent marker
    that still names their content hash. Reproduce that end state: drop the
    finalized ownership row and re-insert the intent marker the ingest wrote
    first.
    """
    row = store.db.execute(
        "SELECT content_hash FROM agent_item_state WHERE source_id = ? AND slug = ?",
        (source_id, slug),
    ).fetchone()
    chash = row["content_hash"] if row else None
    store.db.execute(
        "DELETE FROM agent_item_state WHERE source_id = ? AND slug = ?",
        (source_id, slug),
    )
    if chash is not None:
        store.db.execute(
            "INSERT OR REPLACE INTO agent_ingest_intent "
            "(source_id, slug, content_hash, started_at) VALUES (?, ?, ?, ?)",
            (source_id, slug, chash, "2024-01-01T00:00:00"),
        )
    store.db.commit()
    return chash


class TestResidueIsRemoved:
    @pytest.mark.asyncio
    async def test_items_an_interrupted_ingest_orphaned_are_swept(self, kstore, pipeline):
        sid, _ = ensure_agent_source(kstore)
        await add_agent_document(pipeline, title="Doc", content="orphan body", source_uri=URI)
        assert _bodies(kstore, sid, "orphan body")

        _simulate_hard_kill(kstore, sid, document_slug(URI))
        # Residue: the items are present but no row names them.
        assert _bodies(kstore, sid, "orphan body")

        removed = kstore.reclaim_agent_source_residue()

        assert removed == 1
        assert not _bodies(kstore, sid, "orphan body")

    @pytest.mark.asyncio
    async def test_a_re_add_after_the_sweep_stores_one_copy_not_two(self, kstore, pipeline):
        # The point of the fix: once residue is cleared, re-adding the same
        # document is a clean first add rather than a silent duplicate.
        sid, _ = ensure_agent_source(kstore)
        await add_agent_document(pipeline, title="Doc", content="dup body", source_uri=URI)
        _simulate_hard_kill(kstore, sid, document_slug(URI))

        kstore.reclaim_agent_source_residue()
        await add_agent_document(pipeline, title="Doc", content="dup body", source_uri=URI)

        assert len(_bodies(kstore, sid, "dup body")) == 1
        assert set(get_state(kstore, sid, document_slug(URI))[1]) == _live_ids(kstore, sid)

    @pytest.mark.asyncio
    async def test_only_the_orphaned_document_is_removed(self, kstore, pipeline):
        sid, _ = ensure_agent_source(kstore)
        await add_agent_document(pipeline, title="Keep", content="keep body", source_uri=OTHER_URI)
        await add_agent_document(pipeline, title="Gone", content="gone body", source_uri=URI)

        _simulate_hard_kill(kstore, sid, document_slug(URI))
        removed = kstore.reclaim_agent_source_residue()

        assert removed == 1
        assert _bodies(kstore, sid, "keep body")
        assert not _bodies(kstore, sid, "gone body")
        # The surviving document still owns exactly its own items.
        assert set(get_state(kstore, sid, document_slug(OTHER_URI))[1]) == _live_ids(kstore, sid)


class TestOwnedAndUnownableContentIsSafe:
    @pytest.mark.asyncio
    async def test_a_fully_owned_store_is_left_untouched(self, kstore, pipeline):
        sid, _ = ensure_agent_source(kstore)
        await add_agent_document(pipeline, title="A", content="a body", source_uri=URI)
        await add_agent_document(pipeline, title="B", content="b body", source_uri=OTHER_URI)
        before = _live_ids(kstore, sid)

        removed = kstore.reclaim_agent_source_residue()

        assert removed == 0
        assert _live_ids(kstore, sid) == before

    def test_no_agent_source_is_a_no_op(self, kstore):
        # A store that never used the agent path has no agent:// row at all.
        assert kstore.reclaim_agent_source_residue() == 0

    def test_a_deduped_marker_row_does_not_make_its_winner_residue(self, kstore):
        # A deduped row owns an EMPTY group: its items were adopted by the
        # winner's row and are named THERE. The sweep must read the winner's
        # group, not conclude the loser owns nothing and sweep the shared items.
        sid, _ = ensure_agent_source(kstore)
        winner = kstore.add_item("W", "shared", "document", source_id=sid, content_hash="h")
        now = "2024-01-01T00:00:00"
        kstore.db.execute(
            "INSERT INTO agent_item_state (source_id, slug, content_hash, item_ids, "
            "updated_at, name, status, source_uri) VALUES (?,?,?,?,?,?,?,?)",
            (sid, "winner-slug", "h", json.dumps([winner]), now, "W", "active", "u1"),
        )
        kstore.db.execute(
            "INSERT INTO agent_item_state (source_id, slug, content_hash, item_ids, "
            "updated_at, name, status, source_uri) VALUES (?,?,?,?,?,?,?,?)",
            (sid, "loser-slug", "h", "[]", now, "L", "deduped", "u2"),
        )
        kstore.db.commit()

        removed = kstore.reclaim_agent_source_residue()

        assert removed == 0
        assert winner in _live_ids(kstore, sid)

    def test_an_unreadable_group_fails_safe_and_sweeps_nothing(self, kstore):
        # A corrupt item_ids value names nothing we can trust. The fail-safe
        # choice is to leave its source's items alone, not to treat them as
        # unowned residue and delete them -- even when a stale ingesting marker
        # would otherwise mark the item as crash residue.
        sid, _ = ensure_agent_source(kstore)
        item = kstore.add_item("X", "body", "document", source_id=sid, content_hash="h")
        now = "2024-01-01T00:00:00"
        kstore.db.execute(
            "INSERT INTO agent_item_state (source_id, slug, content_hash, item_ids, "
            "updated_at, name, status, source_uri) VALUES (?,?,?,?,?,?,?,?)",
            (sid, "corrupt", "h", "{not json", now, "X", "active", "u"),
        )
        # A stranded intent marker names this item's hash, so absent the
        # corrupt row the item would be a candidate.
        kstore.db.execute(
            "INSERT INTO agent_ingest_intent (source_id, slug, content_hash, "
            "started_at) VALUES (?,?,?,?)",
            (sid, "ingest-slug", "h", now),
        )
        kstore.db.commit()

        removed = kstore.reclaim_agent_source_residue()

        # The one real item has no readable owner, but a corrupt row is not
        # proof of residue, so the whole sweep stands down and nothing is swept.
        assert removed == 0
        assert item in _live_ids(kstore, sid)


class TestOtherSourcesAreNeverTouched:
    def test_artifact_and_folder_sources_are_out_of_scope(self, kstore):
        # The whole safety argument is that the sweep is agent:// only. An
        # unowned item under an artifact or folder source is a legitimately
        # imported bundle item (those two tables are not restored by import),
        # and must survive.
        ensure_agent_source(kstore)
        art_sid = kstore.add_source(
            name="Artifacts", source_type="artifact", uri="artifact://", properties={}
        )
        folder_sid = kstore.add_source(
            name="Folder", source_type="local_folder", uri="file:///f", properties={}
        )
        art_item = kstore.add_item("A", "imported art", "document", source_id=art_sid)
        folder_item = kstore.add_item("F", "imported file", "document", source_id=folder_sid)
        # Neither has any state row -- exactly how a bundle leaves them.

        removed = kstore.reclaim_agent_source_residue()

        assert removed == 0
        assert art_item in _live_ids(kstore, art_sid)
        assert folder_item in _live_ids(kstore, folder_sid)

    def test_residue_also_held_by_another_source_is_detached_not_destroyed(self, kstore):
        # A residue item the agent source shares with another source (a dedup
        # co-location) must move to the surviving holder, not be destroyed:
        # delete_items_batch_in_txn is called with owner_source_id so the item
        # survives under its other holder.
        sid, _ = ensure_agent_source(kstore)
        other = kstore.add_source(
            name="Folder", source_type="local_folder", uri="file:///f", properties={}
        )
        item = kstore.add_item(
            "Shared", "shared body", "document", source_id=sid, content_hash="sh"
        )
        # The other source co-holds the same item via a location row.
        kstore.add_source_location(item, other)
        # No active row names it, but a stranded intent marker names its hash
        # -> proven residue from the agent side.
        kstore.db.execute(
            "INSERT INTO agent_ingest_intent (source_id, slug, content_hash, "
            "started_at) VALUES (?,?,?,?)",
            (sid, "shared-slug", "sh", "2024-01-01T00:00:00"),
        )
        kstore.db.commit()

        removed = kstore.reclaim_agent_source_residue()

        assert removed == 1
        # The item is NOT gone -- ownership moved to the folder source.
        row = kstore.db.execute("SELECT source_id FROM items WHERE id = ?", (item,)).fetchone()
        assert row is not None, "co-held residue was destroyed instead of detached"
        assert row["source_id"] == other
        # The agent source has no location row for it.
        agent_loc = kstore.db.execute(
            "SELECT 1 FROM source_locations WHERE item_id = ? AND source_id = ?", (item, sid)
        ).fetchone()
        assert agent_loc is None

    def test_an_imported_agent_document_arrives_owned_and_survives(self, kstore):
        # The twin of the artifact case: a bundle DOES restore agent_item_state,
        # so an imported agent document is owned and the sweep leaves it. This is
        # why the sweep is safe on agent:// specifically.
        sid, _ = ensure_agent_source(kstore)
        item = kstore.add_item("I", "imported body", "document", source_id=sid, content_hash="ih")
        kstore.db.execute(
            "INSERT INTO agent_item_state (source_id, slug, content_hash, item_ids, "
            "updated_at, name, status, source_uri) VALUES (?,?,?,?,?,?,?,?)",
            (
                sid,
                "imported-slug",
                "ih",
                json.dumps([item]),
                "2024-01-01T00:00:00",
                "I",
                "active",
                "iu",
            ),
        )
        kstore.db.commit()

        removed = kstore.reclaim_agent_source_residue()

        assert removed == 0
        assert item in _live_ids(kstore, sid)

    def test_an_unowned_imported_agent_item_survives(self, kstore):
        # The data-loss case the premise got wrong: a bundle import leaves an
        # agent item with NO ownership row on several legitimate paths
        # (ownership_row_key_held_locally, an empty/over-claiming group, a bundle
        # with no state tables), and a single-item export/import round trip does
        # the same. Such an item is NOT crash residue -- no interrupted ingest
        # ever ran for it, so there is no ingesting marker naming its hash. The
        # sweep must leave it, or imported content vanishes unrecoverably.
        sid, _ = ensure_agent_source(kstore)
        imported = kstore.add_item(
            "Imp", "imported unowned body", "document", source_id=sid, content_hash="imp"
        )
        # No agent_item_state row of ANY status for this item's hash.

        removed = kstore.reclaim_agent_source_residue()

        assert removed == 0
        assert imported in _live_ids(kstore, sid)

    def test_an_active_group_shields_items_sharing_a_crashed_hash(self, kstore):
        # F1 (GPT 6.1): a stale ingesting marker names a hash, and an item that
        # a healthy group OWNS shares that hash. The owned item is of course
        # never swept -- but neither is an UNOWNED copy of the same hash: once
        # the content is live-held by a healthy row, a bare hash match must not
        # authorize deleting any copy of it, because that copy may be the sole
        # reassigned original (left unowned by _adopt_reassigned_item's
        # ambiguous-hash refusal). The fail-safe sweep stands down for the whole
        # hash.
        sid, _ = ensure_agent_source(kstore)
        owned_item = kstore.add_item(
            "Owned", "collision body", "document", source_id=sid, content_hash="c"
        )
        orphan = kstore.add_item(
            "Orphan", "orphan body", "document", source_id=sid, content_hash="c"
        )
        now = "2024-01-01T00:00:00"
        kstore.db.execute(
            "INSERT INTO agent_item_state (source_id, slug, content_hash, item_ids, "
            "updated_at, name, status, source_uri) VALUES (?,?,?,?,?,?,?,?)",
            (sid, "owned-slug", "c", json.dumps([owned_item]), now, "O", "active", "u1"),
        )
        kstore.db.execute(
            "INSERT INTO agent_ingest_intent (source_id, slug, content_hash, "
            "started_at) VALUES (?,?,?,?)",
            (sid, "crashed-slug", "c", now),
        )
        kstore.db.commit()

        removed = kstore.reclaim_agent_source_residue()

        # Nothing deleted: the content is live-held, so the hash is not safe
        # residue evidence and BOTH copies survive.
        assert removed == 0
        assert owned_item in _live_ids(kstore, sid)
        assert orphan in _live_ids(kstore, sid)


class TestReplacePathAndNormalization:
    @pytest.mark.asyncio
    async def test_a_crash_while_replacing_a_live_group_is_swept(self, kstore, pipeline):
        # A re-add of CHANGED content replaces a live group. If a hard kill lands
        # between the new item commit and the finalize hop, the OLD row still
        # owns the old items while the new items are orphaned. The marker lives
        # in its own table, so it exists alongside the live ownership row and the
        # sweep reaps exactly the new-hash orphans -- the old owned copy survives.
        from kiro_crew.knowledge.agent_source import mark_ingesting

        sid, _ = ensure_agent_source(kstore)
        await add_agent_document(pipeline, title="Doc", content="first body", source_uri=URI)
        slug = document_slug(URI)
        first_hash, first_ids = get_state(kstore, sid, slug)
        assert first_ids

        # The real replace sequence: the intent marker for the NEW content is
        # written before its items commit. Simulate the new items committing and
        # then a hard kill before the finalize hop (so no ownership-row update,
        # marker left behind).
        import hashlib

        new_text = "second body"
        new_hash = hashlib.sha256(new_text.encode()).hexdigest()
        mark_ingesting(kstore, sid, slug, new_hash, "Doc", source_uri=URI)
        orphan = kstore.add_item("Doc", new_text, "document", source_id=sid, content_hash=new_hash)
        # The old ownership row is untouched (replacement never finalized).
        assert get_state(kstore, sid, slug) == (first_hash, first_ids)

        removed = kstore.reclaim_agent_source_residue()

        assert removed == 1
        assert orphan not in _live_ids(kstore, sid)
        # The old owned copy survives.
        for old_id in first_ids:
            assert old_id in _live_ids(kstore, sid)
        # The reaped attempt's marker is retired.
        left = kstore.db.execute(
            "SELECT COUNT(*) c FROM agent_ingest_intent WHERE source_id = ? "
            "AND content_hash = ?",
            (sid, new_hash),
        ).fetchone()["c"]
        assert left == 0

    @pytest.mark.asyncio
    async def test_a_crlf_document_crash_is_reaped(self, kstore, pipeline):
        # The marker hash must match the hash the pipeline stamps on chunks. The
        # reader normalizes CRLF to LF before chunking, so the ingest normalizes
        # the submitted text before hashing. A CRLF document crashed mid-ingest
        # must therefore still be reaped -- its marker hash matches its chunks'.
        sid, _ = ensure_agent_source(kstore)
        await add_agent_document(
            pipeline, title="CRLF", content="line one\r\nline two\r\n", source_uri=URI
        )
        slug = document_slug(URI)
        _, ids = get_state(kstore, sid, slug)
        assert ids
        # The marker written by the ingest and the hash on its items agree: the
        # finalized row's hash equals every item's content_hash.
        row_hash = kstore.db.execute(
            "SELECT content_hash FROM agent_item_state WHERE source_id = ? AND slug = ?",
            (sid, slug),
        ).fetchone()["content_hash"]
        item_hashes = {
            r["content_hash"]
            for r in kstore.db.execute(
                "SELECT content_hash FROM items WHERE source_id = ?", (sid,)
            ).fetchall()
        }
        assert item_hashes == {row_hash}

        _simulate_hard_kill(kstore, sid, slug)
        removed = kstore.reclaim_agent_source_residue()

        # The CRLF document's residue is reaped because the marker hash matches.
        assert removed >= 1
        assert not _live_ids(kstore, sid)


class TestHashAloneNeverAuthorizesDeletion:
    @pytest.mark.asyncio
    async def test_an_unowned_copy_of_live_held_content_survives(self, kstore, pipeline):
        # F1 (GPT 6.1): a matching marker hash alone must not delete an item. If
        # the SAME content is still held by a healthy ownership row anywhere, an
        # unowned copy of that hash is a reassigned/pre-existing item (left
        # unowned by _adopt_reassigned_item's ambiguous-hash refusal), not this
        # attempt's fresh residue. Deleting it on hash evidence would destroy the
        # only indexed copy. The sweep must leave it alone.
        import hashlib

        sid, _ = ensure_agent_source(kstore)
        shared_text = "shared body"
        chash = hashlib.sha256(shared_text.encode()).hexdigest()

        owned_item = kstore.add_item(
            "Owned", shared_text, "document", source_id=sid, content_hash=chash
        )
        # A healthy ownership row names the owned copy -> the content is live.
        kstore.db.execute(
            "INSERT OR REPLACE INTO agent_item_state "
            "(source_id, slug, content_hash, item_ids, updated_at, name, status, source_uri) "
            "VALUES (?, ?, ?, ?, ?, ?, 'active', ?)",
            (sid, "owned-slug", chash, json.dumps([owned_item]), "2024-01-01", "Owned", URI),
        )
        # A SECOND, unowned copy of the same content (the reassigned sole copy).
        reassigned = kstore.add_item(
            "Reassigned", shared_text, "document", source_id=sid, content_hash=chash
        )
        # A crash marker names that content hash.
        kstore.db.execute(
            "INSERT OR REPLACE INTO agent_ingest_intent "
            "(source_id, slug, content_hash, started_at) VALUES (?, ?, ?, ?)",
            (sid, "crashed-slug", chash, "2024-01-01T00:00:00"),
        )
        kstore.db.commit()

        removed = kstore.reclaim_agent_source_residue()

        # Hash match alone did NOT delete the unowned copy, because the content
        # is live-held elsewhere.
        assert removed == 0
        assert reassigned in _live_ids(kstore, sid)
        assert owned_item in _live_ids(kstore, sid)


class TestMarkerRetirementAcrossChunks:
    @pytest.mark.asyncio
    async def test_a_crash_larger_than_one_delete_chunk_is_fully_reaped(self, kstore, pipeline):
        # F2 (GPT 6.1 + Opus): a document's chunks all share one content_hash and
        # one interrupted ingest can exceed _RECLAIM_CHUNK. If the first batch
        # retired that hash's marker, later batches would miss it and
        # the rest of the document would survive as permanent duplicates.
        # Retirement must wait until no unowned same-hash item remains.
        import hashlib

        from kiro_crew.knowledge.store import _RECLAIM_CHUNK

        sid, _ = ensure_agent_source(kstore)
        body = "big body"
        chash = hashlib.sha256(body.encode()).hexdigest()
        n = _RECLAIM_CHUNK + 25  # crosses a batch boundary
        orphans = [
            kstore.add_item(
                "Big", body, "document", source_id=sid, chunk_index=i, content_hash=chash
            )
            for i in range(n)
        ]
        kstore.db.execute(
            "INSERT OR REPLACE INTO agent_ingest_intent "
            "(source_id, slug, content_hash, started_at) VALUES (?, ?, ?, ?)",
            (sid, "big-slug", chash, "2024-01-01T00:00:00"),
        )
        kstore.db.commit()

        removed = kstore.reclaim_agent_source_residue()

        # Every chunk reaped -- none stranded across the batch boundary.
        assert removed == n
        for oid in orphans:
            assert oid not in _live_ids(kstore, sid)
        # Marker retired only after the last orphan was gone.
        left = kstore.db.execute(
            "SELECT COUNT(*) c FROM agent_ingest_intent WHERE source_id = ? AND content_hash = ?",
            (sid, chash),
        ).fetchone()["c"]
        assert left == 0

    @pytest.mark.asyncio
    async def test_a_same_hash_retry_preserves_the_shielded_duplicate(self, kstore, pipeline):
        # A crashed attempt and a later successful retry of the SAME content at
        # the SAME slug share one marker row. The retry's finalize must NOT erase
        # the marker while the earlier attempt's orphan is still unowned.
        #
        # Fail-safe shield (GPT 6.1 F1 + Opus 5.5): because the retry's healthy
        # row owns a live copy of this content, the hash is live-owned, so the sweep leaves EVERY unowned copy of it alone. The earlier
        # attempt's orphan is therefore preserved as a (self-healing) duplicate
        # rather than reaped -- the deliberate cost of never deleting what might
        # be a slug's sole surviving copy. The marker-pair exclusion that once
        # let this specific orphan be reaped is gone, because it also stripped
        # protection from the sole-copy case.
        from kiro_crew.knowledge.agent_source import clear_ingesting, mark_ingesting

        sid, _ = ensure_agent_source(kstore)
        await add_agent_document(pipeline, title="Doc", content="retry body", source_uri=URI)
        slug = document_slug(URI)
        chash, owned_ids = get_state(kstore, sid, slug)
        assert owned_ids

        # An earlier crashed attempt of the SAME content: the real crash
        # sequence writes the intent marker FIRST, then commits items, then dies
        # before the ownership-row write. So mark BEFORE adding the orphan, which
        # makes the orphan's ``created_at`` fall at or after the marker's
        # ``started_at`` -- the attempt-time proof the sweep requires.
        mark_ingesting(kstore, sid, slug, chash, "Doc", source_uri=URI)
        orphan = kstore.add_item("Doc", "retry body", "document", source_id=sid, content_hash=chash)

        # The successful retry's finalize clears the marker -- but must preserve
        # it while the earlier orphan is unreaped.
        clear_ingesting(kstore, sid, slug, chash, preserve_if_orphans=True)
        kstore.db.commit()

        left = kstore.db.execute(
            "SELECT COUNT(*) c FROM agent_ingest_intent WHERE source_id = ? AND content_hash = ?",
            (sid, chash),
        ).fetchone()["c"]
        assert left == 1, "marker preserved while an earlier attempt's orphan is unowned"

        # The hash is live-owned by the retry's healthy row, so the sweep shields
        # it: nothing is deleted and the orphan survives as a duplicate.
        removed = kstore.reclaim_agent_source_residue()
        assert removed == 0
        assert orphan in _live_ids(kstore, sid)
        # The retry's own copy survives too.
        for oid in owned_ids:
            assert oid in _live_ids(kstore, sid)


class TestResidueIsScopedToTheAttempt:
    """A marker hash matched by an item created BEFORE the attempt is not residue.

    GPT 6.1 F1 and Opus both trace deletion of a legitimately-unowned item to
    the sweep tying evidence to a source-wide hash rather than to the attempt.
    The marker carries ``started_at``; an item is this attempt's residue ONLY if
    its ``created_at`` is at or after that. A pre-existing or bundle-imported
    item that merely shares the hash predates the marker and is never deleted.
    """

    @pytest.mark.asyncio
    async def test_a_preexisting_unowned_item_sharing_a_hash_survives(self, kstore, pipeline):
        import hashlib

        sid, _ = ensure_agent_source(kstore)
        text = "bundle imported body"
        chash = hashlib.sha256(text.encode()).hexdigest()

        # A bundle-imported item that legitimately has no ownership row (empty /
        # over-claiming group, key held locally, state-free bundle). It was
        # created in the PAST, before any crash.
        imported = kstore.add_item("Imported", text, "document", source_id=sid, content_hash=chash)
        kstore.db.execute(
            "UPDATE items SET created_at = ? WHERE id = ?",
            ("2024-01-01T00:00:00", imported),
        )
        # A LATER, unrelated interrupted ingest of the same content leaves a
        # crash marker naming the hash -- but it began strictly after the
        # imported item was created.
        kstore.db.execute(
            "INSERT OR REPLACE INTO agent_ingest_intent "
            "(source_id, slug, content_hash, started_at) VALUES (?, ?, ?, ?)",
            (sid, "later-slug", chash, "2025-06-01T00:00:00"),
        )
        kstore.db.commit()

        removed = kstore.reclaim_agent_source_residue()

        # The imported item predates the marker, so it is not this attempt's
        # residue and is never deleted -- even though the hash matches.
        assert removed == 0
        assert imported in _live_ids(kstore, sid)

    @pytest.mark.asyncio
    async def test_a_marker_does_not_pin_itself_open_on_a_preexisting_item(self, kstore, pipeline):
        # The retirement check is attempt-scoped too: a pre-existing same-hash
        # item (created before the marker) must not keep the spent marker alive
        # forever. A genuine orphan created within the attempt IS reaped, and the
        # marker is then retired because no attempt-created orphan remains.
        import hashlib

        sid, _ = ensure_agent_source(kstore)
        text = "mixed body"
        chash = hashlib.sha256(text.encode()).hexdigest()

        preexisting = kstore.add_item("Pre", text, "document", source_id=sid, content_hash=chash)
        kstore.db.execute(
            "UPDATE items SET created_at = ? WHERE id = ?",
            ("2024-01-01T00:00:00", preexisting),
        )
        # Marker begins after the pre-existing item but before the orphan.
        kstore.db.execute(
            "INSERT OR REPLACE INTO agent_ingest_intent "
            "(source_id, slug, content_hash, started_at) VALUES (?, ?, ?, ?)",
            (sid, "slug", chash, "2025-01-01T00:00:00"),
        )
        orphan = kstore.add_item("Orphan", text, "document", source_id=sid, content_hash=chash)
        kstore.db.execute(
            "UPDATE items SET created_at = ? WHERE id = ?",
            ("2025-06-01T00:00:00", orphan),
        )
        kstore.db.commit()

        removed = kstore.reclaim_agent_source_residue()

        # Only the attempt-created orphan is reaped; the pre-existing copy stays.
        assert removed == 1
        assert orphan not in _live_ids(kstore, sid)
        assert preexisting in _live_ids(kstore, sid)
        # The marker is retired: no attempt-created orphan remains, and a
        # pre-existing item must not pin it open.
        left = kstore.db.execute(
            "SELECT COUNT(*) c FROM agent_ingest_intent WHERE source_id = ? AND content_hash = ?",
            (sid, chash),
        ).fetchone()["c"]
        assert left == 0


class TestLockedRecheckStatusGate:
    @pytest.mark.asyncio
    async def test_an_item_cleared_from_active_in_the_window_is_not_swept(self, kstore, pipeline):
        # F2 (GPT 6.1 FLAG): the locked recheck filters status = 'active'. An
        # item flipped out of 'active' by any concurrent writer between the
        # candidate read and the writer lock must not be swept -- the sweep acts
        # only on rows it re-confirms are live residue. Model that end state
        # directly: a marker-named item whose status is not 'active' is left alone.
        import hashlib

        sid, _ = ensure_agent_source(kstore)
        text = "deactivated body"
        chash = hashlib.sha256(text.encode()).hexdigest()

        item = kstore.add_item("Deact", text, "document", source_id=sid, content_hash=chash)
        kstore.db.execute(
            "UPDATE items SET created_at = ?, status = 'archived' WHERE id = ?",
            ("2025-06-01T00:00:00", item),
        )
        kstore.db.execute(
            "INSERT OR REPLACE INTO agent_ingest_intent "
            "(source_id, slug, content_hash, started_at) VALUES (?, ?, ?, ?)",
            (sid, "slug", chash, "2025-01-01T00:00:00"),
        )
        kstore.db.commit()

        removed = kstore.reclaim_agent_source_residue()

        # A non-active item is never residue the sweep deletes.
        assert removed == 0
        row = kstore.db.execute("SELECT status FROM items WHERE id = ?", (item,)).fetchone()
        assert row is not None and row["status"] == "archived"


class TestEmptyGroupDedupRowShieldsItsHash:
    def test_a_deduped_row_with_an_empty_group_shields_the_reassigned_sole_copy(self, kstore):
        # F1 (GPT 6.1, this round): the shield skipped rows whose item group is
        # empty, so a `deduped` row left by a reassignment that could not
        # establish ownership (_adopt_reassigned_item refuses an ambiguous hash)
        # gave its hash NO protection. Its surviving copy is the ONLY one, and a
        # same-content crash marker names its hash -> the sweep deleted it
        # irreversibly. The row's EXISTENCE must shield its hash even with an
        # empty group.
        import hashlib

        sid, _ = ensure_agent_source(kstore)
        text = "sole reassigned body"
        chash = hashlib.sha256(text.encode()).hexdigest()

        # The sole surviving copy, left unowned by the ambiguous-adoption refusal.
        sole = kstore.add_item("Sole", text, "document", source_id=sid, content_hash=chash)
        kstore.db.execute(
            "UPDATE items SET created_at = ? WHERE id = ?",
            ("2025-06-01T00:00:00", sole),
        )
        # A healthy `deduped` row claims the hash but names NO items (empty group).
        kstore.db.execute(
            "INSERT OR REPLACE INTO agent_item_state "
            "(source_id, slug, content_hash, item_ids, updated_at, name, status, source_uri) "
            "VALUES (?, ?, ?, ?, ?, ?, 'deduped', ?)",
            (sid, "deduped-slug", chash, json.dumps([]), "2025-01-01", "Dedup", URI),
        )
        # A crash marker at a DIFFERENT slug names that same hash, with a start
        # before the sole copy's created_at (so the attempt-time gate would
        # otherwise let it be swept).
        kstore.db.execute(
            "INSERT OR REPLACE INTO agent_ingest_intent "
            "(source_id, slug, content_hash, started_at) VALUES (?, ?, ?, ?)",
            (sid, "crashed-slug", chash, "2025-01-01T00:00:00"),
        )
        kstore.db.commit()

        removed = kstore.reclaim_agent_source_residue()

        # The empty-group deduped row shields the hash: the sole copy survives.
        assert removed == 0
        assert sole in _live_ids(kstore, sid)

    def test_a_sole_copy_at_the_same_slug_as_its_marker_is_not_deleted(self, kstore):
        # GPT 6.1 F1 + Opus 5.5: an exclusion once skipped a (slug, hash) pair
        # that ALSO carried a crash marker from the shield, so a same-slug retry's
        # orphan could be reaped. But when that slug's only
        # surviving content is the single unowned copy -- no healthy row names a
        # live item -- that "orphan" IS the sole copy, and the exclusion deleted
        # it irreversibly. The exclusion is gone: a `deduped` row shields its
        # hash even when a crash marker shares its exact slug.
        import hashlib

        sid, _ = ensure_agent_source(kstore)
        text = "same slug sole body"
        chash = hashlib.sha256(text.encode()).hexdigest()
        slug = "contested-slug"

        sole = kstore.add_item("Sole", text, "document", source_id=sid, content_hash=chash)
        kstore.db.execute(
            "UPDATE items SET created_at = ? WHERE id = ?",
            ("2025-06-01T00:00:00", sole),
        )
        # A `deduped` row AND a crash marker at the SAME slug and hash -- the
        # exact pair the removed exclusion un-protected.
        kstore.db.execute(
            "INSERT OR REPLACE INTO agent_item_state "
            "(source_id, slug, content_hash, item_ids, updated_at, name, status, source_uri) "
            "VALUES (?, ?, ?, ?, ?, ?, 'deduped', ?)",
            (sid, slug, chash, json.dumps([]), "2025-01-01", "Dedup", URI),
        )
        kstore.db.execute(
            "INSERT OR REPLACE INTO agent_ingest_intent "
            "(source_id, slug, content_hash, started_at) VALUES (?, ?, ?, ?)",
            (sid, slug, chash, "2025-01-01T00:00:00"),
        )
        kstore.db.commit()

        removed = kstore.reclaim_agent_source_residue()

        assert removed == 0
        assert sole in _live_ids(kstore, sid)


class TestEarliestStartIsKeptAcrossRetries:
    def test_a_same_content_retry_keeps_the_earlier_attempts_start(self, kstore):
        # F2 (GPT 6.1, this round): INSERT OR REPLACE overwrote started_at, so a
        # same-content retry pushed the older orphan below the finalize's
        # `created_at >= started_at` gate, dropping its marker and stranding it.
        # _write_ingest_intent now keeps the EARLIEST start on conflict.
        from kiro_crew.knowledge.agent_source import mark_ingesting

        sid, _ = ensure_agent_source(kstore)
        chash = "deadbeef"
        slug = "retry-slug"

        mark_ingesting(kstore, sid, slug, chash, "Doc", source_uri=URI)
        early = kstore.db.execute(
            "SELECT started_at FROM agent_ingest_intent "
            "WHERE source_id = ? AND slug = ? AND content_hash = ?",
            (sid, slug, chash),
        ).fetchone()["started_at"]

        # A later same-content retry writes the marker again. The stored start
        # must remain the EARLIER one, not the retry's.
        mark_ingesting(kstore, sid, slug, chash, "Doc", source_uri=URI)
        after = kstore.db.execute(
            "SELECT started_at FROM agent_ingest_intent "
            "WHERE source_id = ? AND slug = ? AND content_hash = ?",
            (sid, slug, chash),
        ).fetchone()["started_at"]

        assert after == early
        # Exactly one marker row (the conflict updated in place, not duplicated).
        count = kstore.db.execute(
            "SELECT COUNT(*) c FROM agent_ingest_intent "
            "WHERE source_id = ? AND slug = ? AND content_hash = ?",
            (sid, slug, chash),
        ).fetchone()["c"]
        assert count == 1


class TestIngestionGateHeldAcrossIntent:
    @pytest.mark.asyncio
    async def test_the_add_holds_the_ingestion_gate_before_writing_the_intent(
        self, kstore, pipeline
    ):
        # F3 (GPT 6.1 FLAG, this round): the intent marker was written BEFORE the
        # ingest took the gate, so a maintenance sweep could begin in that gap
        # and retire a live add's fresh marker. The add now holds
        # ingestion_in_flight across the whole span. Prove the gate is held while
        # mark_ingesting runs: patch mark_ingesting to assert the gate depth is
        # non-zero at the moment the marker is written.
        import kiro_crew.knowledge.agent_source as agent_source
        from kiro_crew.knowledge.ingestion import _INGESTION_GATE_DEPTH

        real_mark = agent_source.mark_ingesting
        seen = {}

        def _spy(store, source_id, slug, content_hash, title, *, source_uri):
            seen["depth"] = _INGESTION_GATE_DEPTH.get()
            return real_mark(store, source_id, slug, content_hash, title, source_uri=source_uri)

        agent_source.mark_ingesting = _spy
        try:
            res = await add_agent_document(
                pipeline, title="Doc", content="gated body", source_uri=URI
            )
        finally:
            agent_source.mark_ingesting = real_mark

        assert res["status"] == "added"
        # The gate was held (depth >= 1) at the instant the intent was written.
        assert seen.get("depth", 0) >= 1


class TestOwnedIdDerivationIsShared:
    """The owned-id derivation lives in one place and both callers stand down together.

    ``KnowledgeStore.agent_owned_item_ids`` is the single source of "which items
    a live state row names". The residue sweep and ``clear_ingesting`` both read
    it, and both treat an unreadable group as "cannot tell" -- the sweep returns
    0 and ``clear_ingesting`` keeps the marker -- because a wrongly-deleted item
    is unrecoverable where a stale one is not.
    """

    def test_the_shared_helper_returns_none_on_an_unreadable_group(self, kstore):
        sid, _ = ensure_agent_source(kstore)
        kstore.db.execute(
            "INSERT INTO agent_item_state (source_id, slug, content_hash, item_ids, "
            "updated_at, name, status, source_uri) VALUES (?,?,?,?,?,?,?,?)",
            (sid, "doc", "h", "{not json", "2024-01-01T00:00:00", "Doc", "active", "u"),
        )
        kstore.db.commit()
        assert kstore.agent_owned_item_ids(sid) is None

    def test_clear_ingesting_keeps_the_marker_when_a_group_is_unreadable(self, kstore):
        # clear_ingesting delegates its owned-id derivation to the same store
        # method the sweep uses. On an unreadable group that method returns None,
        # so clear_ingesting must keep the marker (stand down), exactly as the
        # sweep sweeps nothing.
        from kiro_crew.knowledge.agent_source import clear_ingesting, mark_ingesting

        sid, _ = ensure_agent_source(kstore)
        slug = document_slug(URI)
        mark_ingesting(kstore, sid, slug, "h", "Doc", source_uri=URI)
        # An unowned same-hash orphan would normally be reaped, but the
        # unreadable row below makes the owned set unprovable.
        kstore.add_item("Doc", "body", "document", source_id=sid, content_hash="h")
        kstore.db.execute(
            "INSERT INTO agent_item_state (source_id, slug, content_hash, item_ids, "
            "updated_at, name, status, source_uri) VALUES (?,?,?,?,?,?,?,?)",
            (sid, "other", "x", "{not json", "2024-01-01T00:00:00", "Other", "active", "u"),
        )
        kstore.db.commit()

        clear_ingesting(kstore, sid, slug, "h", preserve_if_orphans=True)
        kstore.db.commit()

        left = kstore.db.execute(
            "SELECT COUNT(*) c FROM agent_ingest_intent "
            "WHERE source_id = ? AND content_hash = 'h'",
            (sid,),
        ).fetchone()["c"]
        assert left == 1, "marker kept because the owned set is unprovable"
