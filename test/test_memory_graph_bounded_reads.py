"""The memory graph and the consolidation snapshot read bounded pages.

Counts the rows each ``_fetch_all_locked`` call returns over a large semantic table.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest


@pytest.fixture
def store(tmp_path) -> Iterator[Any]:
    from kiro_crew.vector_memory import VectorMemoryStore

    vs = VectorMemoryStore(db_path=tmp_path / "vm.db")
    vs.init()
    try:
        yield vs
    finally:
        close = getattr(vs, "close", None)
        if callable(close):
            close()


def _meter_rows(vs: Any) -> dict[str, int]:
    counts = {"rows": 0, "calls": 0, "max": 0}
    real = vs._fetch_all_locked

    def metered(*args: Any, **kwargs: Any) -> Any:
        rows = real(*args, **kwargs)
        counts["rows"] += len(rows)
        counts["calls"] += 1
        counts["max"] = max(counts["max"], len(rows))
        return rows

    vs._fetch_all_locked = metered
    return counts


def test_the_memory_graph_reads_a_bounded_number_of_rows(store):
    from kiro_crew.dashboard.handlers.memory_graph import (
        GRAPH_LESSON_LIMIT,
        GRAPH_SEMANTIC_LIMIT,
        _build_memory_graph,
    )

    for index in range(600):
        store.set_semantic(f"project.g_{index:04d}", {"v": index}, 0.9, "t", defer_embedding=True)
    for index in range(300):
        store.set_semantic(
            f"lesson.l_{index:04d}", {"rule": f"rule {index}"}, 0.9, "t", defer_embedding=True
        )
    counts = _meter_rows(store)
    # A row written after the table was seeded, inside the window, is drawn.
    store.set_semantic("project.a_new", {"v": "fresh"}, 0.9, "t", defer_embedding=True)
    mem = SimpleNamespace(
        read_preferences=lambda: "",
        read_projects=lambda: "",
        read_recent_history=lambda *a, **k: "",
        vector_store=store,
    )
    partial: dict[str, bool] = {}
    nodes, _edges = _build_memory_graph(mem, [], partial)
    # One row past each cap is what tells the graph it is partial.
    assert counts["rows"] <= GRAPH_SEMANTIC_LIMIT + GRAPH_LESSON_LIMIT + 2, counts
    assert partial == {"semantic": True, "lessons": True}
    semantic = [n for n in nodes if n["group"] == "semantic"]
    assert 0 < len(semantic) <= GRAPH_SEMANTIC_LIMIT
    # Lessons never crowd the semantic window, and the new row is drawn.
    assert all(not n["label"].startswith("lesson.") for n in semantic)
    assert any(n["label"] == "project.a_new" for n in semantic)
    assert 0 < len([n for n in nodes if n["group"] == "lesson"]) <= GRAPH_LESSON_LIMIT


def test_the_consolidation_snapshot_is_the_whole_table_in_bounded_pages(store):
    from kiro_crew.history_consolidation_memory import (
        _SEMANTIC_SNAPSHOT_PAGE,
        _semantic_snapshot_in_pages,
    )

    for index in range(_SEMANTIC_SNAPSHOT_PAGE * 2 + 37):
        store.set_semantic(f"user.k_{index:05d}", {"v": index}, 0.9, "t", defer_embedding=True)
    whole = store.get_all_semantic()
    counts = _meter_rows(store)
    paged = _semantic_snapshot_in_pages(store)
    assert [r["key"] for r in paged] == [r["key"] for r in whole]
    assert counts["max"] <= _SEMANTIC_SNAPSHOT_PAGE, counts
    assert counts["calls"] == 3, counts


def test_a_store_that_ignores_the_cursor_is_read_once():
    """A fake ``get_all_semantic`` that returns the same rows for every call must not
    loop the snapshot or duplicate its rows."""
    from kiro_crew.history_consolidation_memory import _semantic_snapshot_in_pages

    rows = [{"key": f"k{i:03d}"} for i in range(5)]
    fake = SimpleNamespace(get_all_semantic=lambda **_kw: list(rows))
    assert _semantic_snapshot_in_pages(fake, page=5) == rows  # type: ignore[arg-type]


def _plan_of_bounded_reads(fake_store: Any, db: Any) -> list[list[str]]:
    from kiro_crew.vector_memory_runtime import semantic

    issued: list[tuple[str, tuple]] = []
    fake_store._fetch_all_locked = lambda sql, params=(): issued.append((sql, params)) or []
    semantic.get_all_semantic(fake_store, 200, exclude_lessons=True)
    # The consolidation snapshot's own calls: its first page and a later one.
    rows = [{"key": f"user.k_{i:03d}"} for i in range(2)]
    pages = iter([rows, []])

    def get_all_semantic(**kwargs: Any) -> list[dict]:
        semantic.get_all_semantic(fake_store, **kwargs)
        return next(pages)

    from kiro_crew.history_consolidation_memory import _semantic_snapshot_in_pages

    _semantic_snapshot_in_pages(SimpleNamespace(get_all_semantic=get_all_semantic), page=2)
    return [
        [row[3] for row in db.execute("EXPLAIN QUERY PLAN " + sql, params)]
        for sql, params in issued
    ]


def test_the_bounded_reads_walk_the_key_index_without_sorting_the_table(store):
    """Rows returned are capped above; this pins that SQLite also STOPS at the cap,
    walking the unique key index rather than collecting and sorting every live row."""
    import sqlite3

    from kiro_crew import memory_schema

    plans = _plan_of_bounded_reads(
        SimpleNamespace(
            algorithm_version=store.algorithm_version,
            _sem_rel=store._sem_rel,
            _sem_guard=store._sem_guard,
        ),
        store.db,
    )
    crew_db = sqlite3.connect(":memory:")
    crew_db.executescript(memory_schema.CREW_SCHEMA_SQL)
    plans += _plan_of_bounded_reads(
        SimpleNamespace(
            algorithm_version="v2",
            _sem_rel="memory_items",
            _sem_guard=memory_schema.semantic_guard(memory_schema.LINEAGE_CREW),
        ),
        crew_db,
    )
    # A v1 store on the crew schema reads the table under its view, in the view's columns.
    v1_crew = SimpleNamespace(
        algorithm_version="v1",
        _sem_rel="memory_items",
        _sem_guard=memory_schema.semantic_guard(memory_schema.LINEAGE_CREW),
    )
    plans += _plan_of_bounded_reads(v1_crew, crew_db)
    issued: list[tuple[str, tuple]] = []
    v1_crew._fetch_all_locked = lambda sql, params=(): issued.append((sql, params)) or []
    from kiro_crew.vector_memory_runtime import semantic

    semantic.get_all_semantic(v1_crew, 1, exclude_lessons=True)
    view_columns = [c[0] for c in crew_db.execute("SELECT * FROM semantic_memory").description]
    keyed_columns = [c[0] for c in crew_db.execute(*issued[0]).description]
    assert keyed_columns == view_columns
    assert len(plans) == 9
    for plan in plans:
        assert not any("TEMP B-TREE" in step for step in plan), plan
        assert any("autoindex" in step or "idx_mi_key_live" in step for step in plan), plan


def test_the_lesson_fallback_draws_the_newest_lessons():
    """With no vector store, lessons come from the JSONL file, oldest first; the
    graph keeps the newest :data:`GRAPH_LESSON_LIMIT` of them."""
    from kiro_crew.dashboard.handlers.memory_graph import GRAPH_LESSON_LIMIT, _build_memory_graph

    lessons = [SimpleNamespace(rule=f"lesson rule number {i:04d}") for i in range(500)]
    mem = SimpleNamespace(
        read_preferences=lambda: "",
        read_projects=lambda: "",
        read_recent_history=lambda *a, **k: "",
        vector_store=None,
    )
    partial: dict[str, bool] = {}
    nodes, _edges = _build_memory_graph(mem, lessons, partial)
    assert partial == {"semantic": False, "lessons": True}
    titles = [n["title"] for n in nodes if n["group"] == "lesson"]
    assert len(titles) == GRAPH_LESSON_LIMIT
    assert "lesson rule number 0499" in titles
    assert "lesson rule number 0000" not in titles


def test_a_graph_under_its_caps_is_not_partial(store):
    from kiro_crew.dashboard.handlers.memory_graph import _build_memory_graph

    store.set_semantic("project.small", {"v": 1}, 0.9, "t", defer_embedding=True)
    mem = SimpleNamespace(
        read_preferences=lambda: "",
        read_projects=lambda: "",
        read_recent_history=lambda *a, **k: "",
        vector_store=store,
    )
    partial: dict[str, bool] = {}
    nodes, _edges = _build_memory_graph(mem, [], partial)
    assert partial == {"semantic": False, "lessons": False}
    assert [n["label"] for n in nodes if n["group"] == "semantic"] == ["project.small"]
