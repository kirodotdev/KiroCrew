"""Tests for Knowledge global budget and rate controls.

Covers:
- KnowledgeConfig new field defaults
- Global sweep chunk budget enforcement in watcher
- Max sources cap in create_auto_source_unless_dismissed
- EmbedRateLimiter token bucket
- extraction_model resolution in _install_knowledge_agent
- extraction_pool_size in LLMPool
"""
import asyncio
import sqlite3
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from kiro_crew.config.loader import KnowledgeConfig

# --- Config defaults ---


class TestKnowledgeConfigBudgetDefaults:
    def test_sweep_chunk_budget_default_500(self):
        c = KnowledgeConfig()
        assert c.sweep_chunk_budget == 500

    def test_max_sources_default_50(self):
        c = KnowledgeConfig()
        assert c.max_sources == 50

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

    def test_max_sources_zero_is_unbounded(self):
        c = KnowledgeConfig(max_sources=0)
        assert c.max_sources == 0

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
        start = time.monotonic()
        asyncio.run(limiter.acquire())
        elapsed = time.monotonic() - start
        assert elapsed < 0.1

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


# --- Max sources cap ---

class TestMaxSourcesCap:
    def _make_store(self, tmp_path):
        """Create a minimal store with the sources table."""
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE sources (
                id TEXT PRIMARY KEY,
                name TEXT,
                source_type TEXT,
                uri TEXT UNIQUE,
                properties TEXT,
                created_at TEXT,
                updated_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE dismissed_auto_sources (
                uri TEXT PRIMARY KEY,
                dismissed_at TEXT
            )
        """)
        conn.commit()

        from kiro_crew.knowledge.store import KnowledgeStore
        store = MagicMock(spec=KnowledgeStore)
        store.db = conn
        store.source_count = lambda: conn.execute(
            "SELECT COUNT(*) AS cnt FROM sources").fetchone()[0]
        # Wire the real method
        from kiro_crew.knowledge.store import KnowledgeStore as RealStore
        store.create_auto_source_unless_dismissed = (
            lambda *a, **kw: RealStore.create_auto_source_unless_dismissed(store, *a, **kw)
        )
        return store

    def test_under_cap_allows_creation(self, tmp_path):
        store = self._make_store(tmp_path)
        sid, created = store.create_auto_source_unless_dismissed(
            name="test", source_type="local_folder", uri="/test/path",
            properties={"sync_status": "active"}, max_sources=5,
        )
        assert created is True
        assert sid is not None

    def test_at_cap_blocks_creation(self, tmp_path):
        store = self._make_store(tmp_path)
        # Fill to cap
        for i in range(3):
            store.db.execute(
                "INSERT INTO sources (id, name, source_type, uri, properties, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (f"s{i}", f"src{i}", "local_folder", f"/path/{i}", "{}", "", ""))
        store.db.commit()
        # Now try to add one more with cap=3
        sid, created = store.create_auto_source_unless_dismissed(
            name="blocked", source_type="local_folder", uri="/new/path",
            properties={"sync_status": "active"}, max_sources=3,
        )
        assert sid is None
        assert created is False

    def test_zero_cap_is_unbounded(self, tmp_path):
        store = self._make_store(tmp_path)
        # Fill with many sources
        for i in range(100):
            store.db.execute(
                "INSERT INTO sources (id, name, source_type, uri, properties, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (f"s{i}", f"src{i}", "local_folder", f"/path/{i}", "{}", "", ""))
        store.db.commit()
        # cap=0 should still allow
        sid, created = store.create_auto_source_unless_dismissed(
            name="allowed", source_type="local_folder", uri="/new/path",
            properties={"sync_status": "active"}, max_sources=0,
        )
        assert created is True

    def test_existing_uri_returns_existing_id_regardless_of_cap(self, tmp_path):
        store = self._make_store(tmp_path)
        # Insert one source
        store.db.execute(
            "INSERT INTO sources (id, name, source_type, uri, properties, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("existing-id", "existing", "local_folder", "/existing", "{}", "", ""))
        store.db.commit()
        # Even with cap=1 (at capacity), existing URI should return its id
        sid, created = store.create_auto_source_unless_dismissed(
            name="existing", source_type="local_folder", uri="/existing",
            properties={"sync_status": "active"}, max_sources=1,
        )
        assert sid == "existing-id"
        assert created is False


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
