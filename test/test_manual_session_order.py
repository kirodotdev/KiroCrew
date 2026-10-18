"""The manual session order (sort "Manual"): its store rules, its route, and who may write it.

The order sits in ``pinned_session_order``'s fenced directory as its own file
and reaches browsers on every slot row as ``manual_rank`` / ``manual_rev``.
Only the person's own browser writes it: ``_write_manual_order`` refuses every
caller whose origin is not ``dashboard`` (agents and crew members reach the
gateway with ``X-Internal-Secret``) and every app or member principal.
"""

from __future__ import annotations

import asyncio
import json
import threading

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard import pinned_session_order as pso
from kiro_crew.testing.wait import await_until, default_timeout, until_parked

# ── Store rules ──


class TestStoreRules:
    LIVE = ("a", "b", "c", "d")

    def test_a_seed_keeps_the_order_and_drops_repeats_and_unusable_keys(self) -> None:
        long_key = "k" * (pso.MAX_PINNED_ORDER_KEY_CHARS + 1)
        assert pso.after_manual_seed(["b", "a", "b", long_key, "gone"], self.LIVE) == [
            "b",
            "a",
            "gone",
        ]

    def test_a_move_changes_only_the_moved_key(self) -> None:
        current = ["a", "b", "c", "d"]
        assert pso.after_manual_move(current, "a", "c", after=True, seat=[], live=self.LIVE) == [
            "b",
            "c",
            "a",
            "d",
        ]
        assert pso.after_manual_move(current, "d", "b", after=False, seat=[], live=self.LIVE) == [
            "a",
            "d",
            "b",
            "c",
        ]

    def test_after_none_puts_the_key_at_the_top(self) -> None:
        assert pso.after_manual_move(
            ["a", "b", "c"], "c", None, after=True, seat=[], live=self.LIVE
        ) == ["c", "a", "b"]

    def test_unranked_rows_named_in_seat_take_the_top_first(self) -> None:
        """The anchor may be a new session the browser drew above the ranked ones."""
        out = pso.after_manual_move(
            ["a", "b"], "a", "d", after=True, seat=["d", "c", "b", "a"], live=self.LIVE
        )
        # d and c were unranked (seated on top, in order); b was ranked and keeps
        # its place; a is the moved key, never seated.
        assert out == ["d", "a", "c", "b"]

    def test_seat_skips_keys_with_no_live_slot(self) -> None:
        out = pso.after_manual_move(["a"], "b", "a", after=True, seat=["gone", "c"], live=self.LIVE)
        assert out == ["c", "a", "b"]

    def test_an_anchor_with_no_place_answers_none(self) -> None:
        assert pso.after_manual_move(["a"], "b", "c", after=True, seat=[], live=self.LIVE) is None
        assert pso.after_manual_move(["a"], "b", None, after=False, seat=[], live=self.LIVE) is None

    def test_the_cap_trims_keys_with_no_live_slot_first(self) -> None:
        dead = [f"gone-{i}" for i in range(pso.MAX_PINNED_ORDER_KEYS)]
        out = pso.after_manual_move(dead, "a", None, after=True, seat=["b"], live=("a", "b"))
        assert out is not None and len(out) == pso.MAX_PINNED_ORDER_KEYS
        assert out[:2] == ["a", "b"]

    def test_adopt_ranks_keys_and_raises_the_revision(self) -> None:
        order = pso.ManualOrder()
        assert order.rank("a") is None and not order.loaded
        order.adopt(["b", "a"], True, 1_000)
        assert (order.rank("b"), order.rank("a"), order.rank("c")) == (0, 1, None)
        assert order.loaded and order.stored and order.rev == 1_000
        order.adopt([], False, 1_000)
        assert order.rev == 1_001 and order.rank("b") is None and not order.stored

    def test_the_manual_file_sits_beside_the_pinned_one(self, tmp_path) -> None:
        """One fenced directory, two files: writing one never touches the other,
        and a list the pinned store wrote still loads unchanged."""
        pinned = pso.store_path(tmp_path)
        manual = pso.manual_store_path(tmp_path)
        assert manual.parent == pinned.parent
        assert manual.parent.name == pso.PINNED_ORDER_DIR
        pso.save(pinned, ["p1", "p2"])
        before = pinned.read_bytes()
        pso.save(manual, ["m1", "m2"])
        assert pinned.read_bytes() == before
        assert pso.load_stored(pinned) == (["p1", "p2"], True)
        assert pso.load_stored(manual) == (["m1", "m2"], True)
        pso.clear_stored(manual)
        assert pso.load_stored(manual) == ([], False)
        pso.clear_stored(manual)  # a second clear is fine
        assert pso.load_stored(pinned) == (["p1", "p2"], True)


# ── Routes ──


def _app(state) -> web.Application:
    from kiro_crew.dashboard.chat import api_chat_slots
    from kiro_crew.dashboard.chat_folders import api_chat_slot_pin
    from kiro_crew.dashboard.chat_manual_order import api_chat_manual_order

    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/chat/slots", api_chat_slots)
    app.router.add_patch("/api/chat/slots/{slot}/pin", api_chat_slot_pin)
    app.router.add_post("/api/chat/manual-order", api_chat_manual_order)
    return app


def _state(tmp_path, monkeypatch, keys=("a", "b", "c", "d")):
    from chat_test_helpers import _make_state

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.chat_manual_order.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    for key in keys:
        slot = state.get_or_create_slot(key)
        slot.append("user", "hello")
        slot.drain()
    return state


async def _rows(client: TestClient) -> list[dict]:
    resp = await client.get("/api/chat/slots")
    assert resp.status == 200
    body = await resp.json()
    return body["slots"] if isinstance(body, dict) else body


async def _ranks(client: TestClient) -> dict[str, int | None]:
    return {row["key"]: row.get("manual_rank") for row in await _rows(client)}


def _stored(tmp_path) -> list[str]:
    return json.loads(pso.manual_store_path(tmp_path).read_text(encoding="utf-8"))


async def _post(client: TestClient, body: dict, headers: dict | None = None):
    return await client.post("/api/chat/manual-order", json=body, headers=headers or {})


@pytest.mark.asyncio
async def test_rows_have_no_manual_rank_before_any_write(tmp_path, monkeypatch) -> None:
    state = _state(tmp_path, monkeypatch)
    async with TestClient(TestServer(_app(state))) as client:
        assert await _ranks(client) == {"a": None, "b": None, "c": None, "d": None}


@pytest.mark.asyncio
async def test_a_seed_is_stored_and_ranks_the_rows(tmp_path, monkeypatch) -> None:
    state = _state(tmp_path, monkeypatch)
    async with TestClient(TestServer(_app(state))) as client:
        before = {row["manual_rev"] for row in await _rows(client)}
        resp = await _post(client, {"op": "seed", "keys": ["c", "a", "b", "d"]})
        assert resp.status == 200
        answer = await resp.json()
        assert answer["order"] == ["c", "a", "b", "d"]
        assert answer["rev"] > before.pop()
        assert await _ranks(client) == {"c": 0, "a": 1, "b": 2, "d": 3}
        assert {row["manual_rev"] for row in await _rows(client)} == {answer["rev"]}
    assert _stored(tmp_path) == ["c", "a", "b", "d"]
    # The pinned store's own file is not written by any of this.
    assert not pso.store_path(tmp_path).exists()


@pytest.mark.asyncio
async def test_a_second_seed_never_replaces_a_stored_order(tmp_path, monkeypatch) -> None:
    """Two browsers seeding at once: the second is told what the first stored."""
    state = _state(tmp_path, monkeypatch)
    async with TestClient(TestServer(_app(state))) as client:
        await _post(client, {"op": "seed", "keys": ["c", "a", "b", "d"]})
        resp = await _post(client, {"op": "seed", "keys": ["d", "b", "a", "c"]})
        assert resp.status == 409
        body = await resp.json()
        assert body["code"] == "manual_order_exists"
        assert body["order"] == ["c", "a", "b", "d"] and isinstance(body["rev"], int)
    assert _stored(tmp_path) == ["c", "a", "b", "d"]


@pytest.mark.asyncio
async def test_moves_from_two_browsers_both_land(tmp_path, monkeypatch) -> None:
    """Each move names one session, so neither browser undoes the other's."""
    state = _state(tmp_path, monkeypatch)
    async with TestClient(TestServer(_app(state))) as client:
        await _post(client, {"op": "seed", "keys": ["a", "b", "c", "d"]})
        # Browser one moves d to the top; browser two, which never saw that,
        # moves a after c.
        assert (await _post(client, {"op": "move", "key": "d", "after": None})).status == 200
        resp = await _post(client, {"op": "move", "key": "a", "after": "c"})
        assert resp.status == 200
        assert (await resp.json())["order"] == ["d", "b", "c", "a"]
        assert await _ranks(client) == {"d": 0, "b": 1, "c": 2, "a": 3}


@pytest.mark.asyncio
async def test_a_move_landing_during_another_moves_disk_write_waits_for_it(
    tmp_path, monkeypatch
) -> None:
    """Two moves overlapping across the awaited disk write: the second waits for
    the first to land and moves the order it left, so neither move is lost."""
    from kiro_crew.dashboard import chat_manual_order as cmo

    state = _state(tmp_path, monkeypatch)
    loop = asyncio.get_running_loop()
    hold = default_timeout()
    real_save = pso.save
    writes: list[list[str]] = []
    first_open = asyncio.Event()
    release = threading.Event()

    def save(path, keys: list[str]) -> None:
        writes.append(list(keys))
        if len(writes) == 1:
            # Hold the first move's write open on its worker thread.
            loop.call_soon_threadsafe(first_open.set)
            release.wait(hold)
        real_save(path, keys)

    async with TestClient(TestServer(_app(state))) as client:
        await _post(client, {"op": "seed", "keys": ["a", "b", "c", "d"]})
        monkeypatch.setattr(pso, "save", save)
        first = asyncio.ensure_future(_post(client, {"op": "move", "key": "d", "after": None}))
        try:
            await await_until(first_open, what="the first move opening its disk write")
            second = asyncio.ensure_future(_post(client, {"op": "move", "key": "a", "after": "c"}))
            # The second move parks on the write lock. Without one it reads the
            # order the first move has not landed yet, writes, and answers first.
            parked = asyncio.ensure_future(until_parked(cmo._lock(state)))
            await asyncio.wait({parked, second}, return_when=asyncio.FIRST_COMPLETED)
            if parked.done():
                parked.result()
            else:
                parked.cancel()
        finally:
            release.set()
        answers = [await await_until(task, what="a move's answer") for task in (first, second)]
        assert [resp.status for resp in answers] == [200, 200]
        assert (await answers[1].json())["order"] == ["d", "b", "c", "a"]
        assert await _ranks(client) == {"d": 0, "b": 1, "c": 2, "a": 3}
    assert writes == [["d", "a", "b", "c"], ["d", "b", "c", "a"]]
    assert _stored(tmp_path) == ["d", "b", "c", "a"]


@pytest.mark.asyncio
async def test_a_move_seats_the_new_sessions_it_was_drawn_beside(tmp_path, monkeypatch) -> None:
    state = _state(tmp_path, monkeypatch)
    async with TestClient(TestServer(_app(state))) as client:
        await _post(client, {"op": "seed", "keys": ["a", "b"]})
        # c and d arrived after the seed, so the sidebar draws them on top.
        resp = await _post(client, {"op": "move", "key": "b", "before": "d", "seat": ["d", "c"]})
        assert resp.status == 200
        assert (await resp.json())["order"] == ["b", "d", "c", "a"]


@pytest.mark.asyncio
async def test_undo_of_the_first_drag_clears_the_order(tmp_path, monkeypatch) -> None:
    state = _state(tmp_path, monkeypatch)
    async with TestClient(TestServer(_app(state))) as client:
        rev = (await (await _post(client, {"op": "seed", "keys": ["b", "a"]})).json())["rev"]
        resp = await _post(client, {"op": "clear", "rev": rev})
        assert resp.status == 200
        cleared = await resp.json()
        assert cleared["order"] == [] and cleared["rev"] > rev
        assert set((await _ranks(client)).values()) == {None}
        # Nothing is saved again, so the next seed lands.
        assert (await _post(client, {"op": "seed", "keys": ["a"]})).status == 200
    assert _stored(tmp_path) == ["a"]


@pytest.mark.asyncio
async def test_an_undo_never_clears_a_move_made_elsewhere(tmp_path, monkeypatch) -> None:
    state = _state(tmp_path, monkeypatch)
    async with TestClient(TestServer(_app(state))) as client:
        rev = (await (await _post(client, {"op": "seed", "keys": ["a", "b"]})).json())["rev"]
        await _post(client, {"op": "move", "key": "b", "after": None})
        resp = await _post(client, {"op": "clear", "rev": rev})
        assert resp.status == 409
        body = await resp.json()
        assert body["code"] == "manual_order_changed" and body["order"] == ["b", "a"]
    assert _stored(tmp_path) == ["b", "a"]


@pytest.mark.asyncio
async def test_a_move_beside_a_session_with_no_place_changes_nothing(tmp_path, monkeypatch) -> None:
    state = _state(tmp_path, monkeypatch)
    async with TestClient(TestServer(_app(state))) as client:
        await _post(client, {"op": "seed", "keys": ["a", "b"]})
        resp = await _post(client, {"op": "move", "key": "a", "after": "c"})
        assert resp.status == 409
        body = await resp.json()
        assert body["code"] == "anchor_gone" and body["order"] == ["a", "b"]
    assert _stored(tmp_path) == ["a", "b"]


@pytest.mark.asyncio
async def test_a_move_of_a_closed_or_replaced_session_changes_nothing(
    tmp_path, monkeypatch
) -> None:
    state = _state(tmp_path, monkeypatch)
    seen = state._slots["a"].created_at
    async with TestClient(TestServer(_app(state))) as client:
        await _post(client, {"op": "seed", "keys": ["a", "b"]})
        resp = await _post(client, {"op": "move", "key": "zz", "after": "b"})
        assert resp.status == 409 and (await resp.json())["code"] == "session_gone"
        state._slots["a"].created_at = "2099-01-01T00:00:00+00:00"
        resp = await _post(
            client, {"op": "move", "key": "a", "after": "b", "expected_created": seen}
        )
        assert resp.status == 409 and (await resp.json())["code"] == "session_gone"
    assert _stored(tmp_path) == ["a", "b"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body, code",
    [
        ({}, "op_invalid"),
        ({"op": "replace", "keys": ["a"]}, "op_invalid"),
        ({"op": "seed", "keys": "a"}, "keys_invalid"),
        ({"op": "seed", "keys": ["a", 3]}, "keys_invalid"),
        ({"op": "seed", "keys": ["a", ""]}, "keys_invalid"),
        ({"op": "seed", "keys": ["a", "a"]}, "keys_duplicate"),
        ({"op": "move", "key": "", "after": "a"}, "key_invalid"),
        ({"op": "move", "key": "a"}, "anchor_invalid"),
        ({"op": "move", "key": "a", "after": "b", "before": "c"}, "anchor_invalid"),
        ({"op": "move", "key": "a", "before": None}, "anchor_invalid"),
        ({"op": "move", "key": "a", "after": "a"}, "anchor_invalid"),
        ({"op": "move", "key": "a", "after": "b", "seat": ["c", "c"]}, "seat_duplicate"),
        (
            {"op": "move", "key": "a", "after": "b", "expected_created": 1},
            "expected_created_invalid",
        ),
        ({"op": "clear"}, "rev_invalid"),
        ({"op": "clear", "rev": True}, "rev_invalid"),
    ],
)
async def test_malformed_bodies_are_refused(tmp_path, monkeypatch, body, code) -> None:
    state = _state(tmp_path, monkeypatch)
    async with TestClient(TestServer(_app(state))) as client:
        resp = await _post(client, body)
        assert resp.status == 400
        assert (await resp.json())["code"] == code
    assert not pso.manual_store_path(tmp_path).exists()


@pytest.mark.asyncio
async def test_a_list_past_the_key_bound_is_refused(tmp_path, monkeypatch) -> None:
    state = _state(tmp_path, monkeypatch)
    keys = [f"k{i}" for i in range(pso.MAX_PINNED_ORDER_KEYS + 1)]
    async with TestClient(TestServer(_app(state))) as client:
        resp = await _post(client, {"op": "seed", "keys": keys})
        assert resp.status == 400 and (await resp.json())["code"] == "keys_too_many"
        resp = await _post(client, {"op": "move", "key": "a", "after": None, "seat": keys})
        assert resp.status == 400 and (await resp.json())["code"] == "seat_too_many"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        # The MCP loopback helpers always send the secret; whatever component
        # they name, the origin is an agent's, never the person's browser.
        {"X-Internal-Secret": "s", "X-Internal-Caller": "kirocrew-dashboard"},
        {"X-Internal-Secret": "s"},
        {"X-Internal-Secret": "s", "X-Session-Key": "dashboard:a"},
    ],
    ids=["mcp-tool", "unnamed-internal", "from-a-session"],
)
async def test_an_agent_cannot_write_the_manual_order(tmp_path, monkeypatch, headers) -> None:
    state = _state(tmp_path, monkeypatch)
    async with TestClient(TestServer(_app(state))) as client:
        for body in (
            {"op": "seed", "keys": ["b", "a"]},
            {"op": "move", "key": "a", "after": None},
            {"op": "clear", "rev": state.manual_session_order.rev},
        ):
            resp = await _post(client, body, headers)
            assert resp.status == 403
            assert (await resp.json())["code"] == "manual_order_person_only"
    assert not pso.manual_store_path(tmp_path).exists()
    assert state.manual_session_order.keys == []


@pytest.mark.asyncio
async def test_an_app_or_member_caller_cannot_write_the_manual_order(tmp_path, monkeypatch) -> None:
    state = _state(tmp_path, monkeypatch)
    for principal in ("some-app", "member:ops"):
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_manual_order.folder_principal",
            lambda _state, _req, p=principal: p,
        )
        async with TestClient(TestServer(_app(state))) as client:
            resp = await _post(client, {"op": "seed", "keys": ["b", "a"]})
            assert resp.status == 403
            assert (await resp.json())["code"] == "manual_order_person_only"
    assert not pso.manual_store_path(tmp_path).exists()


@pytest.mark.asyncio
async def test_the_writer_itself_refuses_any_caller_but_the_browser(tmp_path, monkeypatch) -> None:
    """The rule lives in the one function that writes, not only in the route."""
    from aiohttp.test_utils import make_mocked_request

    from kiro_crew.dashboard.chat_manual_order import _write_manual_order

    state = _state(tmp_path, monkeypatch)
    agent = make_mocked_request(
        "POST", "/api/chat/manual-order", headers={"X-Internal-Secret": "s"}
    )
    assert await _write_manual_order(state, ["b", "a"], request=agent) is None
    assert await _write_manual_order(state, None, request=agent) is None
    assert not pso.manual_store_path(tmp_path).exists()
    browser = make_mocked_request("POST", "/api/chat/manual-order")
    assert await _write_manual_order(state, ["b", "a"], request=browser) is True
    assert _stored(tmp_path) == ["b", "a"]


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["seed", "move", "clear"])
async def test_a_write_that_fails_on_disk_changes_no_rank(tmp_path, monkeypatch, op) -> None:
    """Memory changes only after the file write lands, so the order on screen is
    the one a restart loads: a failed write answers 500 and leaves every row's
    manual_rank, the revision and the stored flag as they were."""
    from kiro_crew.dashboard.chat_manual_order import ensure_manual_order_loaded

    state = _state(tmp_path, monkeypatch)
    order = state.manual_session_order
    assert await ensure_manual_order_loaded(state)
    async with TestClient(TestServer(_app(state))) as client:
        if op != "seed":
            await _post(client, {"op": "seed", "keys": ["a", "b", "c", "d"]})
        before = (await _ranks(client), order.rev, order.stored)

        def full_disk(*_args: object) -> None:
            raise OSError("no space left on device")

        monkeypatch.setattr(pso, "save", full_disk)
        monkeypatch.setattr(pso, "clear_stored", full_disk)
        body = {
            "seed": {"op": "seed", "keys": ["d", "c", "b", "a"]},
            "move": {"op": "move", "key": "d", "after": None},
            "clear": {"op": "clear", "rev": order.rev},
        }[op]
        resp = await _post(client, body)
        assert resp.status == 500
        assert (await resp.json())["code"] == "manual_order_write_failed"
        assert (await _ranks(client), order.rev, order.stored) == before
    if op == "seed":
        assert not pso.manual_store_path(tmp_path).exists()
    else:
        assert _stored(tmp_path) == ["a", "b", "c", "d"]


@pytest.mark.asyncio
async def test_a_scoped_caller_reads_no_manual_ranks(tmp_path, monkeypatch) -> None:
    from kiro_crew.dashboard.handlers.updates import _without_pin_ranks
    from kiro_crew.dashboard.ws_event_scope import _strip_pin_rank

    state = _state(tmp_path, monkeypatch)
    async with TestClient(TestServer(_app(state))) as client:
        await _post(client, {"op": "seed", "keys": ["b", "a"]})
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.folder_principal", lambda _state, _req: "some-app"
        )
        rows = await _rows(client)
        assert rows and all("manual_rank" not in row and "manual_rev" not in row for row in rows)
    row = {"key": "a", "pin_rank": None, "pin_rev": 1, "manual_rank": 0, "manual_rev": 2}
    assert _strip_pin_rank(row) == {"key": "a"}
    assert json.loads(_without_pin_ranks(json.dumps([row]))) == [{"key": "a"}]


@pytest.mark.asyncio
async def test_a_manual_order_survives_a_gateway_restart(tmp_path, monkeypatch) -> None:
    from unittest.mock import MagicMock

    from kiro_crew.dashboard.chat_folders import load_pinned_order_after_listen

    first = _state(tmp_path, monkeypatch)
    async with TestClient(TestServer(_app(first))) as client:
        await _post(client, {"op": "seed", "keys": ["c", "a", "b", "d"]})
        await _post(client, {"op": "move", "key": "d", "after": "c"})
    second = _state(tmp_path, monkeypatch)
    second.push_slots_update = MagicMock()
    assert second.manual_session_order.loaded is False
    await load_pinned_order_after_listen(second)
    second.push_slots_update.assert_called_once()
    async with TestClient(TestServer(_app(second))) as client:
        assert await _ranks(client) == {"c": 0, "d": 1, "a": 2, "b": 3}


@pytest.mark.asyncio
async def test_a_write_before_the_deferred_load_reads_the_stored_order(
    tmp_path, monkeypatch
) -> None:
    pso.save(pso.manual_store_path(tmp_path), ["b", "a"])
    state = _state(tmp_path, monkeypatch)
    async with TestClient(TestServer(_app(state))) as client:
        resp = await _post(client, {"op": "seed", "keys": ["a", "b"]})
        assert resp.status == 409 and (await resp.json())["order"] == ["b", "a"]
        resp = await _post(client, {"op": "move", "key": "c", "after": "a"})
        assert resp.status == 200 and (await resp.json())["order"] == ["b", "a", "c"]


@pytest.mark.asyncio
async def test_an_archived_session_keeps_its_place_until_restored(tmp_path, monkeypatch) -> None:
    """A key with no live slot keeps its place, so a restored session returns there."""
    state = _state(tmp_path, monkeypatch)
    async with TestClient(TestServer(_app(state))) as client:
        await _post(client, {"op": "seed", "keys": ["a", "b", "c"]})
        state._slots.pop("b")
        await _post(client, {"op": "move", "key": "c", "after": None})
        assert _stored(tmp_path) == ["c", "a", "b"]
        restored = state.get_or_create_slot("b")
        restored.append("user", "back")
        restored.drain()
        assert (await _ranks(client))["b"] == 2


@pytest.mark.asyncio
async def test_pinning_and_unpinning_leave_the_manual_order_alone(tmp_path, monkeypatch) -> None:
    state = _state(tmp_path, monkeypatch)
    async with TestClient(TestServer(_app(state))) as client:
        await _post(client, {"op": "seed", "keys": ["a", "b", "c"]})
        rev = state.manual_session_order.rev
        assert (await client.patch("/api/chat/slots/b/pin", json={"pinned": True})).status == 200
        assert (await client.patch("/api/chat/slots/b/pin", json={"pinned": False})).status == 200
        assert state.manual_session_order.rev == rev
        assert await _ranks(client) == {"a": 0, "b": 1, "c": 2, "d": None}
    assert _stored(tmp_path) == ["a", "b", "c"]


def test_the_manual_file_is_fenced_with_the_pinned_one(tmp_path, monkeypatch) -> None:
    """No sandbox change: the fence covers the directory both files live in."""
    from kiro_crew import sandbox
    from kiro_crew.security.paths import is_sensitive_path

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    from kiro_crew.config.paths import config_dir

    assert is_sensitive_path(str(pso.manual_store_path(config_dir())))
    assert pso.manual_store_path(config_dir()).parent.name in sandbox._CREW_HIDDEN_LEAVES
