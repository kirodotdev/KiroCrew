"""Tests for artifact -> Knowledge Library auto-ingest (aggregate source model)."""

import asyncio
import hashlib
import threading
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.artifacts import ArtifactNotFoundError, ArtifactStore
from kiro_crew.knowledge import artifact_ingest
from kiro_crew.knowledge.artifact_ingest import (
    ARTIFACT_SOURCE_TYPE,
    ARTIFACT_SOURCE_URI,
    ArtifactKnowledgeSync,
    ensure_artifact_source,
    ingest_artifact,
    reconcile_artifacts,
    refresh_artifact_name,
    remove_artifact,
)
from kiro_crew.knowledge.ingestion import DUPLICATE_JOB_STATUS, IngestionPipeline
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


class TestReconcile:
    @pytest.mark.asyncio
    async def test_reconcile_ingests_eligible_only(self, pipeline, art_store, kstore):
        sid, _ = ensure_artifact_source(kstore)
        art_store.create(name="MD", content="markdown body", kind="markdown")
        art_store.create(name="TXT", content="text body", kind="text")
        art_store.create(name="WID", content="<b>x</b>", kind="widget")
        ingested, removed, deferred = await reconcile_artifacts(
            pipeline, art_store, sid, DEFAULT_KINDS)
        assert (ingested, removed, deferred) == (2, 0, 0)
        contents = _contents(kstore, sid)
        assert any("markdown body" in c for c in contents)
        assert any("text body" in c for c in contents)

    @pytest.mark.asyncio
    async def test_reconcile_empty_kinds_noop(self, pipeline, art_store, kstore):
        sid, _ = ensure_artifact_source(kstore)
        art_store.create(name="MD", content="body", kind="markdown")
        assert await reconcile_artifacts(pipeline, art_store, sid, set()) == (0, 0, 0)

    @pytest.mark.asyncio
    async def test_a_converged_store_costs_nothing(self, pipeline, art_store, kstore):
        """The steady state: reconcile runs every start, so it must be free."""
        sid, _ = ensure_artifact_source(kstore)
        art_store.create(name="MD", content="body", kind="markdown")
        assert await reconcile_artifacts(pipeline, art_store, sid, DEFAULT_KINDS) == (1, 0, 0)
        # Second pass sees identical content -> ingest_artifact returns None.
        assert await reconcile_artifacts(pipeline, art_store, sid, DEFAULT_KINDS) == (0, 0, 0)

    @pytest.mark.asyncio
    async def test_reconcile_drops_state_for_artifacts_deleted_while_off(
        self, pipeline, art_store, kstore
    ):
        """The listener is not registered while the feature is off, so a delete
        during that window never reached the Library. Reconcile must drop it, or
        the text stays searchable with no artifact behind it."""
        sid, _ = ensure_artifact_source(kstore)
        art = art_store.create(name="Doomed", content="doomed body", kind="markdown")
        await reconcile_artifacts(pipeline, art_store, sid, DEFAULT_KINDS)
        assert any("doomed body" in c for c in _contents(kstore, sid))
        art_store.delete(art.slug)  # no listener: the Library is not told
        ingested, removed, deferred = await reconcile_artifacts(
            pipeline, art_store, sid, DEFAULT_KINDS)
        assert (ingested, removed, deferred) == (0, 1, 0)
        assert _contents(kstore, sid) == []

    @pytest.mark.asyncio
    async def test_an_ineligible_kind_is_not_treated_as_deleted(
        self, pipeline, art_store, kstore
    ):
        """Narrowing `kinds` makes an artifact ineligible, not absent. Deleting
        its items on that basis would drop content the user never deleted."""
        sid, _ = ensure_artifact_source(kstore)
        art_store.create(name="MD", content="still here", kind="markdown")
        await reconcile_artifacts(pipeline, art_store, sid, DEFAULT_KINDS)
        ingested, removed, deferred = await reconcile_artifacts(
            pipeline, art_store, sid, {"text"})
        assert removed == 0, "an ineligible kind must not be reaped"
        assert any("still here" in c for c in _contents(kstore, sid))

    @pytest.mark.asyncio
    async def test_a_kind_change_while_off_reaps_the_stale_chunks(
        self, pipeline, art_store, kstore
    ):
        """The indexed group was produced by the PREVIOUS kind's reader, so a
        kind change during the off-window leaves obsolete prose answering
        searches. The live ``_handle`` path removes on that upsert; reconcile
        must do the same."""
        sid, _ = ensure_artifact_source(kstore)
        art = art_store.create(name="Doc", content="markdown prose", kind="markdown")
        await reconcile_artifacts(pipeline, art_store, sid, DEFAULT_KINDS)
        assert any("markdown prose" in c for c in _contents(kstore, sid))
        art_store.update(art.slug, kind="svg")  # no listener registered
        ingested, removed, deferred = await reconcile_artifacts(
            pipeline, art_store, sid, DEFAULT_KINDS)
        assert (ingested, removed, deferred) == (0, 1, 0)
        assert _contents(kstore, sid) == []

    @pytest.mark.asyncio
    async def test_a_kind_change_into_an_excluded_kind_is_still_reaped(
        self, pipeline, art_store, kstore
    ):
        """The new kind being config-excluded does not make the OLD chunks less
        stale: the artifact itself changed, so its group must go even though
        nothing re-ingests it."""
        sid, _ = ensure_artifact_source(kstore)
        art = art_store.create(name="Doc", content="markdown prose", kind="markdown")
        await reconcile_artifacts(pipeline, art_store, sid, {"markdown"})
        art_store.update(art.slug, kind="text")  # eligible-kind change, excluded
        ingested, removed, deferred = await reconcile_artifacts(
            pipeline, art_store, sid, {"markdown"})
        assert (ingested, removed, deferred) == (0, 1, 0)
        assert _contents(kstore, sid) == []

    @pytest.mark.asyncio
    async def test_a_nonempty_stale_snapshot_does_not_overwrite_the_index(
        self, pipeline, art_store, kstore, monkeypatch
    ):
        """A fallback snapshot is not evidence about the live file in either
        direction: blank does not mean emptied, and non-blank does not mean
        current. Ingesting it would replace a newer index with older text."""
        sid, _ = ensure_artifact_source(kstore)
        art = art_store.create(name="Doc", content="newer body", kind="markdown")
        await reconcile_artifacts(pipeline, art_store, sid, DEFAULT_KINDS)
        assert any("newer body" in c for c in _contents(kstore, sid))

        real_get = art_store.get

        def _dead_pointer(slug, *a, **kw):
            got = real_get(slug, *a, **kw)
            got.content = "older snapshot body"
            got.source_path = "/nonexistent/moved.md"
            got.source_missing = True
            return got

        monkeypatch.setattr(art_store, "get", _dead_pointer)
        assert await ingest_artifact(
            pipeline, art_store, art.slug, sid, DEFAULT_KINDS) is None
        contents = _contents(kstore, sid)
        assert any("newer body" in c for c in contents)
        assert not any("older snapshot" in c for c in contents), (
            "a stale fallback snapshot must not replace the indexed content")

    @pytest.mark.asyncio
    async def test_an_emptied_artifact_is_dropped_even_past_the_budget(
        self, pipeline, art_store, kstore
    ):
        """Dropping an emptied artifact costs no extraction, so it must not sit
        behind the ingest budget: a backlog of newer changes would otherwise keep
        the obsolete text searchable for as many restarts as the backlog takes to
        drain."""
        sid, _ = ensure_artifact_source(kstore)
        stale = art_store.create(name="Stale", content="obsolete body", kind="markdown")
        await reconcile_artifacts(pipeline, art_store, sid, DEFAULT_KINDS)
        assert any("obsolete body" in c for c in _contents(kstore, sid))
        # Emptied while off, then buried under newer changes. `list` is
        # newest-first, so with budget=1 the newer artifact consumes the budget
        # and `stale` never reaches the ingest loop.
        art_store.update(stale.slug, content="   ")
        art_store.create(name="Newer", content="newer body", kind="markdown")
        ingested, removed, deferred = await reconcile_artifacts(
            pipeline, art_store, sid, DEFAULT_KINDS, budget=1)
        assert removed == 1, "the emptied artifact must be dropped regardless of budget"
        contents = _contents(kstore, sid)
        assert not any("obsolete body" in c for c in contents), (
            "stale text survived because the drop was budgeted")
        assert any("newer body" in c for c in contents)

    @pytest.mark.asyncio
    async def test_an_emptied_artifact_with_a_dead_source_is_left_alone(
        self, pipeline, art_store, kstore, monkeypatch
    ):
        """The unbudgeted drop must honour the same stand-down as
        `ingest_artifact`: a blank snapshot behind an unreadable live source
        proves nothing about the artifact's real content."""
        sid, _ = ensure_artifact_source(kstore)
        art_store.create(name="Doc", content="indexed body", kind="markdown")
        await reconcile_artifacts(pipeline, art_store, sid, DEFAULT_KINDS)
        real_get = art_store.get

        def _dead_pointer(slug, *a, **kw):
            got = real_get(slug, *a, **kw)
            got.content = ""
            got.source_path = "/nonexistent/moved.md"
            got.source_missing = True
            return got

        monkeypatch.setattr(art_store, "get", _dead_pointer)
        ingested, removed, deferred = await reconcile_artifacts(
            pipeline, art_store, sid, DEFAULT_KINDS)
        assert removed == 0
        assert any("indexed body" in c for c in _contents(kstore, sid))

    @pytest.mark.asyncio
    async def test_an_eligible_kind_change_is_replaced_inside_the_budget(
        self, pipeline, art_store, kstore
    ):
        """A kind change between two ELIGIBLE kinds must not be removed by the
        pre-pass: the re-ingest is budgeted, so a backlog past the budget would
        leave the remainder missing from search entirely rather than merely
        stale. Over-budget drift keeps its old group and defers."""
        sid, _ = ensure_artifact_source(kstore)
        keep = art_store.create(name="Keep", content="keep body", kind="markdown")
        swap = art_store.create(name="Swap", content="swap body", kind="markdown")
        await reconcile_artifacts(pipeline, art_store, sid, DEFAULT_KINDS)
        art_store.update(keep.slug, kind="text")
        art_store.update(swap.slug, kind="text")
        # budget=1: `list` is newest-first, so `swap` is replaced and `keep` waits.
        ingested, removed, deferred = await reconcile_artifacts(
            pipeline, art_store, sid, DEFAULT_KINDS, budget=1)
        assert (ingested, removed, deferred) == (1, 0, 1)
        contents = _contents(kstore, sid)
        assert any("swap body" in c for c in contents)
        assert any("keep body" in c for c in contents), (
            "deferred drift must keep its group, not lose it to a pre-pass delete")
        # The next start finishes the job; nothing was lost in between.
        ingested, removed, deferred = await reconcile_artifacts(
            pipeline, art_store, sid, DEFAULT_KINDS, budget=1)
        assert (ingested, removed, deferred) == (1, 0, 0)
        assert len(_contents(kstore, sid)) == 2

    @pytest.mark.asyncio
    async def test_an_eligible_kind_change_bypasses_the_unchanged_hash_shortcut(
        self, pipeline, art_store, kstore
    ):
        """Content is byte-identical across the kind change, so the recorded
        hash still matches and `ingest_artifact` alone would short-circuit,
        leaving the previous reader's chunks. Clearing the hash rebuilds the
        group."""
        sid, _ = ensure_artifact_source(kstore)
        art = art_store.create(name="Doc", content="same body", kind="markdown")
        await reconcile_artifacts(pipeline, art_store, sid, DEFAULT_KINDS)
        before = {r["id"] for r in kstore.db.execute(
            "SELECT id FROM items WHERE source_id = ?", (sid,)).fetchall()}
        art_store.update(art.slug, kind="text")
        ingested, removed, deferred = await reconcile_artifacts(
            pipeline, art_store, sid, DEFAULT_KINDS)
        assert (ingested, removed, deferred) == (1, 0, 0)
        after = {r["id"] for r in kstore.db.execute(
            "SELECT id FROM items WHERE source_id = ?", (sid,)).fetchall()}
        assert before != after, "the group must be rebuilt, not short-circuited"
        row = kstore.db.execute(
            "SELECT kind FROM artifact_item_state WHERE source_id = ? AND slug = ?",
            (sid, art.slug)).fetchone()
        assert row["kind"] == "text"

    @pytest.mark.asyncio
    async def test_a_failed_replacement_keeps_the_previous_group_searchable(
        self, pipeline, art_store, kstore, monkeypatch
    ):
        """The replacement must never trade a valid index for an extraction that
        might not land. Only the hash is cleared, so a failed re-ingest leaves
        the old group live and the artifact still searchable; the cleared hash
        means the next start retries."""
        sid, _ = ensure_artifact_source(kstore)
        art = art_store.create(name="Doc", content="original body", kind="markdown")
        await reconcile_artifacts(pipeline, art_store, sid, DEFAULT_KINDS)
        art_store.update(art.slug, kind="text")
        monkeypatch.setattr(
            pipeline, "ingest_file",
            AsyncMock(side_effect=RuntimeError("extraction exploded")))
        ingested, removed, deferred = await reconcile_artifacts(
            pipeline, art_store, sid, DEFAULT_KINDS)
        assert (ingested, removed, deferred) == (0, 0, 0)
        assert any("original body" in c for c in _contents(kstore, sid)), (
            "a failed replacement must not leave the artifact unsearchable")

    @pytest.mark.asyncio
    async def test_a_deduped_row_keeps_its_hash_so_its_claim_can_be_released(
        self, pipeline, art_store, kstore
    ):
        """A deduped row owns no items but holds a dedup claim keyed to the hash
        recorded on it -- that hash is how `release_stale_claim` names the
        document it detaches from. Clearing it would strand the claim, and
        deleting the winning holder would then hand this artifact the superseded
        text. The row also already fails the short-circuit's `and old_item_ids`
        leg, so there is nothing to defeat."""
        sid, _ = ensure_artifact_source(kstore)
        art = art_store.create(name="Doc", content="deduped body", kind="markdown")
        await reconcile_artifacts(pipeline, art_store, sid, DEFAULT_KINDS)
        # Force the deduped shape: a claim-holding row with an empty group.
        kstore.db.execute(
            "UPDATE artifact_item_state SET item_ids = '[]', status = 'deduped' "
            "WHERE source_id = ? AND slug = ?", (sid, art.slug))
        kstore.db.commit()
        before = kstore.db.execute(
            "SELECT content_hash FROM artifact_item_state "
            "WHERE source_id = ? AND slug = ?", (sid, art.slug)).fetchone()["content_hash"]
        assert before
        # The helper itself must refuse an empty-group row.
        assert artifact_ingest._invalidate_content_hash(kstore, sid, art.slug) is False
        assert kstore.db.execute(
            "SELECT content_hash FROM artifact_item_state "
            "WHERE source_id = ? AND slug = ?",
            (sid, art.slug)).fetchone()["content_hash"] == before

        art_store.update(art.slug, kind="text")
        seen: list[str | None] = []
        real_release = kstore.release_stale_claim

        def _spy(source_id, prev_hash, *a, **kw):
            seen.append(prev_hash)
            return real_release(source_id, prev_hash, *a, **kw)

        with mock.patch.object(kstore, "release_stale_claim", _spy):
            await reconcile_artifacts(pipeline, art_store, sid, DEFAULT_KINDS)
        assert seen, "the re-ingest must reach the claim-release step"
        assert seen[0] == before, (
            "release_stale_claim needs the ORIGINAL hash to name the document it "
            "detaches from; a nulled hash strands the claim")

    @pytest.mark.asyncio
    async def test_narrowing_the_allowlist_alone_never_reaps(
        self, pipeline, art_store, kstore
    ):
        """The counter-case to the two above, and the reason the decision reads
        the kind RECORDED AT INGEST rather than the current allowlist: the
        artifact is untouched, only the config narrowed. Reaping here would
        delete content the user never changed."""
        sid, _ = ensure_artifact_source(kstore)
        art_store.create(name="MD", content="untouched body", kind="markdown")
        await reconcile_artifacts(pipeline, art_store, sid, DEFAULT_KINDS)
        for _ in range(2):  # repeated starts must not erode it either
            ingested, removed, deferred = await reconcile_artifacts(
                pipeline, art_store, sid, {"text"})
            assert removed == 0
        assert any("untouched body" in c for c in _contents(kstore, sid))

    @pytest.mark.asyncio
    async def test_a_row_predating_the_kind_column_is_left_alone(
        self, pipeline, art_store, kstore
    ):
        """A legacy row carries kind NULL and its current kind is ineligible:
        the only repair available is deletion, and nothing proves it drifted. A
        stale group is recoverable; deleted items are not."""
        sid, _ = ensure_artifact_source(kstore)
        art = art_store.create(name="Doc", content="legacy body", kind="markdown")
        await reconcile_artifacts(pipeline, art_store, sid, DEFAULT_KINDS)
        kstore.db.execute(
            "UPDATE artifact_item_state SET kind = NULL WHERE source_id = ?", (sid,))
        kstore.db.commit()
        art_store.update(art.slug, kind="svg")
        ingested, removed, deferred = await reconcile_artifacts(
            pipeline, art_store, sid, DEFAULT_KINDS)
        assert removed == 0
        assert any("legacy body" in c for c in _contents(kstore, sid))

    @pytest.mark.asyncio
    async def test_a_legacy_eligible_row_is_re_ingested_once_and_backfilled(
        self, pipeline, art_store, kstore
    ):
        """A legacy row's ingested kind is unknown, so an off-window kind edit
        between two eligible kinds cannot be detected and the unchanged hash
        would keep the previous reader's chunks forever. Where the safe repair
        exists (re-ingest, no deletion) take it once, record the kind, and never
        repeat it."""
        sid, _ = ensure_artifact_source(kstore)
        art = art_store.create(name="Doc", content="legacy body", kind="markdown")
        await reconcile_artifacts(pipeline, art_store, sid, DEFAULT_KINDS)
        kstore.db.execute(
            "UPDATE artifact_item_state SET kind = NULL WHERE source_id = ?", (sid,))
        kstore.db.commit()
        ingested, removed, deferred = await reconcile_artifacts(
            pipeline, art_store, sid, DEFAULT_KINDS)
        assert (ingested, removed, deferred) == (1, 0, 0)
        row = kstore.db.execute(
            "SELECT kind FROM artifact_item_state WHERE source_id = ? AND slug = ?",
            (sid, art.slug)).fetchone()
        assert row["kind"] == "markdown", "the column must be backfilled"
        assert any("legacy body" in c for c in _contents(kstore, sid))
        # Self-terminating: the recorded kind now matches, so the next start is
        # free again.
        assert await reconcile_artifacts(
            pipeline, art_store, sid, DEFAULT_KINDS) == (0, 0, 0)

    @pytest.mark.asyncio
    async def test_a_directory_without_metadata_is_not_treated_as_deleted(
        self, pipeline, art_store, kstore, monkeypatch
    ):
        """``ArtifactNotFoundError`` is also what a missing ``meta.json`` raises,
        which a partially-restored artifact directory hits while its content is
        still there. Deletion removes the whole directory, so require both to be
        gone before reaping."""
        sid, _ = ensure_artifact_source(kstore)
        art = art_store.create(name="Doc", content="restoring body", kind="markdown")
        await reconcile_artifacts(pipeline, art_store, sid, DEFAULT_KINDS)
        assert (art_store.root / art.slug).exists()

        def _no_meta(slug, *a, **kw):
            raise ArtifactNotFoundError(slug)

        # `list` omits it (unreadable meta) and `get` raises -- but the directory
        # is still on disk, so this is a restore window, not a deletion.
        monkeypatch.setattr(art_store, "list", lambda *a, **kw: [])
        monkeypatch.setattr(art_store, "get", _no_meta)
        ingested, removed, deferred = await reconcile_artifacts(
            pipeline, art_store, sid, DEFAULT_KINDS)
        assert removed == 0
        assert any("restoring body" in c for c in _contents(kstore, sid)), (
            "a mid-restore window must not delete the indexed group")

    @pytest.mark.asyncio
    async def test_kind_drift_is_reaped_even_with_an_empty_allowlist(
        self, pipeline, art_store, kstore
    ):
        """Same rule as deletions: an empty allowlist means "ingest nothing",
        not "let chunks from a kind that no longer applies stay live"."""
        sid, _ = ensure_artifact_source(kstore)
        art = art_store.create(name="Doc", content="old body", kind="markdown")
        await reconcile_artifacts(pipeline, art_store, sid, DEFAULT_KINDS)
        art_store.update(art.slug, kind="svg")
        assert await reconcile_artifacts(pipeline, art_store, sid, set()) == (0, 1, 0)
        assert _contents(kstore, sid) == []

    @pytest.mark.asyncio
    async def test_a_rename_while_off_refreshes_the_group_label(
        self, pipeline, art_store, kstore
    ):
        """A rename during the off-window never reached the Library. Reconcile
        must refresh the stored label without re-ingesting (content unchanged,
        so no budget spent and items untouched)."""
        sid, _ = ensure_artifact_source(kstore)
        art = art_store.create(name="Before", content="stable body", kind="markdown")
        await reconcile_artifacts(pipeline, art_store, sid, DEFAULT_KINDS)
        art_store.update(art.slug, name="After")  # no listener registered
        item_ids_before = {r["id"] for r in kstore.db.execute(
            "SELECT id FROM items WHERE source_id = ?", (sid,)).fetchall()}
        ingested, removed, deferred = await reconcile_artifacts(
            pipeline, art_store, sid, DEFAULT_KINDS)
        assert (ingested, removed, deferred) == (0, 0, 0)
        row = kstore.db.execute(
            "SELECT name FROM artifact_item_state WHERE source_id = ? AND slug = ?",
            (sid, art.slug)).fetchone()
        assert row["name"] == "After"
        item_ids_after = {r["id"] for r in kstore.db.execute(
            "SELECT id FROM items WHERE source_id = ?", (sid,)).fetchall()}
        assert item_ids_before == item_ids_after

    @pytest.mark.asyncio
    async def test_budget_defers_the_remainder_and_a_later_run_finishes_it(
        self, pipeline, art_store, kstore
    ):
        sid, _ = ensure_artifact_source(kstore)
        for i in range(3):
            art_store.create(name=f"Doc{i}", content=f"body {i}", kind="markdown")
        ingested, removed, deferred = await reconcile_artifacts(
            pipeline, art_store, sid, DEFAULT_KINDS, budget=2)
        assert (ingested, removed, deferred) == (2, 0, 1)
        # The next start picks up what was deferred; nothing is lost.
        ingested, removed, deferred = await reconcile_artifacts(
            pipeline, art_store, sid, DEFAULT_KINDS, budget=2)
        assert (ingested, removed, deferred) == (1, 0, 0)
        assert len(_contents(kstore, sid)) == 3

    @pytest.mark.asyncio
    async def test_reconcile_empty_kinds_still_drops_deleted_state(
        self, pipeline, art_store, kstore
    ):
        """An empty allowlist means "ingest nothing", not "let deleted content
        stay searchable" -- an early return before the removal loop left a
        deleted artifact's text answering searches forever."""
        sid, _ = ensure_artifact_source(kstore)
        art = art_store.create(name="MD", content="body", kind="markdown")
        await reconcile_artifacts(pipeline, art_store, sid, DEFAULT_KINDS)
        assert _contents(kstore, sid) != []
        art_store.delete(art.slug)
        assert await reconcile_artifacts(pipeline, art_store, sid, set()) == (0, 1, 0)
        assert _contents(kstore, sid) == []

    @pytest.mark.asyncio
    async def test_an_unreadable_artifact_is_not_treated_as_deleted(
        self, pipeline, art_store, kstore, monkeypatch
    ):
        """``ArtifactStore.list`` omits an artifact whose meta.json cannot be
        read, so absence from the listing is not proof of deletion. Reaping on
        the listing alone destroys a live artifact's indexed content."""
        sid, _ = ensure_artifact_source(kstore)
        art = art_store.create(name="MD", content="still here", kind="markdown")
        await reconcile_artifacts(pipeline, art_store, sid, DEFAULT_KINDS)

        # The artifact exists but is invisible to list() and unreadable by get().
        monkeypatch.setattr(art_store, "list", lambda *a, **k: [])

        def _unreadable(slug, *a, **k):
            raise OSError("meta.json is briefly unreadable")

        monkeypatch.setattr(art_store, "get", _unreadable)
        ingested, removed, deferred = await reconcile_artifacts(
            pipeline, art_store, sid, DEFAULT_KINDS)
        assert removed == 0, "an unreadable artifact must not be reaped"
        assert any("still here" in c for c in _contents(kstore, sid))
        assert art.slug  # the artifact was never deleted from the store

    @pytest.mark.asyncio
    async def test_removals_run_off_the_event_loop(self, pipeline, art_store, kstore):
        """``remove_artifact`` -> ``delete_items_batch`` -> ``store._load_graph``
        is a full graph rebuild inside a SQLite transaction. Once per slug
        deleted during a long off-window, on the loop, is the wedge
        ``no-blocking-call-on-event-loop`` guards (see #2175 / #2336). Asserts
        the THREAD, so keeping the call but dropping the ``to_thread`` hop fails.
        """
        sid, _ = ensure_artifact_source(kstore)
        art = art_store.create(name="Doomed", content="doomed body", kind="markdown")
        await reconcile_artifacts(pipeline, art_store, sid, DEFAULT_KINDS)
        art_store.delete(art.slug)

        seen_threads: list[int] = []
        real = artifact_ingest.remove_artifact

        def recording(*args, **kwargs):
            seen_threads.append(threading.get_ident())
            return real(*args, **kwargs)

        with mock.patch.object(artifact_ingest, "remove_artifact", recording):
            removed = (await reconcile_artifacts(
                pipeline, art_store, sid, DEFAULT_KINDS))[1]

        assert removed == 1
        assert seen_threads, (
            "remove_artifact was never called -- this test no longer exercises "
            "the removal path and would pass vacuously")
        assert threading.get_ident() not in seen_threads, (
            "remove_artifact ran on the event-loop thread; it must be handed to "
            "asyncio.to_thread")

    @pytest.mark.asyncio
    async def test_an_emptied_artifact_drops_its_indexed_group(
        self, pipeline, art_store, kstore
    ):
        """Emptying an artifact must not leave its previous text searchable.

        ``ingest_artifact`` early-returns on empty content, so without an
        explicit drop the obsolete chunks answer searches forever -- reachable
        both live and via reconcile after an off-window edit.
        """
        sid, _ = ensure_artifact_source(kstore)
        art = art_store.create(name="Doc", content="original body", kind="markdown")
        await reconcile_artifacts(pipeline, art_store, sid, DEFAULT_KINDS)
        assert any("original body" in c for c in _contents(kstore, sid))
        art_store.update(art.slug, content="   ")  # emptied while sync was off
        await reconcile_artifacts(pipeline, art_store, sid, DEFAULT_KINDS)
        assert _contents(kstore, sid) == [], "stale chunks survived an emptied artifact"

    @pytest.mark.asyncio
    async def test_an_unreadable_live_source_does_not_drop_the_group(
        self, pipeline, art_store, kstore, monkeypatch
    ):
        """A file-backed artifact whose live source cannot be read falls back to
        its snapshot with ``source_missing=True``. If the snapshot is also blank,
        "empty" says nothing about the real content -- dropping the group would
        destroy a valid index over a moved file or a transient read failure.
        Same rule as the deletion reap: only provable absence deletes."""
        sid, _ = ensure_artifact_source(kstore)
        art = art_store.create(name="Doc", content="indexed body", kind="markdown")
        await reconcile_artifacts(pipeline, art_store, sid, DEFAULT_KINDS)
        assert any("indexed body" in c for c in _contents(kstore, sid))

        real_get = art_store.get

        def _dead_pointer(slug, *a, **kw):
            got = real_get(slug, *a, **kw)
            got.content = ""
            got.source_path = "/nonexistent/moved.md"
            got.source_missing = True
            return got

        monkeypatch.setattr(art_store, "get", _dead_pointer)
        assert await ingest_artifact(
            pipeline, art_store, art.slug, sid, DEFAULT_KINDS) is None
        assert any("indexed body" in c for c in _contents(kstore, sid)), (
            "a dead source pointer must not delete the indexed group")

    @pytest.mark.asyncio
    async def test_a_duplicate_refusal_does_not_spend_the_budget(
        self, pipeline, art_store, kstore, monkeypatch
    ):
        """A write the pre-ingest hash gate refuses returns a job id but does no
        extraction, and re-attempts on every start. Counting it would let a
        budget's worth of duplicates starve every other artifact forever."""
        sid, _ = ensure_artifact_source(kstore)
        for i in range(3):
            art_store.create(name=f"Dup{i}", content=f"body {i}", kind="markdown")

        real_status = pipeline.get_job_status

        def _always_duplicate(job_id):
            out = dict(real_status(job_id) or {})
            out["status"] = DUPLICATE_JOB_STATUS
            return out

        monkeypatch.setattr(pipeline, "get_job_status", _always_duplicate)
        ingested, removed, deferred = await reconcile_artifacts(
            pipeline, art_store, sid, DEFAULT_KINDS, budget=1)
        assert (ingested, deferred) == (0, 0), (
            "a duplicate refusal consumed the ingest budget, so artifacts behind "
            "it would be deferred on every start")


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
    async def test_start_reconciles_when_source_created(self, pipeline, art_store, kstore):
        art_store.create(name="Pre", content="preexisting body", kind="markdown")
        sync = ArtifactKnowledgeSync(
            art_store=art_store, pipeline=pipeline, kinds=DEFAULT_KINDS,
            loop=asyncio.get_running_loop())
        await sync.start()
        # start() schedules the reconcile as a background task; await it.
        assert sync._reconcile_task is not None
        await sync._reconcile_task
        sid = kstore.get_source_by_uri(ARTIFACT_SOURCE_URI)["id"]
        assert any("preexisting body" in c for c in _contents(kstore, sid))

    @pytest.mark.asyncio
    async def test_start_still_reconciles_when_the_source_row_already_exists(
        self, pipeline, art_store, kstore
    ):
        """The regression this fix exists for.

        On any install that ever had auto-ingest on, the aggregate source row
        already exists. Gating the catch-up pass on ``created`` therefore made a
        later opt-in a no-op and the drift permanent. start() must reconcile
        regardless of whether it inserted the row.
        """
        sid, created = ensure_artifact_source(kstore)  # pre-create, as an upgrade has
        assert created is True
        # Content that appeared while the feature was off, so no listener saw it.
        art_store.create(name="Missed", content="arrived while off", kind="markdown")
        sync = ArtifactKnowledgeSync(
            art_store=art_store, pipeline=pipeline, kinds=DEFAULT_KINDS,
            loop=asyncio.get_running_loop())
        await sync.start()
        assert sync._reconcile_task is not None, (
            "start() skipped the reconcile because the source row already existed")
        await sync._reconcile_task
        assert any("arrived while off" in c for c in _contents(kstore, sid))


class TestKnowledgeConfigDefaults:
    def test_auto_ingest_defaults_off(self):
        # Opt-in: the dataclass default and the loader must agree, or the
        # dashboard toggle and the running gateway disagree about the state.
        from kiro_crew.config.loader import KiroCrewConfig, KnowledgeConfig
        kc = KnowledgeConfig()
        assert kc.auto_ingest_artifacts is False
        assert kc.auto_ingest_artifact_kinds == ["markdown", "text", "html", "json"]
        assert KiroCrewConfig().knowledge.auto_ingest_artifacts is False


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
    reconcile sweep, but wrong for a *change*: an artifact ingested as markdown and
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
