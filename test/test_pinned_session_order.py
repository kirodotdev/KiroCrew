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

from conftest import make_dir_link
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
        assert pso.after_reorder(["a", "b", "c"], ["c", "a"], self.PINNED) == ["c", "b", "a"]

    def test_reorder_leaves_an_unnamed_key_in_its_place(self) -> None:
        """A sidebar that briefly cannot see a pin (an unpin in flight) must not move it."""
        assert pso.after_reorder(
            ["a", "b", "c", "d"], ["d", "a", "c"], {**self.PINNED, "d": True}
        ) == [
            "d",
            "b",
            "a",
            "c",
        ]

    def test_reorder_appends_keys_that_were_not_stored(self) -> None:
        assert pso.after_reorder(["a"], ["c", "a", "b"], self.PINNED) == ["c", "a", "b"]

    def test_the_first_reorder_seats_a_pin_the_request_did_not_name(self) -> None:
        """A pin that landed after the sidebar read the slots, before any order
        was stored, gets a rank instead of staying unranked on every client."""
        flags = {"a": True, "b": True, "late": True, "u": False}
        assert pso.after_reorder([], ["b", "a"], flags) == ["b", "a", "late"]

    def test_prune_drops_a_key_the_loader_would_skip(self, caplog) -> None:
        """A slot key over the length cap is never stored, so a restart cannot
        silently drop a rank the running gateway showed."""
        long_key = "k" * (pso.MAX_PINNED_ORDER_KEY_CHARS + 1)
        with caplog.at_level("WARNING"):
            assert pso.prune(["a", long_key], {"a": True, long_key: True}) == ["a"]
        assert "not usable keys" in caplog.text

    def test_prune_drops_unpinned_and_repeated_keys_but_keeps_unrestored_ones(self) -> None:
        """A key with no live slot may be a session not restored yet."""
        assert pso.prune(["a", "later", "u", "a", "b"], self.PINNED) == ["a", "later", "b"]

    def test_load_missing_file_is_empty(self, tmp_path) -> None:
        assert pso.load_stored(pso.store_path(tmp_path))[0] == []

    def test_load_malformed_file_is_empty(self, tmp_path) -> None:
        path = pso.store_path(tmp_path)
        path.parent.mkdir()
        path.write_text("{not json", encoding="utf-8")
        assert pso.load_stored(path)[0] == []
        path.write_text('{"a": 1}', encoding="utf-8")
        assert pso.load_stored(path)[0] == []

    def test_load_raises_an_unreadable_file_instead_of_reading_it_as_empty(
        self, tmp_path, monkeypatch
    ) -> None:
        """Only a missing file is ``[]``. ``Path.exists`` suppresses some stat
        errors, so the load must not ask it first: an unreadable home read as
        empty would let the next save overwrite the real order."""
        path = pso.store_path(tmp_path)

        def unreadable(*args, **kwargs):
            raise OSError(5, "Input/output error")

        monkeypatch.setattr(type(path), "exists", lambda self, **kw: False)
        monkeypatch.setattr(pso.os, "lstat", unreadable)
        with pytest.raises(OSError):
            pso.load_stored(path)

    def test_a_non_regular_file_at_the_path_is_ignored_not_read(self, tmp_path, caplog) -> None:
        """A directory, link or device planted at the path reads as an empty
        stored order, so the load never opens it and the next save replaces it."""
        path = pso.store_path(tmp_path)
        path.mkdir(parents=True)
        with caplog.at_level("WARNING"):
            assert pso.load_stored(path) == ([], True)
        assert "not a regular file" in caplog.text

    def test_an_oversized_order_file_is_ignored_without_reading_it_whole(
        self, tmp_path, caplog
    ) -> None:
        """The load reads at most one byte past the cap, whatever the file holds."""
        path = pso.store_path(tmp_path)
        path.parent.mkdir()
        path.write_bytes(b"[" + b" " * pso.MAX_PINNED_ORDER_FILE_BYTES + b"]")
        with caplog.at_level("WARNING"):
            assert pso.load_stored(path) == ([], True)
        assert "larger than" in caplog.text

    def test_a_full_order_of_worst_case_keys_reads_back_after_a_save(self, tmp_path) -> None:
        """The read ceiling covers everything :func:`save` writes: a full order
        of maximum-length keys whose every character JSON escapes as a
        12-byte surrogate pair still loads, rather than reading as empty."""
        path = pso.store_path(tmp_path)
        keys = [
            "\U0001f600" * (pso.MAX_PINNED_ORDER_KEY_CHARS - 4) + f"{index:04d}"
            for index in range(pso.MAX_PINNED_ORDER_KEYS)
        ]
        pso.save(path, keys)
        assert path.stat().st_size > pso.MAX_PINNED_ORDER_KEYS * pso.MAX_PINNED_ORDER_KEY_CHARS * 6
        assert pso.load_stored(path) == (keys, True)

    def test_stored_flag_comes_from_the_read_not_a_second_stat(self, tmp_path, monkeypatch) -> None:
        """A read that found the file reports it stored even when a later
        ``Path.exists`` would say otherwise, so a hand-off is never accepted
        over a real order."""
        path = pso.store_path(tmp_path)
        assert pso.load_stored(path) == ([], False)
        pso.save(path, [])
        monkeypatch.setattr(type(path), "exists", lambda self, **kw: False)
        assert pso.load_stored(path) == ([], True)
        path.write_text("{not json", encoding="utf-8")
        assert pso.load_stored(path) == ([], True)

    def test_an_order_over_the_cap_is_trimmed_and_the_overflow_is_logged(
        self, tmp_path, caplog
    ) -> None:
        """The tail beyond the cap is dropped with a count, never silently."""
        path = pso.store_path(tmp_path)
        pso.save(path, [f"chat-{i}" for i in range(pso.MAX_PINNED_ORDER_KEYS + 3)])
        with caplog.at_level("WARNING"):
            keys, stored = pso.load_stored(path)
        assert stored and len(keys) == pso.MAX_PINNED_ORDER_KEYS
        assert keys[-1] == f"chat-{pso.MAX_PINNED_ORDER_KEYS - 1}"
        assert "dropping 3" in caplog.text

    def test_save_then_load_round_trips_and_skips_bad_entries(self, tmp_path) -> None:
        path = pso.store_path(tmp_path)
        pso.save(path, ["b", "a"])
        assert pso.load_stored(path)[0] == ["b", "a"]
        path.write_text(json.dumps(["a", 7, "", "a", "b"]), encoding="utf-8")
        assert pso.load_stored(path)[0] == ["a", "b"]


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
    return json.loads(pso.store_path(tmp_path).read_text(encoding="utf-8"))


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
async def test_every_order_change_raises_the_revision_rows_and_answers_carry(
    tmp_path, monkeypatch
) -> None:
    """A browser orders frames by this revision, so it must rise on every write."""
    state = _state(tmp_path, monkeypatch)
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.get("/api/chat/slots")
        body = await resp.json()
        rows = body["slots"] if isinstance(body, dict) else body
        before = {row["pin_rev"] for row in rows}
        assert len(before) == 1
        resp = await client.post("/api/chat/pinned-order", json={"keys": ["c", "a", "b"]})
        first = (await resp.json())["rev"]
        assert first > before.pop()
        resp = await client.post("/api/chat/pinned-order", json={"keys": ["a", "c", "b"]})
        second = (await resp.json())["rev"]
        assert second > first
        resp = await client.get("/api/chat/slots")
        body = await resp.json()
        rows = body["slots"] if isinstance(body, dict) else body
        assert {row["pin_rev"] for row in rows} == {second}
        await client.patch("/api/chat/slots/b/pin", json={"pinned": False})
        assert state._pinned_order_rev > second


def test_the_revision_keeps_rising_when_the_clock_does_not() -> None:
    """A restart starts past the old process; a stalled clock still moves on."""
    assert pso.next_revision(0, 1_000) == 1_000
    assert pso.next_revision(1_000, 1_000) == 1_001
    assert pso.next_revision(5_000, 1_000) == 5_001


@pytest.mark.asyncio
async def test_reorder_naming_a_replaced_session_changes_nothing(tmp_path, monkeypatch) -> None:
    """A key closed and recreated since the caller read the slots is refused, not ranked."""
    state = _state(tmp_path, monkeypatch)
    seen = {key: state._slots[key].created_at for key in ("a", "b", "c")}
    state._slots["b"].created_at = "2099-01-01T00:00:00+00:00"
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post(
            "/api/chat/pinned-order",
            json={"keys": ["c", "b", "a"], "expected_created": seen},
        )
        assert resp.status == 409
        body = await resp.json()
        assert body["code"] == "session_gone" and body["keys"] == ["b"]
        assert await _ranks(client) == {"a": None, "b": None, "c": None}
    assert not pso.store_path(tmp_path).exists()


@pytest.mark.asyncio
async def test_a_session_replaced_during_the_write_keeps_no_rank(tmp_path, monkeypatch) -> None:
    """The order file is written off the loop, and creating a slot does not
    take the order lock, so a named session can be replaced after the check
    passed. Its successor must not take the place the request gave the old one,
    in memory or on disk."""
    state = _state(tmp_path, monkeypatch)
    seen = {key: state._slots[key].created_at for key in ("a", "b", "c")}
    real_save = state.save_pinned_session_order
    calls: list[list[str]] = []

    def save_and_replace_b(keys: list[str]) -> None:
        calls.append(list(keys))
        real_save(keys)
        if len(calls) == 1:
            state._slots["b"].created_at = "2099-01-01T00:00:00+00:00"

    monkeypatch.setattr(state, "save_pinned_session_order", save_and_replace_b)
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post(
            "/api/chat/pinned-order",
            json={"keys": ["c", "b", "a"], "expected_created": seen},
        )
        assert resp.status == 200
        assert (await resp.json())["order"] == ["c", "a"]
        assert (await _ranks(client))["b"] is None
    assert calls == [["c", "b", "a"], ["c", "a"]]
    assert _stored(tmp_path) == ["c", "a"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra, code",
    [
        ({"only_if_unset": "true"}, "only_if_unset_not_bool"),
        ({"expected_created": ["a"]}, "expected_created_invalid"),
        ({"expected_created": {"a": 1}}, "expected_created_invalid"),
    ],
)
async def test_reorder_refuses_a_malformed_flag_or_token(
    tmp_path, monkeypatch, extra, code
) -> None:
    """A non-boolean ``only_if_unset`` is refused, never read as an unconditional write."""
    state = _state(tmp_path, monkeypatch)
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post("/api/chat/pinned-order", json={"keys": ["b", "a"], **extra})
        assert resp.status == 400
        assert (await resp.json())["code"] == code
    assert not pso.store_path(tmp_path).exists()


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
    assert not pso.store_path(tmp_path).exists()


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
    assert not pso.store_path(tmp_path).exists()


@pytest.mark.asyncio
async def test_an_app_or_member_caller_reads_no_ranks(tmp_path, monkeypatch) -> None:
    """A rank is a place in the person's whole order, so a scoped list omits it."""
    from kiro_crew.dashboard.ws_event_scope import _strip_pin_rank

    state = _state(tmp_path, monkeypatch)
    async with TestClient(TestServer(_app(state))) as client:
        await client.post("/api/chat/pinned-order", json={"keys": ["c", "a", "b"]})
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.folder_principal", lambda _state, _req: "some-app"
        )
        resp = await client.get("/api/chat/slots")
        assert resp.status == 200
        body = await resp.json()
        rows = body["slots"] if isinstance(body, dict) else body
        assert rows and all("pin_rank" not in row and "pin_rev" not in row for row in rows)
    # The app websocket filter strips both the same way.
    assert _strip_pin_rank({"key": "a", "pin_rank": 1, "pin_rev": 7}) == {"key": "a"}


def test_a_scoped_sse_slots_frame_carries_no_ranks() -> None:
    """``/api/stream`` sends the bare list; a non-dashboard reader gets it rank-free."""
    from kiro_crew.dashboard.handlers.updates import _without_pin_ranks

    frame = json.dumps(
        [{"key": "a", "pin_rank": 0, "pin_rev": 3}, {"key": "b", "pin_rank": None, "pin_rev": 3}]
    )
    assert json.loads(_without_pin_ranks(frame)) == [{"key": "a"}, {"key": "b"}]


def test_save_refuses_a_planted_pinned_order_directory_link(tmp_path, monkeypatch) -> None:
    """A ``pinned-order`` link planted before the fence cannot redirect the write.

    ``make_dir_link`` plants a junction on Windows, which needs no privilege
    and is refused the same way, so the fence is checked on every platform.
    """
    home = tmp_path / "datahome"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    make_dir_link(home / pso.PINNED_ORDER_DIR, elsewhere)
    with pytest.raises(OSError):
        pso.save(pso.store_path(home), ["a"])
    assert not (elsewhere / pso.PINNED_ORDER_FILE).exists()


def test_the_create_route_answers_a_scoped_caller_without_a_rank(tmp_path, monkeypatch) -> None:
    """A create that answers with an existing pinned session keeps its rank private."""
    from kiro_crew.dashboard import chat_handlers

    state = _state(tmp_path, monkeypatch)
    state._pinned_session_order = ["b", "a"]
    slot = state.get_or_create_slot("a")
    monkeypatch.setattr(chat_handlers, "folder_principal", lambda _state, _req: "")
    assert chat_handlers._created_slot_row(state, object(), slot)["pin_rank"] == 1
    monkeypatch.setattr(chat_handlers, "folder_principal", lambda _state, _req: "some-app")
    row = chat_handlers._created_slot_row(state, object(), slot)
    assert "pin_rank" not in row and "pin_rev" not in row


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
async def test_a_pin_whose_order_write_fails_is_kept_and_answered_as_an_error(
    tmp_path, monkeypatch
) -> None:
    """The pin stands, but the caller is told its rank was not saved."""
    state = _state(tmp_path, monkeypatch, keys=("a", "b", "c"), pinned=("a", "b"))

    async def _fail(*_args, **_kwargs) -> bool:
        return False

    async with TestClient(TestServer(_app(state))) as client:
        await client.post("/api/chat/pinned-order", json={"keys": ["b", "a"]})
        monkeypatch.setattr("kiro_crew.dashboard.chat_folders._write_pinned_order", _fail)
        resp = await client.patch("/api/chat/slots/c/pin", json={"pinned": True})
        assert resp.status == 500
        body = await resp.json()
        assert body["code"] == "pinned_order_write_failed"
        assert body["pinned"] is True and body["changed"] is True
        assert state.get_or_create_slot("c").pinned is True
        assert await _ranks(client) == {"b": 0, "a": 1, "c": None}
    assert _stored(tmp_path) == ["b", "a"]


@pytest.mark.asyncio
async def test_retrying_a_pin_whose_order_write_failed_repairs_the_order(
    tmp_path, monkeypatch
) -> None:
    """An unchanged retry still writes the rank the failed write left out."""
    from kiro_crew.dashboard import chat_folders

    state = _state(tmp_path, monkeypatch, keys=("a", "b", "c"), pinned=("a", "b"))
    real_write = chat_folders._write_pinned_order

    async def _fail(*_args, **_kwargs) -> bool:
        return False

    async with TestClient(TestServer(_app(state))) as client:
        await client.post("/api/chat/pinned-order", json={"keys": ["b", "a"]})
        monkeypatch.setattr(chat_folders, "_write_pinned_order", _fail)
        resp = await client.patch("/api/chat/slots/c/pin", json={"pinned": True})
        assert resp.status == 500
        monkeypatch.setattr(chat_folders, "_write_pinned_order", real_write)
        resp = await client.patch("/api/chat/slots/c/pin", json={"pinned": True})
        assert resp.status == 200
        assert (await resp.json())["changed"] is False
        assert await _ranks(client) == {"b": 0, "a": 1, "c": 2}
    assert _stored(tmp_path) == ["b", "a", "c"]


@pytest.mark.asyncio
async def test_a_pin_whose_order_cannot_be_read_is_answered_as_an_error(
    tmp_path, monkeypatch
) -> None:
    """The pin stands, the caller is told, and a retry once the read works repairs it."""
    state = _state(tmp_path, monkeypatch, keys=("a", "b", "c"), pinned=("a", "b"))
    async with TestClient(TestServer(_app(state))) as client:
        await client.post("/api/chat/pinned-order", json={"keys": ["b", "a"]})
        real_read = state.read_pinned_session_order
        state._pinned_session_order_loaded = False
        monkeypatch.setattr(state, "read_pinned_session_order", lambda: None)
        resp = await client.patch("/api/chat/slots/c/pin", json={"pinned": True})
        assert resp.status == 503
        body = await resp.json()
        assert body["code"] == "pinned_order_unreadable"
        assert body["pinned"] is True and body["changed"] is True
        assert state.get_or_create_slot("c").pinned is True
        assert _stored(tmp_path) == ["b", "a"]
        monkeypatch.setattr(state, "read_pinned_session_order", real_read)
        resp = await client.patch("/api/chat/slots/c/pin", json={"pinned": True})
        assert resp.status == 200
        assert (await resp.json())["changed"] is False
        assert await _ranks(client) == {"b": 0, "a": 1, "c": 2}
    assert _stored(tmp_path) == ["b", "a", "c"]


@pytest.mark.asyncio
async def test_pin_before_any_reorder_stays_unranked(tmp_path, monkeypatch) -> None:
    state = _state(tmp_path, monkeypatch, keys=("a", "b"), pinned=("a",))
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.patch("/api/chat/slots/b/pin", json={"pinned": True})
        assert resp.status == 200
        assert await _ranks(client) == {"a": None, "b": None}
    assert not pso.store_path(tmp_path).exists()


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
    pso.save(pso.store_path(tmp_path), ["b", "a"])
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

    pso.save(pso.store_path(tmp_path), ["c", "a"])
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


@pytest.mark.asyncio
async def test_a_pin_during_restore_keeps_the_places_of_unrestored_sessions(
    tmp_path, monkeypatch
) -> None:
    """Sessions the gateway has not restored yet keep their stored places."""
    pso.save(pso.store_path(tmp_path), ["x", "a", "y"])
    # Only "a" and "b" are live so far; "x" and "y" are still being restored.
    state = _state(tmp_path, monkeypatch, keys=("a", "b"), pinned=("a",))
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.patch("/api/chat/slots/b/pin", json={"pinned": True})
        assert resp.status == 200
        resp = await client.post("/api/chat/pinned-order", json={"keys": ["b", "a"]})
        assert resp.status == 200
    # The pin appended b ([x, a, y, b]); the reorder swapped b and a in the
    # places they held, and x and y did not move.
    assert _stored(tmp_path) == ["x", "b", "y", "a"]


class TestStoreFencedFromAgents:
    """Only the person may set the order, so agents must not write it on disk."""

    def test_store_path_is_sensitive_on_the_file_tool_plane(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.security.paths import is_sensitive_path

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew.config.paths import config_dir

        store = pso.store_path(config_dir())
        assert is_sensitive_path(str(store))
        assert is_sensitive_path(str(store.parent))

    def test_store_leaf_is_sandbox_hidden_and_precreated(self) -> None:
        from kiro_crew import sandbox

        assert pso.PINNED_ORDER_DIR in sandbox._CREW_HIDDEN_LEAVES
        assert pso.PINNED_ORDER_DIR in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES


@pytest.mark.asyncio
async def test_the_hand_off_keeps_sessions_the_gateway_has_not_restored_yet(
    tmp_path, monkeypatch
) -> None:
    """A browser's old order may name sessions still being restored; they keep their place."""
    state = _state(tmp_path, monkeypatch, keys=("a", "b", "u"), pinned=("a", "b"))
    async with TestClient(TestServer(_app(state))) as client:
        body = {"keys": ["later", "b", "u", "a"], "only_if_unset": True}
        resp = await client.post("/api/chat/pinned-order", json=body)
        assert resp.status == 200
        # "u" is live and unpinned, so it is dropped; "later" is not live yet
        # and keeps its place ahead of the live pins.
        assert (await resp.json())["order"] == ["later", "b", "a"]
        assert await _ranks(client) == {"b": 1, "a": 2, "u": None}
        # A live sidebar reorder still has to name live pins.
        resp = await client.post("/api/chat/pinned-order", json={"keys": ["later", "a"]})
        assert resp.status == 409


@pytest.mark.asyncio
async def test_an_explicit_empty_reorder_is_stored_and_refuses_a_later_hand_off(
    tmp_path, monkeypatch
) -> None:
    """A reorder the person sends is stored even when it changes nothing in memory."""
    state = _state(tmp_path, monkeypatch, keys=("a",), pinned=())
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post("/api/chat/pinned-order", json={"keys": []})
        assert resp.status == 200
        assert _stored(tmp_path) == []
        stale = {"keys": ["a"], "only_if_unset": True}
        resp = await client.post("/api/chat/pinned-order", json=stale)
        assert resp.status == 409


@pytest.mark.asyncio
async def test_an_app_pin_toggle_does_not_move_the_persons_order(tmp_path, monkeypatch) -> None:
    """An app may pin its own session; only the person moves the order."""
    state = _state(tmp_path, monkeypatch)
    async with TestClient(TestServer(_app(state))) as client:
        await client.post("/api/chat/pinned-order", json={"keys": ["a", "b", "c"]})
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_folders.folder_principal", lambda _state, _req: "some-app"
        )
        assert (await client.patch("/api/chat/slots/a/pin", json={"pinned": False})).status == 200
        assert (await client.patch("/api/chat/slots/a/pin", json={"pinned": True})).status == 200
        assert await _ranks(client) == {"a": 0, "b": 1, "c": 2}
    assert _stored(tmp_path) == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_an_app_re_pin_that_brings_back_a_rank_sends_the_full_list(
    tmp_path, monkeypatch
) -> None:
    """A one-row patch carries only ``pinned``, so a returning rank needs the list."""
    from unittest.mock import MagicMock

    state = _state(tmp_path, monkeypatch)
    async with TestClient(TestServer(_app(state))) as client:
        await client.post("/api/chat/pinned-order", json={"keys": ["a", "b", "c"]})
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_folders.folder_principal", lambda _state, _req: "some-app"
        )
        state.push_slots_update = MagicMock()
        state.push_slot_patch = MagicMock()
        assert (await client.patch("/api/chat/slots/a/pin", json={"pinned": False})).status == 200
        assert (await client.patch("/api/chat/slots/a/pin", json={"pinned": True})).status == 200
    # Both pins changed row a's own rank (0 -> none -> 0), so neither is a patch.
    assert state.push_slots_update.call_count == 2
    state.push_slot_patch.assert_not_called()


@pytest.mark.asyncio
async def test_a_refused_hand_off_that_lands_the_read_still_publishes_the_ranks(
    tmp_path, monkeypatch
) -> None:
    """The first writer to read the stored order pushes it, even when it then refuses."""
    from unittest.mock import MagicMock

    pso.save(pso.store_path(tmp_path), ["b", "a"])
    state = _state(tmp_path, monkeypatch, keys=("a", "b"), pinned=("a", "b"))
    state.push_slots_update = MagicMock()
    async with TestClient(TestServer(_app(state))) as client:
        stale = {"keys": ["a", "b"], "only_if_unset": True}
        resp = await client.post("/api/chat/pinned-order", json=stale)
        assert resp.status == 409
    state.push_slots_update.assert_called_once()
    assert list(state._pinned_session_order) == ["b", "a"]


def test_the_cap_trims_keys_with_no_live_slot_before_any_live_pinned_key() -> None:
    """A reorder on a full order of lost sessions still keeps every requested pin."""
    dead = [f"gone-{i}" for i in range(pso.MAX_PINNED_ORDER_KEYS)]
    pinned = {"a": True, "b": True}
    out = pso.after_reorder(dead, ["b", "a"], pinned)
    assert len(out) == pso.MAX_PINNED_ORDER_KEYS
    assert out[-2:] == ["b", "a"]
    assert out[:3] == dead[:3]


def test_a_new_pin_on_a_full_order_logs_the_key_it_evicts(caplog) -> None:
    """The cap's eviction of a key with no live slot is counted, never silent."""
    dead = [f"gone-{i}" for i in range(pso.MAX_PINNED_ORDER_KEYS)]
    with caplog.at_level("WARNING"):
        out = pso.after_pin_change(dead, "a", True, {"a": True}, True)
    assert len(out) == pso.MAX_PINNED_ORDER_KEYS
    assert out[-1] == "a" and dead[-1] not in out
    assert "dropping 1 pinned-order key(s) over the" in caplog.text


def test_the_cap_bounds_the_order_even_when_live_pins_alone_exceed_it() -> None:
    """The stored list never outgrows the cap, whatever holds its keys."""
    live = [f"live-{i}" for i in range(pso.MAX_PINNED_ORDER_KEYS + 1)]
    pinned = dict.fromkeys(live, True)
    out = pso.prune(live, pinned)
    assert out == live[: pso.MAX_PINNED_ORDER_KEYS]
