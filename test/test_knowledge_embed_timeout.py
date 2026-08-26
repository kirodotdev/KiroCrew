"""Tests for configurable knowledge-embedder timeout and content budget."""

from __future__ import annotations

from unittest import mock

from kiro_crew.knowledge.embedder import (
    _EMBED_CONTENT_BUDGET,
    TIMEOUT,
    OllamaEmbedder,
    create_embedder_from_config,
    embedder_signature,
)


class TestEmbedderConfigurableLimits:
    def test_defaults_match_module_constants(self):
        emb = OllamaEmbedder()
        assert emb.timeout_secs == TIMEOUT
        assert emb.content_budget == _EMBED_CONTENT_BUDGET

    def test_explicit_overrides_stored(self):
        emb = OllamaEmbedder(timeout_secs=45.0, content_budget=1234)
        assert emb.timeout_secs == 45.0
        assert emb.content_budget == 1234

    def test_embed_for_item_truncates_at_instance_budget(self):
        emb = OllamaEmbedder(content_budget=10)
        with mock.patch.object(emb, "embed", return_value=[0.0]) as embed:
            emb.embed_for_item("title", None, content="x" * 50)
        sent = embed.call_args.args[0]
        # title + space + truncated content (10 chars); tail dropped.
        assert sent == "title " + "x" * 10

    def test_signature_reflects_instance_content_budget(self):
        # Changing the budget must change the embed signature, else items
        # truncated under the old budget would never be re-embedded.
        a = OllamaEmbedder(model="m")
        b = OllamaEmbedder(model="m", content_budget=42)
        assert embedder_signature(a) != embedder_signature(b)


class TestCreateEmbedderFromConfig:
    def test_reads_knowledge_overrides(self):
        cfg = {
            "memory": {"embedding_provider": "llama_cpp"},
            "knowledge": {"embed_timeout_secs": 45, "embed_content_budget": 9999},
        }
        emb = create_embedder_from_config(cfg)
        assert emb is not None
        assert emb.timeout_secs == 45
        assert emb.content_budget == 9999

    def test_falls_back_to_defaults_when_unset(self):
        emb = create_embedder_from_config({"memory": {"embedding_provider": "llama_cpp"}})
        assert emb is not None
        assert emb.timeout_secs == TIMEOUT
        assert emb.content_budget == _EMBED_CONTENT_BUDGET

    def test_zero_sentinel_falls_back_to_defaults(self):
        cfg = {
            "memory": {"embedding_provider": "llama_cpp"},
            "knowledge": {"embed_timeout_secs": 0, "embed_content_budget": 0},
        }
        emb = create_embedder_from_config(cfg)
        assert emb is not None
        assert emb.timeout_secs == TIMEOUT
        assert emb.content_budget == _EMBED_CONTENT_BUDGET

    def test_negative_values_fall_back_to_defaults(self):
        cfg = {
            "memory": {"embedding_provider": "llama_cpp"},
            "knowledge": {"embed_timeout_secs": -5, "embed_content_budget": -10},
        }
        emb = create_embedder_from_config(cfg)
        assert emb is not None
        assert emb.timeout_secs == TIMEOUT
        assert emb.content_budget == _EMBED_CONTENT_BUDGET

    def test_non_numeric_and_bool_fall_back_to_defaults(self):
        cfg = {
            "memory": {"embedding_provider": "llama_cpp"},
            # non-numeric string and a bool (which is an int subclass) must not
            # slip through the positive-number guard.
            "knowledge": {"embed_timeout_secs": "45", "embed_content_budget": True},
        }
        emb = create_embedder_from_config(cfg)
        assert emb is not None
        assert emb.timeout_secs == TIMEOUT
        assert emb.content_budget == _EMBED_CONTENT_BUDGET

    def test_legacy_none_provider_still_gets_embedder(self):
        # Embeddings are always-on: a legacy "none" (previously-disabled)
        # config still yields an embedder (the loader coerces the provider).
        emb = create_embedder_from_config({"memory": {"embedding_provider": "none"}})
        assert emb is not None
