"""A queued message waits while its card's editor is open.

Without the hold, a turn that ends while the person is still typing in the
card's editor drains the entry as it was: the agent gets the unedited words
and the save lands on an entry that is already gone. These pins cover the
hold itself (the drain leaves user entries queued), the save (the PATCH
releases the hold and the drain then sends the edited words), the close
without a save, and the deadline.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.dashboard import queue_edit_hold
from kiro_crew.dashboard.chat import api_chat_slot_queue_edit
from kiro_crew.testing.wait import async_wait_until, await_until


def _app(state) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_patch("/api/chat/slots/{slot}/queue/{queue_id}", api_chat_slot_queue_edit)
    app.router.add_post(
        "/api/chat/slots/{slot}/queue/{queue_id}/editing",
        queue_edit_hold.api_chat_slot_queue_editing,
    )
    return app


def _idle_state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    slot = state.get_or_create_slot("chat-1")
    qid = slot.queue_append("old words", directive_user_origin=True)
    return state, slot, qid


def _hold(item, holder: str = "") -> None:
    item.setdefault(queue_edit_hold.EDIT_HOLD_KEY, {})[holder] = queue_edit_hold._clock() + 60.0


def _user_rows(slot) -> list[str]:
    return [m["content"] for m in slot.messages if m.get("role") == "user"]


def _patched_turn_start():
    from kiro_crew.dashboard import chat_runner

    return (
        patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()),
        patch.object(chat_runner, "_run_chat", return_value=MagicMock()),
    )


@pytest.fixture(autouse=True)
def _no_release_grace(_floor_monkeypatch):
    _floor_monkeypatch.setattr(queue_edit_hold, "RELEASE_GRACE_SECONDS", 0.0)


async def _settle_background(state, slot, holder: str | None = None) -> None:
    # A release leaves the hold for the grace; its expiry starts the drain.
    # With *holder*, only that editor's hold is waited out.
    def _gone(item) -> bool:
        holds = item.get(queue_edit_hold.EDIT_HOLD_KEY) or {}
        return not holds if holder is None else holder not in holds

    await async_wait_until(
        lambda: all(_gone(i) for i in slot._queue),
        describe=lambda: "the released hold's expiry",
    )
    pending = list(state._background_tasks)
    if pending:
        await await_until(asyncio.gather(*pending), what="the hold's background drains")


@pytest.mark.asyncio
async def test_the_drain_leaves_a_held_entry_queued(tmp_path, monkeypatch):
    from kiro_crew.dashboard import chat_runner

    state, slot, qid = _idle_state(tmp_path, monkeypatch)
    _hold(slot._queue[0])

    spawn, run = _patched_turn_start()
    with spawn as spawned, run:
        assert await chat_runner._start_next_queued_turn(state, slot) is False

    spawned.assert_not_called()
    assert [q["id"] for q in slot._queue] == [qid]
    assert _user_rows(slot) == []


@pytest.mark.asyncio
async def test_a_held_responsive_priority_entry_is_not_promoted(tmp_path, monkeypatch):
    from kiro_crew.dashboard import chat_runner, responsive_chat

    state, slot, qid = _idle_state(tmp_path, monkeypatch)
    slot._queue[0][responsive_chat.RESPONSIVE_KEY] = True
    assert responsive_chat.first_priority_id(slot) == qid
    _hold(slot._queue[0])

    spawn, run = _patched_turn_start()
    with spawn as spawned, run:
        assert await chat_runner._start_next_queued_turn(state, slot) is False

    spawned.assert_not_called()
    assert [q["id"] for q in slot._queue] == [qid]
    assert _user_rows(slot) == []


@pytest.mark.asyncio
async def test_a_held_priority_entry_does_not_park_a_system_entry(tmp_path, monkeypatch):
    from kiro_crew.dashboard import chat_runner, responsive_chat
    from kiro_crew.dashboard.chat_utils import CRON_NOTIFICATION_KIND
    from kiro_crew.dashboard.state import CRON_NOTIFY_PREFIX

    state, slot, qid = _idle_state(tmp_path, monkeypatch)
    slot._queue[0][responsive_chat.RESPONSIVE_KEY] = True
    _hold(slot._queue[0])
    slot._queue.append(
        {
            "id": "cron-1",
            "content": f"{CRON_NOTIFY_PREFIX}daily]: report",
            "kind": CRON_NOTIFICATION_KIND,
        }
    )

    spawn, run = _patched_turn_start()
    with spawn as spawned, run:
        assert await chat_runner._start_next_queued_turn(state, slot) is True

    spawned.assert_called_once()
    assert [q["id"] for q in slot._queue] == [qid]


@pytest.mark.asyncio
async def test_a_drain_picked_id_does_not_pass_the_hold(tmp_path, monkeypatch):
    # A queued arrival or a restored card names its id without the person
    # choosing Run now; the hold still keeps it queued.
    from kiro_crew.dashboard import chat_runner

    state, slot, qid = _idle_state(tmp_path, monkeypatch)
    _hold(slot._queue[0])
    arrived = slot.queue_append("arrived later", directive_user_origin=True)

    spawn, run = _patched_turn_start()
    with spawn as spawned, run:
        assert (
            await chat_runner._start_next_queued_turn(state, slot, required_queue_id=arrived)
            is False
        )

    spawned.assert_not_called()
    assert [q["id"] for q in slot._queue] == [qid, arrived]


@pytest.mark.asyncio
async def test_run_now_on_another_card_passes_the_hold(tmp_path, monkeypatch):
    from kiro_crew.dashboard import chat_runner

    state, slot, qid = _idle_state(tmp_path, monkeypatch)
    _hold(slot._queue[0])
    other = slot.queue_append("run me now", directive_user_origin=True)

    spawn, run = _patched_turn_start()
    with spawn as spawned, run:
        started = await chat_runner._start_next_queued_turn(
            state, slot, allow_user_during_subagents=True, required_queue_id=other
        )

    assert started is True
    spawned.assert_called_once()
    assert [q["id"] for q in slot._queue] == [qid]


@pytest.mark.asyncio
async def test_a_composer_send_queues_behind_a_held_entry(tmp_path, monkeypatch):
    from chat_test_helpers import _make_app

    direct: list[str] = []

    async def fake_run_chat(st, sl, msg, **_kw):
        direct.append(msg)

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", fake_run_chat)
    state, slot, qid = _idle_state(tmp_path, monkeypatch)
    _hold(slot._queue[0])

    spawn, run = _patched_turn_start()
    with spawn as spawned, run:
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat?ws=1", json={"message": "new send", "slot": "chat-1"}
            )
            assert resp.status == 200
            assert (await resp.json()).get("queued") is True

    assert direct == []
    spawned.assert_not_called()
    assert [q["content"] for q in slot._queue] == ["old words", "new send"]


@pytest.mark.asyncio
@pytest.mark.parametrize("named", [False, True], ids=["turn-end", "named-card"])
async def test_a_composer_marked_send_waits_behind_the_card_being_edited(
    tmp_path, monkeypatch, named
):
    """A composer send carries the mark that lets it pass the sub-agent hold. An
    open editor's hold is not that hold: the marked send stays queued behind the
    edited card, whether the turn-end drain picks it or a card names it."""
    from kiro_crew.dashboard import chat_runner
    from kiro_crew.dashboard.chat_utils import COMPOSER_SEND_META

    state, slot, qid = _idle_state(tmp_path, monkeypatch)
    _hold(slot._queue[0])
    later = slot.queue_append("sent while editing", "", {COMPOSER_SEND_META: True})

    spawn, run = _patched_turn_start()
    with spawn as spawned, run:
        kwargs = {"required_queue_id": later} if named else {}
        assert await chat_runner._start_next_queued_turn(state, slot, **kwargs) is False

    spawned.assert_not_called()
    assert [q["content"] for q in slot._queue] == ["old words", "sent while editing"]


@pytest.mark.asyncio
async def test_a_composer_send_an_editor_held_keeps_its_mark_for_after_the_hold(
    tmp_path, monkeypatch
):
    """Live children AND an open editor: the send is queued behind the card, but
    with the composer mark, so once the editor closes it is not then parked
    behind the children as well."""
    from unittest.mock import AsyncMock

    from chat_test_helpers import _make_app

    from kiro_crew.dashboard.chat_utils import COMPOSER_SEND_META, releases_past_subagent_hold

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", AsyncMock())
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    subs = MagicMock(
        running_agents_for=MagicMock(return_value=[{"id": "live", "stalled": False}]),
        queued_count_for_async=AsyncMock(return_value=0),
        queued_count_or_none_async=AsyncMock(return_value=0),
        has_in_memory_pending_work_for=MagicMock(return_value=False),
        published_queued_depths=MagicMock(return_value={}),
    )
    state = _make_state(tmp_path, subagents=subs)
    state.broadcast_ws = MagicMock()
    slot = state.get_or_create_slot("chat-1")
    slot.queue_append("old words", directive_user_origin=True)
    _hold(slot._queue[0])

    spawn, run = _patched_turn_start()
    with spawn as spawned, run:
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat?ws=1", json={"message": "new send", "slot": "chat-1"}
            )
            assert (await resp.json()).get("queued") is True
        spawned.assert_not_called()
        assert [q["content"] for q in slot._queue] == ["old words", "new send"]
        sent = slot._queue[1]
        assert (sent.get("meta") or {}).get(COMPOSER_SEND_META) is True
        assert releases_past_subagent_hold(sent)


@pytest.mark.asyncio
async def test_saving_the_edit_sends_the_edited_words(tmp_path, monkeypatch):
    state, slot, qid = _idle_state(tmp_path, monkeypatch)
    spawn, run = _patched_turn_start()
    with patch("kiro_crew.sel.sel") as mock_sel, spawn as spawned, run:
        mock_sel.return_value = MagicMock()
        async with TestClient(TestServer(_app(state))) as client:
            resp = await client.post(
                f"/api/chat/slots/chat-1/queue/{qid}/editing", json={"editing": True}
            )
            assert resp.status == 200
            # The turn ends while the editor is open: nothing is sent.
            from kiro_crew.dashboard import chat_runner

            assert await chat_runner._start_next_queued_turn(state, slot) is False
            resp = await client.patch(
                f"/api/chat/slots/chat-1/queue/{qid}", json={"content": "new words"}
            )
            assert resp.status == 200
            await _settle_background(state, slot)

    spawned.assert_called_once()
    assert slot._queue == []
    assert _user_rows(slot) == ["new words"]


@pytest.mark.asyncio
async def test_closing_the_editor_without_a_save_sends_the_entry(tmp_path, monkeypatch):
    state, slot, qid = _idle_state(tmp_path, monkeypatch)
    spawn, run = _patched_turn_start()
    with spawn, run:
        async with TestClient(TestServer(_app(state))) as client:
            await client.post(f"/api/chat/slots/chat-1/queue/{qid}/editing", json={"editing": True})
            resp = await client.post(
                f"/api/chat/slots/chat-1/queue/{qid}/editing", json={"editing": False}
            )
            assert resp.status == 200
            await _settle_background(state, slot)

    assert slot._queue == []
    assert _user_rows(slot) == ["old words"]


@pytest.mark.asyncio
async def test_the_hold_route_validates_its_input(tmp_path, monkeypatch):
    state, slot, qid = _idle_state(tmp_path, monkeypatch)
    async with TestClient(TestServer(_app(state))) as client:
        bad = await client.post(f"/api/chat/slots/chat-1/queue/{qid}/editing", json={"editing": 1})
        gone = await client.post(
            "/api/chat/slots/chat-1/queue/no-such-id/editing", json={"editing": True}
        )
        no_slot = await client.post(
            f"/api/chat/slots/nope/queue/{qid}/editing", json={"editing": True}
        )
        codes = ((await bad.json())["code"], (await gone.json())["code"])
    assert (bad.status, gone.status, no_slot.status) == (400, 404, 404)
    assert codes == ("invalid_field_type", "queue_entry_not_found")
    assert queue_edit_hold.EDIT_HOLD_KEY not in slot._queue[0]


@pytest.mark.asyncio
async def test_a_release_on_a_slot_held_for_sign_in_starts_nothing(tmp_path, monkeypatch):
    state, slot, qid = _idle_state(tmp_path, monkeypatch)
    slot._last_turn_auth_required = True
    spawn, run = _patched_turn_start()
    with spawn as spawned, run:
        async with TestClient(TestServer(_app(state))) as client:
            await client.post(f"/api/chat/slots/chat-1/queue/{qid}/editing", json={"editing": True})
            resp = await client.post(
                f"/api/chat/slots/chat-1/queue/{qid}/editing", json={"editing": False}
            )
            assert resp.status == 200
            await _settle_background(state, slot)

    spawned.assert_not_called()
    assert [q["id"] for q in slot._queue] == [qid]


@pytest.mark.asyncio
async def test_cancelling_the_held_entry_sends_the_one_behind_it(tmp_path, monkeypatch):
    from kiro_crew.dashboard.chat import api_chat_slot_queue_cancel

    state, slot, qid = _idle_state(tmp_path, monkeypatch)
    slot.queue_append("next words", directive_user_origin=True)
    app = _app(state)
    app.router.add_delete("/api/chat/slots/{slot}/queue/{queue_id}", api_chat_slot_queue_cancel)
    spawn, run = _patched_turn_start()
    with patch("kiro_crew.sel.sel") as mock_sel, spawn, run:
        mock_sel.return_value = MagicMock()
        async with TestClient(TestServer(app)) as client:
            # Under an editor's own id, as the dashboard holds: the cancel names
            # no holder, so only the entry leaving the queue releases this hold.
            await client.post(
                f"/api/chat/slots/chat-1/queue/{qid}/editing",
                json={"editing": True, "holder": "tab-a"},
            )
            resp = await client.delete(f"/api/chat/slots/chat-1/queue/{qid}")
            assert resp.status == 200
            await _settle_background(state, slot)

    assert slot._queue == []
    assert _user_rows(slot) == ["next words"]


def test_a_hold_past_its_deadline_no_longer_holds(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(queue_edit_hold, "_clock", lambda: now[0])
    item = {"id": "q1", "content": "x", queue_edit_hold.EDIT_HOLD_KEY: {"": 1000.0 + 5.0}}
    assert queue_edit_hold.edit_held(item) is True
    now[0] = 1006.0
    assert queue_edit_hold.edit_held(item) is False


def test_the_dashboard_serves_the_hold_route():
    from kiro_crew.dashboard.routes import chat as chat_routes

    app = web.Application()
    chat_routes.register(app)
    handlers = {
        (r.method, r.resource.canonical): r.handler
        for r in app.router.routes()
        if r.resource is not None
    }
    assert (
        handlers[("POST", "/api/chat/slots/{slot}/queue/{queue_id}/editing")]
        is queue_edit_hold.api_chat_slot_queue_editing
    )


@pytest.mark.asyncio
async def test_a_turn_ending_inside_the_release_grace_sends_nothing(tmp_path, monkeypatch):
    # A move the press hold does not order (from the keyboard, with Ctrl held,
    # from an older dashboard): the first editor's blur releases its card before
    # the second card's click holds it. A turn ending in between finds it held.
    from kiro_crew.dashboard import chat_runner

    grace = queue_edit_hold.EDIT_HOLD_SECONDS
    monkeypatch.setattr(
        queue_edit_hold, "RELEASE_GRACE_SECONDS", grace
    )  # flake-ok: a grace no test outlasts, not a wait
    state, slot, first = _idle_state(tmp_path, monkeypatch)
    second = slot.queue_append("second words", directive_user_origin=True)
    spawn, run = _patched_turn_start()
    with spawn as spawned, run:
        async with TestClient(TestServer(_app(state))) as client:
            url = "/api/chat/slots/chat-1/queue/{}/editing"
            await client.post(url.format(first), json={"editing": True})
            await client.post(url.format(first), json={"editing": False})
            assert await chat_runner._start_next_queued_turn(state, slot) is False
            await client.post(url.format(second), json={"editing": True})

    spawned.assert_not_called()
    assert [q["id"] for q in slot._queue] == [first, second]


_EDITING = "/api/chat/slots/chat-1/queue/{}/editing"


@pytest.mark.asyncio
async def test_closing_one_editor_keeps_the_entry_held_for_the_other(tmp_path, monkeypatch):
    # The same card open in two tabs: closing it in one must not let the
    # entry run with its old words while the other is still typing.
    from kiro_crew.dashboard import chat_runner

    state, slot, qid = _idle_state(tmp_path, monkeypatch)
    spawn, run = _patched_turn_start()
    with spawn as spawned, run:
        async with TestClient(TestServer(_app(state))) as client:
            for tab in ("tab-a", "tab-b"):
                resp = await client.post(
                    _EDITING.format(qid), json={"editing": True, "holder": tab}
                )
                assert resp.status == 200
            await client.post(_EDITING.format(qid), json={"editing": False, "holder": "tab-a"})
            await _settle_background(state, slot, holder="tab-a")
            assert await chat_runner._start_next_queued_turn(state, slot) is False
            spawned.assert_not_called()
            assert [q["id"] for q in slot._queue] == [qid]
            assert queue_edit_hold.edit_held(slot._queue[0])
            await client.post(_EDITING.format(qid), json={"editing": False, "holder": "tab-b"})
            await _settle_background(state, slot)

    spawned.assert_called_once()
    assert _user_rows(slot) == ["old words"]


@pytest.mark.asyncio
async def test_a_lapsed_editor_does_not_hold_the_entry_once_the_other_closes(tmp_path, monkeypatch):
    # One tab was killed and stopped renewing, so its deadline passed; the
    # other tab's release then drains, since a lapsed holder holds nothing.
    now = [1000.0]
    monkeypatch.setattr(queue_edit_hold, "_clock", lambda: now[0])
    state, slot, qid = _idle_state(tmp_path, monkeypatch)
    spawn, run = _patched_turn_start()
    with spawn as spawned, run:
        async with TestClient(TestServer(_app(state))) as client:
            await client.post(_EDITING.format(qid), json={"editing": True, "holder": "killed"})
            now[0] += queue_edit_hold.EDIT_HOLD_SECONDS - 10.0
            await client.post(_EDITING.format(qid), json={"editing": True, "holder": "open"})
            now[0] += 20.0
            assert queue_edit_hold.edit_held(slot._queue[0])
            await client.post(_EDITING.format(qid), json={"editing": False, "holder": "open"})
            await _settle_background(state, slot, holder="open")

    spawned.assert_called_once()
    assert slot._queue == []
    assert _user_rows(slot) == ["old words"]


@pytest.mark.asyncio
async def test_a_release_from_an_editor_that_holds_nothing_changes_nothing(tmp_path, monkeypatch):
    state, slot, qid = _idle_state(tmp_path, monkeypatch)
    spawn, run = _patched_turn_start()
    with spawn as spawned, run:
        async with TestClient(TestServer(_app(state))) as client:
            await client.post(_EDITING.format(qid), json={"editing": True, "holder": "tab-a"})
            before = dict(slot._queue[0][queue_edit_hold.EDIT_HOLD_KEY])
            resp = await client.post(
                _EDITING.format(qid), json={"editing": False, "holder": "stranger"}
            )
            assert resp.status == 200
            assert slot._queue[0][queue_edit_hold.EDIT_HOLD_KEY] == before
            pending = list(state._background_tasks)
            if pending:
                await await_until(asyncio.gather(*pending), what="background drains")

    spawned.assert_not_called()
    assert [q["id"] for q in slot._queue] == [qid]


@pytest.mark.asyncio
async def test_a_save_releases_only_its_own_editor(tmp_path, monkeypatch):
    state, slot, qid = _idle_state(tmp_path, monkeypatch)
    spawn, run = _patched_turn_start()
    with patch("kiro_crew.sel.sel") as mock_sel, spawn as spawned, run:
        mock_sel.return_value = MagicMock()
        async with TestClient(TestServer(_app(state))) as client:
            for tab in ("saver", "still-typing"):
                await client.post(_EDITING.format(qid), json={"editing": True, "holder": tab})
            resp = await client.patch(
                f"/api/chat/slots/chat-1/queue/{qid}",
                json={"content": "new words", "holder": "saver"},
            )
            assert resp.status == 200
            await _settle_background(state, slot, holder="saver")
            spawned.assert_not_called()
            assert [q["content"] for q in slot._queue] == ["new words"]
            assert set(slot._queue[0][queue_edit_hold.EDIT_HOLD_KEY]) == {"still-typing"}
            await client.post(
                _EDITING.format(qid), json={"editing": False, "holder": "still-typing"}
            )
            await _settle_background(state, slot)

    spawned.assert_called_once()
    assert _user_rows(slot) == ["new words"]


@pytest.mark.asyncio
async def test_a_holder_that_is_not_a_string_is_refused(tmp_path, monkeypatch):
    state, slot, qid = _idle_state(tmp_path, monkeypatch)
    async with TestClient(TestServer(_app(state))) as client:
        hold = await client.post(_EDITING.format(qid), json={"editing": True, "holder": 7})
        save = await client.patch(
            f"/api/chat/slots/chat-1/queue/{qid}", json={"content": "new", "holder": ["x"]}
        )
        codes = ((await hold.json())["code"], (await save.json())["code"])
    assert (hold.status, save.status) == (400, 400)
    assert codes == ("invalid_field_type", "invalid_field_type")
    assert slot._queue[0]["content"] == "old words"
    assert queue_edit_hold.EDIT_HOLD_KEY not in slot._queue[0]


@pytest.mark.asyncio
async def test_a_holder_too_long_to_keep_is_refused(tmp_path, monkeypatch):
    # The entry keeps the id the request names, so the length is bounded here.
    state, slot, qid = _idle_state(tmp_path, monkeypatch)
    long_id = "x" * (queue_edit_hold.MAX_HOLDER_LEN + 1)
    async with TestClient(TestServer(_app(state))) as client:
        hold = await client.post(_EDITING.format(qid), json={"editing": True, "holder": long_id})
        save = await client.patch(
            f"/api/chat/slots/chat-1/queue/{qid}", json={"content": "new", "holder": long_id}
        )
        codes = ((await hold.json())["code"], (await save.json())["code"])
        fits = await client.post(
            _EDITING.format(qid),
            json={"editing": True, "holder": "x" * queue_edit_hold.MAX_HOLDER_LEN},
        )
    assert (hold.status, save.status) == (400, 400)
    assert codes == ("holder_too_long", "holder_too_long")
    assert slot._queue[0]["content"] == "old words"
    assert fits.status == 200
    assert set(slot._queue[0][queue_edit_hold.EDIT_HOLD_KEY]) == {
        "x" * queue_edit_hold.MAX_HOLDER_LEN
    }


@pytest.mark.asyncio
async def test_the_entry_holds_a_bounded_set_of_editors(tmp_path, monkeypatch, caplog):
    # A caller minting ids cannot grow the entry: past the cap the earliest
    # deadline is dropped, and each drop is logged, so a lost hold is not
    # mistaken for a lapse.
    now = [1000.0]
    monkeypatch.setattr(queue_edit_hold, "_clock", lambda: now[0])
    state, slot, qid = _idle_state(tmp_path, monkeypatch)
    entry = slot._queue[0]
    queue_edit_hold.place_edit_hold(state, slot, entry, "live")

    with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.queue_edit_hold"):
        for i in range(queue_edit_hold.MAX_EDIT_HOLDERS + 5):
            now[0] += 0.001
            queue_edit_hold.place_edit_hold(state, slot, entry, f"minted-{i}")
    dropped = [r for r in caplog.records if "dropped the earliest hold" in r.getMessage()]
    assert len(dropped) == 6
    holds = entry[queue_edit_hold.EDIT_HOLD_KEY]
    assert len(holds) == queue_edit_hold.MAX_EDIT_HOLDERS
    # The earliest deadlines went first, so the newest editors are the ones held.
    assert "live" not in holds
    assert f"minted-{queue_edit_hold.MAX_EDIT_HOLDERS + 4}" in holds
    assert queue_edit_hold.edit_held(entry)
