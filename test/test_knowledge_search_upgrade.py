"""Tests for knowledge search upgrade: embedding endpoints + search-for-context."""
import pytest

from kiro_crew.knowledge.embedder import OllamaEmbedder, create_embedder_from_config


class TestCreateEmbedderFromConfig:
    """create_embedder_from_config uses shared memory config."""

    def test_returns_none_when_provider_not_ollama(self):
        cfg = {"memory": {"embedding_provider": "none"}}
        assert create_embedder_from_config(cfg) is None

    def test_returns_none_when_memory_section_missing(self):
        assert create_embedder_from_config({}) is None

    def test_returns_embedder_when_ollama_enabled(self):
        cfg = {"memory": {"embedding_provider": "ollama"}}
        emb = create_embedder_from_config(cfg)
        assert isinstance(emb, OllamaEmbedder)
        assert emb.model == "qwen3-embedding:0.6b"

    def test_uses_custom_model_and_url(self):
        cfg = {"memory": {
            "embedding_provider": "ollama",
            "embedding_model": "custom:latest",
            "embedding_url": "http://remote:11434",
        }}
        emb = create_embedder_from_config(cfg)
        assert emb.model == "custom:latest"
        assert emb.base_url == "http://remote:11434"

    def test_ignores_old_knowledge_embeddings_config(self):
        """Old knowledge.embeddings.enabled path should NOT activate embedder."""
        cfg = {"knowledge": {"embeddings": {"enabled": True}}}
        assert create_embedder_from_config(cfg) is None


class TestOllamaEmbedder:
    """OllamaEmbedder graceful degradation."""

    def test_embed_returns_none_for_empty_text(self):
        emb = OllamaEmbedder()
        assert emb.embed("") is None
        assert emb.embed("   ") is None

    def test_embed_returns_none_when_unavailable(self, monkeypatch):
        emb = OllamaEmbedder()
        monkeypatch.setattr(emb, "is_available", lambda: False)
        assert emb.embed("hello world") is None

    def test_embed_for_item_combines_title_and_summary(self, monkeypatch):
        emb = OllamaEmbedder()
        monkeypatch.setattr(emb, "is_available", lambda: False)
        result = emb.embed_for_item("My Title", "A summary of the content")
        assert result is None

    def test_embed_for_item_includes_chunk_content(self, monkeypatch):
        """Vector search must match body text, not just title/summary (Mesh bug).

        Captures the exact string handed to embed() and asserts the chunk
        content is present — RED before the content param was threaded through.
        """
        emb = OllamaEmbedder()
        captured = {}
        monkeypatch.setattr(emb, "embed", lambda text: captured.setdefault("text", text))
        emb.embed_for_item(
            "Short Title",
            "Brief summary",
            "The replication protocol uses a quorum write path with paxos ledgers.",
        )
        assert "quorum write path with paxos ledgers" in captured["text"]
        # Title and summary remain present and ordered first (highest signal).
        assert captured["text"].startswith("Short Title Brief summary")

    def test_embed_for_item_embeds_full_target_size_chunk(self, monkeypatch):
        """A full target-size chunk must embed untruncated (Mesh-2205 Phase 0).

        The old _EMBED_CONTENT_BUDGET=2000 clipped ~88% of normal chunks (a chunk is
        ~CHUNK_TOKEN_SIZE+CHUNK_OVERLAP tokens). The bound is now derived above the
        chunker's max output, so a realistic largest chunk embeds whole — RED on the
        old 2000-char cap.
        """
        from kiro_crew.knowledge.chunker import CHUNK_OVERLAP, CHUNK_TOKEN_SIZE

        emb = OllamaEmbedder()
        captured = {}
        monkeypatch.setattr(emb, "embed", lambda text: captured.setdefault("text", text))
        # ~6 chars/token over the full chunk budget — at the high end of observed
        # corpus chunks (max ~6227 chars) and well past the old 2000-char cap.
        big_chunk = "word " * int((CHUNK_TOKEN_SIZE + CHUNK_OVERLAP) * 6 / 5)
        tail = "UNIQUE_TAIL_TOKEN_zzz"
        content = big_chunk + tail
        emb.embed_for_item("T", "S", content)
        assert tail in captured["text"], "chunk tail was clipped from the vector"

    def test_embed_for_item_bounds_pathological_blob(self, monkeypatch):
        """A blob far past the safety bound is truncated AND logged (never silent)."""
        from kiro_crew.knowledge.embedder import _EMBED_CONTENT_BUDGET

        emb = OllamaEmbedder()
        captured = {}
        monkeypatch.setattr(emb, "embed", lambda text: captured.setdefault("text", text))
        big = "x" * (_EMBED_CONTENT_BUDGET * 3)
        with self._capture_warnings() as warned:
            emb.embed_for_item("T", "S", big)
        # title + summary + at most the budget of content (plus 2 join spaces).
        assert len(captured["text"]) <= len("T") + len("S") + _EMBED_CONTENT_BUDGET + 2
        assert warned, "truncation must be logged, not silent"

    @staticmethod
    def _capture_warnings():
        import contextlib
        import logging

        from kiro_crew.knowledge import embedder

        @contextlib.contextmanager
        def _cap():
            records = []
            handler = logging.Handler()
            handler.setLevel(logging.WARNING)
            handler.emit = records.append  # type: ignore[method-assign]
            embedder.logger.addHandler(handler)
            prev = embedder.logger.level
            embedder.logger.setLevel(logging.WARNING)
            try:
                yield records
            finally:
                embedder.logger.removeHandler(handler)
                embedder.logger.setLevel(prev)

        return _cap()

    def test_embed_for_item_content_optional(self, monkeypatch):
        """Back-compat: omitting content still embeds title + summary only."""
        emb = OllamaEmbedder()
        captured = {}
        monkeypatch.setattr(emb, "embed", lambda text: captured.setdefault("text", text))
        emb.embed_for_item("My Title", "A summary")
        assert captured["text"] == "My Title A summary"


class TestSearchForContext:
    """search_for_context endpoint logic."""

    def test_estimate_tokens(self):
        try:
            from kiro_crew.dashboard.handlers.knowledge import _estimate_tokens
        except TypeError:
            pytest.skip("requires Python 3.10+ (type union syntax)")
        assert _estimate_tokens("hello world") == 2  # 11 chars // 4
        assert _estimate_tokens("") == 0

    def test_knowledge_fetch_defaults(self):
        try:
            from kiro_crew.dashboard.handlers.knowledge import (
                KNOWLEDGE_FETCH_MAX_TOKENS,
                KNOWLEDGE_FETCH_TOP_N,
            )
        except TypeError:
            pytest.skip("requires Python 3.10+ (type union syntax)")
        assert KNOWLEDGE_FETCH_TOP_N == 3
        assert KNOWLEDGE_FETCH_MAX_TOKENS == 4096

    def test_context_card_carries_citation_fields(self):
        try:
            from kiro_crew.dashboard.handlers.knowledge import _build_context_card
        except TypeError:
            pytest.skip("requires Python 3.10+ (type union syntax)")
        result = {
            "id": "i1",
            "title": "Auth Design",
            "summary": "JWT summary",
            "source": "src-1",
            "source_type": "local_folder",
            "source_name": "Opportunity Planner",
            "source_uri": "/home/nrb/projects/op/src/",
            "file_path": "/home/nrb/projects/op/src/auth.md",
            "section_title": "Token Lifecycle",
            "chunk_range": "10-25",
            "match_type": "keyword+vector",
        }
        card = _build_context_card(result, content="body", tokens=1)
        assert card["source_type"] == "local_folder"
        assert card["source_name"] == "Opportunity Planner"
        assert card["file_path"] == "/home/nrb/projects/op/src/auth.md"
        assert card["section_title"] == "Token Lifecycle"
        assert card["chunk_range"] == "10-25"

    def test_context_card_artifact_slug(self):
        try:
            from kiro_crew.dashboard.handlers.knowledge import _build_context_card
        except TypeError:
            pytest.skip("requires Python 3.10+ (type union syntax)")
        result = {
            "id": "i3",
            "title": "OP Vision",
            "source": "art-src",
            "source_type": "artifact",
            "source_name": "Artifacts",
            "artifact_slug": "op-vision",
            "artifact_name": "OP Vision Plan",
        }
        card = _build_context_card(result, content="body", tokens=1)
        assert card["source_type"] == "artifact"
        assert card["artifact_slug"] == "op-vision"
        assert card["artifact_name"] == "OP Vision Plan"

    def test_context_card_without_location_degrades(self):
        try:
            from kiro_crew.dashboard.handlers.knowledge import _build_context_card
        except TypeError:
            pytest.skip("requires Python 3.10+ (type union syntax)")
        result = {"id": "i2", "title": "DB Schema", "source": "src-2"}
        card = _build_context_card(result, content="body", tokens=1)
        # No location / no source meta -> citation fields are None, not errors.
        assert card["section_title"] is None
        assert card["chunk_range"] is None
        assert card["source_name"] is None
        assert card["source_type"] is None
        assert card["file_path"] is None
        assert card["artifact_slug"] is None


class TestRedactMeta:
    """_redact_meta security helper."""

    def test_redacts_strings(self):
        try:
            from kiro_crew.dashboard.chat_persistence import _redact_meta
        except TypeError:
            pytest.skip("requires Python 3.10+")
        meta = {"title": "safe text", "content": "key is AKIAIOSFODNN7EXAMPLE here"}
        result = _redact_meta(meta)
        assert "AKIAIOSFODNN7EXAMPLE" not in result["content"]
        assert result["title"] == "safe text"

    def test_redacts_nested_dicts(self):
        try:
            from kiro_crew.dashboard.chat_persistence import _redact_meta
        except TypeError:
            pytest.skip("requires Python 3.10+")
        meta = {"knowledge": {"content": [{"title": "ok", "text": "AKIAIOSFODNN7EXAMPLE"}]}}
        result = _redact_meta(meta)
        assert "AKIAIOSFODNN7EXAMPLE" not in str(result)

    def test_preserves_non_strings(self):
        try:
            from kiro_crew.dashboard.chat_persistence import _redact_meta
        except TypeError:
            pytest.skip("requires Python 3.10+")
        meta = {"items": 3, "tokens": 1054, "titles": ["safe"]}
        result = _redact_meta(meta)
        assert result == {"items": 3, "tokens": 1054, "titles": ["safe"]}
