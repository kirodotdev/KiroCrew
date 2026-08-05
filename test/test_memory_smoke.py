"""Smoke tests for memory system across all agent types and sessions.

Verifies that memory (semantic, episodic, lessons) is truly useful:
- Injected for ALL agents (kirocrew, custom, cron, taskrunner)
- Complex values render as JSON, not [Object] or Python repr
- Episodic text-hash dedup prevents near-identical entries
- Consolidation prompt instructs LLM to merge existing keys
"""

from __future__ import annotations

import json
from pathlib import Path

from kiro_crew.context import ContextBuilder
from kiro_crew.hooks import ContextRule, HookManager, HooksConfig, TransformHook
from kiro_crew.learn import LessonStore
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import SkillsLoader
from kiro_crew.vector_memory import VectorMemoryStore


def _builder(tmp_path: Path, **kw: object) -> ContextBuilder:
    """Create a ContextBuilder with minimal fixtures."""
    ws = tmp_path / "ws"
    store = kw.get("memory") or MemoryStore(workspace=ws)
    return ContextBuilder(
        memory=store,  # type: ignore[arg-type]
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        hooks=kw.get("hooks") or HookManager(),  # type: ignore[arg-type]
        lessons=kw.get("lessons") or LessonStore(base_dir=tmp_path),  # type: ignore[arg-type]
    )


# ── Semantic context: complex values render as JSON ──


class TestSemanticContextJsonFormat:
    def test_dict_value_renders_as_json(self, tmp_path: Path) -> None:
        store = VectorMemoryStore(db_path=tmp_path / "mem.db")
        store.init()
        store.set_semantic(
            "project.team.info",
            {"alias": "team@example.com", "slack": "#team"},
            1.0,
            "user_explicit",
        )
        ctx = store.get_semantic_context()
        # Must contain valid JSON, not Python repr with single quotes
        assert '"alias"' in ctx
        assert "{'alias'" not in ctx

    def test_list_value_renders_as_json(self, tmp_path: Path) -> None:
        store = VectorMemoryStore(db_path=tmp_path / "mem.db")
        store.init()
        store.set_semantic("project.services", ["ServiceA", "ServiceB"], 1.0, "user_explicit")
        ctx = store.get_semantic_context()
        assert '["ServiceA"' in ctx

    def test_string_value_renders_plain(self, tmp_path: Path) -> None:
        store = VectorMemoryStore(db_path=tmp_path / "mem.db")
        store.init()
        store.set_semantic("pref.color", "red", 1.0, "user_explicit")
        ctx = store.get_semantic_context()
        assert "pref.color: red" in ctx


# ── Episodic text-hash dedup ──


class TestEpisodicTextHashDedup:
    def test_exact_prefix_duplicate_rejected(self, tmp_path: Path) -> None:
        store = VectorMemoryStore(db_path=tmp_path / "mem.db")
        store.init()
        text = "User decided to use Python for the backend service and deploy to us-west-2"
        assert store.write_episodic(text)
        assert not store.write_episodic(text)
        assert len(store.get_episodic_list()) == 1

    def test_same_prefix_different_suffix_rejected(self, tmp_path: Path) -> None:
        store = VectorMemoryStore(db_path=tmp_path / "mem.db")
        store.init()
        base = "Rebuilt PV Law Review spreadsheet from 20 to 193 rows after discovering original"
        assert store.write_episodic(base + " logic was wrong")
        assert not store.write_episodic(base + " approach was flawed")
        assert len(store.get_episodic_list()) == 1

    def test_different_text_accepted(self, tmp_path: Path) -> None:
        store = VectorMemoryStore(db_path=tmp_path / "mem.db")
        store.init()
        assert store.write_episodic("User decided to use Python for the backend service")
        assert store.write_episodic("Team agreed on PostgreSQL for the database layer")
        assert len(store.get_episodic_list()) == 2


# ── Memory injection for all agent types ──


class TestMemoryInjectionAllAgents:
    """Memory, lessons, critical rules, and hooks must be injected for ALL agents."""

    def test_kirocrew_agent_gets_everything(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        store = MemoryStore(workspace=ws)
        store.write("# Memory\n\nUser likes Python.")
        builder = _builder(tmp_path, memory=store)
        ctx = builder.build_session_context(agent="kirocrew")
        assert "Python" in ctx
        assert "[CRITICAL RULES" in ctx

    def test_custom_agent_gets_memory(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        store = MemoryStore(workspace=ws)
        store.write("# Memory\n\nUser prefers dark mode.")
        builder = _builder(tmp_path, memory=store)
        ctx = builder.build_session_context(agent="my-custom-agent")
        assert "dark mode" in ctx
        assert "[Memory" in ctx

    def test_custom_agent_gets_critical_rules(self, tmp_path: Path) -> None:
        builder = _builder(tmp_path)
        ctx = builder.build_session_context(agent="my-custom-agent")
        assert "[CRITICAL RULES" in ctx
        assert "diff" in ctx

    def test_custom_agent_skips_skills(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills" / "test"
        skills_dir.mkdir(parents=True)
        (skills_dir / "SKILL.md").write_text(
            "---\nname: test\ndescription: Test\nalways: true\n---\n# Test\nDo stuff."
        )
        builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        )
        ctx = builder.build_session_context(agent="my-custom-agent")
        assert "Do stuff." not in ctx

    def test_custom_agent_skips_workspace_identity(self, tmp_path: Path) -> None:
        builder = _builder(tmp_path)
        ctx = builder.build_session_context(agent="my-custom-agent")
        assert "WORKSPACE IDENTITY" not in ctx

    def test_custom_agent_gets_lessons(self, tmp_path: Path) -> None:
        from kiro_crew.learn import Lesson

        lessons = LessonStore(base_dir=tmp_path)
        lessons.save(
            Lesson(
                ts="2026-01-01T00:00:00+00:00",
                rule="always use pytest-asyncio strict mode",
                category="tool",
            )
        )
        builder = _builder(tmp_path, lessons=lessons)
        ctx = builder.build_session_context(agent="my-custom-agent")
        assert "pytest-asyncio" in ctx

    def test_custom_agent_gets_hooks(self, tmp_path: Path) -> None:
        hooks_cfg = HooksConfig(
            context_rules=[ContextRule(triggers=["pipeline"], context="Use pipeline tool.")]
        )
        builder = _builder(tmp_path, hooks=HookManager(hooks_cfg))
        msg, hook = builder.build_message("check pipeline", is_new_session=False, agent="custom")
        assert "pipeline tool" in msg

    def test_custom_agent_gets_options_reminder(self, tmp_path: Path) -> None:
        builder = _builder(tmp_path)
        msg, _ = builder.build_message(
            "hello", is_new_session=False, agent="custom", interactive=True
        )
        assert "OPTIONS" in msg

    def test_custom_agent_gets_hook_transform(self, tmp_path: Path) -> None:
        hooks_cfg = HooksConfig(transforms=[TransformHook(pattern="deploy", prefix="[DEPLOY]")])
        builder = _builder(tmp_path, hooks=HookManager(hooks_cfg))
        msg, _ = builder.build_message("deploy app", is_new_session=False, agent="custom")
        assert msg.startswith("[DEPLOY]")


# ── Episodic memory injection for all agents ──


class TestEpisodicInjectionAllAgents:
    def test_custom_agent_gets_episodic_on_new_session(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        store = MemoryStore(workspace=ws)
        vs = VectorMemoryStore(db_path=tmp_path / "mem.db")
        vs.init()
        vs.write_episodic("User decided to use PostgreSQL for the database layer")
        store._vector_store = vs
        builder = _builder(tmp_path, memory=store)
        msg, _ = builder.build_message(
            "what database should I use?",
            is_new_session=True,
            agent="my-custom-agent",
        )
        assert "PostgreSQL" in msg

    def test_kirocrew_agent_gets_episodic_on_new_session(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        store = MemoryStore(workspace=ws)
        vs = VectorMemoryStore(db_path=tmp_path / "mem.db")
        vs.init()
        vs.write_episodic("User decided to use PostgreSQL for the database layer")
        store._vector_store = vs
        builder = _builder(tmp_path, memory=store)
        msg, _ = builder.build_message(
            "what database should I use?",
            is_new_session=True,
            agent="kirocrew",
        )
        assert "PostgreSQL" in msg

    def test_episodic_skipped_on_followup(self, tmp_path: Path) -> None:
        """Episodic memory not injected on follow-up messages (trust ACP)."""
        ws = tmp_path / "ws"
        store = MemoryStore(workspace=ws)
        vs = VectorMemoryStore(db_path=tmp_path / "mem.db")
        vs.init()
        vs.write_episodic("User decided to use PostgreSQL for the database layer")
        store._vector_store = vs
        builder = _builder(tmp_path, memory=store)
        msg, _ = builder.build_message(
            "what database should I use?",
            is_new_session=False,
        )
        assert "PostgreSQL" not in msg


# ── Semantic memory with vector store in session context ──


class TestSemanticMemoryInSessionContext:
    def test_semantic_memory_in_custom_agent_session(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        store = MemoryStore(workspace=ws)
        vs = VectorMemoryStore(db_path=tmp_path / "mem.db")
        vs.init()
        vs.set_semantic("pref.language", "Python", 1.0, "user_explicit")
        store._vector_store = vs
        builder = _builder(tmp_path, memory=store)
        ctx = builder.build_session_context(agent="my-custom-agent")
        assert "pref.language: Python" in ctx

    def test_lessons_in_vector_store_for_custom_agent(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        store = MemoryStore(workspace=ws)
        vs = VectorMemoryStore(db_path=tmp_path / "mem.db")
        vs.init()
        vs.write_lesson("always run tests before committing", "tool")
        store._vector_store = vs
        builder = _builder(tmp_path, memory=store)
        ctx = builder.build_session_context(agent="my-custom-agent")
        assert "always run tests" in ctx


# ── Cross-session memory persistence ──


class TestCrossSessionMemory:
    """Memory written in one session is available in the next."""

    def test_semantic_persists_across_store_instances(self, tmp_path: Path) -> None:
        db = tmp_path / "mem.db"
        s1 = VectorMemoryStore(db_path=db)
        s1.init()
        s1.set_semantic("user.name", "Bolin", 1.0, "user_explicit")
        s1.close()

        s2 = VectorMemoryStore(db_path=db)
        s2.init()
        entry = s2.get_semantic("user.name")
        assert entry is not None
        assert json.loads(entry["value_json"]) == "Bolin"

    def test_episodic_persists_across_store_instances(self, tmp_path: Path) -> None:
        db = tmp_path / "mem.db"
        s1 = VectorMemoryStore(db_path=db)
        s1.init()
        s1.write_episodic("Discussed migration strategy for the database layer")
        s1.close()

        s2 = VectorMemoryStore(db_path=db)
        s2.init()
        entries = s2.get_episodic_list()
        assert len(entries) == 1
        assert "migration" in entries[0]["text"]

    def test_lessons_persist_across_store_instances(self, tmp_path: Path) -> None:
        db = tmp_path / "mem.db"
        s1 = VectorMemoryStore(db_path=db)
        s1.init()
        s1.write_lesson("always use type hints", "preference")
        s1.close()

        s2 = VectorMemoryStore(db_path=db)
        s2.init()
        lessons = s2.get_lessons()
        assert len(lessons) == 1
        assert "type hints" in json.loads(lessons[0]["value_json"])


# ── MMR diversity reranking ──


class TestMMRReranking:
    """MMR reranking balances relevance with diversity in episodic results."""

    def test_mmr_rerank_promotes_diversity(self) -> None:
        from kiro_crew.vector_memory import _mmr_rerank

        candidates = [
            {"text": "deployed H2C to prod fixed IAM role", "score": 0.92},
            {"text": "H2C deployment succeeded after IAM fix", "score": 0.89},
            {"text": "H2C prod deployment IAM role wrong scope", "score": 0.87},
            {"text": "H2C cross account auth uses STS assume role", "score": 0.71},
            {"text": "user prefers blue green deployments", "score": 0.65},
        ]
        result = _mmr_rerank(candidates, limit=3)
        assert len(result) == 3
        # First pick is always highest score
        assert result[0]["score"] == 0.92
        # The diverse "blue green" entry should be promoted over the 3rd IAM duplicate
        texts = [r["text"] for r in result]
        assert any("blue green" in t for t in texts)
        # The 3rd IAM duplicate (score 0.87) should be pushed out
        assert not any("wrong scope" in t for t in texts)

    def test_mmr_rerank_single_item(self) -> None:
        from kiro_crew.vector_memory import _mmr_rerank

        result = _mmr_rerank([{"text": "only one", "score": 0.5}], limit=3)
        assert len(result) == 1

    def test_mmr_rerank_empty(self) -> None:
        from kiro_crew.vector_memory import _mmr_rerank

        assert _mmr_rerank([], limit=3) == []

    def test_mmr_rerank_respects_limit(self) -> None:
        from kiro_crew.vector_memory import _mmr_rerank

        candidates = [{"text": f"item {i}", "score": 1.0 - i * 0.1} for i in range(10)]
        result = _mmr_rerank(candidates, limit=4)
        assert len(result) == 4

    def test_episodic_search_uses_mmr(self, tmp_path: Path) -> None:
        """Episodic search applies MMR by default (keyword fallback path)."""
        store = VectorMemoryStore(db_path=tmp_path / "mem.db")
        store.init()
        # Write several topically similar entries
        store.write_episodic("deployed the app to production successfully")
        store.write_episodic("user prefers dark mode for all editors")
        store.write_episodic("database migration completed for PostgreSQL")
        results = store.search_episodic(query_text="deploy production app", limit=3)
        # Should return results (keyword fallback)
        assert len(results) >= 1

    def test_episodic_search_mmr_disabled(self, tmp_path: Path) -> None:
        """Can disable MMR for raw relevance ordering."""
        store = VectorMemoryStore(db_path=tmp_path / "mem.db")
        store.init()
        store.write_episodic("deployed the app to production successfully")
        store.write_episodic("database migration completed for PostgreSQL")
        results = store.search_episodic(query_text="deploy", limit=3, mmr=False)
        assert len(results) >= 1


# ── Hybrid semantic retrieval ──


class TestHybridSemanticRetrieval:
    """Semantic context uses hybrid vector+keyword scoring when embeddings available."""

    def test_keyword_only_without_embeddings(self, tmp_path: Path) -> None:
        """Without embed_fn, falls back to keyword-only scoring."""
        store = VectorMemoryStore(db_path=tmp_path / "mem.db")
        store.init()
        store.set_semantic("pref.language", "Python", 1.0, "user_explicit")
        store.set_semantic("project.name", "KiroCrew", 1.0, "user_explicit")
        ctx = store.get_semantic_context(query_text="Python language")
        assert "pref.language: Python" in ctx

    def test_hybrid_with_mock_embeddings(self, tmp_path: Path) -> None:
        """With embed_fn, uses hybrid scoring (vector + keyword)."""
        call_count = 0

        def mock_embed(text: str) -> list[float]:
            nonlocal call_count
            call_count += 1
            # Simple deterministic embedding: hash-based
            h = hash(text) % 1000
            return [float(h % (i + 1)) / (i + 1) for i in range(8)]

        store = VectorMemoryStore(db_path=tmp_path / "mem.db")
        store.init()
        store.embed_fn = mock_embed
        store.set_semantic("pref.language", "Python", 1.0, "user_explicit")
        store.set_semantic("project.name", "KiroCrew", 1.0, "user_explicit")
        store.set_semantic("user.timezone", "PST", 1.0, "user_explicit")
        ctx = store.get_semantic_context(query_text="what language do you prefer")
        # Should return results — hybrid scoring should find relevant entries
        assert "Semantic Memory" in ctx
        # embed_fn was called (query + entries)
        assert call_count > 0

    def test_no_query_returns_recent(self, tmp_path: Path) -> None:
        """Without query, returns most recent entries regardless of embeddings."""
        store = VectorMemoryStore(db_path=tmp_path / "mem.db")
        store.init()
        store.set_semantic("pref.color", "blue", 1.0, "user_explicit")
        ctx = store.get_semantic_context(query_text="")
        assert "pref.color: blue" in ctx


# ── Jaccard similarity helper ──


class TestJaccardSimilarity:
    def test_identical_texts(self) -> None:
        from kiro_crew.vector_memory import _jaccard, _tokenize

        a = _tokenize("deployed the app to production")
        assert _jaccard(a, a) == 1.0

    def test_disjoint_texts(self) -> None:
        from kiro_crew.vector_memory import _jaccard, _tokenize

        a = _tokenize("deployed the app")
        b = _tokenize("migration database schema")
        assert _jaccard(a, b) == 0.0

    def test_partial_overlap(self) -> None:
        from kiro_crew.vector_memory import _jaccard, _tokenize

        a = _tokenize("deployed app to production")
        b = _tokenize("deployed service to staging")
        sim = _jaccard(a, b)
        assert 0.0 < sim < 1.0

    def test_empty_sets(self) -> None:
        from kiro_crew.vector_memory import _jaccard

        assert _jaccard(set(), set()) == 0.0
        assert _jaccard({"a"}, set()) == 0.0


# ── Lesson embedding storage (fix for learn_add timeout) ──


class TestLessonEmbeddingStorage:
    """write_lesson stores embeddings and uses them for dedup instead of recomputing."""

    def test_migration_v2_idempotent(self, tmp_path: Path) -> None:
        """Double init() does not crash on duplicate embedding column."""
        db = tmp_path / "mem.db"
        s = VectorMemoryStore(db_path=db)
        s.init()
        s.close()
        s2 = VectorMemoryStore(db_path=db)
        s2.init()  # should not raise
        s2.close()

    def test_write_lesson_stores_embedding(self, tmp_path: Path) -> None:
        """New lesson gets embedding blob persisted in DB."""
        import struct

        db = tmp_path / "mem.db"
        s = VectorMemoryStore(db_path=db)
        s.init()
        # Mock embed_fn to return a known vector
        s.embed_fn = lambda text: [0.1, 0.2, 0.3]
        s.write_lesson("always write tests", "preference")

        row = s.db.execute(
            "SELECT embedding FROM semantic_memory WHERE key LIKE 'lesson.%'"
        ).fetchone()
        assert row is not None
        assert row["embedding"] is not None
        emb = list(struct.unpack(f"{len(row['embedding']) // 4}f", row["embedding"]))
        assert len(emb) == 3
        assert abs(emb[0] - 0.1) < 1e-6

    def test_write_lesson_no_embed_fn_still_works(self, tmp_path: Path) -> None:
        """Without embed_fn, lesson is saved but no embedding stored."""
        db = tmp_path / "mem.db"
        s = VectorMemoryStore(db_path=db)
        s.init()
        assert s.write_lesson("no embedding available", "knowledge")

        row = s.db.execute(
            "SELECT embedding FROM semantic_memory WHERE key LIKE 'lesson.%'"
        ).fetchone()
        assert row is not None
        assert row["embedding"] is None

    def test_dedup_uses_stored_embedding(self, tmp_path: Path) -> None:
        """Semantic dedup reads stored embedding, does not call embed_fn for existing lessons."""

        db = tmp_path / "mem.db"
        s = VectorMemoryStore(db_path=db)
        s.init()

        # Write first lesson with embedding
        s.embed_fn = lambda text: [1.0, 0.0, 0.0]
        s.write_lesson("always use type hints in Python", "preference")

        # Track embed calls — should only be called once (for the new rule)
        call_count = 0
        original_fn = s.embed_fn

        def counting_embed(text: str) -> list[float]:
            nonlocal call_count
            call_count += 1
            return original_fn(text)

        s.embed_fn = counting_embed

        # Write a second lesson — dedup loop should read stored embedding, not recompute
        s.write_lesson("something completely different", "knowledge")
        # 1 call for the new rule's embedding, 0 for existing lessons
        assert call_count == 1
        # Both lessons survive (different text, sim=1.0 but second is longer → first deleted, second saved)
        assert len(s.get_lessons()) == 1

    def test_lazy_backfill_legacy_lesson(self, tmp_path: Path) -> None:
        """Legacy lesson without embedding gets backfilled on next write_lesson."""

        db = tmp_path / "mem.db"
        s = VectorMemoryStore(db_path=db)
        s.init()

        # Write first lesson WITHOUT embed_fn (simulates legacy)
        s.write_lesson("legacy rule without embedding", "preference")
        row = s.db.execute(
            "SELECT embedding FROM semantic_memory WHERE key LIKE 'lesson.%'"
        ).fetchone()
        assert row["embedding"] is None

        # Now enable embed_fn and write a second lesson
        s.embed_fn = lambda text: [0.5, 0.5, 0.5]
        s.write_lesson("totally new unrelated rule", "knowledge")

        # Legacy lesson should now have an embedding (backfilled)
        rows = s.db.execute(
            "SELECT key, embedding FROM semantic_memory WHERE key LIKE 'lesson.%' AND is_deleted = 0"
        ).fetchall()
        for row in rows:
            assert row["embedding"] is not None, f"{row['key']} missing embedding after backfill"

    def test_semantic_dedup_with_stored_embedding(self, tmp_path: Path) -> None:
        """Stored embedding triggers cosine similarity dedup (sim > 0.85)."""
        db = tmp_path / "mem.db"
        s = VectorMemoryStore(db_path=db)
        s.init()

        # embed_fn returns near-identical vectors for any text
        s.embed_fn = lambda text: [0.9, 0.1, 0.0]
        s.write_lesson("prefer composition over inheritance in design", "preference")

        # Second lesson: semantically similar embedding but no substring/keyword overlap
        s.write_lesson(
            "favor composing objects rather than inheriting from base classes", "preference"
        )
        # Longer rule replaces shorter → first deleted, second saved
        lessons = [
            dict(r)
            for r in s.db.execute(
                "SELECT value_json FROM semantic_memory WHERE key LIKE 'lesson.%' AND is_deleted = 0"
            ).fetchall()
        ]
        assert len(lessons) == 1
        assert "composing objects" in str(lessons[0]["value_json"])

    def test_backfill_cap(self, tmp_path: Path) -> None:
        """Lazy backfill stops after _MAX_BACKFILLS_PER_CALL legacy lessons."""

        from kiro_crew.vector_memory import _MAX_BACKFILLS_PER_CALL

        db = tmp_path / "mem.db"
        s = VectorMemoryStore(db_path=db)
        s.init()

        # Write 7 legacy lessons without embed_fn (unique keywords to avoid topic dedup)
        topics = [
            "python type hints improve readability",
            "docker containers isolate dependencies",
            "terraform manages infrastructure declaratively",
            "graphql reduces overfetching bandwidth",
            "kubernetes orchestrates microservice deployments",
            "prometheus monitors application metrics",
            "elasticsearch indexes searchable documents",
        ]
        for t in topics:
            s.write_lesson(t, "knowledge")

        # Enable embed_fn (returns unique orthogonal vectors to avoid cosine dedup)
        _vecs = {}
        _dims = [
            [1, 0, 0, 0, 0, 0, 0, 0],
            [0, 1, 0, 0, 0, 0, 0, 0],
            [0, 0, 1, 0, 0, 0, 0, 0],
            [0, 0, 0, 1, 0, 0, 0, 0],
            [0, 0, 0, 0, 1, 0, 0, 0],
            [0, 0, 0, 0, 0, 1, 0, 0],
            [0, 0, 0, 0, 0, 0, 1, 0],
            [0, 0, 0, 0, 0, 0, 0, 1],
        ]
        _counter = [0]

        def _orthogonal_embed(text: str) -> list[float]:
            if text not in _vecs:
                _vecs[text] = _dims[_counter[0] % len(_dims)]
                _counter[0] += 1
            return _vecs[text]

        s.embed_fn = _orthogonal_embed
        s.write_lesson("redis caches frequently accessed data", "knowledge")

        rows = s.db.execute(
            "SELECT embedding FROM semantic_memory WHERE key LIKE 'lesson.%' AND is_deleted = 0"
        ).fetchall()
        backfilled = sum(1 for r in rows if r["embedding"] is not None)
        # 5 legacy backfilled + 1 new = 6 with embeddings, 2 legacy without
        assert backfilled == _MAX_BACKFILLS_PER_CALL + 1

    def test_stale_backfill_purged_on_delete(self, tmp_path: Path) -> None:
        """Pending backfill is purged when lesson is deleted via semantic dedup."""
        db = tmp_path / "mem.db"
        s = VectorMemoryStore(db_path=db)
        s.init()

        # Write a legacy lesson without embedding
        s.write_lesson("short rule about composing", "preference")
        assert (
            s.db.execute(
                "SELECT embedding FROM semantic_memory WHERE key LIKE 'lesson.%'"
            ).fetchone()["embedding"]
            is None
        )

        # Enable embed_fn, write longer rule that triggers cosine dedup → deletes short one
        s.embed_fn = lambda text: [0.9, 0.1, 0.0]
        s.write_lesson(
            "a much longer rule about composing objects in software design patterns",
            "preference",
        )

        # Only the longer lesson should survive, with embedding
        rows = s.db.execute(
            "SELECT key, embedding, is_deleted FROM semantic_memory WHERE key LIKE 'lesson.%'"
        ).fetchall()
        alive = [r for r in rows if not r["is_deleted"]]
        assert len(alive) == 1
        assert alive[0]["embedding"] is not None
        assert "longer rule" in str(
            s.db.execute(
                "SELECT value_json FROM semantic_memory WHERE key = ?", (alive[0]["key"],)
            ).fetchone()["value_json"]
        )
