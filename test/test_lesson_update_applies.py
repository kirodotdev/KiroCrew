"""Changing one lesson's ``applies`` tier in place.

``write_lesson`` keeps the tier write-once, and a delete plus a re-add re-runs the
new-lesson dedup and stamps the new caller's source. ``update_lesson_applies``
rewrites the one matching row under its own key, on both store lineages;
``PATCH /api/lessons`` and the ``learn_update`` tool reach it.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.dashboard.handlers import cron
from kiro_crew.mcp_tools import learn as learn_tools
from kiro_crew.vector_memory import VectorMemoryStore, create_member_database, open_member_database

RULE = "Use one stable re-tier probe"


def _store(tmp_path, monkeypatch, lineage: str) -> VectorMemoryStore:
    if lineage == "v1":
        store = VectorMemoryStore(db_path=tmp_path / "m.db", embedding_dim=4)
        store.init()
        return store
    from kiro_crew import memory_stores

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr(memory_stores, "memory_stores_root", lambda: tmp_path / "memory_stores")
    path = tmp_path / "memory_stores" / "member-store" / "memory.db"
    create_member_database(path, member_id="member-id", store_id="member-store")
    return open_member_database(path, member_id="member-id", store_id="member-store")


@pytest.mark.parametrize("lineage", ("v1", "member"))
def test_a_retier_keeps_the_row_and_a_stale_read_writes_nothing(
    tmp_path, monkeypatch, lineage
) -> None:
    store = _store(tmp_path, monkeypatch, lineage)
    try:
        assert store.write_lesson(RULE, "knowledge", source="consolidation", applies="always")
        (key,) = [row["key"] for row in store.get_lessons()]
        with store._db_lock, store.db:
            store.db.execute(
                f"UPDATE {store._sem_rel} SET embedding = ? WHERE key = ?", (b"\0" * 16, key)
            )
        before = store.get_semantic(key)

        # The caller read a finding, but the row is a standing rule: nothing is written.
        assert store.update_lesson_applies(RULE, current="on_topic", applies="always") == "conflict"
        assert store.get_semantic(key) == before

        assert (
            store.update_lesson_applies(f" {RULE.upper()} ", current="always", applies="on_topic")
            == "updated"
        )
        after = store.get_semantic(key)
        assert json.loads(after["value_json"]) == {
            **json.loads(before["value_json"]),
            "applies": "on_topic",
        }
        kept = ("source", "confidence", "created_at", "embedding")
        assert {f: after[f] for f in kept} == {f: before[f] for f in kept}
        assert after.get("text", after["value_json"]) == after["value_json"]
        assert [row["key"] for row in store.get_lessons()] == [key]
        assert store.update_lesson_applies(RULE, current="on_topic", applies="on_topic") == (
            "unchanged"
        )

        # A row whose body moved after the read is not written: the UPDATE compares it.
        snapshot = store.get_lessons()
        with store._db_lock, store.db:
            store.db.execute(
                f"UPDATE {store._sem_rel} SET value_json = ? WHERE key = ?",
                (before["value_json"], key),
            )
        moved = store.get_semantic(key)
        monkeypatch.setattr(store, "get_lessons", lambda: snapshot)
        assert store.update_lesson_applies(RULE, current="on_topic", applies="always") == "conflict"
        assert store.get_semantic(key) == moved
    finally:
        store.close()


def test_a_legacy_string_lesson_is_retiered_as_the_mapping_shape(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path, monkeypatch, "v1")
    try:
        assert store.set_semantic_if_absent("lesson.legacy", RULE, 0.9, "import") == "imported"
        assert store.update_lesson_applies(RULE, current="always", applies="on_topic") == (
            "updated"
        )
        assert json.loads(store.get_semantic("lesson.legacy")["value_json"]) == {
            "rule": RULE,
            "category": "knowledge",
            "negative": None,
            "applies": "on_topic",
        }
    finally:
        store.close()


def test_a_near_limit_lesson_changes_tier(tmp_path, monkeypatch) -> None:
    # The size cap measures a lesson's text, so adding its tier does not refuse it.
    store = _store(tmp_path, monkeypatch, "v1")
    rule = "r" * 4060
    try:
        value = {"rule": rule, "category": "knowledge", "negative": None}
        assert store.set_semantic_if_absent("lesson.big", value, 0.9, "import") == "imported"
        assert store.update_lesson_applies(rule, current="always", applies="on_topic") == (
            "updated"
        )
    finally:
        store.close()


@pytest.mark.asyncio
async def test_the_patch_route_retiers_through_the_store(tmp_path, monkeypatch) -> None:
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    store = _store(tmp_path, monkeypatch, "v1")
    try:
        state = SimpleNamespace(push_refresh=MagicMock())
        app = web.Application()
        app["state"] = state
        app.router.add_patch("/api/lessons", cron.api_lessons_update)
        with (
            patch.object(cron, "_recognize_session", new=AsyncMock(return_value=None)),
            patch.object(cron, "_is_restricted_session", return_value=False),
            patch.object(
                cron, "resolve_lesson_memory_store", new=AsyncMock(return_value=("", None))
            ),
            patch.object(cron, "_prepare_member_lesson_store", new=AsyncMock(return_value=None)),
            patch.object(cron, "_get_memory", return_value=MagicMock(vector_store=store)),
            patch.object(cron, "emit_lessons_changed") as emitted,
        ):
            async with TestClient(TestServer(app)) as client:
                body = {"rule": RULE, "repo_scope": "", "current": "always", "applies": "on_topic"}

                async def send(payload: dict) -> tuple[int, dict]:
                    resp = await client.patch(
                        "/api/lessons", json=payload, headers={"X-Session-Key": "dashboard:ui"}
                    )
                    return resp.status, await resp.json()

                # No lesson rows on a V1 store: the lessons are in the JSONL fallback.
                assert await send(body) == (200, {"ok": False, "outcome": "unsupported"})
                assert store.write_lesson(RULE, "knowledge", applies="always")
                assert await send(body) == (200, {"ok": True, "outcome": "updated"})
                status, bad = await send({**body, "applies": "sometimes"})
                assert (status, bad["code"]) == (400, "validation_error")
        state.push_refresh.assert_called_once_with("lessons")
        emitted.assert_called_once_with("updated", "user_explicit")
        assert json.loads(store.get_lessons()[0]["value_json"])["applies"] == "on_topic"
    finally:
        store.close()


def test_learn_update_forwards_the_call_and_says_what_happened() -> None:
    args = {"rule": RULE, "current": "always", "applies": "on_topic"}
    with (
        patch.object(
            learn_tools.mcp_core, "_vet_memory_writes_governance", return_value=None
        ) as vet,
        patch.object(learn_tools.mcp_core, "_patch", return_value={"outcome": "updated"}) as call,
    ):
        assert learn_tools.learn_update("learn_update", args) == (
            f"Lesson now applies on_topic: {RULE}"
        )
    call.assert_called_once_with("/api/lessons", args)
    assert vet.call_args.args[1] == "learn_update"

    with (
        patch.object(learn_tools.mcp_core, "_vet_memory_writes_governance", return_value=None),
        patch.object(learn_tools.mcp_core, "_patch", return_value={"outcome": "conflict"}),
    ):
        assert learn_tools.learn_update("learn_update", args) == (
            "No lesson was changed: it no longer applies always. Read it again with learn_list."
        )
