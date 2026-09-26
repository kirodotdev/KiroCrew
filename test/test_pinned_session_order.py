"""The gateway-kept order of pinned sidebar sessions.

Covers the pure store rules (``pinned_session_order``), the atomic reorder
route (``POST /api/chat/pinned-order``), the pin route's upkeep of the order,
and the ``pin_rank`` every slot row carries.
"""

from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard import pinned_session_order as pso

# ── Pure store rules ──


class TestStoreRules:
    PINNED = {"a": True, "b": True, "c": True, "u": False}

    def test_pin_into_an_unused_order_keeps_it_empty(self) -> None:
        """A person who never reordered keeps the sidebar's plain sort."""
        assert pso.after_pin_change([], "a", True, self.PINNED, stored=False) == []

    def test_pin_appends_once_the_order_is_in_use(self) -> None:
        assert pso.after_pin_change(["b", "a"], "c", True, self.PINNED, stored=True) == [
            "b",
            "a",
            "c",
        ]

    def test_pin_appends_to_a_stored_order_that_has_emptied(self) -> None:
        assert pso.after_pin_change([], "a", True, self.PINNED, stored=True) == ["a"]

    def test_unpin_removes_the_key(self) -> None:
        flags = {**self.PINNED, "a": False}
        assert pso.after_pin_change(["b", "a", "c"], "a", False, flags, stored=True) == ["b", "c"]

    def test_reorder_puts_named_keys_first_and_keeps_the_rest(self) -> None:
        assert pso.after_reorder(["a", "b", "c"], ["c", "a"], self.PINNED) == ["c", "a", "b"]

    def test_prune_drops_gone_unpinned_and_repeated_keys(self) -> None:
        assert pso.prune(["a", "gone", "u", "a", "b"], self.PINNED) == ["a", "b"]

    def test_load_missing_file_is_empty(self, tmp_path) -> None:
        assert pso.load(tmp_path / pso.PINNED_ORDER_FILE) == []

    def test_load_malformed_file_is_empty(self, tmp_path) -> None:
        path = tmp_path / pso.PINNED_ORDER_FILE
        path.write_text("{not json", encoding="utf-8")
        assert pso.load(path) == []
        path.write_text('{"a": 1}', encoding="utf-8")
        assert pso.load(path) == []

    def test_save_then_load_round_trips_and_skips_bad_entries(self, tmp_path) -> None:
        path = tmp_path / pso.PINNED_ORDER_FILE
        pso.save(path, ["b", "a"])
        assert pso.load(path) == ["b", "a"]
        path.write_text(json.dumps(["a", 7, "", "a", "b"]), encoding="utf-8")
        assert pso.load(path) == ["a", "b"]


# ── Routes ──


def _app(state) -> web.Application:
    from kiro_crew.dashboard.chat import api_chat_slots
    from kiro_crew.dashboard.chat_folders import api_chat_pinned_order, api_chat_slot_pin

    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/chat/slots", api_chat_slots)
    app.router.add_patch("/api/chat/slots/{slot}/pin", api_chat_slot_pin)
    app.router.add_post("/api/chat/pinned-order", api_chat_pinned_order)
    return app


def _state(tmp_path, monkeypatch, keys=("a", "b", "c"), pinned=("a", "b", "c")):
    from chat_test_helpers import _make_state

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    for key in keys:
        slot = state.get_or_create_slot(key)
        slot.append("user", "hello")
        slot.drain()
        slot.pinned = key in pinned
    return state


async def _ranks(client: TestClient) -> dict[str, int | None]:
    resp = await client.get("/api/chat/slots")
    assert resp.status == 200
    body = await resp.json()
    rows = body["slots"] if isinstance(body, dict) else body
    return {row["key"]: row.get("pin_rank") for row in rows}


def _stored(tmp_path) -> list[str]:
    return json.loads((tmp_path / pso.PINNED_ORDER_FILE).read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_rows_have_no_rank_before_any_reorder(tmp_path, monkeypatch) -> None:
    """An upgrade with no order file keeps today's sort: nothing is ranked."""
    state = _state(tmp_path, monkeypatch)
    async with TestClient(TestServer(_app(state))) as client:
        assert await _ranks(client) == {"a": None, "b": None, "c": None}


@pytest.mark.asyncio
async def test_reorder_is_stored_and_ranks_the_rows(tmp_path, monkeypatch) -> None:
    state = _state(tmp_path, monkeypatch)
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post("/api/chat/pinned-order", json={"keys": ["c", "a", "b"]})
        assert resp.status == 200
        assert (await resp.json())["order"] == ["c", "a", "b"]
        assert await _ranks(client) == {"c": 0, "a": 1, "b": 2}
    assert _stored(tmp_path) == ["c", "a", "b"]
    assert state.read_pinned_session_order() == (["c", "a", "b"], True)


@pytest.mark.asyncio
async def test_reorder_naming_an_unpinned_session_changes_nothing(tmp_path, monkeypatch) -> None:
    """A stale sidebar gets 409 for the whole request, not a partial write."""
    state = _state(tmp_path, monkeypatch, keys=("a", "b", "u"), pinned=("a", "b"))
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post("/api/chat/pinned-order", json={"keys": ["b", "u", "a"]})
        assert resp.status == 409
        body = await resp.json()
        assert body["code"] == "slot_not_pinned" and body["keys"] == ["u"]
        assert await _ranks(client) == {"a": None, "b": None, "u": None}
    assert not (tmp_path / pso.PINNED_ORDER_FILE).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body, code",
    [
        ({"keys": "a"}, "keys_invalid"),
        ({"keys": ["a", 3]}, "keys_invalid"),
        ({"keys": ["a", ""]}, "keys_invalid"),
        ({"keys": ["a", "a"]}, "keys_duplicate"),
    ],
)
async def test_malformed_reorders_are_refused(tmp_path, monkeypatch, body, code) -> None:
    state = _state(tmp_path, monkeypatch)
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post("/api/chat/pinned-order", json=body)
        assert resp.status == 400
        assert (await resp.json())["code"] == code


@pytest.mark.asyncio
async def test_an_app_or_member_caller_cannot_reorder(tmp_path, monkeypatch) -> None:
    """The order is the person's preference across sessions no app owns."""
    state = _state(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_folders.folder_principal", lambda _state, _req: "some-app"
    )
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post("/api/chat/pinned-order", json={"keys": ["c", "a", "b"]})
        assert resp.status == 403
        assert (await resp.json())["code"] == "pinned_order_person_only"
    assert not (tmp_path / pso.PINNED_ORDER_FILE).exists()


@pytest.mark.asyncio
async def test_pin_and_unpin_keep_the_stored_order(tmp_path, monkeypatch) -> None:
    """A new pin goes to the end; an unpin clears its place for good."""
    state = _state(tmp_path, monkeypatch, keys=("a", "b", "c"), pinned=("a", "b"))
    async with TestClient(TestServer(_app(state))) as client:
        await client.post("/api/chat/pinned-order", json={"keys": ["b", "a"]})
        resp = await client.patch("/api/chat/slots/c/pin", json={"pinned": True})
        assert resp.status == 200
        assert await _ranks(client) == {"b": 0, "a": 1, "c": 2}
        resp = await client.patch("/api/chat/slots/b/pin", json={"pinned": False})
        assert resp.status == 200
        assert await _ranks(client) == {"a": 0, "c": 1, "b": None}
        resp = await client.patch("/api/chat/slots/b/pin", json={"pinned": True})
        assert await _ranks(client) == {"a": 0, "c": 1, "b": 2}
    assert _stored(tmp_path) == ["a", "c", "b"]


@pytest.mark.asyncio
async def test_pin_before_any_reorder_stays_unranked(tmp_path, monkeypatch) -> None:
    state = _state(tmp_path, monkeypatch, keys=("a", "b"), pinned=("a",))
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.patch("/api/chat/slots/b/pin", json={"pinned": True})
        assert resp.status == 200
        assert await _ranks(client) == {"a": None, "b": None}
    assert not (tmp_path / pso.PINNED_ORDER_FILE).exists()


@pytest.mark.asyncio
async def test_a_conditional_hand_off_does_not_replace_a_stored_order(
    tmp_path, monkeypatch
) -> None:
    """A browser's old local order lands only while nobody has set one."""
    state = _state(tmp_path, monkeypatch)
    async with TestClient(TestServer(_app(state))) as client:
        first = {"keys": ["b", "a", "c"], "only_if_unset": True}
        resp = await client.post("/api/chat/pinned-order", json=first)
        assert resp.status == 200
        stale = {"keys": ["c", "b", "a"], "only_if_unset": True}
        resp = await client.post("/api/chat/pinned-order", json=stale)
        assert resp.status == 409
        body = await resp.json()
        assert body["code"] == "pinned_order_exists" and body["order"] == ["b", "a", "c"]
    assert _stored(tmp_path) == ["b", "a", "c"]


@pytest.mark.asyncio
async def test_a_write_before_the_deferred_load_reads_the_stored_order(
    tmp_path, monkeypatch
) -> None:
    """The file is read after the gateway serves; an early writer reads it first."""
    (tmp_path / pso.PINNED_ORDER_FILE).write_text(json.dumps(["b", "a"]), encoding="utf-8")
    state = _state(tmp_path, monkeypatch)
    stale_read = state.read_pinned_session_order()
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.patch("/api/chat/slots/c/pin", json={"pinned": False})
        resp = await client.patch("/api/chat/slots/c/pin", json={"pinned": True})
        assert resp.status == 200
        assert await _ranks(client) == {"b": 0, "a": 1, "c": 2}
    # The boot-time read finishing late must not undo the newer write.
    assert state.adopt_pinned_session_order(stale_read) is False
    assert _stored(tmp_path) == ["b", "a", "c"]


@pytest.mark.asyncio
async def test_the_post_listen_load_publishes_the_ranks(tmp_path, monkeypatch) -> None:
    from unittest.mock import MagicMock

    from kiro_crew.dashboard.chat_folders import load_pinned_order_after_listen

    (tmp_path / pso.PINNED_ORDER_FILE).write_text(json.dumps(["c", "a"]), encoding="utf-8")
    state = _state(tmp_path, monkeypatch)
    state.push_slots_update = MagicMock()
    await load_pinned_order_after_listen(state)
    assert list(state._pinned_session_order) == ["c", "a"]
    state.push_slots_update.assert_called_once()
    await load_pinned_order_after_listen(state)
    state.push_slots_update.assert_called_once()


@pytest.mark.asyncio
async def test_a_stored_empty_order_still_refuses_the_hand_off(tmp_path, monkeypatch) -> None:
    """Unpinning every ranked session leaves an order that is set, just empty."""
    state = _state(tmp_path, monkeypatch, keys=("a", "b"), pinned=("a", "b"))
    async with TestClient(TestServer(_app(state))) as client:
        await client.post("/api/chat/pinned-order", json={"keys": ["b", "a"]})
        await client.patch("/api/chat/slots/a/pin", json={"pinned": False})
        await client.patch("/api/chat/slots/b/pin", json={"pinned": False})
        assert _stored(tmp_path) == []
        await client.patch("/api/chat/slots/a/pin", json={"pinned": True})
        stale = {"keys": ["a"], "only_if_unset": True}
        resp = await client.post("/api/chat/pinned-order", json=stale)
        assert resp.status == 409
        assert (await resp.json())["code"] == "pinned_order_exists"
        assert await _ranks(client) == {"a": 0, "b": None}
    # And after a restart, the file on disk carries the same answer.
    state._pinned_session_order_loaded = False
    assert state.read_pinned_session_order() == (["a"], True)
