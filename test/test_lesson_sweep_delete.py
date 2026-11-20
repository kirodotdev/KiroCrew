"""What a contradiction-sweep delete tells the dashboard, and what it never deletes.

Split from ``test_lesson_contradiction.py`` (file line cap); same helper under test,
``_resolve_and_supersede``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.vector_memory import VectorMemoryStore

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("announce", [True, False])
async def test_a_sweep_delete_announces_lessons_changed(tmp_path, announce):
    """The sweep deletes after learn_add returned: its delete gets its own frames."""
    from kiro_crew import lesson_events
    from kiro_crew.dashboard.handlers.cron import _resolve_and_supersede

    vs = VectorMemoryStore(db_path=tmp_path / "memory.db")
    await asyncio.to_thread(vs.init)
    frames: list[dict] = []
    lesson_events.set_lessons_changed_hook(frames.append)
    state = MagicMock()
    try:
        value = {"rule": "Use X", "category": "tool"}
        await asyncio.to_thread(vs.set_semantic, "lesson.old", value, 1.0, "consolidation")
        seen_body = (await asyncio.to_thread(vs.get_lessons))[0]["value_json"]
        candidates = [{"key": "lesson.old", "rule": "Use X", "similarity": 0.6}]
        with (
            patch(
                "kiro_crew.dashboard.handlers.cron._resolve_contradictions",
                new=AsyncMock(return_value=[("lesson.old", seen_body)]),
            ),
            patch("kiro_crew.dashboard.handlers.cron._sel"),
        ):
            await _resolve_and_supersede(
                state, "dashboard:ui", "Do NOT use X", candidates, vs, announce=announce
            )
        assert await asyncio.to_thread(vs.get_semantic, "lesson.old") is None
        expected = [{"action": "removed", "source": "user_explicit", "count": 1}]
        assert frames == (expected if announce else [])
        # The open Memory tab re-reads on the refresh frame, on every store.
        state.push_refresh.assert_called_once_with("lessons")
    finally:
        lesson_events.set_lessons_changed_hook(None)
        await asyncio.to_thread(vs.close)


async def test_a_same_body_re_save_by_a_person_during_the_verdict_is_kept(tmp_path):
    """The source filter is a write-time snapshot: the delete re-checks it."""
    from kiro_crew.dashboard.handlers.cron import _resolve_and_supersede

    vs = VectorMemoryStore(db_path=tmp_path / "memory.db")
    await asyncio.to_thread(vs.init)
    value = {"rule": "Use X", "category": "tool"}
    state = MagicMock()
    try:
        await asyncio.to_thread(vs.set_semantic, "lesson.old", value, 1.0, "consolidation")
        seen_body = (await asyncio.to_thread(vs.get_lessons))[0]["value_json"]

        async def verdict_while_person_re_saves(*_args, **_kwargs):
            await asyncio.to_thread(vs.set_semantic, "lesson.old", value, 1.0, "user_explicit")
            return [("lesson.old", seen_body)]

        candidates = [{"key": "lesson.old", "rule": "Use X", "similarity": 0.6}]
        with (
            patch(
                "kiro_crew.dashboard.handlers.cron._resolve_contradictions",
                new=verdict_while_person_re_saves,
            ),
            patch("kiro_crew.dashboard.handlers.cron._sel"),
        ):
            await _resolve_and_supersede(state, "dashboard:ui", "Do NOT use X", candidates, vs)
        row = await asyncio.to_thread(vs.get_semantic, "lesson.old")
        assert row is not None and row["source"] == "user_explicit"
        state.push_refresh.assert_not_called()
    finally:
        await asyncio.to_thread(vs.close)
