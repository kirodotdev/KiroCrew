"""What retires a waiting-on-you ask besides its own card, and what never does.

Only a live row the owner typed retires every ask. A replayed row, a teammate's or
channel member's message and every machine-written row retire none. A queued card
answer drains as its own row, so it retires only its own ask. A pinned ask makes no
slot busy. A fork starts empty, a rewind or regenerate keeps the asks pinned, an
owner's edit-resend retires them, and a delete or expiry treats them as the RFC says.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app
from waiting_on_you_helpers import (  # noqa: F401 -- fixtures are requested by name
    SLOT,
    ask,
    clock_fixture,
    make_app,
    new_store,
    pin,
    settle_retirements,
    state_fixture,
    store_fixture,
    user_rows,
    waiting_rows,
)

from kiro_crew.dashboard import waiting_on_you as store_mod
from kiro_crew.dashboard import waiting_on_you_events as events
from kiro_crew.dashboard.chat_delivery import TURN_ACTOR_META_KEY
from kiro_crew.history_schema import HUMAN_TURN_META_KEY
from kiro_crew.shared_crew.contracts import Principal
from kiro_crew.shared_crew.identity import local_operator_principal
from kiro_crew.testing.wait import async_wait_until, until_parked

OWNER = local_operator_principal("local-app").to_public_dict()
TEAMMATE = local_operator_principal("teammate-ann").to_public_dict()


def _append(state, content: str, meta: dict | None, *, broadcast: bool = True) -> None:
    state._slots[SLOT].append("user", content, "msg msg-u", meta=meta, broadcast=broadcast)


@pytest.mark.asyncio
async def test_a_live_owner_message_retires_every_ask(state, store):
    await pin(store, ask("first"))
    await pin(store, ask("second"))
    _append(state, "something else", {HUMAN_TURN_META_KEY: True, "author": OWNER})
    await settle_retirements()
    assert store.asks(SLOT) == []
    retired = store.snapshot_for_turn(SLOT)["retired"]
    assert [(r["key"], r["reason"]) for r in retired] == [
        ("first", "message"),
        ("second", "message"),
    ]
    assert [r["meta"]["card"]["status"] for r in waiting_rows(state)] == ["retired", "retired"]


@pytest.mark.asyncio
async def test_an_ask_declared_while_the_message_waits_for_the_store_stays_pinned(state, store):
    """The owner wrote seeing only "first". The retirement that row scheduled waits
    for the store while "second" is being saved, and must not take "second" with it."""
    await pin(store, ask("first"))
    _append(state, "something else", {HUMAN_TURN_META_KEY: True, "author": OWNER})
    await pin(store, ask("second"))
    await settle_retirements()
    assert [a["key"] for a in store.asks(SLOT)] == ["second"]
    retired = store.snapshot_for_turn(SLOT)["retired"]
    assert [(r["key"], r["reason"]) for r in retired] == [("first", "message")]


@pytest.mark.parametrize(
    ("label", "meta", "broadcast"),
    [
        ("replayed owner row", {HUMAN_TURN_META_KEY: True, "author": OWNER}, False),
        ("auto-nudge", {"nudge": True}, True),
        ("cron inject", {"cronLabel": "nightly", TURN_ACTOR_META_KEY: "cron"}, True),
        ("peer session_send", {"relay": {"session": "dashboard:other"}}, True),
        ("app send", {TURN_ACTOR_META_KEY: "app"}, True),
        ("issue radar wake", {TURN_ACTOR_META_KEY: "crew", "issueRadar": True}, True),
        ("teammate", {HUMAN_TURN_META_KEY: True, "author": TEAMMATE}, True),
        (
            "channel row, unknown sender",
            {HUMAN_TURN_META_KEY: True, "author": Principal.unknown().to_public_dict()},
            True,
        ),
    ],
)
@pytest.mark.asyncio
async def test_rows_that_are_not_the_owner_writing_retire_nothing(
    state, store, label, meta, broadcast
):
    await pin(store, ask("first"))
    _append(state, f"from {label}", meta, broadcast=broadcast)
    await settle_retirements()
    assert [a["key"] for a in store.asks(SLOT)] == ["first"], label


@pytest.mark.asyncio
async def test_resuming_owner_authored_history_does_not_retire_asks(state, store):
    history_slot = "history-replay"
    history_key = f"dashboard:{history_slot}"
    await asyncio.to_thread(
        state.conversation_log.append,
        history_key,
        "user",
        "an earlier owner message",
        extra_meta={HUMAN_TURN_META_KEY: True, "author": OWNER},
    )
    await pin(store, ask(), slot=history_slot)

    async with TestClient(TestServer(_make_app(state))) as client:
        response = await client.post(
            f"/api/chat/slots/{history_slot}/resume", json={"key": history_key}
        )
        assert response.status == 200, await response.text()

    await settle_retirements()
    assert [item["key"] for item in store.asks(history_slot)] == ["scope-override"]

    state._slots[history_slot].append(
        "user",
        "a new owner message",
        "msg msg-u",
        meta={HUMAN_TURN_META_KEY: True, "author": OWNER},
    )
    await settle_retirements()
    assert store.asks(history_slot) == []


@pytest.mark.asyncio
async def test_a_queued_answer_and_a_queued_message_drain_as_separate_rows(state, store):
    from kiro_crew.dashboard.chat_runner import _start_next_queued_turn

    first = await pin(store, ask("first"))
    await pin(store, ask("second"))
    with (
        patch("kiro_crew.dashboard.chat_handlers._run_chat", new=AsyncMock()),
        patch("kiro_crew.crew_pause.holds_slot", return_value=True),
    ):
        async with TestClient(TestServer(make_app(state))) as client:
            claim = {"key": "first", "ask_id": first["ask_id"]}
            resp = await client.post(
                "/api/chat?ws=1",
                json={"slot": SLOT, "message": "Hold", "meta": {"waitingOnYouAnswer": claim}},
            )
            assert (await resp.json()).get("queued") is True
            resp = await client.post("/api/chat?ws=1", json={"slot": SLOT, "message": "and also"})
            assert (await resp.json()).get("queued") is True
    slot = state._slots[SLOT]
    assert len(slot._queue) == 2
    assert [a["key"] for a in store.asks(SLOT)] == ["first", "second"]
    cfg = MagicMock()
    cfg.dashboard.merge_queued_messages = True
    with (
        patch("kiro_crew.dashboard.chat_runner._run_chat", new=AsyncMock()),
        patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=cfg),
    ):
        await _start_next_queued_turn(state, slot)
        await slot.task
        await settle_retirements()
        assert [a["key"] for a in store.asks(SLOT)] == ["second"]
        slot.task = None
        await _start_next_queued_turn(state, slot)
        await slot.task
        await settle_retirements()
    assert [r["content"] for r in user_rows(state)] == ["Hold", "and also"]
    assert store.asks(SLOT) == []


@pytest.mark.asyncio
async def test_a_pinned_ask_makes_no_slot_busy(state, store):
    from kiro_crew.dashboard.chat_utils_completion import idle_compaction_blocker
    from kiro_crew.dashboard.responsive_chat import pending_interaction

    await pin(store, ask())
    slot = state._slots[SLOT]
    row = slot.to_dict()
    assert row["needs_input"] is False
    assert [a["key"] for a in row["waiting_on_you"]] == ["scope-override"]
    assert pending_interaction(slot) is False and pending_interaction(slot, idle=True) is False
    assert idle_compaction_blocker(state, f"dashboard:{SLOT}") is None


@pytest.mark.asyncio
async def test_a_slot_with_no_asks_carries_an_empty_list(state, store):
    assert state._slots[SLOT].to_dict()["waiting_on_you"] == []


@pytest.mark.asyncio
async def test_a_fork_starts_with_no_asks_and_its_replay_retires_none(state, store):
    slot = state._slots[SLOT]
    slot.append("user", "earlier", "msg msg-u", meta={HUMAN_TURN_META_KEY: True, "author": OWNER})
    slot.append("assistant", "reply", "msg msg-a")
    await settle_retirements()
    await pin(store, ask())
    state.sessions._session_map.get = MagicMock(return_value="")
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post(f"/api/chat/slots/{SLOT}/fork", json={})
        assert resp.status == 200, await resp.text()
        child = (await resp.json())["key"]
    await settle_retirements()
    assert child != SLOT
    assert state._slots[child].to_dict()["waiting_on_you"] == []
    assert [a["key"] for a in store.asks(SLOT)] == ["scope-override"]


@pytest.mark.asyncio
async def test_a_rewind_keeps_the_slots_asks_pinned(state, store):
    from kiro_crew.dashboard.chat_rewind import api_chat_slot_rewind

    slot = state._slots[SLOT]
    slot.append("user", "question", "msg msg-u", ts="2026-05-21T16:00:00Z")
    slot.append("assistant", "answer", "msg msg-a", ts="2026-05-21T16:00:01Z")
    await pin(store, ask("first"))
    await pin(store, ask("second"))
    state.sessions._session_map.get = MagicMock(return_value="")
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/rewind", api_chat_slot_rewind)
    with patch("kiro_crew.dashboard.chat_rewind._run_chat", new=AsyncMock()):
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                f"/api/chat/slots/{SLOT}/rewind",
                json={"at_message_index": 0, "content": "edited"},
            )
            assert resp.status == 200, await resp.text()
    if slot.task:
        await asyncio.wait_for(asyncio.shield(slot.task), timeout=30)
    await settle_retirements()
    # The agent still sees them in its block and clears any that has stopped applying.
    assert [a["key"] for a in store.asks(SLOT)] == ["first", "second"]


def test_only_edit_resend_is_wrapped_to_retire_the_asks():
    from kiro_crew.dashboard import chat
    from kiro_crew.dashboard.routes import sessions

    app = web.Application()
    sessions.register(app)
    handlers = {
        route.resource.canonical: route.handler
        for route in app.router.routes()
        if route.method == "POST" and route.resource is not None
    }
    assert handlers["/api/chat/slots/{slot}/rewind"] is chat.api_chat_slot_rewind
    assert handlers["/api/chat/slots/{slot}/regenerate"] is chat.api_chat_slot_regenerate
    edit_resend = handlers["/api/chat/slots/{slot}/edit-resend"]
    assert edit_resend is not chat.api_chat_slot_edit_resend
    assert edit_resend.__wrapped__ is chat.api_chat_slot_edit_resend


@pytest.mark.parametrize(
    ("status", "user", "kept"),
    [
        (200, "local-app", []),
        (409, "local-app", ["first", "second"]),
        (200, "someone-else", ["first", "second"]),
    ],
    ids=["owner-success", "refused", "not-the-owner"],
)
@pytest.mark.asyncio
async def test_a_successful_owner_edit_resend_retires_every_ask(state, store, status, user, kept):
    await pin(store, ask("first"))
    await pin(store, ask("second"))

    async def edit_resend(_request):
        return web.json_response({"ok": status == 200}, status=status)

    app = make_app(state, user=user)
    app.router.add_post(
        "/api/chat/slots/{slot}/edit-resend", events.retiring_on_success(edit_resend)
    )
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(f"/api/chat/slots/{SLOT}/edit-resend", json={})
        assert resp.status == status
    assert [a["key"] for a in store.asks(SLOT)] == kept
    if not kept:
        retired = store.snapshot_for_turn(SLOT)["retired"]
        assert {r["reason"] for r in retired} == {"message"}


@pytest.mark.asyncio
@pytest.mark.parametrize("history_key", [f"dashboard:{SLOT}", f"dashboard_{SLOT}", SLOT])
async def test_the_delete_teardown_removes_a_closed_sessions_records(state, store, history_key):
    """Both delete routes, one session and "Delete all", run this teardown only once
    the transcript is gone; a session whose tab was already closed has no live slot."""
    from kiro_crew.dashboard.handlers.sessions_parts.slot_teardown import (
        _remove_slot_for_history_key,
    )

    await pin(store, ask())
    state._slots.pop(SLOT)
    await _remove_slot_for_history_key(state, history_key)
    assert store.asks(SLOT) == [] and not store.has_any(SLOT)


@pytest.mark.asyncio
async def test_the_delete_teardown_keeps_the_records_of_a_key_a_live_slot_holds(state, store):
    """A slot key is recycled: a live slot on it is a successor, whose asks stay."""
    await pin(store, ask())
    await events.forget_deleted_session(state, {SLOT, f"dashboard_{SLOT}"})
    assert [a["key"] for a in store.asks(SLOT)] == ["scope-override"]


@pytest.mark.asyncio
async def test_a_delete_during_a_pending_first_declaration_removes_it_once_written(state, store):
    """The declaration holds the store lock while it writes and has not published its
    record yet; the removal waits for it rather than finding nothing and leaving."""
    import threading

    write_started, release = threading.Event(), threading.Event()
    real_write = store_mod.signed_json_store.write_locked

    def _slow_write(*args):
        write_started.set()
        release.wait(timeout=5)
        return real_write(*args)

    state._slots.pop(SLOT)
    with patch.object(store_mod.signed_json_store, "write_locked", side_effect=_slow_write):
        declaring = asyncio.create_task(pin(store, ask()))
        await async_wait_until(lambda: write_started.is_set() or declaring.done())
        forgetting = asyncio.create_task(events.forget_deleted_session(state, {SLOT}))
        # The removal is parked behind the declaration's lock before the write lands.
        await until_parked(store._lock)
        release.set()
        await declaring
        await forgetting
    assert store.asks(SLOT) == [] and not store.has_any(SLOT)


@pytest.mark.asyncio
async def test_a_removal_whose_write_failed_is_retried_by_the_sweep(state, store):
    await pin(store, ask())
    state._slots.pop(SLOT)
    with patch.object(
        store_mod.signed_json_store, "write_locked", side_effect=OSError("disk full")
    ):
        await events.forget_deleted_session(state, {SLOT})
    assert store.has_any(SLOT) and events._FORGET_RETRIES == {SLOT}
    await events.sweep_once(state)
    assert not store.has_any(SLOT) and events._FORGET_RETRIES == set()


@pytest.mark.asyncio
async def test_startup_pushes_every_ask_restored_after_a_tab_connected(state, store, clock):
    other = "chat-2-1700000002"
    await pin(store, ask("first"))
    await pin(store, ask("second"), slot=other)
    restarted = new_store(clock)
    state._waiting_on_you_store = restarted
    state.push_slot_patch.reset_mock()
    stopped = MagicMock()
    stopped.is_set.return_value = True

    with patch("kiro_crew.shutdown_event", stopped):
        await events._sweep_loop(state)

    assert [entry.args for entry in state.push_slot_patch.call_args_list] == [
        (SLOT, ("waiting_on_you", "needs_full_chat")),
        (other, ("waiting_on_you", "needs_full_chat")),
    ]


@pytest.mark.asyncio
async def test_an_ask_expires_seven_days_after_its_last_change_and_the_window_drops_it(
    state, store, clock
):
    await pin(store, ask("old"))
    clock.now += 6 * 24 * 3600
    await pin(store, ask("newer"))
    clock.now += 24 * 3600 + 1
    state.push_slot_patch.reset_mock()
    await events.sweep_once(state)
    assert [a["key"] for a in store.asks(SLOT)] == ["newer"]
    state.push_slot_patch.assert_called_with(SLOT, ("waiting_on_you", "needs_full_chat"))
    assert [r["meta"]["card"]["status"] for r in waiting_rows(state)][-1] == "expired"
    retired = store.snapshot_for_turn(SLOT)["retired"]
    assert [(r["key"], r["reason"]) for r in retired] == [("old", "expired")]


@pytest.mark.asyncio
async def test_a_replaced_ask_restarts_its_expiry(state, store, clock):
    await pin(store, ask("k"))
    clock.now += 6 * 24 * 3600
    await pin(store, {**ask("k"), "title": "Changed title"})
    clock.now += 2 * 24 * 3600
    await events.sweep_once(state)
    assert [a["title"] for a in store.asks(SLOT)] == ["Changed title"]


def test_the_store_change_is_announced_only_by_the_slots_push():
    """No frame of its own: ``publish`` writes rows and patches the slot row."""
    import inspect

    source = inspect.getsource(events)
    assert "broadcast_ws(" not in source
    assert store_mod.STORE_FILENAME == "waiting_on_you.json"


@pytest.mark.asyncio
async def test_the_field_rides_the_slot_patch_frame_as_well_as_the_full_list(state, store):
    from kiro_crew.dashboard.state import DashboardState

    sent: list[dict] = []
    del state.push_slot_patch  # the real one, on the real state
    state._has_slot_patch_clients = lambda: True
    state._has_legacy_slots_audience = lambda: False
    state._send_slot_patch = sent.append
    assert state.push_slot_patch.__func__ is DashboardState.push_slot_patch
    pinned = await pin(store, ask())
    await events.publish(state, SLOT, events.ROW_PINNED, [pinned])
    assert sent == [{"slots": [{"key": SLOT, "waiting_on_you": [pinned], "needs_full_chat": True}]}]
    assert state._slots[SLOT].to_dict()["waiting_on_you"] == [pinned]
    assert state._slots[SLOT].to_dict()["needs_full_chat"] is True

    sent.clear()
    gone = await store.clear(SLOT, None)
    await events.publish(state, SLOT, events.ROW_CLEARED, gone)
    assert sent == [{"slots": [{"key": SLOT, "waiting_on_you": [], "needs_full_chat": False}]}]


@pytest.mark.asyncio
async def test_the_turn_a_message_opens_waits_for_its_retirement_before_reading(state, store):
    """The owner dismissed one ask and writes while another is pinned. The turn starts
    while that row's retirement is still saving, and must tell the agent the remaining
    ask closed, never that it is both pinned and closed."""
    import threading

    dismissed = await pin(store, ask("dismissed"))
    await store.dismiss(SLOT, "dismissed", dismissed["ask_id"])
    await pin(store, ask("remaining"))
    write_started, release = threading.Event(), threading.Event()
    real_write = store_mod.signed_json_store.write_locked

    def _slow_write(*args):
        write_started.set()
        release.wait(timeout=5)
        return real_write(*args)

    with patch.object(store_mod.signed_json_store, "write_locked", side_effect=_slow_write):
        _append(state, "something else", {HUMAN_TURN_META_KEY: True, "author": OWNER})
        await async_wait_until(write_started.is_set)
        turn = asyncio.create_task(events.turn_block(state, state._slots[SLOT]))
        await asyncio.sleep(0)
        release.set()
        block = await turn
    assert "Pinned now" not in block
    assert "- remaining, closed because the person wrote" in block
    assert "- dismissed," in block


@pytest.mark.asyncio
async def test_a_retirement_retried_after_the_store_recovers_keeps_an_ask_declared_since(
    state, store, clock
):
    """The owner writes while a transient read error leaves the store unloaded, so
    the retirement fails and is queued. Once the store loads, the agent pins a new
    ask before the sweep retries: the retry closes only what was pinned when the
    owner wrote."""
    await pin(store, ask("seen"))
    store._loaded = False
    with patch.object(store_mod, "_read_capped", side_effect=OSError("busy")):
        _append(state, "something else", {HUMAN_TURN_META_KEY: True, "author": OWNER})
        await settle_retirements()
    assert [a["key"] for a in store.asks(SLOT)] == ["seen"]
    assert SLOT in events._RETIRE_RETRIES
    clock.now += 60
    await pin(store, ask("later"))

    await events.sweep_once(state)

    assert [a["key"] for a in store.asks(SLOT)] == ["later"]
    retired = store.snapshot_for_turn(SLOT)["retired"]
    assert [(r["key"], r["reason"]) for r in retired] == [("seen", "message")]


@pytest.mark.asyncio
async def test_a_steer_the_turn_ended_before_taking_retires_only_the_asks_shown_then(state, store):
    """The owner steers a running turn, which then pins a second ask and ends before
    taking the steer. The requeued steer drains as a queued row, and closes only the
    ask the owner could see when they wrote."""
    from kiro_crew.dashboard import chat_runner
    from kiro_crew.dashboard.chat_delivery import steer_into_running_turn

    seen = await pin(store, ask("seen"))
    slot = state._slots[SLOT]
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    client = MagicMock()
    client.supports_steer = True
    client.steer = AsyncMock(return_value=True)
    slot._acp_client = client

    await steer_into_running_turn(state, slot, "go left", owner_origin=True, author=OWNER)
    await pin(store, ask("later"))
    chat_runner._requeue_unconsumed_steers(state, slot)

    entry = next(e for e in slot._queue if e["content"] == "go left")
    assert entry["meta"][events.SHOWN_ASKS_META_KEY] == [seen["ask_id"]]
    _append(state, "go left", {**entry["meta"], HUMAN_TURN_META_KEY: True})
    await settle_retirements()
    assert [a["key"] for a in store.asks(SLOT)] == ["later"]


@pytest.mark.asyncio
async def test_a_steer_the_turn_takes_retires_only_the_asks_shown_when_it_was_sent(state, store):
    """The owner steers a running turn and the agent pins a second ask while the steer
    is being written. The steer's own row closes only the ask the owner could see."""
    from kiro_crew.dashboard.chat_delivery import steer_into_running_turn

    await pin(store, ask("seen"))
    slot = state._slots[SLOT]
    task = MagicMock()
    task.done.return_value = False
    slot.task = task

    async def _write_while_the_agent_pins(*_args, **_kwargs) -> bool:
        await pin(store, ask("later"))
        return True

    client = MagicMock()
    client.supports_steer = True
    client.steer = AsyncMock(side_effect=_write_while_the_agent_pins)
    slot._acp_client = client

    await steer_into_running_turn(
        state, slot, "go left", user_origin=True, owner_origin=True, author=OWNER
    )
    await settle_retirements()

    assert [m["content"] for m in slot.messages if m.get("role") == "user"] == ["go left"]
    assert [a["key"] for a in store.asks(SLOT)] == ["later"]
