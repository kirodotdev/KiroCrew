"""Ownership durability for the agent aggregate source.

The items an add commits become durable inside ``ingest_file``. Recording which
document owns them afterwards puts several awaits in between -- the temp-file
cleanup, the job-status read -- and each is a cancellation point. Interrupted
there, the items are owned by nobody, and BOTH duplicate defences for the
aggregate read that ownership row: ``get_state`` reports no previous group, so
the next add replaces nothing, and ``find_document_by_hash`` cannot see the
content either. The document is then stored twice, and an edited re-add leaves
the superseded version searchable for good.
"""

from __future__ import annotations

import asyncio
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
    return [{"content": text, "chunk_index": 0, "section_title": None,
             "line_start": 0, "line_end": 0}]


@pytest.fixture()
def kstore(tmp_path):
    s = KnowledgeStore(str(tmp_path / "knowledge.db"))
    yield s
    s.close()


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
    return IngestionPipeline(store=kstore, extractor=extractor, chunker=chunker,
                             reader=FileReader(), embedder=None, dedup_enabled=False)


IMPORTED_ID = "imported-item-1"


def _imported_item(source_id):
    """A row shaped the way ``import_bundle`` expects, landing in the aggregate."""
    return {
        "id": IMPORTED_ID, "title": "Imported", "content": "imported body",
        "item_type": "document", "source_id": source_id, "chunk_index": 0,
        "namespace": "default", "summary": None, "tags": "[]", "embedding": None,
        "status": "active", "created_at": "2024-01-01T00:00:00",
        "updated_at": "2024-01-01T00:00:00",
    }


def _bodies(store, source_id, needle):
    return [r["content"] for r in store.db.execute(
        "SELECT content FROM items WHERE source_id = ?", (source_id,)).fetchall()
        if needle in r["content"]]


def _live_ids(store, source_id):
    return {r["id"] for r in store.db.execute(
        "SELECT id FROM items WHERE source_id = ?", (source_id,)).fetchall()}


def _interrupt_after_the_ingest(pipeline, exc):
    """Fail at the first await AFTER the items are durable.

    ``get_job_status`` is the first thing the add touches once ``ingest_file``
    has returned, so raising there reproduces an interruption inside the window
    without stubbing out the ownership write itself.
    """
    def boom(job_id):
        raise exc
    pipeline.get_job_status = boom


class TestOwnershipSurvivesAnInterruptedAdd:
    @pytest.mark.asyncio
    async def test_re_adding_the_same_document_does_not_store_it_twice(
        self, kstore, pipeline
    ):
        sid, _ = ensure_agent_source(kstore)
        _interrupt_after_the_ingest(pipeline, RuntimeError("gateway went away"))
        with pytest.raises(RuntimeError):
            await add_agent_document(
                pipeline, title="Doc", content="alpha body", source_uri=URI)
        assert len(_bodies(kstore, sid, "alpha body")) == 1

        del pipeline.get_job_status
        await add_agent_document(
            pipeline, title="Doc", content="alpha body", source_uri=URI)

        assert len(_bodies(kstore, sid, "alpha body")) == 1

    @pytest.mark.asyncio
    async def test_an_edited_re_add_does_not_strand_the_old_version(
        self, kstore, pipeline
    ):
        # The sharper harm: replacement is driven by the ownership record, so
        # losing it means the superseded text can never be removed.
        sid, _ = ensure_agent_source(kstore)
        _interrupt_after_the_ingest(pipeline, RuntimeError("gateway went away"))
        with pytest.raises(RuntimeError):
            await add_agent_document(
                pipeline, title="Doc", content="version one", source_uri=URI)

        del pipeline.get_job_status
        await add_agent_document(
            pipeline, title="Doc", content="version two", source_uri=URI)

        assert not _bodies(kstore, sid, "version one")
        assert len(_bodies(kstore, sid, "version two")) == 1

    @pytest.mark.asyncio
    async def test_a_second_document_with_identical_text_is_still_refused(
        self, kstore, pipeline
    ):
        # `find_document_by_hash` is the aggregate's own duplicate gate and also
        # reads the ownership row, so it fails in the same instant.
        sid, _ = ensure_agent_source(kstore)
        _interrupt_after_the_ingest(pipeline, RuntimeError("gateway went away"))
        with pytest.raises(RuntimeError):
            await add_agent_document(
                pipeline, title="Doc", content="shared body", source_uri=URI)

        del pipeline.get_job_status
        result = await add_agent_document(
            pipeline, title="Other", content="shared body", source_uri=OTHER_URI)

        assert result["status"] == "duplicate"
        assert len(_bodies(kstore, sid, "shared body")) == 1

    @pytest.mark.asyncio
    async def test_a_cancelled_add_still_leaves_the_document_owned(
        self, kstore, pipeline
    ):
        # Cancellation is the ordinary case here, not an exotic one: the add
        # runs on the gateway loop and the awaits after the ingest are real
        # cancellation points.
        sid, _ = ensure_agent_source(kstore)
        _interrupt_after_the_ingest(pipeline, asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await add_agent_document(
                pipeline, title="Doc", content="alpha body", source_uri=URI)

        _, owned = get_state(kstore, sid, document_slug(URI))
        assert set(owned) == _live_ids(kstore, sid)

    @pytest.mark.asyncio
    async def test_the_recorded_group_is_exactly_this_documents_items(
        self, kstore, pipeline
    ):
        # The group is recorded from inside the ingest now, so pin that it still
        # names this document's items and not a neighbour's.
        sid, _ = ensure_agent_source(kstore)
        await add_agent_document(
            pipeline, title="First", content="first body", source_uri=URI)
        first_owned = set(get_state(kstore, sid, document_slug(URI))[1])

        await add_agent_document(
            pipeline, title="Second", content="second body", source_uri=OTHER_URI)
        second_owned = set(get_state(kstore, sid, document_slug(OTHER_URI))[1])

        assert first_owned and second_owned
        assert not (first_owned & second_owned)
        assert first_owned | second_owned == _live_ids(kstore, sid)

    @pytest.mark.asyncio
    async def test_an_edit_replaces_the_group_rather_than_growing_it(
        self, kstore, pipeline
    ):
        sid, _ = ensure_agent_source(kstore)
        await add_agent_document(
            pipeline, title="Doc", content="version one", source_uri=URI)

        await add_agent_document(
            pipeline, title="Doc", content="version two", source_uri=URI)

        assert not _bodies(kstore, sid, "version one")
        assert len(_bodies(kstore, sid, "version two")) == 1
        assert set(get_state(kstore, sid, document_slug(URI))[1]) == _live_ids(kstore, sid)

    @pytest.mark.asyncio
    async def test_an_unrelated_document_is_untouched_by_a_neighbours_edit(
        self, kstore, pipeline
    ):
        sid, _ = ensure_agent_source(kstore)
        await add_agent_document(
            pipeline, title="Keep", content="keep body", source_uri=OTHER_URI)
        await add_agent_document(
            pipeline, title="Doc", content="version one", source_uri=URI)

        await add_agent_document(
            pipeline, title="Doc", content="version two", source_uri=URI)

        assert len(_bodies(kstore, sid, "keep body")) == 1
        assert set(get_state(kstore, sid, document_slug(OTHER_URI))[1]) == {
            i for i in _live_ids(kstore, sid)
            if i not in set(get_state(kstore, sid, document_slug(URI))[1])}

    @pytest.mark.asyncio
    async def test_a_concurrent_import_never_joins_this_documents_group(
        self, kstore, pipeline
    ):
        # The group must come from what THIS ingest wrote. `import_bundle` is an
        # independent writer into the same aggregate -- its own transaction, and
        # outside the add lock -- so anything it commits mid-ingest would be
        # swept into the group by a before/after comparison of the source.
        sid, _ = ensure_agent_source(kstore)
        real_extract = pipeline.extractor.extract_batch

        async def import_lands_mid_ingest(contents):
            kstore.import_bundle({"items": [_imported_item(sid)]})
            return await real_extract(contents)

        pipeline.extractor.extract_batch = import_lands_mid_ingest

        await add_agent_document(
            pipeline, title="Doc", content="alpha body", source_uri=URI)

        _, owned = get_state(kstore, sid, document_slug(URI))
        assert IMPORTED_ID not in owned, (
            "the document claims ownership of an item another writer committed")

    @pytest.mark.asyncio
    async def test_editing_a_document_cannot_delete_concurrently_imported_knowledge(
        self, kstore, pipeline
    ):
        # The destructive consequence: ownership carries delete authority, so a
        # group that over-claims deletes someone else's knowledge on the next
        # edit of this document.
        sid, _ = ensure_agent_source(kstore)
        real_extract = pipeline.extractor.extract_batch

        async def import_lands_mid_ingest(contents):
            kstore.import_bundle({"items": [_imported_item(sid)]})
            pipeline.extractor.extract_batch = real_extract
            return await real_extract(contents)

        pipeline.extractor.extract_batch = import_lands_mid_ingest
        await add_agent_document(
            pipeline, title="Doc", content="version one", source_uri=URI)

        await add_agent_document(
            pipeline, title="Doc", content="version two", source_uri=URI)

        assert IMPORTED_ID in _live_ids(kstore, sid), (
            "editing an agent document deleted imported knowledge")
        assert _bodies(kstore, sid, "imported body")

    @pytest.mark.asyncio
    async def test_a_partial_ingest_records_no_ownership(self, kstore, pipeline):
        # The callback fires only on the success branch. A chunk that fails
        # leaves the ingest short of its total, and the finalizer rolls the
        # created items back -- so recording a group there would name items that
        # are about to be deleted.
        sid, _ = ensure_agent_source(kstore)
        kstore.add_source_location = MagicMock(side_effect=RuntimeError("no room"))

        result = await add_agent_document(
            pipeline, title="Doc", content="alpha body", source_uri=URI)

        assert result["status"] == "error"
        assert get_state(kstore, sid, document_slug(URI)) == (None, [])
        assert not _bodies(kstore, sid, "alpha body")

    @pytest.mark.asyncio
    async def test_a_refused_duplicate_still_records_its_marker(
        self, kstore, pipeline
    ):
        # The deduped branch returns before the finalize hop, so its state write
        # stays on the caller side and must keep working.
        sid, _ = ensure_agent_source(kstore)
        await add_agent_document(
            pipeline, title="Doc", content="shared body", source_uri=URI)

        result = await add_agent_document(
            pipeline, title="Other", content="shared body", source_uri=OTHER_URI)

        assert result["status"] == "duplicate"
        assert len(_bodies(kstore, sid, "shared body")) == 1
