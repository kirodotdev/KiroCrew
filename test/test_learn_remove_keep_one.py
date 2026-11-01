"""``learn_remove(keep_one=true)`` collapses duplicate lessons to one copy.

A substring delete removes EVERY match, so two copies of one rule could only be
cleaned up by deleting both and re-adding one -- which loses the original row's
metadata. ``keep_one`` removes only the extra copies: of the matches, rows with
the same rule text and scope are collapsed to the oldest, and a different lesson
is never removed in this mode. Covered on both stores, the route and the tool.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.dashboard.handlers import cron
from kiro_crew.learn import LessonStore
from kiro_crew.mcp_tools import learn as learn_tools
from kiro_crew.vector_memory import VectorMemoryStore

RULE = "run the gate before pushing"


def _store(tmp_path) -> VectorMemoryStore:
    store = VectorMemoryStore(db_path=tmp_path / "m.db", embedding_dim=4)
    store.init()
    return store


def _raw_lesson(store: VectorMemoryStore, key: str, rule: str, created_at: str, **extra) -> None:
    # Duplicates in a real store sit under different keys (an import's sha256
    # key beside the writer's md5 one), which the deduping writer never makes,
    # so the rows are written raw and their age pinned.
    assert (
        store.set_semantic_if_absent(
            key, {"rule": rule, "category": "preference", **extra}, 1.0, "import"
        )
        == "imported"
    )
    store.db.execute("UPDATE semantic_memory SET created_at = ? WHERE key = ?", (created_at, key))
    store.db.commit()


def _vector_rows(store: VectorMemoryStore) -> list[tuple[str, str]]:
    return sorted((e["key"], json.loads(e["value_json"])["rule"]) for e in store.get_lessons())


def test_vector_keep_one_keeps_the_oldest_copy_of_each_lesson(tmp_path) -> None:
    store = _store(tmp_path)
    try:
        _raw_lesson(store, "lesson.b000000000000002", RULE, "2026-10-02T00:00:00")
        _raw_lesson(store, "lesson.a000000000000001", f"  {RULE.upper()} ", "2026-10-01T00:00:00")
        _raw_lesson(store, "lesson.c000000000000003", RULE, "2026-10-03T00:00:00")
        # A different lesson the substring also reaches, and a scoped twin:
        # each is the only copy of its own lesson and must stay.
        _raw_lesson(store, "lesson.d000000000000004", f"{RULE} twice", "2026-10-04T00:00:00")
        _raw_lesson(
            store, "lesson.e000000000000005", RULE, "2026-10-05T00:00:00", repo_scope="src/pkg"
        )

        assert store.delete_lesson("run the gate", keep_one=True) is True
        assert _vector_rows(store) == [
            ("lesson.a000000000000001", f"  {RULE.upper()} "),
            ("lesson.d000000000000004", f"{RULE} twice"),
            ("lesson.e000000000000005", RULE),
        ]
        # Nothing left to collapse: no row is removed and the call says so.
        assert store.delete_lesson("run the gate", keep_one=True) is False
        assert len(store.get_lessons()) == 3
        # The default path is unchanged: every match goes.
        assert store.delete_lesson("run the gate") is True
        assert store.get_lessons() == []
    finally:
        store.close()


def test_vector_keep_one_honours_the_scope_selector(tmp_path) -> None:
    store = _store(tmp_path)
    try:
        _raw_lesson(store, "lesson.a100000000000001", RULE, "2026-10-01T00:00:00")
        _raw_lesson(store, "lesson.a200000000000002", RULE, "2026-10-02T00:00:00")
        _raw_lesson(
            store, "lesson.b100000000000001", RULE, "2026-10-01T00:00:00", repo_scope="src/pkg"
        )
        _raw_lesson(
            store, "lesson.b200000000000002", RULE, "2026-10-02T00:00:00", repo_scope="src/pkg/"
        )
        # Only the scoped group is collapsed; the global pair is not reached.
        assert store.delete_lesson(RULE, "src/pkg", keep_one=True) is True
        assert [k for k, _ in _vector_rows(store)] == [
            "lesson.a100000000000001",
            "lesson.a200000000000002",
            "lesson.b100000000000001",
        ]
    finally:
        store.close()


def test_vector_keep_one_groups_on_rule_and_clause_not_rendered_text(tmp_path) -> None:
    store = _store(tmp_path)
    try:
        # Renders the same as the next row, but the separator is part of its rule.
        _raw_lesson(store, "lesson.a000000000000001", "Use ruff — NOT: for typing", "2026-10-01")
        _raw_lesson(
            store, "lesson.a000000000000002", "Use ruff", "2026-10-02", negative="for typing"
        )
        # Same rule as the next row, different clause: a different lesson.
        _raw_lesson(store, "lesson.a000000000000003", "Use ruff", "2026-10-03", negative="ever")
        # A true copy of the second row: the only one that goes.
        _raw_lesson(
            store, "lesson.a000000000000004", "use ruff ", "2026-10-04", negative="For typing"
        )
        assert store.delete_lesson("use ruff", keep_one=True) is True
        assert [k for k, _ in _vector_rows(store)] == [
            "lesson.a000000000000001",
            "lesson.a000000000000002",
            "lesson.a000000000000003",
        ]
    finally:
        store.close()


def test_vector_keep_one_keeps_rows_with_different_unusable_scopes(tmp_path) -> None:
    store = _store(tmp_path)
    try:
        _raw_lesson(store, "lesson.a000000000000001", RULE, "2026-10-01", repo_scope="/repo/a")
        _raw_lesson(store, "lesson.a000000000000002", RULE, "2026-10-02", repo_scope="/repo/b")
        _raw_lesson(store, "lesson.a000000000000003", RULE, "2026-10-03", repo_scope="/repo/b")
        assert store.delete_lesson(RULE, keep_one=True) is True
        assert [k for k, _ in _vector_rows(store)] == [
            "lesson.a000000000000001",
            "lesson.a000000000000002",
        ]
    finally:
        store.close()


def _rewrite(store: VectorMemoryStore, key: str, rule: str, **extra) -> None:
    # A concurrent writer landing between the delete's read and its tombstone:
    # same key, a new body (a clause attached by save-time enrichment).
    store.set_semantic(key, {"rule": rule, "category": "preference", **extra}, 1.0, "x")


def test_vector_keep_one_keeps_a_copy_rewritten_after_the_read(tmp_path) -> None:
    store = _store(tmp_path)
    try:
        _raw_lesson(store, "lesson.a000000000000001", RULE, "2026-10-01")
        _raw_lesson(store, "lesson.a000000000000002", RULE, "2026-10-02")
        _raw_lesson(store, "lesson.a000000000000003", RULE, "2026-10-03")
        snapshot = store.get_lessons()
        _rewrite(store, "lesson.a000000000000002", RULE, negative="new clause")
        with patch.object(store, "get_lessons", return_value=snapshot):
            assert store.delete_lesson(RULE, keep_one=True) is True
        # The enriched copy is kept; only the untouched extra goes.
        assert [k for k, _ in _vector_rows(store)] == [
            "lesson.a000000000000001",
            "lesson.a000000000000002",
        ]
    finally:
        store.close()


def test_vector_keep_one_leaves_a_group_whose_survivor_changed(tmp_path) -> None:
    store = _store(tmp_path)
    try:
        _raw_lesson(store, "lesson.a000000000000001", RULE, "2026-10-01")
        _raw_lesson(store, "lesson.a000000000000002", RULE, "2026-10-02")
        snapshot = store.get_lessons()
        _rewrite(store, "lesson.a000000000000001", RULE, negative="new clause")
        with patch.object(store, "get_lessons", return_value=snapshot):
            assert store.delete_lesson(RULE, keep_one=True) is False
        assert len(store.get_lessons()) == 2
    finally:
        store.close()


def test_vector_keep_one_keeps_a_copy_in_another_tier(tmp_path) -> None:
    store = _store(tmp_path)
    try:
        # An older finding does not stand in for a newer standing rule.
        _raw_lesson(store, "lesson.a000000000000001", RULE, "2026-10-01", applies="on_topic")
        _raw_lesson(store, "lesson.a000000000000002", RULE, "2026-10-02", applies="always")
        _raw_lesson(store, "lesson.a000000000000003", RULE, "2026-10-03", applies="always")
        assert store.delete_lesson(RULE, keep_one=True) is True
        assert [k for k, _ in _vector_rows(store)] == [
            "lesson.a000000000000001",
            "lesson.a000000000000002",
        ]
    finally:
        store.close()


def test_vector_keep_one_never_keeps_an_expired_copy_over_an_active_one(tmp_path) -> None:
    store = _store(tmp_path)
    try:
        _raw_lesson(store, "lesson.a000000000000001", RULE, "2026-10-01")
        _raw_lesson(store, "lesson.a000000000000002", RULE, "2026-10-02")
        # The oldest copy's validity window has closed, so prompts do not serve it.
        store.set_semantic(
            "lesson.a000000000000001",
            {"rule": RULE, "category": "preference"},
            1.0,
            "x",
            metadata={"valid_from": "2020-01-01T00:00:00Z", "valid_until": "2020-01-02T00:00:00Z"},
        )
        assert [r["key"] for r in store._eligible_rows(store.get_lessons(), "directive")] == [
            "lesson.a000000000000002"
        ]
        # The active copy is the only servable one, so nothing is collapsed.
        assert store.delete_lesson(RULE, keep_one=True) is False
        assert len(store.get_lessons()) == 2
    finally:
        store.close()


def test_vector_keep_one_keeps_copies_with_different_validity_windows(tmp_path) -> None:
    store = _store(tmp_path)
    try:
        _raw_lesson(store, "lesson.a000000000000001", RULE, "2026-10-01")
        _raw_lesson(store, "lesson.a000000000000002", RULE, "2026-10-02")
        _raw_lesson(store, "lesson.a000000000000003", RULE, "2026-10-03")
        # The oldest copy is servable today but expires later; the others never do.
        store.set_semantic(
            "lesson.a000000000000001",
            {"rule": RULE, "category": "preference"},
            1.0,
            "x",
            metadata={"valid_until": "2999-01-01T00:00:00Z"},
        )
        assert len(store._eligible_rows(store.get_lessons(), "directive")) == 3
        # Only the two undated copies collapse; the dated one is a different lesson.
        assert store.delete_lesson(RULE, keep_one=True) is True
        assert [k for k, _ in _vector_rows(store)] == [
            "lesson.a000000000000001",
            "lesson.a000000000000002",
        ]
    finally:
        store.close()


def test_jsonl_keep_one_keeps_a_row_whose_clause_is_not_text(tmp_path) -> None:
    lines = [
        {"ts": "t0", "rule": RULE, "category": "preference", "negative": ["skip typing"]},
        {"ts": "t1", "rule": RULE, "category": "preference"},
        {"ts": "t2", "rule": RULE, "category": "preference"},
    ]
    (tmp_path / "lessons.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
    )
    store = LessonStore(base_dir=tmp_path)
    assert store.remove(RULE, keep_one=True) is True
    assert [le.ts for le in store.load_all()] == ["t0", "t1"]


def test_jsonl_keep_one_keeps_a_copy_that_differs_in_any_stored_field(tmp_path) -> None:
    lines = [
        {"ts": "t0", "rule": RULE, "category": "preference"},
        {"ts": "t1", "rule": RULE, "category": "tool"},
        {"ts": "t2", "rule": RULE, "category": "preference"},
    ]
    (tmp_path / "lessons.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
    )
    store = LessonStore(base_dir=tmp_path)
    assert store.remove(RULE, keep_one=True) is True
    assert [le.ts for le in store.load_all()] == ["t0", "t1"]


def test_jsonl_keep_one_keeps_a_copy_in_another_tier(tmp_path) -> None:
    lines = [
        {"ts": "t0", "rule": RULE, "category": "preference", "applies": "on_topic"},
        {"ts": "t1", "rule": RULE, "category": "preference", "applies": "always"},
        {"ts": "t2", "rule": RULE, "category": "preference", "applies": "always"},
    ]
    (tmp_path / "lessons.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
    )
    store = LessonStore(base_dir=tmp_path)
    assert store.remove(RULE, keep_one=True) is True
    assert [(le.ts, le.applies) for le in store.load_all()] == [
        ("t0", "on_topic"),
        ("t1", "always"),
    ]


def test_jsonl_keep_one_keeps_a_copy_with_a_different_clause(tmp_path) -> None:
    lines = [
        {"ts": "t0", "rule": RULE, "category": "preference"},
        {"ts": "t1", "rule": RULE, "category": "preference", "negative": "skip it"},
        {"ts": "t2", "rule": RULE, "category": "preference", "negative": " Skip it"},
    ]
    (tmp_path / "lessons.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
    )
    store = LessonStore(base_dir=tmp_path)
    assert store.remove(RULE, keep_one=True) is True
    assert [(le.ts, le.negative) for le in store.load_all()] == [("t0", None), ("t1", "skip it")]


def test_jsonl_keep_one_keeps_the_first_copy_of_each_lesson(tmp_path) -> None:
    lines = [
        {"ts": "t0", "rule": RULE, "category": "preference", "negative": "first"},
        {"ts": "t1", "rule": f"{RULE} twice", "category": "preference"},
        {"ts": "t2", "rule": f" {RULE.upper()}", "category": "preference", "negative": "First "},
        {"ts": "t3", "rule": RULE, "category": "preference", "repo_scope": "src/pkg"},
        {"ts": "t4", "rule": RULE, "category": "preference", "negative": "first"},
        {"ts": "t5", "rule": "unrelated", "category": "preference"},
    ]
    (tmp_path / "lessons.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
    )
    store = LessonStore(base_dir=tmp_path)
    assert store.remove("run the gate", keep_one=True) is True
    assert [(le.ts, le.rule) for le in store.load_all()] == [
        ("t0", RULE),
        ("t1", f"{RULE} twice"),
        ("t3", RULE),
        ("t5", "unrelated"),
    ]
    # The survivor is the original row: its own timestamp and clause spelling.
    assert (store.load_all()[0].ts, store.load_all()[0].negative) == ("t0", "first")
    assert store.remove("run the gate", keep_one=True) is False
    assert len(store.load_all()) == 4


async def _delete(state, vector_store, body: dict):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    app = web.Application()
    app["state"] = state
    app.router.add_route("*", "/api/lessons", cron.api_lessons_delete)
    with (
        patch.object(cron, "_recognize_session", new=AsyncMock(return_value=None)),
        patch.object(cron, "_blocks_reads_session", return_value=False),
        patch.object(cron, "_sel"),
        patch.object(cron, "resolve_lesson_memory_store", new=AsyncMock(return_value=("", None))),
        patch.object(cron, "_prepare_member_lesson_store", new=AsyncMock(return_value=None)),
        patch.object(cron, "_get_memory", return_value=MagicMock(vector_store=vector_store)),
    ):
        async with TestClient(TestServer(app)) as client:
            resp = await client.delete(
                "/api/lessons", json=body, headers={"X-Session-Key": "dashboard:ui"}
            )
            return resp.status, await resp.json()


@pytest.mark.asyncio
async def test_delete_route_collapses_vector_duplicates(tmp_path) -> None:
    store = _store(tmp_path)
    try:
        _raw_lesson(store, "lesson.0000000000000001", RULE, "2026-10-01T00:00:00")
        _raw_lesson(store, "lesson.0000000000000002", RULE, "2026-10-02T00:00:00")
        state = SimpleNamespace(lessons=MagicMock(), push_refresh=MagicMock())
        status, body = await _delete(state, store, {"rule": RULE, "keep_one": True})
        assert (status, body) == (200, {"ok": True})
        assert [k for k, _ in _vector_rows(store)] == ["lesson.0000000000000001"]
        state.lessons.remove.assert_not_called()
    finally:
        store.close()


@pytest.mark.asyncio
async def test_delete_route_threads_keep_one_to_the_jsonl_tier_and_refuses_a_non_boolean() -> None:
    state = SimpleNamespace(
        lessons=SimpleNamespace(remove=MagicMock(return_value=True)), push_refresh=MagicMock()
    )
    status, _ = await _delete(state, None, {"rule": RULE, "keep_one": True})
    assert status == 200
    state.lessons.remove.assert_called_once_with(RULE, None, exact=False, keep_one=True)

    state.lessons.remove.reset_mock()
    # A truthy STRING is not a boolean, and nothing is deleted on a refused body.
    status, body = await _delete(state, None, {"rule": RULE, "keep_one": "true"})
    assert status == 400
    assert body["code"] == "keep_one_not_bool"
    state.lessons.remove.assert_not_called()


def test_learn_remove_tool_forwards_keep_one_and_words_the_result() -> None:
    with patch.object(learn_tools.mcp_core, "_delete", return_value={"ok": True}) as delete:
        out = learn_tools.learn_remove("learn_remove", {"query": RULE, "keep_one": True})
    delete.assert_called_once_with("/api/lessons", {"rule": RULE, "keep_one": True})
    assert out == f"Removed extra copies of lessons matching: {RULE} (one of each kept)"

    with patch.object(learn_tools.mcp_core, "_delete", return_value={"ok": False}):
        out = learn_tools.learn_remove("learn_remove", {"query": RULE, "keep_one": True})
    assert out == f"No lessons were removed: no extra copies match: {RULE}"

    # Off by default: the body is the one every existing caller sends.
    with patch.object(learn_tools.mcp_core, "_delete", return_value={"ok": True}) as delete:
        learn_tools.learn_remove("learn_remove", {"query": RULE, "keep_one": False})
    delete.assert_called_once_with("/api/lessons", {"rule": RULE})
