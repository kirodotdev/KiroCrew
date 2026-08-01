"""Tests for knowledge search upgrade: embedding endpoints + search-for-context."""
import pytest

from kiro_crew.knowledge.embedder import (
    InProcessEmbedder,
    OllamaEmbedder,
    create_embedder_from_config,
)


class TestCreateEmbedderFromConfig:
    """create_embedder_from_config uses shared memory config."""

    def test_always_returns_embedder_even_with_legacy_none(self):
        """Embeddings are always-on: even a legacy 'none' config gets an embedder."""
        cfg = {"memory": {"embedding_provider": "none"}}
        assert isinstance(create_embedder_from_config(cfg), InProcessEmbedder)

    def test_returns_embedder_when_memory_section_missing(self):
        """Embeddings are default-on: no memory section → llama_cpp embedder."""
        emb = create_embedder_from_config({})
        assert isinstance(emb, InProcessEmbedder)

    def test_returns_embedder_when_llama_cpp_enabled(self):
        cfg = {"memory": {"embedding_provider": "llama_cpp"}}
        emb = create_embedder_from_config(cfg)
        assert isinstance(emb, InProcessEmbedder)
        assert emb.model == "qwen3-embedding:0.6b"

    def test_legacy_ollama_provider_coerces_to_embedder(self):
        """'ollama' in an old config transparently upgrades to in-process."""
        cfg = {"memory": {"embedding_provider": "ollama"}}
        emb = create_embedder_from_config(cfg)
        assert isinstance(emb, InProcessEmbedder)

    def test_ollama_embedder_alias_kept(self):
        """OllamaEmbedder remains an alias of InProcessEmbedder for transition."""
        assert OllamaEmbedder is InProcessEmbedder

    def test_legacy_embedding_model_key_is_ignored(self):
        """The Ollama-era embedding_model key must NOT pin the identity —
        honoring it would mislabel vectors produced by the bundled model and
        defeat swap-triggered re-embeds (model id derives from the backend)."""
        cfg = {"memory": {
            "embedding_provider": "llama_cpp",
            "embedding_model": "custom:latest",
        }}
        emb = create_embedder_from_config(cfg)
        assert emb is not None
        assert emb.model != "custom:latest"

    def test_ignores_old_knowledge_embeddings_config(self):
        """Old knowledge.embeddings config is ignored; memory config (default-on)
        decides — an embedder is created regardless of the knowledge section."""
        cfg = {"knowledge": {"embeddings": {"enabled": False}}}
        assert isinstance(create_embedder_from_config(cfg), InProcessEmbedder)


class TestInProcessEmbedder:
    """InProcessEmbedder graceful degradation."""

    def test_embed_returns_none_for_empty_text(self):
        emb = InProcessEmbedder()
        assert emb.embed("") is None
        assert emb.embed("   ") is None

    def test_embed_returns_none_when_unavailable(self, monkeypatch):
        emb = InProcessEmbedder()
        monkeypatch.setattr(emb, "is_available", lambda: False)
        assert emb.embed("hello world") is None

    def test_embed_delegates_to_shared_embedder(self, monkeypatch):
        """embed() routes through the process-wide LlamaCppEmbedder singleton."""

        class _FakeShared:
            def __init__(self):
                self.texts = []

            def is_ready(self):
                return True

            def embed(self, text):
                self.texts.append(text)
                return [0.1, 0.2]

        fake = _FakeShared()
        monkeypatch.setattr(
            "kiro_crew.embeddings.get_shared_embedder", lambda: fake
        )
        emb = InProcessEmbedder()
        assert emb.embed("hello world") == [0.1, 0.2]
        assert fake.texts == ["hello world"]

    def test_is_available_negative_cached_when_not_ready(self, monkeypatch):
        """Model not loaded and probe fails → unavailable, negatively cached."""

        class _FakeShared:
            def __init__(self):
                self.embed_calls = 0

            def is_ready(self):
                return False

            def embed(self, text):
                self.embed_calls += 1
                return None  # model still downloading

        fake = _FakeShared()
        monkeypatch.setattr(
            "kiro_crew.embeddings.get_shared_embedder", lambda: fake
        )
        emb = InProcessEmbedder()
        assert emb.is_available() is False
        assert emb.is_available() is False  # served from negative cache
        assert fake.embed_calls == 1

    def test_embed_for_item_combines_title_and_summary(self, monkeypatch):
        emb = InProcessEmbedder()
        monkeypatch.setattr(emb, "is_available", lambda: False)
        result = emb.embed_for_item("My Title", "A summary of the content")
        assert result is None

    def test_embed_for_item_includes_chunk_content(self, monkeypatch):
        """Vector search must match body text, not just title/summary (Mesh bug).

        Captures the exact string handed to embed() and asserts the chunk
        content is present — RED before the content param was threaded through.
        """
        emb = InProcessEmbedder()
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
        """A full target-size chunk must embed untruncated (Phase 0).

        The old _EMBED_CONTENT_BUDGET=2000 clipped ~88% of normal chunks (a chunk is
        ~CHUNK_TOKEN_SIZE+CHUNK_OVERLAP tokens). The bound is now derived above the
        chunker's max output, so a realistic largest chunk embeds whole — RED on the
        old 2000-char cap.
        """
        from kiro_crew.knowledge.chunker import CHUNK_OVERLAP, CHUNK_TOKEN_SIZE

        emb = InProcessEmbedder()
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

        emb = InProcessEmbedder()
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
        emb = InProcessEmbedder()
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
            from kiro_crew.dashboard.chat_utils import _redact_meta
        except TypeError:
            pytest.skip("requires Python 3.10+")
        meta = {"title": "safe text", "content": "key is AKIAIOSFODNN7EXAMPLE here"}
        result = _redact_meta(meta)
        assert "AKIAIOSFODNN7EXAMPLE" not in result["content"]
        assert result["title"] == "safe text"

    def test_redacts_nested_dicts(self):
        try:
            from kiro_crew.dashboard.chat_utils import _redact_meta
        except TypeError:
            pytest.skip("requires Python 3.10+")
        meta = {"knowledge": {"content": [{"title": "ok", "text": "AKIAIOSFODNN7EXAMPLE"}]}}
        result = _redact_meta(meta)
        assert "AKIAIOSFODNN7EXAMPLE" not in str(result)

    def test_preserves_non_strings(self):
        try:
            from kiro_crew.dashboard.chat_utils import _redact_meta
        except TypeError:
            pytest.skip("requires Python 3.10+")
        meta = {"items": 3, "tokens": 1054, "titles": ["safe"]}
        result = _redact_meta(meta)
        assert result == {"items": 3, "tokens": 1054, "titles": ["safe"]}
