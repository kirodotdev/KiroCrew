"""Tests for the agent's write path into the Knowledge Library.

Covers the aggregate ``agent://`` source, the pre-ingest duplicate gate that
covers every write path, and the per-sweep chunk budget.
"""

from __future__ import annotations

import json
import os
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.knowledge import agent_source as agent_source_mod
from kiro_crew.knowledge.agent_source import (
    AGENT_SOURCE_TYPE,
    AGENT_SOURCE_URI,
    add_agent_document,
    document_slug,
    ensure_agent_source,
    get_state,
    remove_document,
    set_state,
)
from kiro_crew.knowledge.folder_watcher import FolderWatcher
from kiro_crew.knowledge.ingestion import DUPLICATE_JOB_STATUS, IngestionPipeline
from kiro_crew.knowledge.readers import FileReader
from kiro_crew.knowledge.store import KnowledgeStore


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


def _items(kstore, source_id):
    return [r["content"] for r in kstore.db.execute(
        "SELECT content FROM items WHERE source_id = ?", (source_id,)).fetchall()]


class TestAggregateSource:
    def test_created_once_and_reused(self, kstore):
        sid, created = ensure_agent_source(kstore)
        assert created and sid
        again, created2 = ensure_agent_source(kstore)
        assert again == sid and created2 is False
        row = kstore.db.execute(
            "SELECT source_type, uri, name FROM sources WHERE id = ?", (sid,)).fetchone()
        assert row["source_type"] == AGENT_SOURCE_TYPE
        assert row["uri"] == AGENT_SOURCE_URI
        assert row["name"] == "Auto-added"

    def test_deleting_it_clears_contents_without_disabling_the_feature(self, kstore):
        # Deleting the container must NOT become a second, hidden, permanent off
        # switch: the feature already has a visible toggle. A tombstone here left
        # the user with no way back while that toggle still read on.
        sid, _ = ensure_agent_source(kstore)
        kstore.delete_source_cascade(sid, dismiss_uri=AGENT_SOURCE_URI)
        again, created = ensure_agent_source(kstore)
        assert created is True and again

    def test_empty_source_survives_a_reopen(self, kstore, tmp_path):
        # The orphan sweep on store construction deletes item-less sources; the
        # aggregate must be exempt via its item-state table or the very first
        # document lands in a source that was reaped at boot.
        sid, _ = ensure_agent_source(kstore)
        set_state(kstore, sid, "slug1", "H1", [], "Doc")
        kstore.close()
        reopened = KnowledgeStore(str(tmp_path / "knowledge.db"))
        try:
            assert reopened.db.execute(
                "SELECT COUNT(*) FROM sources WHERE uri = ?",
                (AGENT_SOURCE_URI,)).fetchone()[0] == 1
        finally:
            reopened.close()


class TestSlug:
    def test_stable_for_the_same_identity(self):
        assert document_slug("Design Doc") == document_slug("Design Doc")

    def test_independent_of_content(self):
        # Content-derived slugs would accumulate a new group per edit instead of
        # replacing the document's existing one.
        assert document_slug("Doc") != document_slug("Other")


class TestState:
    def test_roundtrip(self, kstore):
        sid, _ = ensure_agent_source(kstore)
        set_state(kstore, sid, "s", "H1", ["i1", "i2"], "Doc")
        assert get_state(kstore, sid, "s") == ("H1", ["i1", "i2"])

    def test_missing_is_empty(self, kstore):
        sid, _ = ensure_agent_source(kstore)
        assert get_state(kstore, sid, "nope") == (None, [])

    def test_status_is_written_explicitly_on_every_upsert(self, kstore):
        # INSERT OR REPLACE would otherwise reset a 'deduped' marker to the
        # column default, and the document would be re-ingested and re-collapsed
        # on every pass.
        sid, _ = ensure_agent_source(kstore)
        set_state(kstore, sid, "s", "H1", ["i1"], "Doc", status="deduped")
        assert kstore.db.execute(
            "SELECT status FROM agent_item_state WHERE slug = 's'").fetchone()[0] == "deduped"

    def test_remove_drops_items_and_state(self, kstore):
        sid, _ = ensure_agent_source(kstore)
        iid = kstore.add_item(title="t", content="c", item_type="document",
                              source_id=sid, content_hash="H1")
        set_state(kstore, sid, "s", "H1", [iid], "Doc")
        assert remove_document(kstore, sid, "s") == 1
        assert get_state(kstore, sid, "s") == (None, [])
        assert _items(kstore, sid) == []

    def test_cascade_clears_agent_state(self, kstore):
        sid, _ = ensure_agent_source(kstore)
        set_state(kstore, sid, "s", "H1", ["i1"], "Doc")
        kstore.delete_source_cascade(sid)
        assert kstore.db.execute(
            "SELECT COUNT(*) FROM agent_item_state").fetchone()[0] == 0


class TestAddDocument:
    @pytest.mark.asyncio
    async def test_inline_content_is_added_and_retrievable(self, pipeline, kstore):
        res = await add_agent_document(pipeline, title="Design Doc",
                                       content="the interesting design body", source_uri="test://Design Doc")
        assert res["status"] == "added"
        assert res["items"] == 1
        assert "the interesting design body" in _items(kstore, res["source_id"])

    @pytest.mark.asyncio
    async def test_second_identical_add_ingests_once(self, pipeline, kstore):
        first = await add_agent_document(pipeline, title="Doc", content="same body", source_uri="test://Doc")
        second = await add_agent_document(pipeline, title="Doc", content="same body", source_uri="test://Doc")
        assert first["status"] == "added"
        assert second["status"] == "duplicate"
        assert len(_items(kstore, first["source_id"])) == 1

    @pytest.mark.asyncio
    async def test_edit_replaces_the_group_not_appends(self, pipeline, kstore):
        first = await add_agent_document(pipeline, title="Doc", content="v1 body", source_uri="test://Doc")
        await add_agent_document(pipeline, title="Doc", content="v2 body", source_uri="test://Doc")
        contents = _items(kstore, first["source_id"])
        assert contents == ["v2 body"]

    @pytest.mark.asyncio
    async def test_two_documents_coexist_in_the_aggregate(self, pipeline, kstore):
        a = await add_agent_document(pipeline, title="A", content="body a", source_uri="test://A")
        await add_agent_document(pipeline, title="B", content="body b", source_uri="test://B")
        assert sorted(_items(kstore, a["source_id"])) == ["body a", "body b"]

    @pytest.mark.asyncio
    async def test_there_is_no_path_parameter(self, pipeline, tmp_path):
        # A path would have to be opened here on behalf of whatever supplied it,
        # and an agent-supplied path is exactly where a component can be swapped
        # for a link to a credential file between the check and the open. The
        # agent reads the file with its own tools and passes the text instead.
        import inspect
        sig = inspect.signature(add_agent_document)
        assert "path" not in sig.parameters
        assert "content" in sig.parameters

    @pytest.mark.asyncio
    async def test_content_is_required(self, pipeline):
        res = await add_agent_document(pipeline, title="Doc", content="", source_uri="test://Doc")
        assert res["status"] == "error"
        assert "content is required" in res["error"]

    @pytest.mark.asyncio
    async def test_title_required(self, pipeline):
        assert (await add_agent_document(pipeline, title="  ", content="b", source_uri="test://"))["status"] == "error"

    @pytest.mark.asyncio
    async def test_missing_content_is_an_error(self, pipeline):
        assert (await add_agent_document(pipeline, title="X", content="", source_uri="test://X"))["status"] == "error"

    @pytest.mark.asyncio
    async def test_empty_document_refused(self, pipeline):
        assert (await add_agent_document(pipeline, title="X", content="   ", source_uri="test://X"))["status"] == "error"

    @pytest.mark.asyncio
    async def test_credentials_are_redacted_before_storage(self, pipeline, kstore):
        secret = "AKIAIOSFODNN7EXAMPLE"
        res = await add_agent_document(
            pipeline, title="Runbook", content=f"use key {secret} to deploy", source_uri="test://Runbook")
        stored = " ".join(_items(kstore, res["source_id"]))
        assert secret not in stored

    @pytest.mark.asyncio
    async def test_add_still_works_after_the_source_was_deleted(self, pipeline, kstore):
        # Deleting the source clears its contents; the visible
        # knowledge.auto_add_documents toggle is the only off switch, so a later
        # add recreates the row and lands.
        sid, _ = ensure_agent_source(kstore)
        kstore.delete_source_cascade(sid, dismiss_uri=AGENT_SOURCE_URI)
        res = await add_agent_document(pipeline, title="Doc", content="body", source_uri="test://Doc")
        assert res["status"] == "added"

    @pytest.mark.asyncio
    async def test_audit_fields_are_redacted(self, pipeline, monkeypatch):
        # SEL is a persisted, readable surface, so a credential in an
        # agent-authored title or reason must not reach it. `reason` never passes
        # through the content redaction at all.
        secret = "AKIAIOSFODNN7EXAMPLE"
        spy = MagicMock()
        monkeypatch.setattr(agent_source_mod, "sel", lambda: spy)
        res = await add_agent_document(
            pipeline, title=f"Runbook {secret}", content="body text",
            reason=f"needed for {secret}", source_uri="test://doc")
        assert res["status"] == "added"
        logged = " ".join(
            str(c.kwargs) for c in spy.log_tool_invocation.call_args_list)
        assert secret not in logged, "credential reached the audit trail"


class TestPreIngestDuplicateGate:
    """No duplicate is WRITTEN -- on every path, not just the agent's."""

    @pytest.mark.asyncio
    async def test_agent_add_refused_when_content_exists_elsewhere(self, pipeline, kstore):
        other = kstore.add_source(name="Up", source_type="local_file",
                                  uri="upload://n.md")
        await pipeline.ingest_text("shared body", title="n.md", source_id=other)
        res = await add_agent_document(pipeline, title="Copy", content="shared body", source_uri="test://Copy")
        assert res["status"] == "duplicate"

    @pytest.mark.asyncio
    async def test_upload_refused_when_content_exists_elsewhere(self, pipeline, kstore, tmp_path):
        first = kstore.add_source(name="A", source_type="local_file", uri="upload://a.md")
        await pipeline.ingest_text("shared body", title="a.md", source_id=first)
        second = kstore.add_source(name="B", source_type="local_file", uri="upload://b.md")
        f = tmp_path / "b.md"
        f.write_text("shared body")
        job = await pipeline.ingest_file(str(f), source_id=second)
        assert pipeline.get_job_status(job)["status"] == DUPLICATE_JOB_STATUS
        assert _items(kstore, second) == []

    @pytest.mark.asyncio
    async def test_re_ingest_into_the_same_source_still_proceeds(self, pipeline, kstore):
        # Same content, same source, is a replacement -- not a duplicate.
        sid = kstore.add_source(name="A", source_type="local_file", uri="upload://a.md")
        await pipeline.ingest_text("body", title="a.md", source_id=sid)
        await pipeline.ingest_text("body", title="a.md", source_id=sid)
        assert len(_items(kstore, sid)) == 1

    @pytest.mark.asyncio
    async def test_folder_scan_marks_the_file_deduped(self, pipeline, kstore, tmp_path):
        # The holder is an obsidian_vault: EQUAL rank to the incoming folder, which
        # is what makes the gate refuse. A transient holder (an upload) is outranked
        # by a persistent source and allowed to land, so an upload here would stop
        # exercising the refusal path this test pins.
        other = kstore.add_source(name="Vault", source_type="obsidian_vault",
                                  uri="vault://x")
        await pipeline.ingest_text("folder body", title="x.md", source_id=other)
        folder = tmp_path / "docs"
        folder.mkdir()
        (folder / "x.md").write_text("folder body")
        sid = kstore.add_source(name="F", source_type="local_folder", uri=str(folder))
        fw = FolderWatcher(kstore, pipeline)
        stats = await fw.scan_source({"id": sid, "uri": str(folder),
                                      "source_type": "local_folder", "properties": "{}"})
        assert stats["skipped"] == 1
        row = kstore.db.execute(
            "SELECT status, item_ids FROM folder_file_state WHERE source_id = ?",
            (sid,)).fetchone()
        assert row["status"] == "deduped"
        assert row["item_ids"] == "[]"

    @pytest.mark.asyncio
    async def test_a_refused_write_does_not_leave_the_superseded_items_behind(
            self, pipeline, kstore, tmp_path):
        # A folder file whose content CHANGES to something already stored
        # elsewhere. Refusing the write is not the same as doing nothing: the
        # file's previous items are now superseded. Leaving them would keep the
        # old text searchable, and because the state row is recorded with an
        # empty group they would never be reclaimed when the file is deleted.
        folder = tmp_path / "docs"
        folder.mkdir()
        f = folder / "x.md"
        f.write_text("original body")
        sid = kstore.add_source(name="F", source_type="local_folder", uri=str(folder))
        source = {"id": sid, "uri": str(folder), "source_type": "local_folder",
                  "properties": "{}"}
        fw = FolderWatcher(kstore, pipeline)
        await fw.scan_source(source)
        assert _items(kstore, sid) == ["original body"]

        # Another source now holds what this file is about to become.
        # Equal-rank holder, for the reason given in the test above.
        other = kstore.add_source(name="Vault", source_type="obsidian_vault",
                                  uri="vault://y")
        await pipeline.ingest_text("replacement body", title="y.md", source_id=other)

        f.write_text("replacement body")
        os.utime(f, (time.time() + 10, time.time() + 10))
        await fw.scan_source(source)

        # No stale copy of the old text survives anywhere in the folder source.
        assert _items(kstore, sid) == []
        row = kstore.db.execute(
            "SELECT status, item_ids FROM folder_file_state WHERE source_id = ?",
            (sid,)).fetchone()
        assert row["status"] == "deduped"
        assert row["item_ids"] == "[]"

    @pytest.mark.asyncio
    async def test_a_refused_agent_add_records_state_instead_of_a_dead_group(
            self, pipeline, kstore):
        # Same defect on the aggregate path: the gate deleted the document's old
        # items, so the state row must not keep pointing at them.
        first = await add_agent_document(pipeline, title="Doc", content="v1 body", source_uri="test://Doc")
        other = kstore.add_source(name="Up", source_type="local_file",
                                  uri="upload://v2.md")
        await pipeline.ingest_text("v2 body", title="v2.md", source_id=other)

        res = await add_agent_document(pipeline, title="Doc", content="v2 body", source_uri="test://Doc")
        assert res["status"] == "duplicate"
        row = kstore.db.execute(
            "SELECT status, item_ids FROM agent_item_state WHERE slug = ?",
            (res["slug"],)).fetchone()
        assert row["status"] == "deduped"
        assert row["item_ids"] == "[]"
        assert _items(kstore, first["source_id"]) == []

    def test_lookup_excludes_the_named_source(self, kstore):
        sid = kstore.add_source(name="A", source_type="local_file", uri="upload://a.md")
        kstore.add_item(title="t", content="c", item_type="document",
                        source_id=sid, content_hash="H1")
        assert kstore.find_doc_by_content_hash("H1")["source_id"] == sid
        assert kstore.find_doc_by_content_hash("H1", exclude_source_id=sid) is None
        assert kstore.find_doc_by_content_hash("") is None
        assert kstore.find_doc_by_content_hash("nope") is None


class TestContentHashWritePath:
    """Every ingest path must stamp items.content_hash, or exact dedup goes blind."""

    @pytest.mark.asyncio
    async def test_ingest_text_stamps_every_item(self, pipeline, kstore):
        sid = kstore.add_source(name="A", source_type="local_file", uri="upload://a.md")
        await pipeline.ingest_text("body text", title="a.md", source_id=sid)
        hashes = [r["content_hash"] for r in kstore.db.execute(
            "SELECT content_hash FROM items WHERE source_id = ?", (sid,)).fetchall()]
        assert hashes and all(hashes)

    @pytest.mark.asyncio
    async def test_ingest_file_stamps_every_item(self, pipeline, kstore, tmp_path):
        f = tmp_path / "a.md"
        f.write_text("file body")
        sid = kstore.add_source(name="A", source_type="local_file", uri="upload://a.md")
        await pipeline.ingest_file(str(f), source_id=sid)
        hashes = [r["content_hash"] for r in kstore.db.execute(
            "SELECT content_hash FROM items WHERE source_id = ?", (sid,)).fetchall()]
        assert hashes and all(hashes)

    @pytest.mark.asyncio
    async def test_agent_add_stamps_every_item(self, pipeline, kstore):
        res = await add_agent_document(pipeline, title="Doc", content="agent body", source_uri="test://Doc")
        hashes = [r["content_hash"] for r in kstore.db.execute(
            "SELECT content_hash FROM items WHERE source_id = ?",
            (res["source_id"],)).fetchall()]
        assert hashes and all(hashes)


class TestChunkBudget:
    @pytest.mark.asyncio
    async def test_sweep_stops_at_the_budget_and_resumes_later(self, kstore, tmp_path):
        folder = tmp_path / "docs"
        folder.mkdir()
        for i in range(5):
            (folder / f"d{i}.md").write_text(f"body {i}")
        sid = kstore.add_source(name="F", source_type="local_folder", uri=str(folder))
        source = {"id": sid, "uri": str(folder), "source_type": "local_folder",
                  "properties": "{}"}

        pipe = MagicMock()
        pipe._dedup_enabled = False
        fw = FolderWatcher(kstore, pipe)
        calls: list[str] = []

        async def _ingest(file_path, source_id, namespace, props, old_ids, root: str = ""):
            calls.append(file_path)
            return ["i-" + file_path, "j-" + file_path], "done"  # 2 chunks per file

        fw._ingest_file = _ingest  # type: ignore[assignment]

        first = await fw.scan_source(source, chunk_budget=4)
        assert first["budget_reached"] == 1
        assert first["chunks"] == 4
        assert len(calls) == 2  # 2 files x 2 chunks

        # Remaining files complete on later sweeps, none lost.
        calls.clear()
        await fw.scan_source(source, chunk_budget=4)
        assert len(calls) == 2
        calls.clear()
        last = await fw.scan_source(source, chunk_budget=4)
        assert len(calls) == 1
        assert "budget_reached" not in last
        done = kstore.db.execute(
            "SELECT COUNT(*) FROM folder_file_state WHERE source_id = ? AND status = 'done'",
            (sid,)).fetchone()[0]
        assert done == 5

    @pytest.mark.asyncio
    async def test_no_budget_ingests_everything(self, kstore, tmp_path):
        folder = tmp_path / "docs"
        folder.mkdir()
        for i in range(4):
            (folder / f"d{i}.md").write_text(f"body {i}")
        sid = kstore.add_source(name="F", source_type="local_folder", uri=str(folder))
        pipe = MagicMock()
        pipe._dedup_enabled = False
        fw = FolderWatcher(kstore, pipe)
        calls: list[str] = []

        async def _ingest(file_path, source_id, namespace, props, old_ids, root: str = ""):
            calls.append(file_path)
            return ["i"], "done"

        fw._ingest_file = _ingest  # type: ignore[assignment]
        stats = await fw.scan_source({"id": sid, "uri": str(folder),
                                      "source_type": "local_folder", "properties": "{}"})
        assert len(calls) == 4
        assert "budget_reached" not in stats
        assert stats["new"] == 4


class TestIncludeExtensions:
    def test_none_preserves_todays_behaviour(self, tmp_path):
        (tmp_path / "a.md").write_text("x")
        (tmp_path / "b.py").write_text("x")
        fw = FolderWatcher(store=None, pipeline=None)
        got = {os.path.basename(p) for p, _ in fw._walk(str(tmp_path), [], set(), None)}
        assert got == {"a.md", "b.py"}

    def test_a_set_restricts(self, tmp_path):
        (tmp_path / "a.md").write_text("x")
        (tmp_path / "b.py").write_text("x")
        fw = FolderWatcher(store=None, pipeline=None)
        got = {os.path.basename(p)
               for p, _ in fw._walk(str(tmp_path), [], set(), {".md"})}
        assert got == {"a.md"}

    def test_empty_set_takes_nothing(self, tmp_path):
        (tmp_path / "a.md").write_text("x")
        fw = FolderWatcher(store=None, pipeline=None)
        assert fw._walk(str(tmp_path), [], set(), set()) == []

    def test_cannot_widen_past_reader_support(self, tmp_path):
        (tmp_path / "a.xyz").write_text("x")
        fw = FolderWatcher(store=None, pipeline=None)
        assert fw._walk(str(tmp_path), [], set(), {".xyz"}) == []

    def test_min_size_drops_small_files(self, tmp_path):
        (tmp_path / "small.md").write_text("x")
        (tmp_path / "big.md").write_text("x" * 100)
        fw = FolderWatcher(store=None, pipeline=None)
        got = {os.path.basename(p)
               for p, _ in fw._walk(str(tmp_path), [], set(), None, 50)}
        assert got == {"big.md"}

    def test_properties_are_coerced_not_trusted(self, kstore, tmp_path):
        # Source properties are user-editable JSON, so a bad value must degrade
        # rather than raise mid-scan.
        from kiro_crew.knowledge.folder_watcher import (
            _prop_extensions,
            _prop_int,
            _prop_str_set,
        )
        assert _prop_extensions("nonsense") is None
        assert _prop_extensions(["md", ".PDF", 7, ""]) == {".md", ".pdf"}
        assert _prop_int("x") == 0 and _prop_int(-3) == 0 and _prop_int(True) == 0
        assert _prop_str_set(None) == set()
        assert _prop_str_set([" a ", "", 3]) == {"a"}


class TestAgentSourceIsDeduped:
    def test_agent_documents_participate_in_dedup(self, kstore):
        # Every source participates in dedup; the unit is the document, so an
        # aggregate's documents are de-duplicated individually.
        from kiro_crew.knowledge.dedup import enumerate_docs
        sid, _ = ensure_agent_source(kstore)
        a = kstore.add_item(title="a", content="a", item_type="document",
                            source_id=sid, content_hash="H1")
        b = kstore.add_item(title="b", content="b", item_type="document",
                            source_id=sid, content_hash="H2")
        set_state(kstore, sid, "sa", "H1", [a], "Doc A")
        set_state(kstore, sid, "sb", "H2", [b], "Doc B")
        docs = enumerate_docs(kstore)
        agent_docs = [d for d in docs if d.source_id == sid]
        assert len(agent_docs) == 2
        # Each carries its OWN name, not the aggregate source's, so the fuzzy
        # tier's filename gate compares documents rather than the container.
        assert {d.filename for d in agent_docs} == {"Doc A", "Doc B"}
        # And distinct identities, so removing one cannot mark the other removed.
        assert len({d.key for d in agent_docs}) == 2

    def test_state_item_ids_are_json_lists(self, kstore):
        sid, _ = ensure_agent_source(kstore)
        set_state(kstore, sid, "s", "H1", ["i1"], "Doc")
        raw = kstore.db.execute(
            "SELECT item_ids FROM agent_item_state WHERE slug = 's'").fetchone()[0]
        assert json.loads(raw) == ["i1"]


@pytest.mark.asyncio
async def test_same_title_different_uri_are_separate_documents(pipeline, kstore):
    """Two unrelated documents sharing a title must both survive.

    Keying the item group on the title alone made the second add a REPLACE of the
    first: same key -> same state row -> the first document's items deleted.
    """
    first = await add_agent_document(
        pipeline, title="README", content="Alpha service overview. " * 40,
        source_uri="/repo/alpha/README.md")
    second = await add_agent_document(
        pipeline, title="README", content="Beta service overview. " * 40,
        source_uri="/repo/beta/README.md")

    assert first["status"] == "added", first
    assert second["status"] == "added", second
    assert first["slug"] != second["slug"]

    # Both groups still hold their own items.
    _h1, ids1 = get_state(kstore, first["source_id"], first["slug"])
    _h2, ids2 = get_state(kstore, second["source_id"], second["slug"])
    assert ids1 and ids2
    assert not set(ids1) & set(ids2)
    live = {r["id"] for r in kstore.db.execute(
        "SELECT id FROM items WHERE source_id = ?", (first["source_id"],)).fetchall()}
    assert set(ids1) <= live, "first document's items were deleted by the second add"
    assert set(ids2) <= live


@pytest.mark.asyncio
async def test_same_uri_replaces_the_document(pipeline, kstore):
    """Re-adding the same source_uri still REPLACES, so edits do not accumulate."""
    first = await add_agent_document(
        pipeline, title="Design", content="Version one. " * 40,
        source_uri="/repo/design.md")
    second = await add_agent_document(
        pipeline, title="Design (revised)", content="Version two. " * 40,
        source_uri="/repo/design.md")

    assert first["slug"] == second["slug"], "uri identity must survive a retitle"
    _h, ids = get_state(kstore, second["source_id"], second["slug"])
    live = {r["id"] for r in kstore.db.execute(
        "SELECT id FROM items WHERE source_id = ?", (second["source_id"],)).fetchall()}
    assert live == set(ids), "superseded items should be gone, not accumulated"


def test_document_slug_keys_on_uri_not_title():
    assert document_slug("/a/README.md") != document_slug("/b/README.md")
    assert document_slug("/a/README.md") == document_slug("/a/README.md")


@pytest.mark.asyncio
async def test_folder_scan_lands_when_only_a_transient_source_holds_the_content(
        pipeline, kstore, tmp_path):
    """The persistent copy must win, so the folder file is ingested, not skipped.

    Refusing it left the only searchable copy inside a one-shot upload; deleting
    that upload then left none, because the folder file was marked ``deduped``.
    """
    upload = kstore.add_source(name="dropped.md", source_type="local_file",
                               uri="upload://dropped.md")
    await pipeline.ingest_text("shared body", title="dropped.md", source_id=upload)

    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "dropped.md").write_text("shared body")
    sid = kstore.add_source(name="F", source_type="local_folder", uri=str(folder))
    fw = FolderWatcher(kstore, pipeline)
    stats = await fw.scan_source({"id": sid, "uri": str(folder),
                                  "source_type": "local_folder", "properties": "{}"})

    assert stats["skipped"] == 0, "persistent folder copy should not be refused"
    assert _items(kstore, sid) == ["shared body"]
    row = kstore.db.execute(
        "SELECT status FROM folder_file_state WHERE source_id = ?", (sid,)).fetchone()
    assert row["status"] == "done"


@pytest.mark.asyncio
async def test_redaction_cannot_collapse_two_uris_into_one_document(pipeline, kstore):
    """Two URIs differing only in a credential-shaped segment stay separate.

    Redaction is lossy: both of these become "...?key=[REDACTED: credential]".
    Hashing the redacted form gave them one slug, so the second add deleted the
    first document's items. The identity is hashed from the raw URI instead.
    """
    u1 = "https://wiki.example.com/doc?key=AKIAIOSFODNN7EXAMPLE"
    u2 = "https://wiki.example.com/doc?key=AKIAJKLMNOPQR2SAMPLE"
    first = await add_agent_document(pipeline, title="Runbook",
                                     content="Alpha runbook. " * 40, source_uri=u1)
    second = await add_agent_document(pipeline, title="Runbook",
                                      content="Beta runbook. " * 40, source_uri=u2)
    assert first["status"] == "added" and second["status"] == "added"
    assert first["slug"] != second["slug"]

    _h1, ids1 = get_state(kstore, first["source_id"], first["slug"])
    live = {r["id"] for r in kstore.db.execute(
        "SELECT id FROM items WHERE source_id = ?", (first["source_id"],)).fetchall()}
    assert ids1 and set(ids1) <= live

    # The stored/returned URI is redacted even though the identity is not.
    assert "AKIA" not in second["source_uri"]
    assert "REDACTED" in second["source_uri"]


@pytest.mark.asyncio
async def test_identical_content_under_two_uris_is_refused(pipeline, kstore):
    """The aggregate holds many documents, so the pipeline gate cannot see inside it."""
    body = "One true document. " * 40
    first = await add_agent_document(pipeline, title="A", content=body,
                                     source_uri="/repo/a.md")
    second = await add_agent_document(pipeline, title="B", content=body,
                                      source_uri="/repo/b.md")
    assert first["status"] == "added"
    assert second["status"] == "duplicate", second
    # No state row for the refused document, so nothing can strand it.
    _h, ids = get_state(kstore, first["source_id"], second["slug"])
    assert ids == []
    row = kstore.db.execute(
        "SELECT COUNT(*) c FROM agent_item_state WHERE source_id = ? AND slug = ?",
        (first["source_id"], second["slug"])).fetchone()
    assert row["c"] == 0, "a refused add must leave no row to suppress a later retry"


@pytest.mark.asyncio
async def test_a_refused_duplicate_can_be_added_after_the_holder_is_removed(
        pipeline, kstore):
    """Refusing must not permanently suppress the content."""
    body = "Recoverable document. " * 40
    first = await add_agent_document(pipeline, title="A", content=body,
                                     source_uri="/repo/a.md")
    assert (await add_agent_document(pipeline, title="B", content=body,
                                     source_uri="/repo/b.md"))["status"] == "duplicate"
    remove_document(kstore, first["source_id"], first["slug"])
    again = await add_agent_document(pipeline, title="B", content=body,
                                     source_uri="/repo/b.md")
    assert again["status"] == "added", again


@pytest.mark.asyncio
async def test_source_uri_is_required(pipeline):
    res = await add_agent_document(pipeline, title="Doc", content="body " * 40)
    assert res["status"] == "error"
    assert "source_uri" in res["error"]


@pytest.mark.asyncio
async def test_a_deduped_folder_file_is_reingested_after_it_changes(
        pipeline, kstore, tmp_path):
    """A gate refusal must not permanently remove a file from the scan.

    'deduped' records why the file has no items; it is gated on mtime like 'done',
    so an unchanged file stays cheap while an edit brings it back. Skipping the
    status unconditionally outlives the copy that caused it, so the file could
    never be indexed again -- reachable here because auto-registered project
    sources make two persistent folders holding the same document ordinary.
    """
    vault = kstore.add_source(name="Vault", source_type="obsidian_vault",
                              uri="vault://v")
    await pipeline.ingest_text("shared body", title="x.md", source_id=vault)

    folder = tmp_path / "docs"
    folder.mkdir()
    f = folder / "x.md"
    f.write_text("shared body")
    sid = kstore.add_source(name="F", source_type="local_folder", uri=str(folder))
    source = {"id": sid, "uri": str(folder), "source_type": "local_folder",
              "properties": "{}"}
    fw = FolderWatcher(kstore, pipeline)

    # Equal rank -> refused, and recorded as deduped.
    assert (await fw.scan_source(source))["skipped"] == 1
    row = kstore.db.execute(
        "SELECT status FROM folder_file_state WHERE source_id = ?", (sid,)).fetchone()
    assert row["status"] == "deduped"

    # Unchanged: still skipped, no re-ingest attempt.
    assert _items(kstore, sid) == []

    # Edited to unique content -> reconsidered and indexed.
    f.write_text("now a different document entirely")
    os.utime(f, (time.time() + 10, time.time() + 10))
    await fw.scan_source(source)
    assert _items(kstore, sid) == ["now a different document entirely"]
    row = kstore.db.execute(
        "SELECT status FROM folder_file_state WHERE source_id = ?", (sid,)).fetchone()
    assert row["status"] == "done"


@pytest.mark.asyncio
async def test_a_deduped_row_does_not_block_a_later_retry(pipeline, kstore):
    """A refused write leaves a hash with no items; that must not look 'unchanged'.

    The pipeline gate records `deduped` with the content hash and an empty group.
    Matching on hash alone reported "unchanged since last add" for a document the
    Library does not hold -- permanently, even after the copy that caused the
    refusal was deleted.
    """
    body = "Shared document body. " * 40
    other = kstore.add_source(name="Up", source_type="local_file", uri="upload://o.md")
    await pipeline.ingest_text(body, title="o.md", source_id=other)

    # Refused by the pipeline gate, recorded as deduped with an empty group.
    first = await add_agent_document(pipeline, title="Doc", content=body,
                                     source_uri="/repo/doc.md")
    assert first["status"] == "duplicate", first
    sid, _ = ensure_agent_source(kstore)
    row = kstore.db.execute(
        "SELECT status, item_ids FROM agent_item_state WHERE source_id = ? AND slug = ?",
        (sid, document_slug("/repo/doc.md"))).fetchone()
    assert row["status"] == "deduped" and row["item_ids"] == "[]"
    doc_hash = kstore.db.execute(
        "SELECT content_hash FROM agent_item_state WHERE source_id = ? AND slug = ?",
        (sid, document_slug("/repo/doc.md"))).fetchone()["content_hash"]

    # Removing the holder no longer destroys the document: the refusal recorded this
    # source as a location, so ownership MOVES here and the row adopts what it
    # inherits. "Unchanged" is then the truthful answer -- the Library does hold it.
    kstore.delete_source_cascade(other)
    row = kstore.db.execute(
        "SELECT status, item_ids FROM agent_item_state WHERE source_id = ? AND slug = ?",
        (sid, document_slug("/repo/doc.md"))).fetchone()
    assert row["status"] == "active" and row["item_ids"] != "[]", row
    again = await add_agent_document(pipeline, title="Doc", content=body,
                                     source_uri="/repo/doc.md")
    assert again["status"] == "duplicate", again

    # The guard this test exists for: when the content is GENUINELY gone, a stale hash
    # with no items must not still read as "unchanged since last add".
    kstore.detach_source_location_by_hash(sid, doc_hash)
    kstore.delete_items_batch(json.loads(row["item_ids"]))
    kstore.db.execute(
        "UPDATE agent_item_state SET status = 'deduped', item_ids = '[]' "
        "WHERE source_id = ? AND slug = ?", (sid, document_slug("/repo/doc.md")))
    kstore.db.commit()
    third = await add_agent_document(pipeline, title="Doc", content=body,
                                     source_uri="/repo/doc.md")
    assert third["status"] == "added", third
    _h, ids = get_state(kstore, sid, third["slug"])
    assert ids, "the document should hold items after the retry"


def test_removing_a_deduped_agent_document_releases_its_claim(tmp_path):
    """Same gap the artifact path had: an empty group left the claim behind."""
    from kiro_crew.knowledge.agent_source import remove_document
    from kiro_crew.knowledge.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "k.db"))
    try:
        agg = store.add_source(name="Auto-added", source_type="agent",
                               uri="agent://auto")
        folder = store.add_source(name="docs", source_type="local_folder",
                                  uri=str(tmp_path / "docs"))
        h = "7" * 64
        iid = store.add_item(title="note.md", content="body", item_type="document",
                             source_id=folder, content_hash=h)
        store.add_source_location(iid, folder)
        store.add_source_location(iid, agg)
        store.db.execute(
            "INSERT INTO agent_item_state (source_id, slug, content_hash, item_ids, "
            "updated_at, name, status) "
            "VALUES (?, 'note', ?, '[]', '2024-01-01', 'note.md', 'deduped')",
            (agg, h))
        store.db.commit()

        assert agg in store.sources_holding_item(iid)
        remove_document(store, agg, "note")

        assert agg not in store.sources_holding_item(iid)
        assert store.get_item(iid) is not None, "the folder's item is untouched"
        store.delete_source_cascade(folder)
        assert store.get_item(iid) is None
    finally:
        store.db.close()
