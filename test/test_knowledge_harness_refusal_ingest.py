"""An enterprise knowledge-pool refusal fails the ingest instead of storing it empty.

In an enterprise build the knowledge pool never falls back to the Kiro CLI harness
(RFC D6): it raises :class:`KnowledgeHarnessUnavailable` instead. That refusal must
reach the ingest, which ends the job ``failed`` with the refusal's remedy, writes no
item, forgets the content hash (so the unchanged document is ingested again later),
and keeps a replaced document's old items. Any other extraction error keeps its
existing path, so an ``oss`` build is unchanged.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from kiro_crew.knowledge.chunker import HeadingAwareChunker
from kiro_crew.knowledge.extractor import EntityExtractor
from kiro_crew.knowledge.ingestion import IngestionPipeline
from kiro_crew.knowledge.llm_pool import KnowledgeHarnessUnavailable, LLMPool, _kiro_fallback
from kiro_crew.knowledge.readers import FileReader
from kiro_crew.knowledge.store import KnowledgeStore

_REPLY = json.dumps(
    {"title": "t", "entities": [], "relations": [], "category": "document", "summary": "s"}
)


@pytest.fixture
def enterprise(monkeypatch):
    monkeypatch.setattr("kiro_crew.edition.EDITION", "enterprise")


@pytest.fixture()
def kstore(tmp_path):
    s = KnowledgeStore(str(tmp_path / "knowledge.db"))
    yield s
    s.close()


class _Pool:
    """A pool stand-in: refuses the way the enterprise pool does, or replies."""

    def __init__(self) -> None:
        self.refuse = True
        self.error: Exception | None = None

    async def send(self, prompt: str, timeout: float = 0) -> str:
        if self.error is not None:
            raise self.error
        if self.refuse:
            _kiro_fallback("kas cannot run the knowledge pool's client")
        return _REPLY

    async def send_batch(self, prompts: list[str], timeout: float = 0) -> list[str]:
        return [await self.send(p, timeout=timeout) for p in prompts]


@pytest.fixture()
def pool():
    return _Pool()


@pytest.fixture()
def pipeline(kstore, pool):
    return IngestionPipeline(
        store=kstore,
        extractor=EntityExtractor(pool=pool),  # type: ignore[arg-type]
        chunker=HeadingAwareChunker(),
        reader=FileReader(),
        embedder=None,
        dedup_enabled=False,
    )


def _job(kstore, source_id):
    return kstore.db.execute(
        "SELECT status, error FROM ingestion_jobs WHERE source_id = ? ORDER BY rowid DESC",
        (source_id,),
    ).fetchone()


def _items(kstore, source_id):
    rows = kstore.db.execute("SELECT id FROM items WHERE source_id = ?", (source_id,))
    return {r["id"] for r in rows.fetchall()}


def _props(kstore, source_id):
    row = kstore.db.execute("SELECT properties FROM sources WHERE id = ?", (source_id,)).fetchone()
    return json.loads(row["properties"] or "{}")


class TestThePoolAndExtractorPassTheRefusalOn:
    def test_send_batch_raises_the_refusal(self, enterprise):
        pool = LLMPool.__new__(LLMPool)

        async def _send(prompt, timeout=0):
            _kiro_fallback("no client")

        pool.send = _send  # type: ignore[method-assign]
        with pytest.raises(KnowledgeHarnessUnavailable):
            asyncio.run(pool.send_batch(["a", "b"]))

    def test_send_batch_still_blanks_any_other_failure(self):
        pool = LLMPool.__new__(LLMPool)

        async def _send(prompt, timeout=0):
            raise RuntimeError("worker died")

        pool.send = _send  # type: ignore[method-assign]
        assert asyncio.run(pool.send_batch(["a", "b"])) == ["", ""]

    def test_extractor_raises_the_refusal(self, enterprise, pool):
        extractor = EntityExtractor(pool=pool)  # type: ignore[arg-type]
        with pytest.raises(KnowledgeHarnessUnavailable):
            asyncio.run(extractor.extract_batch(["text"]))
        with pytest.raises(KnowledgeHarnessUnavailable):
            asyncio.run(extractor.extract("text"))


class TestEnterpriseRefusalFailsTheIngest:
    def test_new_file_job_fails_and_the_hash_is_not_kept(
        self, enterprise, pipeline, pool, kstore, tmp_path
    ):
        doc = tmp_path / "doc.md"
        doc.write_text("# Heading\n\nSome body text for the document.", encoding="utf-8")
        with pytest.raises(KnowledgeHarnessUnavailable):
            asyncio.run(pipeline.ingest_file(str(doc)))
        source = kstore.get_source_by_uri(str(doc.resolve()))
        job = _job(kstore, source["id"])
        assert job["status"] == "failed"
        assert "kiro-cli update" in job["error"]
        assert _items(kstore, source["id"]) == set()
        assert "content_hash" not in _props(kstore, source["id"])

        # Once the harness is fixed the unchanged document is ingested, not skipped.
        pool.refuse = False
        assert asyncio.run(pipeline.ingest_file(str(doc))) is not None
        assert _items(kstore, source["id"])

    def test_hash_cleanup_is_a_locked_delta_that_keeps_other_properties(
        self, enterprise, pipeline, pool, kstore, tmp_path, monkeypatch
    ):
        doc = tmp_path / "doc.md"
        doc.write_text("# Heading\n\nSome body text for the document.", encoding="utf-8")
        calls = []
        real = kstore.revise_source_properties

        def _revise(source_id, revise, **kw):
            # A pause lands on the row just before the cleanup's locked take.
            real(source_id, lambda p: p.update(scan_paused=True))
            calls.append(source_id)
            return real(source_id, revise, **kw)

        monkeypatch.setattr(kstore, "revise_source_properties", _revise)
        with pytest.raises(KnowledgeHarnessUnavailable):
            asyncio.run(pipeline.ingest_file(str(doc)))
        source = kstore.get_source_by_uri(str(doc.resolve()))
        assert calls == [source["id"]]
        props = _props(kstore, source["id"])
        assert props.get("scan_paused") is True
        assert "content_hash" not in props

    def test_replace_ingest_keeps_the_old_items(self, enterprise, pipeline, pool, kstore, tmp_path):
        doc = tmp_path / "doc.md"
        doc.write_text("# Heading\n\nThe first version.", encoding="utf-8")
        pool.refuse = False
        asyncio.run(pipeline.ingest_file(str(doc)))
        source = kstore.get_source_by_uri(str(doc.resolve()))
        old = _items(kstore, source["id"])
        assert old

        doc.write_text("# Heading\n\nThe second version.", encoding="utf-8")
        pool.refuse = True
        with pytest.raises(KnowledgeHarnessUnavailable):
            asyncio.run(pipeline.ingest_file(str(doc)))
        assert _items(kstore, source["id"]) == old
        assert _job(kstore, source["id"])["status"] == "failed"

    def test_text_job_fails_and_the_hash_is_not_kept(self, enterprise, pipeline, pool, kstore):
        with pytest.raises(KnowledgeHarnessUnavailable):
            asyncio.run(pipeline.ingest_text("Some body text for the note.", "note"))
        source = kstore.db.execute("SELECT id FROM sources WHERE name = 'note'").fetchone()
        job = _job(kstore, source["id"])
        assert job["status"] == "failed"
        assert "kiro-cli update" in job["error"]
        assert _items(kstore, source["id"]) == set()
        assert "content_hash" not in _props(kstore, source["id"])

        pool.refuse = False
        assert asyncio.run(pipeline.ingest_text("Some body text for the note.", "note")) is not None


class TestOssIsUnchanged:
    def test_oss_never_refuses(self, pipeline, kstore, tmp_path):
        doc = tmp_path / "doc.md"
        doc.write_text("# Heading\n\nSome body text.", encoding="utf-8")
        # In oss the fallback is Kiro CLI, not a refusal.
        assert _kiro_fallback("any") == ""

    def test_oss_other_extraction_error_still_stores_the_document(
        self, pipeline, pool, kstore, tmp_path
    ):
        pool.error = RuntimeError("worker died")
        doc = tmp_path / "doc.md"
        doc.write_text("# Heading\n\nSome body text.", encoding="utf-8")
        assert asyncio.run(pipeline.ingest_file(str(doc))) is not None
        source = kstore.get_source_by_uri(str(doc.resolve()))
        assert _job(kstore, source["id"])["status"] == "completed"
        assert _props(kstore, source["id"]).get("content_hash")
