"""Tests for _count_lessons: JSONL + vector store combined count."""

from __future__ import annotations

from unittest.mock import MagicMock

from kiro_crew.dashboard.state import DashboardState
from kiro_crew.learn import Lesson, LessonStore


def _make_state(
    jsonl_lessons: list[Lesson] | None = None,
    vector_lessons: list[dict] | None = None,
    has_vector_store: bool = True,
) -> DashboardState:
    """Create a minimal DashboardState with mocked dependencies."""
    lessons_store = MagicMock(spec=LessonStore)
    lessons_store.load_all.return_value = jsonl_lessons or []

    state = DashboardState(
        sessions=MagicMock(count=0),
        crons=MagicMock(**{"list_jobs.return_value": []}),
        lessons=lessons_store,
        start_time=0.0,
        subagents=MagicMock(count=0),
    )

    if has_vector_store:
        ctx = MagicMock()
        ctx.memory.vector_store.get_lessons.return_value = vector_lessons or []
        state.context_builder = ctx
    else:
        state.context_builder = None

    return state


class TestCountLessons:
    def test_jsonl_only_no_vector_store(self):
        """When vector store is disabled, count only JSONL lessons."""
        lessons = [Lesson(ts="", rule="rule1", category="knowledge")]
        state = _make_state(jsonl_lessons=lessons, has_vector_store=False)
        assert state._count_lessons() == 1

    def test_vector_store_only(self):
        """When JSONL is empty but vector store has lessons, count those."""
        vs_lessons = [{"rule": "r1"}, {"rule": "r2"}, {"rule": "r3"}]
        state = _make_state(vector_lessons=vs_lessons)
        assert state._count_lessons() == 3

    def test_both_jsonl_and_vector_store(self):
        """Sum of JSONL + vector store lessons."""
        jsonl = [Lesson(ts="", rule="a", category="knowledge"),
                 Lesson(ts="", rule="b", category="tool")]
        vs = [{"rule": "c"}, {"rule": "d"}, {"rule": "e"}]
        state = _make_state(jsonl_lessons=jsonl, vector_lessons=vs)
        assert state._count_lessons() == 5

    def test_both_empty(self):
        """Zero when both stores are empty."""
        state = _make_state()
        assert state._count_lessons() == 0

    def test_status_snapshot_uses_count_lessons(self):
        """status_snapshot() uses _count_lessons when lessons param not given."""
        vs_lessons = [{"rule": f"r{i}"} for i in range(10)]
        state = _make_state(vector_lessons=vs_lessons)
        snap = state.status_snapshot()
        assert snap["lessons"] == 10

    def test_status_snapshot_explicit_lessons_overrides(self):
        """When lessons param is explicitly passed, it overrides _count_lessons."""
        vs_lessons = [{"rule": f"r{i}"} for i in range(10)]
        state = _make_state(vector_lessons=vs_lessons)
        snap = state.status_snapshot(lessons=42)
        assert snap["lessons"] == 42
