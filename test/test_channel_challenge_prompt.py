"""A channel challenge link's prompt keeps its "already on the channel" mark.

The dashboard sends the prompt a channel challenge link carries with
``meta.origin == "channel_challenge"``. Every path ``POST /api/chat`` can take it
down must hand that mark to the turn, so the mirror does not quote the words back
into the thread they came from. Asserted on the real route: an immediate turn gets
the keyword, a queued or held entry gets the meta the drain reads, and a typed
send gets neither.
"""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from dashboard_owner_helpers import as_owner

from kiro_crew.dashboard import channel_prompt, chat_handlers, chat_runner

_CHALLENGE_META = {"origin": channel_prompt.CHANNEL_CHALLENGE_ORIGIN}


@pytest.fixture
def env(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    state.owner_id = ""
    slot = state.get_or_create_slot("chat-1")
    slot._titled = True
    slot._auto_tagged = True
    monkeypatch.setattr("kiro_crew.dashboard.chat_delivery.start_queue_persist", lambda *a: None)
    monkeypatch.setattr(chat_runner, "_gateway_shutdown_requested", lambda state: False)
    monkeypatch.setattr(chat_runner, "_memory_preparation_pending", lambda state: False)
    turns: list[tuple[str, dict]] = []

    async def fake_run_chat(state, slot, message, **kwargs):
        turns.append((message, kwargs))

    monkeypatch.setattr(chat_handlers, "_run_chat", fake_run_chat)
    app = as_owner(web.Application())
    app["state"] = state
    app.router.add_post("/api/chat", chat_handlers.api_chat)
    return state, slot, turns, app


def _send(slot, message="what changed?", meta=None):
    body = {"slot": slot.key, "message": message}
    if meta is not None:
        body["meta"] = dict(meta)
    return body


def _busy(slot):
    slot.task = asyncio.get_running_loop().create_future()
    slot._acp_client = type(
        "C", (), {"awaiting_permission": False, "has_active_turn": lambda self: True}
    )()


def _entry_marked(slot) -> list[bool]:
    return [
        (item.get("meta") or {}).get(channel_prompt.PROMPT_ON_CHANNEL_META_KEY) is True
        for item in slot._queue
    ]


@pytest.mark.asyncio
async def test_an_immediate_challenge_send_tells_the_turn(env):
    _, slot, turns, app = env
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/chat?ws=1", json=_send(slot, meta=_CHALLENGE_META))
        assert resp.status == 200
        await asyncio.sleep(0)
    ((text, kwargs),) = turns
    assert text == "what changed?"
    assert kwargs["_prompt_on_channel"] is True
    # Still the person's own words in every other respect.
    assert kwargs["_directive_user_origin"] is True


@pytest.mark.asyncio
async def test_a_typed_send_carries_no_mark(env):
    _, slot, turns, app = env
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/chat?ws=1", json=_send(slot, meta={"origin": "widget"}))
        assert resp.status == 200
        await asyncio.sleep(0)
    ((_, kwargs),) = turns
    assert "_prompt_on_channel" not in kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize(("meta", "marked"), [(_CHALLENGE_META, [True]), (None, [False])])
async def test_a_send_queued_behind_a_running_turn_keeps_its_mark(env, meta, marked):
    _, slot, turns, app = env
    _busy(slot)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/chat?ws=1", json=_send(slot, meta=meta))
        assert resp.status == 200
        assert (await resp.json())["queued"] is True
    assert turns == []
    assert _entry_marked(slot) == marked
    slot.task.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize(("meta", "marked"), [(_CHALLENGE_META, [True]), (None, [False])])
async def test_a_held_send_keeps_its_mark(env, monkeypatch, meta, marked):
    _, slot, turns, app = env
    # Idle, but a queued card's edit hold keeps the send (and the drain) waiting.
    for target in (
        "kiro_crew.dashboard.queue_edit_hold.edit_hold_blocks_user_turn",
        "kiro_crew.dashboard.chat_turn.queue_drain.edit_hold_blocks_user_turn",
    ):
        monkeypatch.setattr(target, lambda s: True)

    async def _no_drain(*_args, **_kwargs):
        return None

    # The entry is read where the send left it, before any drain consumes it.
    monkeypatch.setattr(chat_runner, "_drain_parked_queues", _no_drain)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/chat?ws=1", json=_send(slot, meta=meta))
        assert resp.status == 200
    assert turns == []
    assert _entry_marked(slot) == marked


def test_only_the_challenge_origin_claims_it():
    claims = channel_prompt.claims_prompt_on_channel
    assert claims({"meta": {"origin": "channel_challenge"}})
    assert not claims({"meta": {"origin": "widget"}})
    assert not claims({"meta": {"origin": ["channel_challenge"]}})
    assert not claims({"meta": "channel_challenge"})
    # The card send's top-level origin is a different claim.
    assert not claims({"origin": "channel_challenge"})
    assert not claims({})
    assert not claims(None)


def test_a_stamp_lands_only_on_the_named_entry():
    class _Owner:
        _queue = [{"id": "q1", "meta": {"sendId": "s"}}, {"id": "q2"}]

    owner = _Owner()
    channel_prompt.stamp_queued(owner, "q2", {"meta": _CHALLENGE_META})
    channel_prompt.stamp_queued(owner, "q1", {"meta": {"origin": "widget"}})
    assert owner._queue[0]["meta"] == {"sendId": "s"}
    assert owner._queue[1]["meta"] == {channel_prompt.PROMPT_ON_CHANNEL_META_KEY: True}


def test_an_edit_that_changes_the_words_drops_the_mark():
    # The editor's words are not in the linked thread, so they get their echo.
    from kiro_crew.dashboard.state import _ChatSlot

    slot = _ChatSlot("s1")
    key = channel_prompt.PROMPT_ON_CHANNEL_META_KEY
    kept = slot.queue_append("from the link", meta={key: True})
    edited = slot.queue_append("from the link too", meta={key: True})
    assert slot.queue_edit_by_id(kept, "from the link") is True
    assert slot.queue_edit_by_id(edited, "a new question") is True
    assert _entry_marked(slot) == [True, False]
