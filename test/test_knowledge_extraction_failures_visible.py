"""Knowledge extraction failures are visible, and interrupted ingest jobs are reclaimed.

An extraction or source-summary call that fails, comes back empty, or comes back
unparseable stores the chunk with no entities and no summary. Each of those outcomes
logs a WARNING that names the source id and the reason, and never the document text
or the model's reply, so a library whose graph stays empty says why.

A source-ingest job row left 'processing' by a gateway that stopped mid-ingest is
moved to 'abandoned' by a startup hook that runs before the new process ingests
anything; live rows and corpus-rebuild rows are left alone.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from kiro_crew.dashboard.handlers import knowledge as kh
from kiro_crew.knowledge.chunker import HeadingAwareChunker
from kiro_crew.knowledge.extractor import EntityExtractor
from kiro_crew.knowledge.folder_watcher import FolderWatcher
from kiro_crew.knowledge.ingestion import IngestionPipeline
from kiro_crew.knowledge.readers import FileReader
from kiro_crew.knowledge.store import KnowledgeStore

#: Document text that must never reach the log.
SECRET_TEXT = "ZEBRA-CANARY-TEXT"

GOOD_REPLY = json.dumps(
    {
        "title": "Billing storage",
        "entities": [{"name": "DynamoDB", "type": "technology", "description": "db"}],
        "relations": [],
        "category": "design_doc",
        "summary": "Billing stores invoices in DynamoDB.",
    }
)


class _Pool:
    """A stand-in extraction pool answering every prompt with *reply*."""

    def __init__(self, reply: str = GOOD_REPLY, summary_reply: str = "", raise_on=None):
        self.reply = reply
        self.summary_reply = summary_reply
        self.raise_on = raise_on
        self.batch_prompts: list[str] = []

    async def send_batch(self, prompts, timeout=60.0):
        if self.raise_on == "batch":
            raise RuntimeError(f"pipe broken while sending {SECRET_TEXT}")
        self.batch_prompts.extend(prompts)
        return [self.reply for _ in prompts]

    async def send(self, prompt, timeout=60.0):
        if self.raise_on == "send":
            raise RuntimeError(f"pipe broken while sending {SECRET_TEXT}")
        return self.summary_reply


def _source(source_id: str):
    """The extraction-source context, imported per call so collection never depends on it."""
    from kiro_crew.knowledge.extractor import extraction_source

    return extraction_source(source_id)


def _reclaim(store, *, created_before: str) -> int:
    from kiro_crew.knowledge.ingestion import reclaim_interrupted_ingest_jobs

    return reclaim_interrupted_ingest_jobs(store, created_before=created_before)


def _interrupted_error() -> str:
    from kiro_crew.knowledge.ingestion import INTERRUPTED_JOB_ERROR

    return INTERRUPTED_JOB_ERROR


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


@pytest.fixture
def kstore(tmp_path):
    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    yield store
    store.close()


def _pipeline(store, pool) -> IngestionPipeline:
    return IngestionPipeline(
        store=store,
        extractor=EntityExtractor(pool=pool),
        chunker=HeadingAwareChunker(),
        reader=FileReader(),
        embedder=None,
    )


class TestExtractionFailuresAreLogged:
    @pytest.mark.asyncio
    async def test_empty_and_unparseable_replies_warn_with_the_source(self, caplog):
        replies = iter(["", f"I could not do that for {SECRET_TEXT}", GOOD_REPLY])

        class _Mixed(_Pool):
            async def send_batch(self, prompts, timeout=60.0):
                return [next(replies) for _ in prompts]

        extractor = EntityExtractor(pool=_Mixed())
        caplog.set_level(logging.WARNING, logger="kiro_crew.knowledge.extractor")
        with _source("src-123"):
            results = await extractor.extract_batch(["a", "b", "c"])

        assert results[2]["summary"] == "Billing stores invoices in DynamoDB."
        assert results[0]["summary"] == "" and results[1]["summary"] == ""
        warnings = _warnings(caplog)
        assert len(warnings) == 1
        assert "source_id=src-123" in warnings[0]
        assert "2 of 3" in warnings[0]
        assert "1 empty, 1 unparseable" in warnings[0]
        assert SECRET_TEXT not in warnings[0]

    @pytest.mark.asyncio
    async def test_a_failed_batch_warns_with_the_error_type_only(self, caplog):
        extractor = EntityExtractor(pool=_Pool(raise_on="batch"))
        caplog.set_level(logging.WARNING, logger="kiro_crew.knowledge.extractor")
        with _source("src-9"):
            results = await extractor.extract_batch(["a", "b"])

        assert all(r["summary"] == "" for r in results)
        warnings = _warnings(caplog)
        assert len(warnings) == 1
        assert "source_id=src-9" in warnings[0] and "RuntimeError" in warnings[0]
        assert SECRET_TEXT not in warnings[0]

    @pytest.mark.asyncio
    async def test_single_chunk_extract_warns_on_an_unparseable_reply(self, caplog):
        # extract() sends one prompt through the pool's ``send``.
        extractor = EntityExtractor(pool=_Pool(summary_reply="not json at all"))
        caplog.set_level(logging.WARNING, logger="kiro_crew.knowledge.extractor")
        with _source("src-1"):
            result = await extractor.extract("some text")

        assert result["entities"] == []
        assert any("source_id=src-1" in m and "unparseable" in m for m in _warnings(caplog))

    @pytest.mark.asyncio
    async def test_good_replies_log_nothing(self, caplog):
        extractor = EntityExtractor(pool=_Pool())
        caplog.set_level(logging.WARNING, logger="kiro_crew.knowledge.extractor")
        await extractor.extract_batch(["a", "b"])
        assert _warnings(caplog) == []

    @pytest.mark.asyncio
    async def test_no_pool_is_not_a_failure(self, caplog):
        caplog.set_level(logging.WARNING, logger="kiro_crew.knowledge.extractor")
        await EntityExtractor(pool=None).extract_batch(["a"])
        assert _warnings(caplog) == []


class TestVaultIngestRunsAndReportsExtraction:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("source_type", ["obsidian_vault", "local_folder"])
    async def test_folder_scan_extracts_and_names_the_source_on_failure(
        self, kstore, tmp_path, caplog, source_type
    ):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "note.md").write_text(f"# Note\n\n{SECRET_TEXT} lives here.\n")
        pool = _Pool(reply=f"sorry, {SECRET_TEXT}")
        source_id = kstore.add_source("vault", source_type, str(vault))
        source = dict(
            kstore.db.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        )
        caplog.set_level(logging.WARNING, logger="kiro_crew.knowledge")

        await FolderWatcher(kstore, _pipeline(kstore, pool)).scan_source(source)

        # The scan sent the file's chunks to the extractor ...
        assert pool.batch_prompts
        # ... and the unusable reply is reported against this source.
        matching = [m for m in _warnings(caplog) if f"source_id={source_id}" in m]
        assert any("unparseable" in m for m in matching), _warnings(caplog)
        assert all(SECRET_TEXT not in m for m in _warnings(caplog))


class TestSourceSummaryFailuresAreLogged:
    def _seed(self, store) -> str:
        source_id = store.add_source("doc", "local_folder", "/tmp/doc")
        store.add_item(
            title="t",
            content=SECRET_TEXT,
            item_type="document",
            source_id=source_id,
            chunk_index=0,
            summary=f"summary of {SECRET_TEXT}",
            namespace="default",
        )
        return source_id

    @pytest.mark.asyncio
    async def test_unusable_reply_warns(self, kstore, caplog):
        source_id = self._seed(kstore)
        pipeline = _pipeline(kstore, _Pool(summary_reply=f"no idea about {SECRET_TEXT}"))
        caplog.set_level(logging.WARNING, logger="kiro_crew.knowledge.ingestion")

        await pipeline.generate_source_summary(source_id)

        warnings = _warnings(caplog)
        assert any(f"source_id={source_id}" in m and "unusable" in m for m in warnings)
        assert all(SECRET_TEXT not in m for m in warnings)

    @pytest.mark.asyncio
    async def test_failed_call_warns_with_the_error_type_only(self, kstore, caplog):
        source_id = self._seed(kstore)
        pipeline = _pipeline(kstore, _Pool(raise_on="send"))
        caplog.set_level(logging.WARNING, logger="kiro_crew.knowledge.ingestion")

        await pipeline.generate_source_summary(source_id)

        warnings = _warnings(caplog)
        assert any(f"source_id={source_id}" in m and "RuntimeError" in m for m in warnings)
        assert all(SECRET_TEXT not in m for m in warnings)


def _insert_job(store, job_id: str, source_id, status: str, created_at: str) -> None:
    store.db.execute(
        "INSERT INTO ingestion_jobs (id, source_id, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (job_id, source_id, status, created_at, created_at),
    )
    store.db.commit()


def _status(store, job_id: str) -> tuple[str, str | None]:
    row = store.db.execute(
        "SELECT status, error FROM ingestion_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    return row["status"], row["error"]


#: A fixed boot instant: the sweep compares stored ISO strings, so no clock is read.
BOOT = datetime(2026, 1, 15, 12, 0, 0)


class TestInterruptedIngestJobsAreReclaimed:
    def _seed(self, store, boot: datetime) -> None:
        source_id = store.add_source("vault", "obsidian_vault", "/tmp/vault")
        before = (boot - timedelta(hours=3)).isoformat()
        after = (boot + timedelta(seconds=1)).isoformat()
        _insert_job(store, "stranded", source_id, "processing", before)
        _insert_job(store, "done", source_id, "completed", before)
        _insert_job(store, "rebuild", None, "processing", before)
        _insert_job(store, "live", source_id, "processing", after)

    def test_only_source_jobs_from_before_boot_move(self, kstore):
        boot = BOOT
        self._seed(kstore, boot)

        moved = _reclaim(kstore, created_before=boot.isoformat())

        assert moved == 1
        assert _status(kstore, "stranded") == ("abandoned", _interrupted_error())
        assert _status(kstore, "done") == ("completed", None)
        # The corpus rebuild keeps its own staleness rule.
        assert _status(kstore, "rebuild") == ("processing", None)
        # A job this process started is never reclaimed.
        assert _status(kstore, "live") == ("processing", None)

    @pytest.mark.asyncio
    async def test_startup_hook_reclaims_in_the_background_with_the_boot_cutoff(self, kstore):
        boot = BOOT
        await asyncio.to_thread(self._seed, kstore, boot)
        tasks: set = set()
        app = {
            "state": SimpleNamespace(knowledge_store=kstore),
            "_knowledge_ingest_boot_ts": boot.isoformat(),
            "_bg_tasks": tasks,
        }

        # The hook schedules the sweep and returns; startup does not wait on it.
        await kh._reclaim_interrupted_ingest_jobs(app)
        assert len(tasks) == 1
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)

        assert (await asyncio.to_thread(_status, kstore, "stranded"))[0] == "abandoned"
        assert (await asyncio.to_thread(_status, kstore, "live"))[0] == "processing"

    @pytest.mark.asyncio
    async def test_setup_registers_the_reclaim_before_any_ingest_hook(
        self, kstore, monkeypatch, tmp_path
    ):
        from aiohttp import web

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.knowledge_lifecycle.config_dir", lambda: tmp_path
        )
        app = web.Application()
        app["state"] = SimpleNamespace(knowledge_store=kstore)

        kh.setup_knowledge_routes(app)
        try:
            hooks = list(app.on_startup)
            reclaim = hooks.index(kh._reclaim_interrupted_ingest_jobs)
            assert reclaim < hooks.index(kh._start_watcher_async)
            assert reclaim < hooks.index(kh._start_artifact_ingest_async)
            assert isinstance(app["_knowledge_ingest_boot_ts"], str)
        finally:
            for callback in list(app.on_cleanup):
                await callback(app)
