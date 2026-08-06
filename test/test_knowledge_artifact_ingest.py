"""Tests for artifact -> Knowledge Library auto-ingest (aggregate source model)."""

import asyncio
import hashlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.artifacts import ArtifactStore
from kiro_crew.knowledge.artifact_ingest import (
    ARTIFACT_SOURCE_TYPE,
    ARTIFACT_SOURCE_URI,
    ArtifactKnowledgeSync,
    backfill_artifacts,
    ensure_artifact_source,
    ingest_artifact,
    refresh_artifact_name,
    remove_artifact,
)
from kiro_crew.knowledge.ingestion import IngestionPipeline
from kiro_crew.knowledge.readers import FileReader
from kiro_crew.knowledge.store import KnowledgeStore

DEFAULT_KINDS = {"markdown", "text", "html", "json"}


def _one_chunk(text, **kw):
    """Single-chunk stub shared by all chunker dispatch methods in tests."""
    return [{"content": text, "chunk_index": 0, "section_title": None,
             "line_start": 0, "line_end": 0}]


@pytest.fixture()
def kstore(tmp_path):
    s = KnowledgeStore(str(tmp_path / "knowledge.db"))
    yield s
    s.close()


@pytest.fixture()
def pipeline(kstore):
    """IngestionPipeline with a real FileReader (artifact ingest now routes
    through ingest_file -> reader) but mocked chunker/extractor.

    The chunker echoes the reader's text as one chunk; the extractor returns a
    trivial extraction. No embedder (embedding is a no-op), and the LLM pool is
    None so generate_source_summary short-circuits.
    """
    extractor = MagicMock()
    extractor._pool = None
    extractor.extract_batch = AsyncMock(
        return_value=[{"category": "document", "summary": "s", "entities": []}]
    )
    chunker = MagicMock()
    # ingest_file dispatches by extension: .md -> chunk_markdown, code -> chunk_code,
    # .pptx -> chunk_slides, everything else -> chunk(text, source_uri=...). Stub them
    # all to a single chunk so total==1 matches the single mocked extraction below.
    chunker.chunk.side_effect = _one_chunk
    chunker.chunk_markdown.side_effect = _one_chunk
    chunker.chunk_code.side_effect = _one_chunk
    chunker.chunk_slides.side_effect = _one_chunk
    return IngestionPipeline(
        store=kstore, extractor=extractor, chunker=chunker,
        reader=FileReader(), embedder=None,
    )


@pytest.fixture()
def art_store(tmp_path):
    return ArtifactStore(root=tmp_path / "artifacts")


def _contents(kstore, source_id):
    return [r["content"] for r in kstore.db.execute(
        "SELECT content FROM items WHERE source_id = ?", (source_id,)).fetchall()]


def _item_ids(kstore, source_id):
    return {r["id"] for r in kstore.db.execute(
        "SELECT id FROM items WHERE source_id = ?", (source_id,)).fetchall()}


class TestIngestTextGroupReplace:
    """ingest_text with old_item_ids replaces only that group within a source."""

    @pytest.mark.asyncio
    async def test_replace_only_target_group(self, pipeline, kstore):
        sid = kstore.add_source(name="Agg", source_type="artifact", uri="artifact://")
        # Two independent groups under one source.
        await pipeline.ingest_text("group A v1", title="A", source_id=sid, old_item_ids=[])
        a_ids = _item_ids(kstore, sid)
        await pipeline.ingest_text("group B v1", title="B", source_id=sid, old_item_ids=[])
        b_ids = _item_ids(kstore, sid) - a_ids
        # Replace only group A.
        await pipeline.ingest_text(
            "group A v2", title="A", source_id=sid, old_item_ids=list(a_ids))
        contents = _contents(kstore, sid)
        assert any("group A v2" in c for c in contents)
        assert not any("group A v1" in c for c in contents)
        # Group B is untouched.
        assert any("group B v1" in c for c in contents)
        assert b_ids <= _item_ids(kstore, sid)

    @pytest.mark.asyncio
    async def test_replace_all_when_old_item_ids_none(self, pipeline, kstore):
        sid = kstore.add_source(name="Single", source_type="manual", uri="manual://x")
        await pipeline.ingest_text("first", title="S", source_id=sid)
        await pipeline.ingest_text("second", title="S", source_id=sid)
        contents = _contents(kstore, sid)
        assert any("second" in c for c in contents)
        assert not any("first" in c for c in contents)

    @pytest.mark.asyncio
    async def test_group_replace_defers_dedup(self, pipeline, kstore):
        """A group-level replace (old_item_ids provided -- the aggregate
        Artifacts source path) must NOT run cross-source dedup; a whole-source
        replace (old_item_ids is None) still does."""
        sid = kstore.add_source(name="Agg", source_type="artifact", uri="artifact://")
        pipeline._maybe_dedup = MagicMock()
        await pipeline.ingest_text("body", title="A", source_id=sid, old_item_ids=[])
        pipeline._maybe_dedup.assert_not_called()
        await pipeline.ingest_text("other", title="B", source_id=sid)
        # The just-written document's hash is passed too: a source id alone is
        # ambiguous once one source holds several documents.
        pipeline._maybe_dedup.assert_called_once_with(
            sid, hashlib.sha256(b"other").hexdigest())


class TestEnsureArtifactSource:
    def test_creates_once_then_idempotent(self, kstore):
        sid1, created1 = ensure_artifact_source(kstore)
        assert created1 is True
        src = kstore.get_source_by_uri(ARTIFACT_SOURCE_URI)
        assert src["source_type"] == ARTIFACT_SOURCE_TYPE
        sid2, created2 = ensure_artifact_source(kstore)
        assert created2 is False
        assert sid1 == sid2


class TestIngestArtifact:
    @pytest.mark.asyncio
    async def test_markdown_ingested_into_aggregate(self, pipeline, art_store, kstore):
        sid, _ = ensure_artifact_source(kstore)
        art = art_store.create(name="Notes", content="# Heading\nbody", kind="markdown")
        job = await ingest_artifact(pipeline, art_store, art.slug, sid, DEFAULT_KINDS)
        assert job is not None
        # Lands under the single aggregate source, not a per-slug source.
        assert kstore.get_source_by_uri(ARTIFACT_SOURCE_URI)["id"] == sid
        assert any("body" in c for c in _contents(kstore, sid))

    @pytest.mark.asyncio
    async def test_widget_excluded(self, pipeline, art_store, kstore):
        # widget is not a substantial-document kind -- excluded from the default
        # allowlist (a remote widget also round-trips back to kind="widget").
        sid, _ = ensure_artifact_source(kstore)
        art = art_store.create(name="W", content="<div>x</div>", kind="widget")
        assert await ingest_artifact(pipeline, art_store, art.slug, sid, DEFAULT_KINDS) is None
        assert _contents(kstore, sid) == []

    @pytest.mark.asyncio
    async def test_html_ingested_with_prose_extraction(self, pipeline, art_store, kstore):
        # html routes through the reader's _read_html: prose kept, markup stripped.
        sid, _ = ensure_artifact_source(kstore)
        art = art_store.create(
            name="Report", content="<h1>Title</h1><p>hello <b>world</b></p>", kind="html")
        job = await ingest_artifact(pipeline, art_store, art.slug, sid, DEFAULT_KINDS)
        assert job is not None
        contents = _contents(kstore, sid)
        assert any("hello" in c and "world" in c for c in contents)
        assert not any("<b>" in c or "<h1>" in c for c in contents)

    @pytest.mark.asyncio
    async def test_empty_skipped(self, pipeline, art_store, kstore):
        sid, _ = ensure_artifact_source(kstore)
        art = art_store.create(name="Empty", content="   ", kind="text")
        assert await ingest_artifact(pipeline, art_store, art.slug, sid, DEFAULT_KINDS) is None

    @pytest.mark.asyncio
    async def test_unchanged_content_short_circuits(self, pipeline, art_store, kstore):
        sid, _ = ensure_artifact_source(kstore)
        art = art_store.create(name="Doc", content="stable body", kind="markdown")
        assert await ingest_artifact(pipeline, art_store, art.slug, sid, DEFAULT_KINDS) is not None
        # Second ingest with identical content is a per-slug hash no-op.
        assert await ingest_artifact(pipeline, art_store, art.slug, sid, DEFAULT_KINDS) is None

    @pytest.mark.asyncio
    async def test_credentials_and_name_redacted(self, pipeline, art_store, kstore):
        sid, _ = ensure_artifact_source(kstore)
        secret = "AKIAIOSFODNN7EXAMPLE"
        name_secret = "AKIAIOSFODNN7TITLE00"
        art = art_store.create(
            name=f"Leaky {name_secret}", content=f"config key {secret} here", kind="text")
        await ingest_artifact(pipeline, art_store, art.slug, sid, DEFAULT_KINDS)
        rows = kstore.db.execute(
            "SELECT title, content FROM items WHERE source_id = ?", (sid,)).fetchall()
        assert all(secret not in r["content"] for r in rows)
        assert all(name_secret not in (r["title"] or "") for r in rows)

    @pytest.mark.asyncio
    async def test_file_backed_sensitive_source_path_skipped(
        self, pipeline, art_store, kstore, tmp_path, monkeypatch
    ):
        sid, _ = ensure_artifact_source(kstore)
        src_file = tmp_path / "secret.md"
        src_file.write_text("# notes\nsensitive body", encoding="utf-8")
        art = art_store.create(
            name="Backed", content="# notes\nsensitive body", kind="markdown",
            source_path=str(src_file))
        monkeypatch.setattr(
            "kiro_crew.knowledge.artifact_ingest.is_sensitive_path", lambda p: True)
        assert await ingest_artifact(pipeline, art_store, art.slug, sid, DEFAULT_KINDS) is None
        assert _contents(kstore, sid) == []

    @pytest.mark.asyncio
    async def test_edit_replaces_only_that_artifacts_group(self, pipeline, art_store, kstore):
        sid, _ = ensure_artifact_source(kstore)
        a = art_store.create(name="A", content="alpha v1", kind="markdown")
        b = art_store.create(name="B", content="bravo v1", kind="markdown")
        await ingest_artifact(pipeline, art_store, a.slug, sid, DEFAULT_KINDS)
        await ingest_artifact(pipeline, art_store, b.slug, sid, DEFAULT_KINDS)
        b_ids_before = {r["id"] for r in kstore.db.execute(
            "SELECT i.id FROM items i WHERE i.content LIKE 'bravo%'").fetchall()}
        # Edit A only.
        art_store.update(a.slug, content="alpha v2", snapshot=True)
        await ingest_artifact(pipeline, art_store, a.slug, sid, DEFAULT_KINDS)
        contents = _contents(kstore, sid)
        assert any("alpha v2" in c for c in contents)
        assert not any("alpha v1" in c for c in contents)
        # B's items are intact (same ids, content unchanged).
        assert any("bravo v1" in c for c in contents)
        b_ids_after = {r["id"] for r in kstore.db.execute(
            "SELECT i.id FROM items i WHERE i.content LIKE 'bravo%'").fetchall()}
        assert b_ids_before == b_ids_after


class TestRemoveArtifact:
    @pytest.mark.asyncio
    async def test_remove_deletes_only_that_group(self, pipeline, art_store, kstore):
        sid, _ = ensure_artifact_source(kstore)
        a = art_store.create(name="A", content="alpha body", kind="markdown")
        b = art_store.create(name="B", content="bravo body", kind="markdown")
        await ingest_artifact(pipeline, art_store, a.slug, sid, DEFAULT_KINDS)
        await ingest_artifact(pipeline, art_store, b.slug, sid, DEFAULT_KINDS)
        removed = remove_artifact(kstore, sid, a.slug)
        assert removed == 1
        contents = _contents(kstore, sid)
        assert not any("alpha" in c for c in contents)
        assert any("bravo" in c for c in contents)
        # State row for A is gone; B's remains.
        rows = kstore.db.execute(
            "SELECT slug FROM artifact_item_state WHERE source_id = ?", (sid,)).fetchall()
        slugs = {r["slug"] for r in rows}
        assert a.slug not in slugs
        assert b.slug in slugs

    def test_remove_unknown_is_noop(self, kstore):
        sid, _ = ensure_artifact_source(kstore)
        assert remove_artifact(kstore, sid, "never-ingested") == 0


class TestBackfill:
    @pytest.mark.asyncio
    async def test_backfill_ingests_eligible_only(self, pipeline, art_store, kstore):
        sid, _ = ensure_artifact_source(kstore)
        art_store.create(name="MD", content="markdown body", kind="markdown")
        art_store.create(name="TXT", content="text body", kind="text")
        art_store.create(name="WID", content="<b>x</b>", kind="widget")
        n = await backfill_artifacts(pipeline, art_store, sid, DEFAULT_KINDS)
        assert n == 2
        contents = _contents(kstore, sid)
        assert any("markdown body" in c for c in contents)
        assert any("text body" in c for c in contents)

    @pytest.mark.asyncio
    async def test_backfill_empty_kinds_noop(self, pipeline, art_store, kstore):
        sid, _ = ensure_artifact_source(kstore)
        art_store.create(name="MD", content="body", kind="markdown")
        assert await backfill_artifacts(pipeline, art_store, sid, set()) == 0


class TestArtifactKnowledgeSync:
    @pytest.mark.asyncio
    async def test_handle_upsert_then_delete(self, pipeline, art_store, kstore):
        sync = ArtifactKnowledgeSync(
            art_store=art_store, pipeline=pipeline, kinds=DEFAULT_KINDS,
            loop=asyncio.get_running_loop())
        art = art_store.create(name="Doc", content="hello body", kind="markdown")
        await sync._handle("upsert", art.slug)
        sid = kstore.get_source_by_uri(ARTIFACT_SOURCE_URI)["id"]
        assert any("hello body" in c for c in _contents(kstore, sid))
        await sync._handle("delete", art.slug)
        assert _contents(kstore, sid) == []

    @pytest.mark.asyncio
    async def test_start_backfills_when_source_created(self, pipeline, art_store, kstore):
        art_store.create(name="Pre", content="preexisting body", kind="markdown")
        sync = ArtifactKnowledgeSync(
            art_store=art_store, pipeline=pipeline, kinds=DEFAULT_KINDS,
            loop=asyncio.get_running_loop())
        await sync.start()
        # start() schedules the backfill as a background task; await it.
        assert sync._backfill_task is not None
        await sync._backfill_task
        sid = kstore.get_source_by_uri(ARTIFACT_SOURCE_URI)["id"]
        assert any("preexisting body" in c for c in _contents(kstore, sid))

    @pytest.mark.asyncio
    async def test_start_no_backfill_when_source_exists(self, pipeline, art_store, kstore):
        ensure_artifact_source(kstore)  # pre-create the row
        sync = ArtifactKnowledgeSync(
            art_store=art_store, pipeline=pipeline, kinds=DEFAULT_KINDS,
            loop=asyncio.get_running_loop())
        await sync.start()
        assert sync._backfill_task is None


class TestKnowledgeConfigDefaults:
    def test_defaults_on(self):
        from kiro_crew.config.loader import KiroCrewConfig, KnowledgeConfig
        kc = KnowledgeConfig()
        assert kc.auto_ingest_artifacts is True
        assert kc.auto_ingest_artifact_kinds == ["markdown", "text", "html", "json"]
        assert KiroCrewConfig().knowledge.auto_ingest_artifacts is True


class TestGroupLabelAndRename:
    @pytest.mark.asyncio
    async def test_name_stored_as_group_label(self, pipeline, art_store, kstore):
        sid, _ = ensure_artifact_source(kstore)
        art = art_store.create(name="My Doc", content="body text", kind="markdown")
        await ingest_artifact(pipeline, art_store, art.slug, sid, DEFAULT_KINDS)
        row = kstore.db.execute(
            "SELECT name FROM artifact_item_state WHERE source_id = ? AND slug = ?",
            (sid, art.slug)).fetchone()
        assert row["name"] == "My Doc"

    @pytest.mark.asyncio
    async def test_attach_file_paths_labels_artifact_items_by_name(
        self, pipeline, art_store, kstore
    ):
        from kiro_crew.dashboard.handlers.knowledge import _attach_file_paths
        sid, _ = ensure_artifact_source(kstore)
        art = art_store.create(name="Grouped Doc", content="body", kind="markdown")
        await ingest_artifact(pipeline, art_store, art.slug, sid, DEFAULT_KINDS)
        items = [{"id": r["id"], "source_id": sid} for r in kstore.db.execute(
            "SELECT id FROM items WHERE source_id = ?", (sid,)).fetchall()]
        assert items
        _attach_file_paths(kstore, items)
        assert all(i["_file_path"] == "Grouped Doc" for i in items)

    @pytest.mark.asyncio
    async def test_rename_refreshes_label_without_reingest(self, pipeline, art_store, kstore):
        sid, _ = ensure_artifact_source(kstore)
        art = art_store.create(name="Old Name", content="stable body", kind="markdown")
        await ingest_artifact(pipeline, art_store, art.slug, sid, DEFAULT_KINDS)
        item_ids_before = {r["id"] for r in kstore.db.execute(
            "SELECT id FROM items WHERE source_id = ?", (sid,)).fetchall()}
        # Refresh the label (rename path) -- no re-ingest, items untouched.
        assert refresh_artifact_name(kstore, sid, art.slug, "New Name") is True
        row = kstore.db.execute(
            "SELECT name FROM artifact_item_state WHERE source_id = ? AND slug = ?",
            (sid, art.slug)).fetchone()
        assert row["name"] == "New Name"
        item_ids_after = {r["id"] for r in kstore.db.execute(
            "SELECT id FROM items WHERE source_id = ?", (sid,)).fetchall()}
        assert item_ids_before == item_ids_after

    def test_refresh_unknown_is_noop(self, kstore):
        sid, _ = ensure_artifact_source(kstore)
        assert refresh_artifact_name(kstore, sid, "never-ingested", "X") is False

    @pytest.mark.asyncio
    async def test_rename_event_updates_label(self, pipeline, art_store, kstore):
        sync = ArtifactKnowledgeSync(
            art_store=art_store, pipeline=pipeline, kinds=DEFAULT_KINDS,
            loop=asyncio.get_running_loop())
        art = art_store.create(name="Before", content="hello body", kind="markdown")
        await sync._handle("upsert", art.slug)
        sid = kstore.get_source_by_uri(ARTIFACT_SOURCE_URI)["id"]
        art_store.update(art.slug, name="After")  # metadata-only rename
        await sync._handle("rename", art.slug)
        row = kstore.db.execute(
            "SELECT name FROM artifact_item_state WHERE source_id = ? AND slug = ?",
            (sid, art.slug)).fetchone()
        assert row["name"] == "After"


class TestKindChangeReconciliation:
    """A kind change is what decides Knowledge eligibility, so it has to be
    reconciled rather than skipped.

    ``ingest_artifact`` early-returns on an ineligible kind. That is right for a
    backfill sweep, but wrong for a *change*: an artifact ingested as markdown and
    then switched to svg would keep answering searches from prose that no longer
    describes it. The dashboard now lets a user change the type directly, so this
    transition is reachable from the UI rather than only from a widget pull.
    """

    @pytest.mark.asyncio
    async def test_becoming_ineligible_removes_the_stale_chunks(
        self, kstore, pipeline, art_store
    ) -> None:
        sync = ArtifactKnowledgeSync(
            art_store=art_store, pipeline=pipeline, kinds=DEFAULT_KINDS,
            loop=asyncio.get_running_loop())
        art = art_store.create(name="Doc", content="hello body", kind="markdown")
        await sync._handle("upsert", art.slug)
        sid = kstore.get_source_by_uri(ARTIFACT_SOURCE_URI)["id"]
        assert any("hello body" in c for c in _contents(kstore, sid))

        # svg is excluded from the eligible kinds (.svg is not a reader format).
        art_store.update(art.slug, kind="svg")
        await sync._handle("upsert", art.slug)
        assert _contents(kstore, sid) == []

    @pytest.mark.asyncio
    async def test_becoming_eligible_ingests_it(self, kstore, pipeline, art_store) -> None:
        sync = ArtifactKnowledgeSync(
            art_store=art_store, pipeline=pipeline, kinds=DEFAULT_KINDS,
            loop=asyncio.get_running_loop())
        art = art_store.create(name="Doc", content="hello body", kind="svg")
        await sync._handle("upsert", art.slug)
        sid = kstore.get_source_by_uri(ARTIFACT_SOURCE_URI)["id"]
        assert _contents(kstore, sid) == []

        art_store.update(art.slug, kind="markdown")
        await sync._handle("upsert", art.slug)
        assert any("hello body" in c for c in _contents(kstore, sid))

    @pytest.mark.asyncio
    async def test_a_vanished_artifact_is_not_an_error(
        self, kstore, pipeline, art_store
    ) -> None:
        """The event is processed asynchronously, so the artifact may already be
        gone by the time it lands."""
        sync = ArtifactKnowledgeSync(
            art_store=art_store, pipeline=pipeline, kinds=DEFAULT_KINDS,
            loop=asyncio.get_running_loop())
        await sync._handle("upsert", "never-existed")


class TestKindChangeFiresAnEvent:
    """The store has to REPORT a kind change; without the event the listener
    above never runs and the index silently keeps the old chunks."""

    def test_a_kind_only_update_fires_upsert(self, art_store) -> None:
        art = art_store.create(name="Doc", content="body", kind="markdown")
        seen: list[tuple[str, str]] = []
        art_store.set_change_listener(lambda action, slug: seen.append((action, slug)))
        art_store.update(art.slug, kind="svg")
        assert seen == [("upsert", art.slug)]

    def test_a_no_op_kind_update_fires_nothing(self, art_store) -> None:
        """Re-selecting the kind it already has is not a change to reconcile."""
        art = art_store.create(name="Doc", content="body", kind="markdown")
        seen: list[tuple[str, str]] = []
        art_store.set_change_listener(lambda action, slug: seen.append((action, slug)))
        art_store.update(art.slug, kind="markdown")
        assert seen == []

    def test_a_rename_alongside_a_kind_change_still_reingests(self, art_store) -> None:
        """Upsert has to win over the rename-only path: a rename event refreshes
        the label without re-chunking, which would leave the stale chunks."""
        art = art_store.create(name="Doc", content="body", kind="markdown")
        seen: list[tuple[str, str]] = []
        art_store.set_change_listener(lambda action, slug: seen.append((action, slug)))
        art_store.update(art.slug, name="Renamed", kind="svg")
        assert seen == [("upsert", art.slug)]


def test_removing_a_deduped_artifact_releases_its_claim_on_the_winner(tmp_path):
    """A deduped artifact owns no items but still holds the winner's.

    Deleting the artifact must drop that claim, or a later winner deletion hands the
    document to a source whose artifact is gone and the text stays searchable.
    """
    from kiro_crew.knowledge.artifact_ingest import remove_artifact
    from kiro_crew.knowledge.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "k.db"))
    try:
        agg = store.add_source(name="Artifacts", source_type="artifact",
                               uri="artifact://library")
        folder = store.add_source(name="docs", source_type="local_folder",
                                  uri=str(tmp_path / "docs"))
        h = "9" * 64
        iid = store.add_item(title="spec.md", content="body", item_type="document",
                             source_id=folder, content_hash=h)
        store.add_source_location(iid, folder)
        store.add_source_location(iid, agg)
        store.db.execute(
            "INSERT INTO artifact_item_state (source_id, slug, content_hash, "
            "item_ids, updated_at, status) "
            "VALUES (?, 'spec', ?, '[]', '2024-01-01', 'deduped')", (agg, h))
        store.db.commit()

        assert agg in store.sources_holding_item(iid)
        remove_artifact(store, agg, "spec")

        assert agg not in store.sources_holding_item(iid), (
            "a removed artifact must release its claim")
        assert store.get_item(iid) is not None, "the winner's item is untouched"
        # And the winner's deletion now takes the document with it.
        store.delete_source_cascade(folder)
        assert store.get_item(iid) is None
    finally:
        store.db.close()
