"""Tests for source-level summary generation."""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.knowledge.ingestion import IngestionPipeline
from kiro_crew.knowledge.store import KnowledgeStore


@pytest.fixture()
def store(tmp_path):
    s = KnowledgeStore(str(tmp_path / "test.db"))
    yield s
    s.close()


@pytest.fixture()
def pipeline(store):
    extractor = MagicMock()
    extractor._pool = MagicMock()
    extractor._pool.send = AsyncMock()
    chunker = MagicMock()
    reader = MagicMock()
    return IngestionPipeline(store=store, extractor=extractor, chunker=chunker, reader=reader)


class TestGenerateSourceSummary:
    """Tests for generate_source_summary()."""

    @pytest.mark.asyncio
    async def test_no_op_when_pool_unavailable(self, store, tmp_path):
        """Should silently return when LLM pool is None."""
        extractor = MagicMock()
        extractor._pool = None
        p = IngestionPipeline(store=store, extractor=extractor, chunker=MagicMock(), reader=MagicMock())
        sid = store.add_source("test", "local_file", "/tmp/test.md")
        store.add_item("title", "content", "doc", source_id=sid, summary="A summary")
        await p.generate_source_summary(sid)
        row = store.db.execute("SELECT summary_topic FROM sources WHERE id = ?", (sid,)).fetchone()
        assert row["summary_topic"] is None

    @pytest.mark.asyncio
    async def test_no_op_when_no_summaries(self, pipeline, store):
        """Should return early when items have no summaries."""
        sid = store.add_source("test", "local_file", "/tmp/test.md")
        store.add_item("title", "content", "doc", source_id=sid)  # no summary
        await pipeline.generate_source_summary(sid)
        pipeline.extractor._pool.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_stores_topic_and_themes(self, pipeline, store):
        """Should parse LLM response and store topic + themes."""
        sid = store.add_source("test", "local_file", "/tmp/test.md")
        store.add_item("title", "content", "doc", source_id=sid, summary="KiroCrew is an AI agent platform")
        pipeline.extractor._pool.send.return_value = '{"topic": "AI agent platform overview", "themes": ["AI", "agents", "orchestration"]}'
        await pipeline.generate_source_summary(sid)
        row = store.db.execute("SELECT summary_topic, summary_themes FROM sources WHERE id = ?", (sid,)).fetchone()
        assert row["summary_topic"] == "AI agent platform overview"
        assert json.loads(row["summary_themes"]) == ["AI", "agents", "orchestration"]

    @pytest.mark.asyncio
    async def test_caps_input_at_4000_chars(self, pipeline, store):
        """Should truncate chunk summaries to 4000 chars."""
        sid = store.add_source("test", "local_file", "/tmp/test.md")
        # Add items with long summaries
        for i in range(10):
            store.add_item(f"t{i}", "c", "doc", source_id=sid, summary="x" * 500, chunk_index=i)
        pipeline.extractor._pool.send.return_value = '{"topic": "test", "themes": ["a"]}'
        await pipeline.generate_source_summary(sid)
        # Verify the prompt was capped
        call_args = pipeline.extractor._pool.send.call_args[0][0]
        # The sections part should be <= 4000 chars
        sections_start = call_args.index("Sections:\n") + len("Sections:\n")
        sections_end = call_args.index("\n\nRespond")
        assert len(call_args[sections_start:sections_end]) <= 4000

    @pytest.mark.asyncio
    async def test_redacts_sensitive_themes(self, pipeline, store):
        """Themes containing sensitive content should be dropped (not fall back to original)."""
        sid = store.add_source("test", "local_file", "/tmp/test.md")
        store.add_item("title", "content", "doc", source_id=sid, summary="Some summary")
        # Simulate LLM returning a theme that _redact strips entirely
        pipeline.extractor._pool.send.return_value = '{"topic": "safe topic", "themes": ["safe", "AKIA1234567890ABCDEF"]}'
        await pipeline.generate_source_summary(sid)
        row = store.db.execute("SELECT summary_themes FROM sources WHERE id = ?", (sid,)).fetchone()
        themes = json.loads(row["summary_themes"])
        # The AWS key pattern should be redacted away, not preserved via 'or t'
        assert "AKIA1234567890ABCDEF" not in str(themes)

    @pytest.mark.asyncio
    async def test_handles_malformed_llm_response(self, pipeline, store):
        """Should not crash on non-JSON LLM response."""
        sid = store.add_source("test", "local_file", "/tmp/test.md")
        store.add_item("title", "content", "doc", source_id=sid, summary="A summary")
        pipeline.extractor._pool.send.return_value = "I cannot produce JSON right now."
        await pipeline.generate_source_summary(sid)
        row = store.db.execute("SELECT summary_topic FROM sources WHERE id = ?", (sid,)).fetchone()
        assert row["summary_topic"] is None  # graceful degradation

    @pytest.mark.asyncio
    async def test_handles_pool_exception(self, pipeline, store):
        """Should not crash when LLM pool raises."""
        sid = store.add_source("test", "local_file", "/tmp/test.md")
        store.add_item("title", "content", "doc", source_id=sid, summary="A summary")
        pipeline.extractor._pool.send.side_effect = RuntimeError("pool dead")
        await pipeline.generate_source_summary(sid)
        row = store.db.execute("SELECT summary_topic FROM sources WHERE id = ?", (sid,)).fetchone()
        assert row["summary_topic"] is None  # no crash, graceful degradation

    @pytest.mark.asyncio
    async def test_limits_to_5_themes(self, pipeline, store):
        """Should cap themes at 5 even if LLM returns more."""
        sid = store.add_source("test", "local_file", "/tmp/test.md")
        store.add_item("title", "content", "doc", source_id=sid, summary="A summary")
        pipeline.extractor._pool.send.return_value = '{"topic": "test", "themes": ["a","b","c","d","e","f","g"]}'
        await pipeline.generate_source_summary(sid)
        row = store.db.execute("SELECT summary_themes FROM sources WHERE id = ?", (sid,)).fetchone()
        themes = json.loads(row["summary_themes"])
        assert len(themes) <= 5
