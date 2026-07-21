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

        assert "huge-export.md" in str(exc_info.value)
        assert "max_ingest_file_mb" in str(exc_info.value)
        warned = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("huge-export.md" in m and "max_ingest_file_mb" in m for m in warned)
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
