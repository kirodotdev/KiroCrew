"""What retires a waiting-on-you ask besides its own card, and what never does.

Only a live row the owner typed retires every ask. A replayed row, a teammate's or
channel member's message and every machine-written row retire none. A queued card
answer drains as its own row, so it retires only its own ask. A pinned ask makes no
slot busy, and a fork, rewind, delete or expiry treats the asks as the RFC says.
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

OWNER = local_operator_principal("local-app").to_public_dict()
TEAMMATE = local_operator_principal("teammate-ann").to_public_dict()


async def _settle_retirements() -> None:
    while events._RETIRE_TASKS:
        await asyncio.gather(*list(events._RETIRE_TASKS))


def _append(state, content: str, meta: dict | None, *, broadcast: bool = True) -> None:
    state._slots[SLOT].append("user", content, "msg msg-u", meta=meta, broadcast=broadcast)


@pytest.mark.asyncio
async def test_a_live_owner_message_retires_every_ask(state, store):
    await pin(store, ask("first"))
    await pin(store, ask("second"))
    _append(state, "something else", {HUMAN_TURN_META_KEY: True, "author": OWNER})
    await _settle_retirements()
    assert store.asks(SLOT) == []
    retired = store.snapshot_for_turn(SLOT)["retired"]
    assert [(r["key"], r["reason"]) for r in retired] == [
        ("first", "message"),
        ("second", "message"),
    ]
    assert [r["meta"]["card"]["status"] for r in waiting_rows(state)] == ["retired", "retired"]


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
        (
            "a card answer's own row",
            {HUMAN_TURN_META_KEY: True, "author": OWNER, "waitingOnYou": {"key": "first"}},
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
    await _settle_retirements()
    assert [a["key"] for a in store.asks(SLOT)] == ["first"], label


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
            resp = await client.post(
                f"/api/chat/slots/{SLOT}/waiting-on-you/first/answer",
                json={"ask_id": first["ask_id"], "answer_id": "r" * 12, "label": "Hold"},
            )
            assert (await resp.json())["outcome"] == "queued"
            resp = await client.post("/api/chat?ws=1", json={"slot": SLOT, "message": "and also"})
            assert (await resp.json()).get("queued") is True
    slot = state._slots[SLOT]
    assert len(slot._queue) == 2
    assert [a["key"] for a in store.asks(SLOT)] == ["second"]
    cfg = MagicMock()
    cfg.dashboard.merge_queued_messages = True
    with (
        patch("kiro_crew.dashboard.chat_runner._run_chat", new=AsyncMock()),
        patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=cfg),
    ):
        await _start_next_queued_turn(state, slot)
        await slot.task
        await _settle_retirements()
        assert [a["key"] for a in store.asks(SLOT)] == ["second"]
        slot.task = None
        await _start_next_queued_turn(state, slot)
        await slot.task
        await _settle_retirements()
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
    await _settle_retirements()
    await pin(store, ask())
    state.sessions._session_map.get = MagicMock(return_value="")
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post(f"/api/chat/slots/{SLOT}/fork", json={})
        assert resp.status == 200, await resp.text()
        child = (await resp.json())["key"]
    await _settle_retirements()
    assert child != SLOT
    assert state._slots[child].to_dict()["waiting_on_you"] == []
    assert [a["key"] for a in store.asks(SLOT)] == ["scope-override"]


@pytest.mark.asyncio
async def test_a_rewind_clears_the_slots_asks(state, store):
    from kiro_crew.dashboard.chat_rewind import api_chat_slot_rewind

    slot = state._slots[SLOT]
    slot.append("user", "question", "msg msg-u", ts="2026-05-21T16:00:00Z")
    slot.append("assistant", "answer", "msg msg-a", ts="2026-05-21T16:00:01Z")
    await pin(store, ask("first"))
    await pin(store, ask("second"))
    state.sessions._session_map.get = MagicMock(return_value="")
    app = web.Application()
    app["state"] = state
    app.router.add_post(
        "/api/chat/slots/{slot}/rewind", events.clearing_on_success(api_chat_slot_rewind)
    )
    with patch("kiro_crew.dashboard.chat_rewind._run_chat", new=AsyncMock()):
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                f"/api/chat/slots/{SLOT}/rewind",
                json={"at_message_index": 0, "content": "edited"},
            )
            assert resp.status == 200, await resp.text()
    if slot.task:
        await asyncio.wait_for(asyncio.shield(slot.task), timeout=30)
    assert store.asks(SLOT) == []
    # The agent re-declares from where the conversation stands; nothing is replayed.
    assert store.snapshot_for_turn(SLOT)["retired"] == []


@pytest.mark.asyncio
async def test_a_failed_rewind_keeps_the_asks(state, store):
    await pin(store, ask())

    async def refused(_request):
        return web.json_response({"error": "slot is running"}, status=409)

    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/rewind", events.clearing_on_success(refused))
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(f"/api/chat/slots/{SLOT}/rewind", json={})
        assert resp.status == 409
    assert len(store.asks(SLOT)) == 1


@pytest.mark.asyncio
async def test_deleting_a_session_removes_its_records(state, store):
    await pin(store, ask())

    async def deleted(_request):
        return web.json_response({"ok": True})

    app = web.Application()
    app["state"] = state
    app.router.add_delete("/api/sessions/{key}", events.forgetting_on_success(deleted))
    async with TestClient(TestServer(app)) as client:
        resp = await client.delete(f"/api/sessions/dashboard:{SLOT}")
        assert resp.status == 200
    assert store.asks(SLOT) == [] and not store.has_any(SLOT)


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
        (SLOT, ("waiting_on_you",)),
        (other, ("waiting_on_you",)),
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
    state.push_slot_patch.assert_called_with(SLOT, ("waiting_on_you",))
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
    assert sent == [{"slots": [{"key": SLOT, "waiting_on_you": [pinned]}]}]
    assert state._slots[SLOT].to_dict()["waiting_on_you"] == [pinned]
