"""A model reply that parses as JSON but has the wrong field types."""

from __future__ import annotations

import json

import pytest

from kiro_crew.knowledge.chunker import HeadingAwareChunker
from kiro_crew.knowledge.extractor import EntityExtractor
from kiro_crew.knowledge.ingestion import IngestionPipeline
from kiro_crew.knowledge.readers import FileReader
from kiro_crew.knowledge.store import KnowledgeStore

ODD_REPLIES = [
    {"title": ["a"], "entities": ["DynamoDB"], "summary": 5},
    {"entities": None, "relations": "uses", "category": {"x": 1}},
    {
        "entities": [{"name": 7, "description": ["d"]}, {"name": "Billing"}],
        "relations": [{"source": None, "target": "Billing", "type": 3}],
    },
]


class _Pool:
    def __init__(self, reply: dict) -> None:
        self.reply = json.dumps(reply)

    async def send_batch(self, prompts, timeout=60.0):
        return [self.reply for _ in prompts]

    async def send(self, prompt, timeout=60.0):
        return ""


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ODD_REPLIES)
async def test_an_odd_reply_does_not_fail_the_ingest(tmp_path, reply) -> None:
    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    try:
        pipeline = IngestionPipeline(
            store=store,
            extractor=EntityExtractor(pool=_Pool(reply)),
            chunker=HeadingAwareChunker(),
            reader=FileReader(),
            embedder=None,
        )
        sid = store.add_source(name="s", source_type="manual", uri="manual://s")
        job_id = await pipeline.ingest_text("Billing stores invoices.", title="Doc", source_id=sid)
        assert (pipeline.get_job_status(job_id) or {}).get("status") == "completed"
        rows = store.db.execute("SELECT content FROM items WHERE source_id = ?", (sid,)).fetchall()
        assert [r["content"] for r in rows] == ["Billing stores invoices."]
    finally:
        store.close()


def test_validate_keeps_only_the_shapes_the_ingest_reads() -> None:
    got = EntityExtractor._validate(ODD_REPLIES[2])
    assert got["entities"] == [{}, {"name": "Billing"}]
    assert got["relations"] == [{"target": "Billing"}]
    assert (got["title"], got["category"], got["summary"]) == ("", "document", "")
