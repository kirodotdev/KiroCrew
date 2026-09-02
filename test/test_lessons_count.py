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
        # _count_lessons uses the O(1) COUNT(*) accessor, not get_lessons(),
        # so the status paths never materialize the lesson corpus for a count.
        ctx.memory.vector_store.count_lessons.return_value = len(vector_lessons or [])
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
        jsonl = [
            Lesson(ts="", rule="a", category="knowledge"),
            Lesson(ts="", rule="b", category="tool"),
        ]
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


class TestVectorStoreCountLessons:
    def test_count_lessons_matches_get_lessons_on_a_real_store(self, tmp_path):
        """Executes the real SQL (every other test stubs it): the COUNT(*)
        predicate must agree with ``get_lessons`` — same ``is_deleted = 0``
        filter and same ``key LIKE 'lesson.%'`` scope — including after a
        soft-delete. A predicate typo here would silently drift the Overview
        card from the lessons page forever, with a green mock-only suite.
        """
        from kiro_crew.vector_memory import VectorMemoryStore

        store = VectorMemoryStore(db_path=tmp_path / "mem.db")
        store.init()
        for i in range(3):
            store.set_semantic(f"lesson.rule_{i}", f"rule {i}", 1.0, "user_explicit")
        # Non-lesson row: must not be counted by either accessor.
        store.set_semantic("pref.os", "linux", 0.9, "user_explicit")

        assert store.count_lessons() == len(store.get_lessons()) == 3

        # Soft-delete one lesson: both accessors must drop it.
        assert store.delete_semantic("lesson.rule_0", "user_explicit")
        assert store.count_lessons() == len(store.get_lessons()) == 2
