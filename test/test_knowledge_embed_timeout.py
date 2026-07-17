"""Tests for configurable knowledge-embedder timeout and content budget."""
from __future__ import annotations

import json
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

    def test_embed_uses_instance_timeout(self):
        emb = OllamaEmbedder(timeout_secs=42.0)
        emb._available = True  # skip the availability probe
        resp = mock.MagicMock()
        resp.read.return_value = json.dumps({"embedding": [0.1, 0.2]}).encode()
        cm = mock.MagicMock()
        cm.__enter__.return_value = resp
        with mock.patch(
            "kiro_crew.knowledge.embedder.urllib.request.urlopen", return_value=cm
        ) as urlopen:
            out = emb.embed("hello")
        assert out == [0.1, 0.2]
        # The configured timeout must be what reaches urlopen.
        assert urlopen.call_args.kwargs["timeout"] == 42.0

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
        assert embedder_signature(OllamaEmbedder()) != embedder_signature(
            OllamaEmbedder(content_budget=42)
        )


class TestCreateEmbedderFromConfig:
    def test_reads_knowledge_overrides(self):
        cfg = {
            "memory": {"embedding_provider": "ollama"},
            "knowledge": {"embed_timeout_secs": 45, "embed_content_budget": 9999},
        }
        emb = create_embedder_from_config(cfg)
        assert emb is not None
        assert emb.timeout_secs == 45
        assert emb.content_budget == 9999

    def test_falls_back_to_defaults_when_unset(self):
        emb = create_embedder_from_config({"memory": {"embedding_provider": "ollama"}})
        assert emb is not None
        assert emb.timeout_secs == TIMEOUT
        assert emb.content_budget == _EMBED_CONTENT_BUDGET

    def test_zero_sentinel_falls_back_to_defaults(self):
        cfg = {
            "memory": {"embedding_provider": "ollama"},
            "knowledge": {"embed_timeout_secs": 0, "embed_content_budget": 0},
        }
        emb = create_embedder_from_config(cfg)
        assert emb is not None
        assert emb.timeout_secs == TIMEOUT
        assert emb.content_budget == _EMBED_CONTENT_BUDGET

    def test_negative_values_fall_back_to_defaults(self):
        cfg = {
            "memory": {"embedding_provider": "ollama"},
            "knowledge": {"embed_timeout_secs": -5, "embed_content_budget": -10},
        }
        emb = create_embedder_from_config(cfg)
        assert emb is not None
        assert emb.timeout_secs == TIMEOUT
        assert emb.content_budget == _EMBED_CONTENT_BUDGET

    def test_non_numeric_and_bool_fall_back_to_defaults(self):
        cfg = {
            "memory": {"embedding_provider": "ollama"},
            # non-numeric string and a bool (which is an int subclass) must not
            # slip through the positive-number guard.
            "knowledge": {"embed_timeout_secs": "45", "embed_content_budget": True},
        }
        emb = create_embedder_from_config(cfg)
        assert emb is not None
        assert emb.timeout_secs == TIMEOUT
        assert emb.content_budget == _EMBED_CONTENT_BUDGET

    def test_returns_none_when_provider_not_ollama(self):
        assert create_embedder_from_config({"memory": {"embedding_provider": "none"}}) is None
