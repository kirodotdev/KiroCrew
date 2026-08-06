"""Regression tests for oversized-file ingest guard and off-loop chunking.

Bug: a very large Markdown file (tens of MB, e.g. a CSV->MD conversion) in a
watched Knowledge folder hung gateway startup -- the folder scan chunked it
synchronously on the asyncio event loop (chunk_markdown -> _recursive_split is
CPU-bound), tripping the 25s faulthandler loop watchdog with no user-facing
error. Fixes under test:

1. ``knowledge.max_ingest_file_mb`` size guard at ingest (clear WARNING naming
   the file + ``FileTooLargeError``), enforced BEFORE the file is read.
2. Chunking dispatched via ``asyncio.to_thread`` so it can never block the loop.
3. FolderWatcher records the oversized file as failed with the actionable
   message (visible in the dashboard) instead of a raw faulthandler dump, and
   never retry-loops on it.
4. KnowledgeWatcher single-file path marks the source errored WITHOUT
   persisting mtime/hash, so raising ``knowledge.max_ingest_file_mb`` (config
   is read live) recovers the file automatically on the next scan.
5. ``ingest_file`` refuses sensitive paths before any filesystem access
   (defense-in-depth on top of caller-side pre-filtering).
"""
from __future__ import annotations

import json
import logging
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.knowledge import ingestion as ingestion_mod
from kiro_crew.knowledge.chunker import HeadingAwareChunker
from kiro_crew.knowledge.folder_watcher import FolderWatcher
from kiro_crew.knowledge.ingestion import FileTooLargeError, IngestionPipeline
from kiro_crew.knowledge.readers import FileReader
from kiro_crew.knowledge.store import KnowledgeStore
from kiro_crew.knowledge.watcher import KnowledgeWatcher


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
    return IngestionPipeline(
        store=kstore, extractor=extractor, chunker=HeadingAwareChunker(),
        reader=FileReader(), embedder=None,
    )


def _set_limit_mb(monkeypatch, mb: float) -> None:
    monkeypatch.setattr(ingestion_mod, "_max_ingest_file_mb", lambda: mb)


class TestSizeGuard:
    @pytest.mark.asyncio
    async def test_oversized_file_raises_with_actionable_warning(
            self, pipeline, tmp_path, monkeypatch, caplog):
        big = tmp_path / "huge-export.md"
        big.write_text("# big\n" + "word " * 5000)
        _set_limit_mb(monkeypatch, 0.001)
        sel_spy = MagicMock()
        monkeypatch.setattr(ingestion_mod, "sel", lambda: sel_spy)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.knowledge.ingestion"):
            with pytest.raises(FileTooLargeError) as exc_info:
                await pipeline.ingest_file(str(big))

        # The caller who supplied the name gets it back, so the skip is actionable.
        assert "huge-export.md" in str(exc_info.value)
        assert "max_ingest_file_mb" in str(exc_info.value)

        warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        # ... but the application log must NOT carry it. A document name is
        # caller-supplied and free-form enough to carry a credential, and the log is
        # the one sink here with no redaction contract and the widest reach.
        assert warned, "an oversized skip must still be visible in the log"
        assert not any("huge-export.md" in m for m in warned), (
            "the document name must not reach the application log")
        # The log still has to be worth reading: what to raise, and where to look.
        assert any("max_ingest_file_mb" in m and "source_id=" in m for m in warned)
        sel_spy.log_tool_invocation.assert_called_once()
        assert sel_spy.log_tool_invocation.call_args.kwargs["outcome"] == "denied"
        assert "oversized" in sel_spy.log_tool_invocation.call_args.kwargs["resources"]

    @pytest.mark.asyncio
    async def test_guard_fires_before_file_is_read(self, pipeline, tmp_path, monkeypatch):
        big = tmp_path / "big.md"
        big.write_text("word " * 5000)
        _set_limit_mb(monkeypatch, 0.001)
        read_spy = MagicMock(side_effect=AssertionError("oversized file must not be read"))
        monkeypatch.setattr(pipeline.reader, "read", read_spy)

        with pytest.raises(FileTooLargeError):
            await pipeline.ingest_file(str(big))
        read_spy.assert_not_called()

    @pytest.mark.asyncio
    async def test_zero_limit_disables_guard(self, pipeline, tmp_path, monkeypatch):
        doc = tmp_path / "doc.md"
        doc.write_text("# Title\nsome body text")
        _set_limit_mb(monkeypatch, 0)

        job_id = await pipeline.ingest_file(str(doc))
        assert job_id is not None

    @pytest.mark.asyncio
    async def test_file_under_limit_ingests_normally(self, pipeline, tmp_path, monkeypatch):
        doc = tmp_path / "small.md"
        doc.write_text("# Title\nsome body text")
        _set_limit_mb(monkeypatch, 20.0)

        job_id = await pipeline.ingest_file(str(doc))
        assert job_id is not None

    def test_config_load_failure_falls_back_to_default(self, monkeypatch):
        import kiro_crew.config.loader as loader_mod
        monkeypatch.setattr(
            loader_mod.KiroCrewConfig, "load",
            classmethod(MagicMock(side_effect=RuntimeError("boom"))))
        assert ingestion_mod._max_ingest_file_mb() == ingestion_mod.DEFAULT_MAX_INGEST_FILE_MB


class TestChunkingOffLoop:
    @pytest.mark.asyncio
    async def test_ingest_file_chunks_off_the_event_loop(
            self, pipeline, tmp_path, monkeypatch):
        doc = tmp_path / "doc.md"
        doc.write_text("# Title\nsome body text")
        _set_limit_mb(monkeypatch, 20.0)
        loop_thread = threading.get_ident()
        seen: list[int] = []
        real = ingestion_mod._run_chunker

        def _spy(chunker, ext, text, uri):
            seen.append(threading.get_ident())
            return real(chunker, ext, text, uri)

        monkeypatch.setattr(ingestion_mod, "_run_chunker", _spy)
        await pipeline.ingest_file(str(doc))
        assert seen and all(t != loop_thread for t in seen)

    @pytest.mark.asyncio
    async def test_ingest_text_chunks_off_the_event_loop(self, pipeline):
        loop_thread = threading.get_ident()
        seen: list[int] = []
        real_chunk = pipeline.chunker.chunk

        def _spy(text, source_uri=None):
            seen.append(threading.get_ident())
            return real_chunk(text, source_uri=source_uri)

        pipeline.chunker = MagicMock(chunk=MagicMock(side_effect=_spy))
        await pipeline.ingest_text("some body text", title="t")
        assert seen and all(t != loop_thread for t in seen)

    @pytest.mark.asyncio
    async def test_store_entities_runs_off_the_event_loop(self, pipeline):
        """Mesh-3029: the per-chunk _store_entities pass (O(entities) find_entity
        scans + per-entity commit/graph mutation) runs via asyncio.to_thread, not
        inline on the loop where it stalled past the 25s loop-stall watchdog."""
        loop_thread = threading.get_ident()
        seen: list[int] = []
        real = pipeline._store_entities

        def _spy(extraction, item_id):
            seen.append(threading.get_ident())
            return real(extraction, item_id)

        pipeline._store_entities = _spy  # type: ignore[method-assign]
        await pipeline.ingest_text("some body text", title="t")
        assert seen and all(t != loop_thread for t in seen)

    @pytest.mark.asyncio
    async def test_maybe_dedup_runs_off_the_event_loop(self, pipeline):
        """Mesh-3029: the end-of-ingest _maybe_dedup (dedup_document -> merge_entities
        -> _load_graph full rebuild) runs via asyncio.to_thread, not on the loop."""
        loop_thread = threading.get_ident()
        seen: list[int] = []
        real = pipeline._maybe_dedup

        def _spy(source_id, content_hash=""):
            seen.append(threading.get_ident())
            return real(source_id, content_hash)

        pipeline._maybe_dedup = _spy  # type: ignore[method-assign]
        await pipeline.ingest_text("some body text", title="t")
        assert seen and all(t != loop_thread for t in seen)

    def test_run_chunker_dispatch(self):
        chunker = MagicMock()
        ingestion_mod._run_chunker(chunker, ".pptx", "t", "u")
        chunker.chunk_slides.assert_called_once_with("t")
        ingestion_mod._run_chunker(chunker, ".py", "t", "u")
        chunker.chunk_code.assert_called_once_with("t", language="py")
        ingestion_mod._run_chunker(chunker, ".md", "t", "u")
        chunker.chunk_markdown.assert_called_once_with("t")
        ingestion_mod._run_chunker(chunker, ".txt", "t", "u")
        chunker.chunk.assert_called_once_with("t", source_uri="u")


class TestFolderWatcherOversized:
    @pytest.mark.asyncio
    async def test_oversized_folder_file_marked_failed_with_message(
            self, kstore, pipeline, tmp_path, monkeypatch):
        folder = tmp_path / "vault"
        folder.mkdir()
        (folder / "big.md").write_text("word " * 5000)
        (folder / "small.md").write_text("# ok\nfine")
        _set_limit_mb(monkeypatch, 0.001)

        source_id = kstore.add_source(
            name="vault", source_type="local_folder", uri=str(folder))
        watcher = FolderWatcher(kstore, pipeline)
        stats = await watcher.scan_source(
            {"id": source_id, "uri": str(folder), "source_type": "local_folder",
             "properties": "{}"})

        assert stats["failed"] == 1
        row = kstore.db.execute(
            "SELECT status, error_message FROM folder_file_state "
            "WHERE source_id = ? AND file_path LIKE '%big.md'", (source_id,)).fetchone()
        assert row["status"] == "failed"
        assert "max_ingest_file_mb" in row["error_message"]

    @pytest.mark.asyncio
    async def test_oversized_folder_file_not_retried_on_next_scan(
            self, kstore, pipeline, tmp_path, monkeypatch):
        folder = tmp_path / "vault"
        folder.mkdir()
        (folder / "big.md").write_text("word " * 5000)
        _set_limit_mb(monkeypatch, 0.001)

        source_id = kstore.add_source(
            name="vault", source_type="local_folder", uri=str(folder))
        source = {"id": source_id, "uri": str(folder), "source_type": "local_folder",
                  "properties": "{}"}
        watcher = FolderWatcher(kstore, pipeline)
        await watcher.scan_source(source)

        ingest_spy = AsyncMock(side_effect=AssertionError("failed file must not be retried"))
        monkeypatch.setattr(pipeline, "ingest_file", ingest_spy)
        stats = await watcher.scan_source(source)
        assert stats["skipped"] == 1
        ingest_spy.assert_not_called()


class TestWatcherSingleFileOversized:
    @pytest.mark.asyncio
    async def test_oversized_local_file_errored_then_recovers_after_limit_raise(
            self, kstore, pipeline, tmp_path, monkeypatch):
        big = tmp_path / "big.md"
        big.write_text("# big\n" + "word " * 5000)
        _set_limit_mb(monkeypatch, 0.001)

        source_id = kstore.add_source(
            name="big.md", source_type="local_file", uri=str(big))
        watcher = KnowledgeWatcher(kstore, pipeline)
        monkeypatch.setattr(watcher, "_maybe_reembed_stale", AsyncMock())

        await watcher._scan()

        row = kstore.db.execute(
            "SELECT sync_status, properties FROM sources WHERE id = ?",
            (source_id,)).fetchone()
        assert row["sync_status"] == "error"
        props = json.loads(row["properties"])
        assert "mtime" not in props

        _set_limit_mb(monkeypatch, 100.0)
        await watcher._scan()

        row = kstore.db.execute(
            "SELECT sync_status, properties FROM sources WHERE id = ?",
            (source_id,)).fetchone()
        assert row["sync_status"] == "synced"
        props = json.loads(row["properties"])
        assert props.get("mtime")
        assert props.get("content_hash")


class TestSensitivePathGuard:
    @pytest.mark.asyncio
    async def test_ingest_refuses_sensitive_path_before_any_read(
            self, pipeline, tmp_path, monkeypatch):
        doc = tmp_path / "doc.md"
        doc.write_text("# ok")
        _set_limit_mb(monkeypatch, 100.0)
        monkeypatch.setattr(ingestion_mod, "is_sensitive_path", lambda _p: True)
        sel_spy = MagicMock()
        monkeypatch.setattr(ingestion_mod, "sel", lambda: sel_spy)
        size_spy = MagicMock(side_effect=AssertionError("sensitive path must not be stat'ed"))
        monkeypatch.setattr(ingestion_mod.os.path, "getsize", size_spy)
        read_spy = MagicMock(side_effect=AssertionError("sensitive path must not be read"))
        monkeypatch.setattr(pipeline.reader, "read", read_spy)

        with pytest.raises(PermissionError, match="sensitive path"):
            await pipeline.ingest_file(str(doc))
        size_spy.assert_not_called()
        read_spy.assert_not_called()
        sel_spy.log_tool_invocation.assert_called_once()
        assert sel_spy.log_tool_invocation.call_args.kwargs["outcome"] == "denied"
        assert "sensitive_path" in sel_spy.log_tool_invocation.call_args.kwargs["resources"]


class TestChunkPropsCoercion:
    """Per-source chunk_size/chunk_overlap come from the user-editable source
    ``properties`` JSON (the dashboard add_source API accepts an arbitrary
    ``properties`` object). ``int("abc")`` raised ValueError AFTER the
    ingestion-job row was inserted, so a malformed value aborted the ingest and
    stranded the job in 'processing'. Malformed values must fall back to the
    defaults and the ingest must complete."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_size", ["abc", "1.5", "", "  "])
    async def test_malformed_chunk_size_falls_back_and_ingests(
            self, pipeline, kstore, tmp_path, monkeypatch, bad_size):
        _set_limit_mb(monkeypatch, 100.0)
        doc = tmp_path / "doc.md"
        doc.write_text("# Title\n\nsome body text for chunking\n")
        sid = kstore.add_source(
            name="doc.md", source_type="local_file", uri=str(doc.resolve()),
            properties={"chunk_size": bad_size, "chunk_overlap": bad_size},
        )
        job_id = await pipeline.ingest_file(str(doc), source_id=sid)
        assert job_id is not None
        row = kstore.db.execute(
            "SELECT status FROM ingestion_jobs WHERE id = ?", (job_id,)).fetchone()
        assert row["status"] == "completed"

    @pytest.mark.asyncio
    async def test_nonpositive_chunk_size_falls_back(
            self, pipeline, kstore, tmp_path, monkeypatch):
        """A negative target_size breaks the recursive splitter — treat it as
        absent (fall back to the default) rather than building a chunker that
        can't make progress."""
        _set_limit_mb(monkeypatch, 100.0)
        built: list = []
        real_chunker = ingestion_mod.HeadingAwareChunker

        def _spy(*args, **kwargs):
            c = real_chunker(*args, **kwargs)
            built.append(c)
            return c

        monkeypatch.setattr(ingestion_mod, "HeadingAwareChunker", _spy)
        doc = tmp_path / "doc0.md"
        doc.write_text("# Title\n\nbody\n")
        sid = kstore.add_source(
            name="doc0.md", source_type="local_file", uri=str(doc.resolve()),
            properties={"chunk_size": -5, "chunk_overlap": -1},
        )
        job_id = await pipeline.ingest_file(str(doc), source_id=sid)
        assert job_id is not None
        row = kstore.db.execute(
            "SELECT status FROM ingestion_jobs WHERE id = ?", (job_id,)).fetchone()
        assert row["status"] == "completed"
        assert built, "per-source chunker was not built"
        from kiro_crew.knowledge.chunker import CHUNK_OVERLAP, CHUNK_TOKEN_SIZE
        assert built[-1].target_size == CHUNK_TOKEN_SIZE
        assert built[-1].overlap == CHUNK_OVERLAP

    @pytest.mark.asyncio
    async def test_any_post_insert_exception_marks_job_failed(
            self, pipeline, kstore, tmp_path, monkeypatch):
        """The job row is persisted as 'processing' BEFORE any fallible work.
        Any exception between the insert and finalize (chunker, extractor, DB)
        previously stranded the job in 'processing' forever, and the folder
        watcher retried the file every scan. It must be marked 'failed' and
        the source 'error', while the original exception still propagates."""
        _set_limit_mb(monkeypatch, 100.0)
        pipeline.extractor.extract_batch = AsyncMock(
            side_effect=RuntimeError("extractor pool down"))
        doc = tmp_path / "boom.md"
        doc.write_text("# Title\n\nbody\n")
        sid = kstore.add_source(
            name="boom.md", source_type="local_file", uri=str(doc.resolve()))
        with pytest.raises(RuntimeError, match="extractor pool down"):
            await pipeline.ingest_file(str(doc), source_id=sid)
        job = kstore.db.execute(
            "SELECT status FROM ingestion_jobs WHERE source_id = ?", (sid,)).fetchone()
        assert job["status"] == "failed"
        src = kstore.db.execute(
            "SELECT sync_status FROM sources WHERE id = ?", (sid,)).fetchone()
        assert src["sync_status"] == "error"

    @pytest.mark.asyncio
    async def test_valid_chunk_size_still_respected(
            self, pipeline, kstore, tmp_path, monkeypatch):
        _set_limit_mb(monkeypatch, 100.0)
        built: list = []
        real_chunker = ingestion_mod.HeadingAwareChunker

        def _spy(*args, **kwargs):
            c = real_chunker(*args, **kwargs)
            built.append(c)
            return c

        monkeypatch.setattr(ingestion_mod, "HeadingAwareChunker", _spy)
        doc = tmp_path / "docv.md"
        doc.write_text("# Title\n\nbody\n")
        sid = kstore.add_source(
            name="docv.md", source_type="local_file", uri=str(doc.resolve()),
            properties={"chunk_size": "128", "chunk_overlap": 7},
        )
        job_id = await pipeline.ingest_file(str(doc), source_id=sid)
        assert job_id is not None
        assert built, "per-source chunker was not built"
        assert built[-1].target_size == 128
        assert built[-1].overlap == 7


def test_persistent_source_outranks_a_transient_holder(tmp_path):
    """A folder copy must be allowed to land when only an upload holds the content.

    The gate honours the same persistent-over-transient ranking as ``pick_winner``,
    so arrival order cannot leave the only searchable copy inside a transient
    upload whose deletion would take the content with it.
    """
    from kiro_crew.knowledge.ingestion import IngestionPipeline
    from kiro_crew.knowledge.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "k.db"))
    try:
        pipe = IngestionPipeline(store=store, extractor=MagicMock(),
                                 chunker=MagicMock(), reader=MagicMock(),
                                 embedder=None)
        upload = store.add_source(name="dropped.md", source_type="upload",
                                  uri="upload://dropped.md")
        folder = store.add_source(name="notes", source_type="local_folder",
                                  uri=str(tmp_path / "notes"))
        h = "f" * 64
        store.db.execute(
            "INSERT INTO items (id, source_id, title, content, item_type, "
            "content_hash, created_at, updated_at) VALUES ('i1', ?, "
            "'dropped.md', 'body', 'document', ?, '2024-01-01T00:00:00', "
            "'2024-01-01T00:00:00')", (upload, h))
        store.db.commit()

        # Incoming folder copy outranks the transient upload -> write proceeds.
        assert pipe._skip_as_duplicate(h, folder) is None

        # Reverse direction still refuses: a transient copy adds nothing.
        assert pipe._skip_as_duplicate(h, upload) is None  # same source, not a dup
        other_upload = store.add_source(name="again.md", source_type="upload",
                                        uri="upload://again.md")
        assert pipe._skip_as_duplicate(h, other_upload) is not None
    finally:
        store.close()


def test_oversized_file_message_redacts_the_caller_supplied_name(tmp_path, monkeypatch, caplog):
    """An upload's filename is caller-supplied and can carry a credential.

    It reaches a gateway WARNING, a persisted SEL event and the raised error, so
    the name is redacted at every one of those sinks.
    """
    import asyncio
    import logging

    from kiro_crew.knowledge.ingestion import FileTooLargeError, IngestionPipeline
    from kiro_crew.knowledge.readers import FileReader
    from kiro_crew.knowledge.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "k.db"))
    try:
        pipe = IngestionPipeline(store=store, extractor=MagicMock(),
                                 chunker=MagicMock(), reader=FileReader(),
                                 embedder=None)
        big = tmp_path / "payload.md"
        big.write_text("x" * 4096)
        _set_limit_mb(monkeypatch, 0.001)

        leaky = "report AKIAIOSFODNN7EXAMPLE.md"
        with caplog.at_level(logging.WARNING):
            with pytest.raises(FileTooLargeError) as excinfo:
                asyncio.get_event_loop().run_until_complete(
                    pipe.ingest_file(str(big), original_name=leaky))

        assert "AKIAIOSFODNN7EXAMPLE" not in str(excinfo.value)
        assert "REDACTED" in str(excinfo.value)
        assert "AKIAIOSFODNN7EXAMPLE" not in caplog.text
    finally:
        store.close()


def test_the_duplicate_gate_records_the_refusing_source_as_a_holder(tmp_path):
    """Refusing the write must not cost the source its claim on the document.

    Under "one document, many locations" a source that HAS a copy is recorded as a
    location even when it stores no second physical copy. Without that, the copy is
    invisible to the reference count: deleting the holder destroys the only items
    while this source's file is still on disk, and nothing brings the content back.
    """
    from kiro_crew.knowledge.ingestion import IngestionPipeline
    from kiro_crew.knowledge.store import KnowledgeStore

    store = KnowledgeStore(str(tmp_path / "k.db"))
    try:
        pipe = IngestionPipeline(store=store, extractor=MagicMock(),
                                 chunker=MagicMock(), reader=MagicMock(),
                                 embedder=None)
        first = store.add_source(name="notes-a", source_type="local_folder",
                                 uri=str(tmp_path / "a"))
        second = store.add_source(name="notes-b", source_type="local_folder",
                                  uri=str(tmp_path / "b"))
        h = "c" * 64
        store.db.execute(
            "INSERT INTO items (id, source_id, title, content, item_type, "
            "content_hash, created_at, updated_at) VALUES ('i1', ?, "
            "'dup.md', 'body', 'document', ?, '2024-01-01T00:00:00', "
            "'2024-01-01T00:00:00')", (first, h))
        store.add_source_location("i1", first)
        store.db.commit()

        # Same content arriving in the second folder is refused (no second copy)...
        assert pipe._skip_as_duplicate(h, second) is not None
        # ... but the second folder is now recorded as holding the document.
        assert second in store.sources_holding_item("i1"), (
            "a refused source must still be recorded as a location")

        # So deleting the first folder moves the document instead of destroying it.
        store.delete_source_cascade(first)
        item = store.get_item("i1")
        assert item is not None, "the document must survive while another file holds it"
        assert item["source_id"] == second
    finally:
        store.close()


@pytest.mark.asyncio
async def test_an_auto_added_source_scrubs_secrets_before_extraction(
        pipeline, kstore, tmp_path):
    """A folder nobody chose must not hand a credential to the extraction worker.

    Auto-registration indexes project documents with no confirmation step, so the
    user never agreed to this folder's contents leaving the machine. The document's
    own text is therefore scrubbed ahead of chunking, extraction and storage.
    """
    doc = tmp_path / "runbook.md"
    doc.write_text("# Runbook\n\nUse AKIAIOSFODNN7EXAMPLE to rotate the fleet.\n")
    src = kstore.add_source(
        name="project docs", source_type="local_folder", uri=str(tmp_path),
        properties={"sync_status": "active", "auto_added": True})

    await pipeline.ingest_file(str(doc), source_id=src)

    stored = " ".join(
        r["content"] or "" for r in kstore.db.execute(
            "SELECT content FROM items WHERE source_id = ?", (src,)).fetchall())
    assert stored, "the document should have been ingested"
    assert "AKIAIOSFODNN7EXAMPLE" not in stored, (
        "an auto-added source must not store a raw credential")
    # What the extractor was handed must be scrubbed too -- that is the call that
    # leaves the machine.
    sent = " ".join(
        str(c) for call in pipeline.extractor.extract_batch.await_args_list
        for c in (call.args[0] if call.args else []))
    assert "AKIAIOSFODNN7EXAMPLE" not in sent, (
        "the credential must not reach the extraction worker")


@pytest.mark.asyncio
async def test_a_hand_registered_source_is_left_alone(pipeline, kstore, tmp_path):
    """The scrub is scoped to sources the user did NOT choose.

    Registering a folder by hand is a deliberate act, and silently rewriting its
    indexed text would be a behaviour change for every existing source.
    """
    doc = tmp_path / "notes.md"
    doc.write_text("# Notes\n\nUse AKIAIOSFODNN7EXAMPLE to rotate the fleet.\n")
    src = kstore.add_source(name="my docs", source_type="local_folder",
                            uri=str(tmp_path), properties={"sync_status": "active"})

    await pipeline.ingest_file(str(doc), source_id=src)

    stored = " ".join(
        r["content"] or "" for r in kstore.db.execute(
            "SELECT content FROM items WHERE source_id = ?", (src,)).fetchall())
    assert "AKIAIOSFODNN7EXAMPLE" in stored, (
        "a hand-registered source keeps its existing behaviour")
