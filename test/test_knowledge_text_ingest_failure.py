"""A failed text ingest ends its job instead of stranding it in 'processing'.

The job row is written as 'processing' before any fallible work runs. The file
path ends that job when the work fails (see ``ingest_file``); the text path had
no such handler, so an extraction error left the row in 'processing' forever: a
caller polling ``get_job_status`` waited for a job that never ended, and the
job list showed a run that was not running.
"""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew.knowledge.chunker import HeadingAwareChunker
from kiro_crew.knowledge.ingestion import IngestionPipeline
from kiro_crew.knowledge.readers import FileReader
from kiro_crew.knowledge.store import KnowledgeStore


class _FailingExtractor:
    """An extractor whose extraction fails with an ordinary error."""

    async def extract_batch(self, chunks: list[str]) -> list[dict]:
        raise RuntimeError("the extraction worker died")

    async def extract(self, chunk: str) -> dict:
        raise RuntimeError("the extraction worker died")


@pytest.fixture()
def kstore(tmp_path):
    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    yield store
    store.close()


@pytest.fixture()
def pipeline(kstore):
    return IngestionPipeline(
        store=kstore,
        extractor=_FailingExtractor(),  # type: ignore[arg-type]
        chunker=HeadingAwareChunker(),
        reader=FileReader(),
        embedder=None,
        dedup_enabled=False,
    )


def _job(kstore):
    row = kstore.db.execute(
        "SELECT status, error FROM ingestion_jobs ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    return dict(row)


def test_a_failed_text_ingest_ends_its_job(pipeline, kstore):
    with pytest.raises(RuntimeError):
        asyncio.run(pipeline.ingest_text("Some notes to ingest.", title="note"))

    job = _job(kstore)
    assert job["status"] == "failed"
    # The source reads 'error' too, so a later sweep does not treat it as synced.
    source = kstore.db.execute(
        "SELECT sync_status FROM sources ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    assert source["sync_status"] == "error"


def test_the_failed_job_is_visible_to_a_polling_caller(pipeline, kstore):
    with pytest.raises(RuntimeError):
        asyncio.run(pipeline.ingest_text("Some notes to ingest.", title="note"))

    job_id = kstore.db.execute(
        "SELECT id FROM ingestion_jobs ORDER BY rowid DESC LIMIT 1"
    ).fetchone()["id"]
    status = pipeline.get_job_status(job_id)
    assert status is not None
    assert status["status"] == "failed"
