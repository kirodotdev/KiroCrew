"""Tests for LLM-based lesson contradiction detection."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.vector_memory import VectorMemoryStore


class TestFindContradictionCandidates:
    """Unit tests for VectorMemoryStore.find_contradiction_candidates."""

    def test_no_embed_fn_returns_empty(self, tmp_path):
        store = VectorMemoryStore(db_path=tmp_path / "mem.db")
        store.init()
        # No embed_fn set -> returns empty
        assert store.find_contradiction_candidates("some rule") == []

    def test_no_lessons_returns_empty(self, tmp_path):
        store = VectorMemoryStore(db_path=tmp_path / "mem.db")
        store.init()
        store.embed_fn = lambda t: [0.1] * 384
        assert store.find_contradiction_candidates("some rule") == []

    def test_high_similarity_excluded(self, tmp_path):
        """Lessons with cosine >= 0.85 are excluded (handled by existing dedup)."""
        emb = [0.5] * 384
        store = VectorMemoryStore(db_path=tmp_path / "mem.db")
        store.init()
        store.embed_fn = lambda t: emb
        # Write a lesson that will have identical embedding
        store.write_lesson("Use chronological order")
        result = store.find_contradiction_candidates("Use chronological order")
        # sim=1.0 >= 0.85, should be excluded
        assert result == []

    def test_max_5_candidates(self, tmp_path):
        """Caps the candidate list at 5 when more lessons fall in-window."""
        # Word-disjoint lesson texts so write_lesson's substring and
        # topic-overlap dedup never collapse them (each survives as its own
        # lesson). Embeddings are built so every lesson sits at cosine ~0.6
        # with the query (inside [0.4, 0.85)) yet only ~0.36 with each other
        # (below the 0.85 semantic-dedup bar), so all 10 are stored.
        query_text = "novel guidance"
        query_emb = [1.0] + [0.0] * 383
        lesson_words = [
            "alpha", "bravo", "charlie", "delta", "echo",
            "foxtrot", "golf", "hotel", "india", "juliett",
        ]
        emb_map = {query_text: query_emb}
        for i, word in enumerate(lesson_words):
            # 0.6 along query axis + 0.8 along a private axis -> unit vector.
            emb = [0.6] + [0.0] * 383
            emb[i + 1] = 0.8
            emb_map[word] = emb

        store = VectorMemoryStore(db_path=tmp_path / "mem.db")
        store.init()
        store.embed_fn = lambda text: emb_map.get(text)
        for word in lesson_words:
            assert store.write_lesson(word)
        result = store.find_contradiction_candidates(query_text)
        assert len(result) == 5


@pytest.mark.asyncio
class TestResolveContradictions:
    """Tests for _resolve_contradictions async helper."""

    async def test_contradictory_verdict_returns_key(self):
        from kiro_crew.dashboard.handlers.cron import _resolve_contradictions

        state = MagicMock()
        mock_provider = AsyncMock()
        state.sessions.get_or_create = AsyncMock(return_value=(mock_provider, True, False))
        state.sessions.release = MagicMock()

        candidates = [{"key": "lesson.old", "rule": "Use X format", "similarity": 0.65}]

        with patch(
            "kiro_crew.dashboard.handlers.cron.stream_and_collect",
            new=AsyncMock(return_value="CONTRADICTORY"),
        ):
            result = await _resolve_contradictions(state, "Do NOT use X format", candidates)

        assert result == ["lesson.old"]

    async def test_complementary_verdict_keeps_lesson(self):
        from kiro_crew.dashboard.handlers.cron import _resolve_contradictions

        state = MagicMock()
        mock_provider = AsyncMock()
        state.sessions.get_or_create = AsyncMock(return_value=(mock_provider, True, False))
        state.sessions.release = MagicMock()

        candidates = [{"key": "lesson.keep", "rule": "Add CR links", "similarity": 0.55}]

        with patch(
            "kiro_crew.dashboard.handlers.cron.stream_and_collect",
            new=AsyncMock(return_value="COMPLEMENTARY"),
        ):
            result = await _resolve_contradictions(state, "Add stakeholder quotes", candidates)

        assert result == []

    async def test_llm_failure_skips_gracefully(self):
        from kiro_crew.dashboard.handlers.cron import _resolve_contradictions

        state = MagicMock()
        state.sessions.get_or_create = AsyncMock(side_effect=RuntimeError("no provider"))

        candidates = [{"key": "lesson.err", "rule": "Some rule", "similarity": 0.5}]
        result = await _resolve_contradictions(state, "New rule", candidates)
        assert result == []


@pytest.mark.asyncio
class TestResolveAndSupersede:
    """Tests for the backgrounded _resolve_and_supersede helper."""

    async def test_deletes_contradicted_keys(self):
        from kiro_crew.dashboard.handlers.cron import _resolve_and_supersede

        state = MagicMock()
        vs = MagicMock()
        candidates = [{"key": "lesson.old", "rule": "Use X", "similarity": 0.6}]
        with patch(
            "kiro_crew.dashboard.handlers.cron._resolve_contradictions",
            new=AsyncMock(return_value=["lesson.old"]),
        ), patch("kiro_crew.dashboard.handlers.cron._sel"):
            await _resolve_and_supersede(state, "dashboard:ui", "Do NOT use X", candidates, vs)
        vs.delete_semantic.assert_called_once_with("lesson.old", "contradiction_superseded")

    async def test_swallows_exceptions(self):
        """A failed sweep must not propagate — the lesson is already persisted."""
        from kiro_crew.dashboard.handlers.cron import _resolve_and_supersede

        state = MagicMock()
        vs = MagicMock()
        candidates = [{"key": "lesson.x", "rule": "r", "similarity": 0.5}]
        with patch(
            "kiro_crew.dashboard.handlers.cron._resolve_contradictions",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            # Must not raise.
            await _resolve_and_supersede(state, "dashboard:ui", "new", candidates, vs)
        vs.delete_semantic.assert_not_called()

    async def test_one_bad_key_does_not_abort_batch(self):
        """A failure on one key still drains the remaining contradicted keys."""
        from kiro_crew.dashboard.handlers.cron import _resolve_and_supersede

        state = MagicMock()
        vs = MagicMock()
        vs.delete_semantic.side_effect = [RuntimeError("already deleted"), None]
        candidates = [{"key": "lesson.a", "rule": "r", "similarity": 0.6}]
        with patch(
            "kiro_crew.dashboard.handlers.cron._resolve_contradictions",
            new=AsyncMock(return_value=["lesson.a", "lesson.b"]),
        ), patch("kiro_crew.dashboard.handlers.cron._sel"):
            await _resolve_and_supersede(state, "dashboard:ui", "new", candidates, vs)
        # Both keys attempted despite the first raising.
        assert vs.delete_semantic.call_count == 2


@pytest.mark.asyncio
class TestApiLessonsCreateSchedulesSweep:
    """The handler seam: api_lessons_create registers a background task iff
    the contradiction scan finds candidates (locks the fire-and-forget wiring)."""

    def _request(self, state):
        request = MagicMock()
        request.app = {"state": state}
        request.headers = {"X-Session-Key": "dashboard:ui"}
        request.json = AsyncMock(return_value={"rule": "a real rule", "category": "knowledge"})
        return request

    async def _run(self, candidates):
        from kiro_crew.dashboard.handlers import cron

        state = MagicMock()
        state._background_tasks = set()
        vs = MagicMock()
        vs.embed_lesson.return_value = [0.1] * 384
        vs.find_contradiction_candidates.return_value = candidates
        with patch.object(cron, "_get_memory", return_value=MagicMock(vector_store=vs)), \
             patch.object(cron, "_is_restricted_session", return_value=False), \
             patch.object(cron, "_sel"), \
             patch.object(cron, "_resolve_and_supersede", new=AsyncMock()):
            resp = await cron.api_lessons_create(self._request(state))
        assert resp.status == 200
        # Let any scheduled task settle so it doesn't leak a warning.
        tasks = list(state._background_tasks)
        for t in tasks:
            await t
        return tasks

    async def test_schedules_when_candidates_found(self):
        tasks = await self._run([{"key": "lesson.old", "rule": "r", "similarity": 0.6}])
        assert len(tasks) == 1

    async def test_no_task_when_no_candidates(self):
        tasks = await self._run([])
        assert tasks == []
