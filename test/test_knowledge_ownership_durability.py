"""Ownership durability for aggregate knowledge documents.

Two boundaries dropped the per-document ownership rows that
``artifact_item_state`` / ``folder_file_state`` / ``agent_item_state`` hold:

* an ingest interrupted between ``ingest_file``'s commit and the ownership
  write leaves items nothing points at, and the replacement path keys off the
  very record that was lost -- so the next ingest adds a second copy; and
* ``export_all`` / ``import_bundle`` did not carry those tables, so imported
  items arrived unowned and were indistinguishable from that residue.

They are one gap: reaping unowned items is only safe once imported items are
owned.
"""

import asyncio
import hashlib
import json
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.artifacts import ArtifactStore
from kiro_crew.knowledge import artifact_ingest
from kiro_crew.knowledge.artifact_ingest import (
    ensure_artifact_source,
    ingest_artifact,
    reconcile_artifacts,
    sweep_unowned_items,
)
from kiro_crew.knowledge.ingestion import IngestionPipeline
from kiro_crew.knowledge.readers import FileReader
from kiro_crew.knowledge.store import KnowledgeStore

DEFAULT_KINDS = {"markdown", "text", "html", "json"}


def _one_chunk(text, **kw):
    return [{"content": text, "chunk_index": 0, "section_title": None,
             "line_start": 0, "line_end": 0}]


def _make_pipeline(store):
    extractor = MagicMock()
    extractor._pool = None
    extractor.extract_batch = AsyncMock(
        return_value=[{"category": "document", "summary": "s", "entities": []}]
    )
    chunker = MagicMock()
    chunker.chunk.side_effect = _one_chunk
    chunker.chunk_markdown.side_effect = _one_chunk
    chunker.chunk_code.side_effect = _one_chunk
    chunker.chunk_slides.side_effect = _one_chunk
    return IngestionPipeline(
        store=store, extractor=extractor, chunker=chunker,
        reader=FileReader(), embedder=None,
    )


@pytest.fixture()
def kstore(tmp_path):
    s = KnowledgeStore(str(tmp_path / "knowledge.db"))
    yield s
    s.close()


@pytest.fixture()
def pipeline(kstore):
    return _make_pipeline(kstore)


@pytest.fixture()
def art_store(tmp_path):
    return ArtifactStore(root=tmp_path / "artifacts")


def _contents(store, source_id):
    return [r["content"] for r in store.db.execute(
        "SELECT content FROM items WHERE source_id = ?", (source_id,)).fetchall()]


def _owned_ids(store, source_id):
    owned = set()
    for r in store.db.execute(
            "SELECT item_ids FROM artifact_item_state WHERE source_id = ?",
            (source_id,)).fetchall():
        owned.update(json.loads(r["item_ids"] or "[]"))
    return owned


def _interrupted():
    """Simulate the documented crash window faithfully: the items commit, and
    the process dies before ``_set_state`` records the group.

    Suppressing the ownership write is the whole of the window, so this leaves
    exactly the on-disk state a real interruption leaves -- including the intent
    the ingest declared beforehand, which a crash cannot roll back.
    """
    return mock.patch.object(artifact_ingest, "_set_state", lambda *a, **kw: None)


def _outstanding_intent(store, source_id, slug, updated_at="2000-01-01T00:00:00"):
    """Leave an ``ingesting`` marker, so the sweep has something attributable
    and preservation assertions are not passing merely because it stood down."""
    store.db.execute(
        "INSERT OR REPLACE INTO artifact_item_state "
        "(source_id, slug, content_hash, item_ids, updated_at, name, status) "
        "VALUES (?, ?, NULL, '[]', ?, ?, 'ingesting')",
        (source_id, slug, updated_at, slug))
    store.db.commit()


def _orphan_without_provenance(store, source_id, slug):
    """Items committed with NO record of the slug at all.

    Not reachable from a crash any more -- the intent is written before the
    items -- but it is exactly the shape knowledge restored from a pre-ownership
    bundle arrives in, so nothing may delete it.
    """
    store.db.execute(
        "DELETE FROM artifact_item_state WHERE source_id = ? AND slug = ?",
        (source_id, slug))
    store.db.commit()


class TestInterruptedIngest:
    @pytest.mark.asyncio
    async def test_a_lost_ownership_row_does_not_duplicate_the_artifact(
        self, kstore, pipeline, art_store
    ):
        sid, _ = ensure_artifact_source(kstore)
        art_store.create(name="Doc", content="orphan body", kind="markdown")
        with _interrupted():
            await reconcile_artifacts(pipeline, art_store, sid, DEFAULT_KINDS)

        await reconcile_artifacts(pipeline, art_store, sid, DEFAULT_KINDS)

        assert len([c for c in _contents(kstore, sid) if "orphan body" in c]) == 1

    @pytest.mark.asyncio
    async def test_ownership_is_durable_again_after_the_repair(
        self, kstore, pipeline, art_store
    ):
        # The repair is only worth anything if the surviving copy is OWNED --
        # an unowned survivor would be reaped by the next pass instead of
        # replaced by it.
        sid, _ = ensure_artifact_source(kstore)
        art_store.create(name="Doc", content="orphan body", kind="markdown")
        with _interrupted():
            await reconcile_artifacts(pipeline, art_store, sid, DEFAULT_KINDS)
        await reconcile_artifacts(pipeline, art_store, sid, DEFAULT_KINDS)

        live = {r["id"] for r in kstore.db.execute(
            "SELECT id FROM items WHERE source_id = ?", (sid,)).fetchall()}
        assert live and live <= _owned_ids(kstore, sid)

    @pytest.mark.asyncio
    async def test_a_later_edit_still_replaces_the_repaired_group(
        self, kstore, pipeline, art_store
    ):
        # Replacement is driven by the ownership record, so the real proof that
        # ownership was restored is that the NEXT edit replaces rather than adds.
        sid, _ = ensure_artifact_source(kstore)
        art = art_store.create(name="Doc", content="orphan body", kind="markdown")
        with _interrupted():
            await reconcile_artifacts(pipeline, art_store, sid, DEFAULT_KINDS)
        await reconcile_artifacts(pipeline, art_store, sid, DEFAULT_KINDS)

        art_store.update(art.slug, content="revised body")
        await reconcile_artifacts(pipeline, art_store, sid, DEFAULT_KINDS)

        bodies = _contents(kstore, sid)
        assert len([c for c in bodies if "revised body" in c]) == 1
        assert not [c for c in bodies if "orphan body" in c]

    @pytest.mark.asyncio
    async def test_the_next_live_upsert_does_not_duplicate_without_a_reconcile(
        self, kstore, pipeline, art_store
    ):
        # The defect is reachable from the live listener too: the crash is
        # followed by an ordinary artifact save, with no gateway restart in
        # between, so nothing has run the reconcile pass. `_handle` takes an
        # upsert straight to `ingest_artifact`.
        sid, _ = ensure_artifact_source(kstore)
        art = art_store.create(name="Doc", content="orphan body", kind="markdown")
        with _interrupted():
            await ingest_artifact(pipeline, art_store, art.slug, sid, DEFAULT_KINDS)

        art_store.update(art.slug, content="revised body")
        await ingest_artifact(pipeline, art_store, art.slug, sid, DEFAULT_KINDS)

        bodies = _contents(kstore, sid)
        assert len([c for c in bodies if "revised body" in c]) == 1
        assert not [c for c in bodies if "orphan body" in c]

    @pytest.mark.asyncio
    async def test_a_live_upsert_repair_leaves_the_group_owned(
        self, kstore, pipeline, art_store
    ):
        sid, _ = ensure_artifact_source(kstore)
        art = art_store.create(name="Doc", content="orphan body", kind="markdown")
        with _interrupted():
            await ingest_artifact(pipeline, art_store, art.slug, sid, DEFAULT_KINDS)

        art_store.update(art.slug, content="revised body")
        await ingest_artifact(pipeline, art_store, art.slug, sid, DEFAULT_KINDS)

        live = {r["id"] for r in kstore.db.execute(
            "SELECT id FROM items WHERE source_id = ?", (sid,)).fetchall()}
        assert live and live <= _owned_ids(kstore, sid)

    @pytest.mark.asyncio
    async def test_a_first_ingest_of_a_second_artifact_reaps_nothing(
        self, kstore, pipeline, art_store
    ):
        # The live repair fires on "this slug has no ownership row", which is
        # also true of a brand-new artifact. It must not take the other
        # artifact's items with it on the way in.
        sid, _ = ensure_artifact_source(kstore)
        first = art_store.create(name="First", content="first body", kind="markdown")
        await ingest_artifact(pipeline, art_store, first.slug, sid, DEFAULT_KINDS)

        second = art_store.create(name="Second", content="second body", kind="markdown")
        await ingest_artifact(pipeline, art_store, second.slug, sid, DEFAULT_KINDS)

        bodies = _contents(kstore, sid)
        assert len([c for c in bodies if "first body" in c]) == 1
        assert len([c for c in bodies if "second body" in c]) == 1

    @pytest.mark.asyncio
    async def test_reconcile_is_idempotent_over_a_converged_store(
        self, kstore, pipeline, art_store
    ):
        sid, _ = ensure_artifact_source(kstore)
        art_store.create(name="Doc", content="stable body", kind="markdown")
        await reconcile_artifacts(pipeline, art_store, sid, DEFAULT_KINDS)
        await reconcile_artifacts(pipeline, art_store, sid, DEFAULT_KINDS)
        await reconcile_artifacts(pipeline, art_store, sid, DEFAULT_KINDS)

        assert len([c for c in _contents(kstore, sid) if "stable body" in c]) == 1


def _intent_slugs(store, source_id):
    return {r["slug"] for r in store.db.execute(
        "SELECT slug FROM artifact_item_state WHERE source_id = ? AND status = ?",
        (source_id, "ingesting")).fetchall()}


def _source_item_ids(store, source_id):
    return {r["id"] for r in store.db.execute(
        "SELECT id FROM items WHERE source_id = ?", (source_id,)).fetchall()}


class TestIngestIntentLifecycle:
    """An outstanding intent is a licence to delete, so it must be retired the
    moment it stops describing reality -- and kept whenever it still does.

    The two directions fail differently: a leaked intent authorises reaping
    knowledge no crash produced, while a prematurely retired one loses the only
    record that can attribute real residue.
    """

    @pytest.mark.asyncio
    async def test_a_failure_before_any_item_commits_retires_the_intent(
        self, kstore, pipeline, art_store, monkeypatch
    ):
        sid, _ = ensure_artifact_source(kstore)
        art = art_store.create(name="Doc", content="body", kind="markdown")
        monkeypatch.setattr(
            pipeline, "ingest_file",
            AsyncMock(side_effect=RuntimeError("reader exploded")))

        with pytest.raises(RuntimeError):
            await ingest_artifact(pipeline, art_store, art.slug, sid, DEFAULT_KINDS)

        assert _source_item_ids(kstore, sid) == set()
        assert _intent_slugs(kstore, sid) == set()

    @pytest.mark.asyncio
    async def test_a_partial_or_failed_terminal_return_retires_the_intent(
        self, kstore, pipeline, art_store, monkeypatch
    ):
        sid, _ = ensure_artifact_source(kstore)
        art = art_store.create(name="Doc", content="body", kind="markdown")
        monkeypatch.setattr(pipeline, "get_job_status", lambda job_id: {"status": "error"})

        await ingest_artifact(pipeline, art_store, art.slug, sid, DEFAULT_KINDS)

        assert _intent_slugs(kstore, sid) == set()

    @pytest.mark.asyncio
    async def test_an_interruption_after_the_item_commit_keeps_the_intent(
        self, kstore, pipeline, art_store
    ):
        # The real #2670 window. The items are durable and nothing names them,
        # so the intent is the only thing that can attribute them later.
        sid, _ = ensure_artifact_source(kstore)
        art = art_store.create(name="Doc", content="body", kind="markdown")

        with _interrupted():
            await ingest_artifact(pipeline, art_store, art.slug, sid, DEFAULT_KINDS)

        assert _source_item_ids(kstore, sid)
        assert _intent_slugs(kstore, sid) == {art.slug}

    @pytest.mark.asyncio
    async def test_cancellation_before_the_item_commit_retires_the_intent(
        self, kstore, pipeline, art_store, monkeypatch
    ):
        # `ingest_file` is a cancellation-aware pipeline, so a cancelled turn is
        # an ordinary outcome here, not an exotic one. Cancelled before anything
        # committed means there is no residue and the intent must not survive.
        sid, _ = ensure_artifact_source(kstore)
        art = art_store.create(name="Doc", content="body", kind="markdown")
        monkeypatch.setattr(
            pipeline, "ingest_file", AsyncMock(side_effect=asyncio.CancelledError()))

        with pytest.raises(asyncio.CancelledError):
            await ingest_artifact(pipeline, art_store, art.slug, sid, DEFAULT_KINDS)

        assert _source_item_ids(kstore, sid) == set()
        assert _intent_slugs(kstore, sid) == set()

    @pytest.mark.asyncio
    async def test_cancellation_after_the_item_commit_keeps_the_intent(
        self, kstore, pipeline, art_store, monkeypatch
    ):
        # Mirror image: the pipeline's own finalizer runs to completion under
        # cancellation, so items CAN be durable by the time the cancel lands.
        sid, _ = ensure_artifact_source(kstore)
        art = art_store.create(name="Doc", content="body", kind="markdown")
        real_ingest = pipeline.ingest_file

        async def commit_then_cancel(*args, **kwargs):
            await real_ingest(*args, **kwargs)
            raise asyncio.CancelledError()

        monkeypatch.setattr(pipeline, "ingest_file", commit_then_cancel)

        with pytest.raises(asyncio.CancelledError):
            await ingest_artifact(pipeline, art_store, art.slug, sid, DEFAULT_KINDS)

        assert _source_item_ids(kstore, sid)
        assert _intent_slugs(kstore, sid) == {art.slug}

    @pytest.mark.asyncio
    async def test_a_leaked_intent_would_authorize_deleting_later_knowledge(
        self, kstore, pipeline, art_store, monkeypatch, tmp_path
    ):
        # The consequence, stated as a test: an intent that outlives its failure
        # is a standing licence for the sweep, and the next unowned knowledge to
        # arrive -- a pre-ownership restore -- is inside its time window.
        sid, _ = ensure_artifact_source(kstore)
        art = art_store.create(name="Doc", content="body", kind="markdown")
        monkeypatch.setattr(
            pipeline, "ingest_file",
            AsyncMock(side_effect=RuntimeError("reader exploded")))
        with pytest.raises(RuntimeError):
            await ingest_artifact(pipeline, art_store, art.slug, sid, DEFAULT_KINDS)

        restored = kstore.add_item(title="Legacy", content="legacy body",
                                   item_type="document", source_id=sid)

        assert sweep_unowned_items(kstore, sid) == 0
        assert restored in _source_item_ids(kstore, sid)


class _BarrierConnection:
    """Proxy a SQLite connection, firing *hook* once just before the first
    statement whose SQL contains *marker*.

    ``export_all`` issues its SELECTs one after another. Placing the barrier
    immediately before the ownership read reproduces, deterministically, a
    concurrent writer that commits in the gap between the item read and the
    state read.
    """

    def __init__(self, real, marker, hook):
        self._real = real
        self._marker = marker
        self._hook = hook

    def execute(self, sql, *args, **kwargs):
        if self._hook is not None and self._marker in sql:
            hook, self._hook = self._hook, None
            hook()
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _export_with_barrier(store, monkeypatch, marker, hook, **kwargs):
    """Run ``store.export_all`` with a writer committing mid-scan."""
    real_prop = KnowledgeStore.db
    barrier = _BarrierConnection(real_prop.fget(store), marker, hook)
    monkeypatch.setattr(
        KnowledgeStore, "db",
        property(lambda self: barrier if self is store else real_prop.fget(self)))
    try:
        return store.export_all(**kwargs)
    finally:
        monkeypatch.undo()


def _put_item(store, source_id, item_id, content, created_at="2024-01-01T00:00:00"):
    store.db.execute(
        "INSERT INTO items (id, title, content, item_type, source_id, chunk_index, "
        "namespace, tags, status, created_at, updated_at) "
        "VALUES (?, ?, ?, 'document', ?, 0, 'default', '[]', 'active', ?, ?)",
        (item_id, "Doc", content, source_id, created_at, created_at))
    store.db.commit()


def _put_ownership(store, source_id, slug, content_hash, item_ids):
    store.db.execute(
        "INSERT OR REPLACE INTO artifact_item_state "
        "(source_id, slug, content_hash, item_ids, updated_at, name, status) "
        "VALUES (?, ?, ?, ?, ?, ?, 'active')",
        (source_id, slug, content_hash, json.dumps(item_ids), "now", slug))
    store.db.commit()


class TestExportSnapshotCoherence:
    """A bundle is read with several statements and no shared read transaction,
    so a writer that commits between them can tear it across tables.

    Ownership naming items the same bundle does not carry is the phantom the
    namespace fix already ruled out for scoped exports -- reachable again here
    through concurrency rather than filtering.
    """

    @pytest.mark.asyncio
    async def test_a_full_export_never_owns_items_it_did_not_export(
        self, kstore, tmp_path, monkeypatch
    ):
        sid, _ = ensure_artifact_source(kstore)
        _put_item(kstore, sid, "v1-item", "v1 body")
        _put_ownership(kstore, sid, "doc", "hash-v1", ["v1-item"])

        writer = KnowledgeStore(str(tmp_path / "knowledge.db"))

        def a_concurrent_ingest_completes():
            # Exactly what ingest_file + _set_state do: replace the group and
            # record the new ownership, committed as the export runs.
            writer.db.execute("DELETE FROM items WHERE id = ?", ("v1-item",))
            writer.db.commit()
            _put_item(writer, sid, "v2-item", "v2 body")
            _put_ownership(writer, sid, "doc", "hash-v2", ["v2-item"])

        try:
            bundle = _export_with_barrier(
                kstore, monkeypatch, "FROM artifact_item_state",
                a_concurrent_ingest_completes)
        finally:
            writer.close()

        exported = {i["id"] for i in bundle["items"]}
        owned = set()
        for row in bundle["artifact_item_state"]:
            owned.update(json.loads(row["item_ids"] or "[]"))
        assert owned <= exported, (
            "the bundle claims ownership of items it does not contain")

    @pytest.mark.asyncio
    async def test_a_torn_restore_still_indexes_the_document(
        self, kstore, pipeline, art_store, tmp_path, monkeypatch
    ):
        # The consequence. `ingest_artifact` short-circuits on "recorded hash
        # matches AND the row still holds a group" without checking the group
        # still exists, so ownership restored ahead of its items convinces the
        # target it is already indexed.
        sid, _ = ensure_artifact_source(kstore)
        art = art_store.create(name="Doc", content="v2 body", kind="markdown")
        _put_item(kstore, sid, "v1-item", "v1 body")
        _put_ownership(kstore, sid, art.slug, "hash-v1", ["v1-item"])
        v2_hash = hashlib.sha256("v2 body".encode()).hexdigest()

        writer = KnowledgeStore(str(tmp_path / "knowledge.db"))

        def a_concurrent_ingest_completes():
            writer.db.execute("DELETE FROM items WHERE id = ?", ("v1-item",))
            writer.db.commit()
            _put_item(writer, sid, "v2-item", "v2 body")
            _put_ownership(writer, sid, art.slug, v2_hash, ["v2-item"])

        try:
            bundle = _export_with_barrier(
                kstore, monkeypatch, "FROM artifact_item_state",
                a_concurrent_ingest_completes)
        finally:
            writer.close()

        fresh = KnowledgeStore(str(tmp_path / "restored.db"))
        try:
            fresh.import_bundle(bundle)
            await ingest_artifact(
                _make_pipeline(fresh), art_store, art.slug, sid, DEFAULT_KINDS)

            assert [c for c in _contents(fresh, sid) if "v2 body" in c], (
                "the restored store believes it already holds the document")
        finally:
            fresh.close()

    @pytest.mark.asyncio
    async def test_a_scoped_export_does_not_strand_its_items_unowned(
        self, kstore, tmp_path, monkeypatch
    ):
        # Mirror risk for the scoped path: narrowing keys off the exported item
        # ids, so a torn read could drop ownership for items the bundle DOES
        # carry, landing them unowned in the restore.
        sid, _ = ensure_artifact_source(kstore)
        _put_item(kstore, sid, "v1-item", "v1 body")
        _put_ownership(kstore, sid, "doc", "hash-v1", ["v1-item"])

        writer = KnowledgeStore(str(tmp_path / "knowledge.db"))

        def a_concurrent_ingest_completes():
            _put_item(writer, sid, "v2-item", "v2 body")
            _put_ownership(writer, sid, "doc", "hash-v2", ["v2-item"])

        try:
            bundle = _export_with_barrier(
                kstore, monkeypatch, "FROM artifact_item_state",
                a_concurrent_ingest_completes, namespace="default")
        finally:
            writer.close()

        exported = {i["id"] for i in bundle["items"]}
        owned = set()
        for row in bundle["artifact_item_state"]:
            owned.update(json.loads(row["item_ids"] or "[]"))
        assert exported <= owned, (
            "exported items lost their ownership row to a concurrent write")


class TestIntentDoesNotCrossTheBundleBoundary:
    """An ``ingesting`` row is not ownership metadata -- it is authority to
    delete, granted by an interruption that happened on ONE host.

    A backup taken while an ingest is in flight would otherwise carry that
    authority to a machine that never ran the ingest, where nothing it points at
    can exist.
    """

    def test_a_full_export_does_not_carry_a_pending_intent(self, kstore):
        sid, _ = ensure_artifact_source(kstore)
        _outstanding_intent(kstore, sid, "in-flight")

        bundle = kstore.export_all()

        assert [r["slug"] for r in bundle["artifact_item_state"]] == []

    def test_an_imported_intent_cannot_authorize_deleting_local_knowledge(
        self, kstore, tmp_path
    ):
        # The whole chain: export races an in-flight ingest on host A, the
        # bundle is restored on host B, and B's own knowledge is inside the
        # foreign intent's window.
        sid, _ = ensure_artifact_source(kstore)
        _outstanding_intent(kstore, sid, "in-flight", updated_at="2020-01-01T00:00:00")
        bundle = kstore.export_all()

        fresh = KnowledgeStore(str(tmp_path / "restored.db"))
        try:
            fresh.import_bundle(bundle)
            local = fresh.add_item(title="Local", content="local body",
                                   item_type="document", source_id=sid)

            reaped = sweep_unowned_items(fresh, sid)

            assert reaped == 0
            assert local in _source_item_ids(fresh, sid)
        finally:
            fresh.close()

    def test_a_foreign_item_newer_than_the_intent_is_not_deletable(
        self, kstore, tmp_path
    ):
        # `import_bundle` preserves each item's ORIGINAL created_at, and both it
        # and the intent timestamp are naive local wall clock. A bundle from a
        # host running ahead -- or simply a different timezone -- restores items
        # dated after an intent they have nothing to do with, so wall-clock
        # ordering alone cannot establish provenance.
        sid, _ = ensure_artifact_source(kstore)
        _outstanding_intent(kstore, sid, "in-flight", updated_at="2020-01-01T00:00:00")
        bundle = kstore.export_all()
        bundle["items"].append({
            "id": "foreign-1", "title": "Foreign", "content": "foreign body",
            "item_type": "document", "source_id": sid, "chunk_index": 0,
            "namespace": "default", "summary": None, "tags": "[]",
            "embedding": None, "status": "active",
            "created_at": "2030-01-01T00:00:00", "updated_at": "2030-01-01T00:00:00",
        })

        fresh = KnowledgeStore(str(tmp_path / "restored.db"))
        try:
            fresh.import_bundle(bundle)

            reaped = sweep_unowned_items(fresh, sid)

            assert reaped == 0
            assert "foreign-1" in _source_item_ids(fresh, sid)
        finally:
            fresh.close()


class TestSweepPreservation:
    def test_items_under_other_sources_are_never_reaped(self, kstore):
        # The sweep's whole safety argument is that it is scoped to the one
        # source where ownership is total. Manually added knowledge has no
        # per-document state row anywhere and must survive -- and the intent
        # below makes the sweep actually run, so this is not vacuous.
        sid, _ = ensure_artifact_source(kstore)
        manual = kstore.add_source(name="Notes", source_type="manual",
                                   uri="manual://notes")
        kstore.add_item(title="Note", content="hand written", item_type="document",
                        source_id=manual)
        _outstanding_intent(kstore, sid, "doc")

        sweep_unowned_items(kstore, sid)

        assert _contents(kstore, manual) == ["hand written"]

    @pytest.mark.asyncio
    async def test_an_orphan_with_no_provenance_is_never_reaped(
        self, kstore, pipeline, art_store
    ):
        # Unowned is not evidence of a crash. With no intent outstanding the
        # items could equally be a pre-ownership restore, and deleting them
        # would destroy content nothing ever interrupted.
        sid, _ = ensure_artifact_source(kstore)
        art = art_store.create(name="Doc", content="unattributable", kind="markdown")
        await ingest_artifact(pipeline, art_store, art.slug, sid, DEFAULT_KINDS)
        _orphan_without_provenance(kstore, sid, art.slug)

        assert sweep_unowned_items(kstore, sid) == 0
        assert [c for c in _contents(kstore, sid) if "unattributable" in c]

    def test_an_unreadable_group_stands_the_sweep_down(self, kstore):
        # A row whose group cannot be parsed says it owns SOMETHING but not
        # what. Reaping on that would delete the items it exists to protect.
        sid, _ = ensure_artifact_source(kstore)
        iid = kstore.add_item(title="Doc", content="protected body",
                              item_type="document", source_id=sid)
        kstore.db.execute(
            "INSERT INTO artifact_item_state "
            "(source_id, slug, content_hash, item_ids, updated_at, name, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sid, "doc", "h", "{not json", "now", "Doc", "active"))
        _outstanding_intent(kstore, sid, "other")
        kstore.db.commit()

        assert sweep_unowned_items(kstore, sid) == 0
        assert _contents(kstore, sid) == ["protected body"]
        assert iid


class TestItemGroupDecoding:
    # Valid JSON is not a valid group. A decoded str or dict is iterable, so
    # `set.update` would enrol its characters or keys as owned item ids -- the
    # real ids would then read as unowned and be deleted.
    @pytest.mark.parametrize("raw", [
        '"abc123"',        # JSON string -> would contribute {'a','b','c','1',..}
        '{"a": 1}',        # JSON object -> would contribute its keys
        '["ok", 7]',       # list with a non-string member
        '12',              # JSON number
        'true',            # JSON bool
        '{not json',       # malformed
    ])
    def test_a_group_that_is_not_a_list_of_strings_is_refused(self, raw):
        assert artifact_ingest._decode_item_group(raw) is None

    @pytest.mark.parametrize("raw,expected", [
        ('["i1", "i2"]', ["i1", "i2"]),
        ('[]', []),
        ('', []),
        (None, []),
    ])
    def test_a_well_formed_group_decodes(self, raw, expected):
        assert artifact_ingest._decode_item_group(raw) == expected

    def test_a_scalar_group_cannot_cause_the_sweep_to_delete_real_items(
        self, kstore
    ):
        # The behavioural half: the characters of "abc123" must never be
        # mistaken for ownership of this source's actual items.
        sid, _ = ensure_artifact_source(kstore)
        kstore.add_item(title="Doc", content="real body", item_type="document",
                        source_id=sid)
        kstore.db.execute(
            "INSERT INTO artifact_item_state "
            "(source_id, slug, content_hash, item_ids, updated_at, name, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sid, "doc", "h", '"abc123"', "2000-01-01T00:00:00", "Doc",
             "ingesting"))
        kstore.db.commit()

        assert sweep_unowned_items(kstore, sid) == 0
        assert _contents(kstore, sid) == ["real body"]


class TestBundleOwnership:
    def test_export_carries_the_per_document_state_tables(self, kstore):
        sid = kstore.add_source(name="Artifacts", source_type="artifact",
                                uri="kirocrew://artifacts")
        kstore.db.execute(
            "INSERT INTO artifact_item_state "
            "(source_id, slug, content_hash, item_ids, updated_at, name, status, kind) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (sid, "doc", "h1", json.dumps(["i1"]), "now", "Doc", "active", "markdown"))
        kstore.db.commit()

        bundle = kstore.export_all()

        assert [r["slug"] for r in bundle["artifact_item_state"]] == ["doc"]
        assert "folder_file_state" in bundle
        assert "agent_item_state" in bundle

    @pytest.mark.asyncio
    async def test_imported_knowledge_arrives_owned_and_survives_the_sweep(
        self, kstore, pipeline, art_store, tmp_path
    ):
        # The coupling the two halves share: without ownership in the bundle,
        # the sweep above cannot tell imported knowledge from crash residue.
        sid, _ = ensure_artifact_source(kstore)
        art_store.create(name="Doc", content="exported body", kind="markdown")
        await reconcile_artifacts(pipeline, art_store, sid, DEFAULT_KINDS)
        bundle = kstore.export_all()

        fresh = KnowledgeStore(str(tmp_path / "restored.db"))
        try:
            fresh.import_bundle(bundle)
            assert _owned_ids(fresh, sid)

            reaped = sweep_unowned_items(fresh, sid)

            assert reaped == 0
            assert len([c for c in _contents(fresh, sid) if "exported body" in c]) == 1
        finally:
            fresh.close()

    @pytest.mark.asyncio
    async def test_a_later_ingest_replaces_imported_knowledge(
        self, kstore, pipeline, art_store, tmp_path
    ):
        # Ownership is what makes a document replaceable. An imported item that
        # arrived unowned would be added alongside on the next ingest.
        sid, _ = ensure_artifact_source(kstore)
        art = art_store.create(name="Doc", content="exported body", kind="markdown")
        await reconcile_artifacts(pipeline, art_store, sid, DEFAULT_KINDS)
        bundle = kstore.export_all()

        fresh = KnowledgeStore(str(tmp_path / "restored.db"))
        try:
            fresh.import_bundle(bundle)
            art_store.update(art.slug, content="revised body")

            await reconcile_artifacts(
                _make_pipeline(fresh), art_store, sid, DEFAULT_KINDS)

            bodies = _contents(fresh, sid)
            assert len([c for c in bodies if "revised body" in c]) == 1
            assert not [c for c in bodies if "exported body" in c]
        finally:
            fresh.close()

    def test_importing_the_same_bundle_twice_does_not_multiply_ownership(
        self, kstore, tmp_path
    ):
        sid = kstore.add_source(name="Artifacts", source_type="artifact",
                                uri="kirocrew://artifacts")
        kstore.db.execute(
            "INSERT INTO artifact_item_state "
            "(source_id, slug, content_hash, item_ids, updated_at, name, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sid, "doc", "h1", json.dumps(["i1"]), "now", "Doc", "active"))
        kstore.db.commit()
        bundle = kstore.export_all()

        fresh = KnowledgeStore(str(tmp_path / "restored.db"))
        try:
            fresh.import_bundle(bundle)
            fresh.import_bundle(bundle)

            rows = fresh.db.execute(
                "SELECT slug FROM artifact_item_state WHERE source_id = ?",
                (sid,)).fetchall()
            assert len(rows) == 1
        finally:
            fresh.close()

    def test_a_namespace_scoped_export_leaves_foreign_ownership_behind(
        self, kstore, tmp_path
    ):
        # export_all(namespace=...) scopes items, relations, source_locations and
        # mentions. Ownership rows name their items in a JSON column that no
        # foreign key reaches, so exporting them whole would ship rows pointing
        # at items the bundle does not contain.
        sid = kstore.add_source(name="Artifacts", source_type="artifact",
                                uri="kirocrew://artifacts")
        keep = kstore.add_item(title="Keep", content="kept body",
                               item_type="document", source_id=sid, namespace="keep")
        drop = kstore.add_item(title="Drop", content="dropped body",
                               item_type="document", source_id=sid, namespace="drop")
        for slug, iid in (("keep", keep), ("drop", drop)):
            kstore.db.execute(
                "INSERT INTO artifact_item_state "
                "(source_id, slug, content_hash, item_ids, updated_at, name, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (sid, slug, f"h-{slug}", json.dumps([iid]), "now", slug, "active"))
        kstore.db.commit()

        bundle = kstore.export_all(namespace="keep")

        fresh = KnowledgeStore(str(tmp_path / "restored.db"))
        try:
            fresh.import_bundle(bundle)
            owned = _owned_ids(fresh, sid)
            assert keep in owned
            assert drop not in owned
        finally:
            fresh.close()

    @pytest.mark.asyncio
    async def test_a_scoped_restore_does_not_silently_skip_the_document(
        self, kstore, pipeline, art_store, tmp_path
    ):
        # The harm a phantom ownership row does: `ingest_artifact` short-circuits
        # on "hash unchanged AND still holding its items". A row imported with
        # item ids the bundle never carried satisfies both, so the document is
        # skipped and never becomes searchable in the restored store.
        sid, _ = ensure_artifact_source(kstore)
        art = art_store.create(name="Doc", content="doc body", kind="markdown")
        await ingest_artifact(pipeline, art_store, art.slug, sid, DEFAULT_KINDS)

        bundle = kstore.export_all(namespace="nothing-here")

        fresh = KnowledgeStore(str(tmp_path / "restored.db"))
        try:
            fresh.import_bundle(bundle)
            await ingest_artifact(
                _make_pipeline(fresh), art_store, art.slug, sid, DEFAULT_KINDS)

            assert [c for c in _contents(fresh, sid) if "doc body" in c]
        finally:
            fresh.close()

    def test_a_partly_exported_group_keeps_its_exported_items_owned(
        self, kstore, tmp_path
    ):
        # Narrowed, not dropped: dropping the row would land the exported half
        # unowned, which is exactly the state the residue sweep reaps.
        sid = kstore.add_source(name="Artifacts", source_type="artifact",
                                uri="kirocrew://artifacts")
        inside = kstore.add_item(title="In", content="in body", item_type="document",
                                 source_id=sid, namespace="keep")
        outside = kstore.add_item(title="Out", content="out body",
                                  item_type="document", source_id=sid,
                                  namespace="drop")
        kstore.db.execute(
            "INSERT INTO artifact_item_state "
            "(source_id, slug, content_hash, item_ids, updated_at, name, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sid, "split", "h", json.dumps([inside, outside]), "now", "Split",
             "active"))
        kstore.db.commit()

        bundle = kstore.export_all(namespace="keep")

        fresh = KnowledgeStore(str(tmp_path / "restored.db"))
        try:
            fresh.import_bundle(bundle)
            owned = _owned_ids(fresh, sid)
            assert owned == {inside}
            assert sweep_unowned_items(fresh, sid) == 0
        finally:
            fresh.close()

    def test_a_full_export_still_carries_a_group_that_owns_nothing(self, kstore):
        # A deduped marker holds a hash claim rather than a group. Scoping drops
        # it (no item of it is in a scoped bundle), but a full backup must keep
        # it or the restored store re-ingests and re-collapses the document.
        sid = kstore.add_source(name="Artifacts", source_type="artifact",
                                uri="kirocrew://artifacts")
        kstore.db.execute(
            "INSERT INTO artifact_item_state "
            "(source_id, slug, content_hash, item_ids, updated_at, name, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sid, "loser", "h", "[]", "now", "Loser", "deduped"))
        kstore.db.commit()

        assert [r["slug"] for r in kstore.export_all()["artifact_item_state"]] == [
            "loser"]

    @pytest.mark.asyncio
    async def test_legacy_imported_knowledge_survives_a_reconcile(
        self, kstore, pipeline, art_store, tmp_path
    ):
        # Importing without raising proves nothing. A bundle written before
        # ownership travelled lands artifact-source items that NO state row
        # names, which is exactly the shape of crash residue -- so the sweep
        # must not be able to delete them on the next start.
        sid, _ = ensure_artifact_source(kstore)
        art_store.create(name="Doc", content="legacy body", kind="markdown")
        await reconcile_artifacts(pipeline, art_store, sid, DEFAULT_KINDS)
        legacy = kstore.export_all()
        for table, _ in (("folder_file_state", ""), ("artifact_item_state", ""),
                         ("agent_item_state", "")):
            legacy.pop(table, None)

        # Restored onto a machine that does not hold the artifacts themselves,
        # which is what makes the deletion permanent: nothing re-ingests them.
        elsewhere = ArtifactStore(root=tmp_path / "empty-artifacts")
        fresh = KnowledgeStore(str(tmp_path / "restored.db"))
        try:
            fresh.import_bundle(legacy)
            assert [c for c in _contents(fresh, sid) if "legacy body" in c]

            await reconcile_artifacts(
                _make_pipeline(fresh), elsewhere, sid, DEFAULT_KINDS)

            assert [c for c in _contents(fresh, sid) if "legacy body" in c]
        finally:
            fresh.close()

    def test_a_bundle_written_before_ownership_travelled_still_imports(self, kstore):
        # Backward compatibility: an older export carries no state tables at
        # all, and must restore exactly as it did before rather than fail.
        legacy = {
            "items": [], "entities": [], "relations": [],
            "sources": [{"id": "s1", "name": "Old", "source_type": "manual",
                         "uri": "manual://old"}],
            "source_locations": [], "mentions": [],
        }

        kstore.import_bundle(legacy)

        assert kstore.get_source_by_uri("manual://old") is not None
