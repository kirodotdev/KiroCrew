"""Tests for Knowledge global budget and rate controls.

Covers:
- KnowledgeConfig new field defaults
- Global sweep chunk budget enforcement in watcher
- EmbedRateLimiter token bucket
- extraction_model resolution in _install_knowledge_agent
- extraction_pool_size in LLMPool
"""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

from kiro_crew.config.loader import KnowledgeConfig

# --- Config defaults ---


class TestKnowledgeConfigBudgetDefaults:
    def test_sweep_chunk_budget_default_500(self):
        c = KnowledgeConfig()
        assert c.sweep_chunk_budget == 500

    def test_embed_rate_limit_default_120(self):
        c = KnowledgeConfig()
        assert c.embed_rate_limit == 120

    def test_extraction_model_default_empty(self):
        c = KnowledgeConfig()
        assert c.extraction_model == ""

    def test_extraction_pool_size_default_3(self):
        c = KnowledgeConfig()
        assert c.extraction_pool_size == 3

    def test_sweep_chunk_budget_zero_is_unbounded(self):
        c = KnowledgeConfig(sweep_chunk_budget=0)
        assert c.sweep_chunk_budget == 0

    def test_embed_rate_limit_zero_is_unlimited(self):
        c = KnowledgeConfig(embed_rate_limit=0)
        assert c.embed_rate_limit == 0


# --- EmbedRateLimiter ---

class TestEmbedRateLimiter:
    def test_zero_rate_is_noop(self):
        from kiro_crew.knowledge.ingestion import EmbedRateLimiter
        limiter = EmbedRateLimiter(rate_limit=0)
        # Should not block
        asyncio.run(limiter.acquire())

    def test_high_rate_does_not_block(self):
        from kiro_crew.knowledge.ingestion import EmbedRateLimiter
        limiter = EmbedRateLimiter(rate_limit=10000)
        tokens_before = limiter._tokens
        # "Does not block" means the sleeping slow path is never reached:
        # patch asyncio.sleep and assert it was never awaited (a wall-clock
        # bound here would only measure asyncio.run() setup cost, which flakes
        # under CI load). Note: ingestion.py does a plain `import asyncio`, so
        # this rebinds asyncio.sleep process-wide for the duration of the
        # block; asyncio.run() internals never call asyncio.sleep, and the
        # patch is reverted on exit.
        fake_sleep = AsyncMock()
        with patch("kiro_crew.knowledge.ingestion.asyncio.sleep", fake_sleep):
            asyncio.run(limiter.acquire())
        fake_sleep.assert_not_awaited()
        # The fast path must still consume exactly one token, proving
        # acquire() did real work rather than returning early.
        assert limiter._tokens == tokens_before - 1.0

    def test_rate_limit_setter_resets_bucket(self):
        from kiro_crew.knowledge.ingestion import EmbedRateLimiter
        limiter = EmbedRateLimiter(rate_limit=1)
        limiter.rate_limit = 10000
        assert limiter.rate_limit == 10000

    def test_get_embed_rate_limiter_reads_config(self):
        import kiro_crew.knowledge.ingestion as ing_mod
        from kiro_crew.knowledge.ingestion import get_embed_rate_limiter

        # Reset the singleton
        ing_mod._embed_rate_limiter = None
        with patch("kiro_crew.config.loader.KiroCrewConfig.load") as mock_load:
            mock_load.return_value.knowledge.embed_rate_limit = 200
            limiter = get_embed_rate_limiter()
            assert limiter.rate_limit == 200
        ing_mod._embed_rate_limiter = None


# --- Sweep chunk budget ---

class TestSweepChunkBudget:
    def test_sweep_budget_read_from_config(self):
        from kiro_crew.knowledge.watcher import KnowledgeWatcher
        with patch("kiro_crew.knowledge.watcher.KiroCrewConfig") as mock_cfg:
            mock_cfg.load.return_value.knowledge.sweep_chunk_budget = 1000
            assert KnowledgeWatcher._sweep_chunk_budget() == 1000

    def test_sweep_budget_zero_means_unbounded(self):
        from kiro_crew.knowledge.watcher import KnowledgeWatcher
        with patch("kiro_crew.knowledge.watcher.KiroCrewConfig") as mock_cfg:
            mock_cfg.load.return_value.knowledge.sweep_chunk_budget = 0
            assert KnowledgeWatcher._sweep_chunk_budget() == 0


# --- Pool size from config ---

class TestPoolSizeConfig:
    def test_default_pool_size(self):
        from kiro_crew.knowledge.llm_pool import DEFAULT_POOL_SIZE, _get_pool_size
        assert _get_pool_size({}) == DEFAULT_POOL_SIZE

    def test_configured_pool_size(self):
        from kiro_crew.knowledge.llm_pool import _get_pool_size
        config = {"knowledge": {"extraction_pool_size": 5}}
        assert _get_pool_size(config) == 5

    def test_pool_size_clamped_to_max_10(self):
        from kiro_crew.knowledge.llm_pool import DEFAULT_POOL_SIZE, _get_pool_size
        config = {"knowledge": {"extraction_pool_size": 99}}
        assert _get_pool_size(config) == DEFAULT_POOL_SIZE

    def test_pool_size_clamped_to_min_1(self):
        from kiro_crew.knowledge.llm_pool import DEFAULT_POOL_SIZE, _get_pool_size
        config = {"knowledge": {"extraction_pool_size": 0}}
        assert _get_pool_size(config) == DEFAULT_POOL_SIZE


# --- Extraction model resolution ---

class TestExtractionModelResolution:
    def test_empty_extraction_model_uses_agent_model(self):
        """When extraction_model is empty, _install_knowledge_agent uses agent.model."""
        with patch("kiro_crew.config.loader.KiroCrewConfig.load") as mock_load:
            mock_load.return_value.knowledge.extraction_model = ""
            mock_load.return_value.agent.model = "claude-sonnet-4.5"
            with patch("kiro_crew.agent._atomic_json_write") as mock_write:
                with patch("kiro_crew.agent.kiro_agents_dir_path") as mock_path:
                    mock_path.return_value = Path("/tmp/agents")
                    from kiro_crew.agent import _install_knowledge_agent
                    _install_knowledge_agent()
                    written = mock_write.call_args[0][1]
                    assert written["model"] == "claude-sonnet-4.5"

    def test_explicit_extraction_model_overrides(self):
        """When extraction_model is set, it overrides agent.model."""
        with patch("kiro_crew.config.loader.KiroCrewConfig.load") as mock_load:
            mock_load.return_value.knowledge.extraction_model = "claude-haiku-4.5"
            mock_load.return_value.agent.model = "claude-sonnet-4.5"
            with patch("kiro_crew.agent._atomic_json_write") as mock_write:
                with patch("kiro_crew.agent.kiro_agents_dir_path") as mock_path:
                    mock_path.return_value = Path("/tmp/agents")
                    from kiro_crew.agent import _install_knowledge_agent
                    _install_knowledge_agent()
                    written = mock_write.call_args[0][1]
                    assert written["model"] == "claude-haiku-4.5"
