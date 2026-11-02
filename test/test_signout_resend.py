"""A send made after a sign-in failure waits behind the prompts the failure held."""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard.chat_utils_injected_rows import SIGNOUT_RETAINED_KEY
from kiro_crew.dashboard.slot_queue_repository import PUT_BACK_KEY


def _capture(monkeypatch) -> list[str]:
    ran: list[str] = []

    async def fake_run_chat(_st, _sl, msg, **_kwargs):
        ran.append(msg)

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", fake_run_chat)
    monkeypatch.setattr("kiro_crew.dashboard.chat_runner._run_chat", fake_run_chat)
    return ran


@pytest.fixture
def state(tmp_path, monkeypatch):
    from dataclasses import replace

    from kiro_crew.shared_crew import identity

    actor = replace(identity.unknown_actor(), principal=identity.local_operator_principal("me"))
    monkeypatch.setattr(identity, "actor_of", lambda _request: actor)
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    return _make_state(tmp_path)


@pytest.fixture
def slot(state):
    return state.get_or_create_slot("s1")


@pytest.fixture
def ran(monkeypatch):
    return _capture(monkeypatch)


async def _send(state, text: str) -> dict:
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat?ws=1", json={"message": text, "slot": "s1"})
        assert resp.status == 200
        return await resp.json()


async def _held(state, slot, ran, text: str) -> dict:
    await _send(state, "warm up")
    author = slot.messages[-1]["meta"]["author"]
    ran.clear()
    slot._last_turn_auth_required = True
    qid = slot.queue_append(text, meta={"author": author}, directive_user_origin=True)
    entry = slot._queue[0]
    assert entry["id"] == qid
    entry.update({PUT_BACK_KEY: "g1", SIGNOUT_RETAINED_KEY: True})
    return entry


@pytest.mark.asyncio
async def test_a_new_send_waits_behind_the_held_prompts(state, slot, ran):
    await _held(state, slot, ran, "held during the outage")

    data = await _send(state, "sent after signing in")
    if slot.task is not None:
        await slot.task

    assert data.get("queued") is True
    assert ran[:1] == ["held during the outage"]


@pytest.mark.asyncio
async def test_a_slash_command_needs_no_sign_in_so_it_runs_past_held_prompts(state, slot, ran):
    await _held(state, slot, ran, "held during the outage")

    data = await _send(state, "/usage")
    if slot.task is not None:
        await slot.task

    assert data.get("queued") is not True and ran == ["/usage"]


@pytest.mark.asyncio
async def test_a_composer_send_after_sign_in_runs_held_prompts_past_a_child(
    state, slot, ran, monkeypatch
):
    from kiro_crew.dashboard import chat_handlers, chat_runner

    await _held(state, slot, ran, "held during the outage")
    for module in (chat_handlers, chat_runner):
        monkeypatch.setattr(module, "subagents_hold_user_messages", lambda *_a: True)

    await _send(state, "sent after signing in")
    if slot.task is not None:
        await slot.task

    assert ran[:1] == ["held during the outage"]


@pytest.mark.asyncio
async def test_sending_the_held_words_again_runs_them_once(state, slot, ran):
    from unittest.mock import MagicMock

    held = await _held(state, slot, ran, "deploy the service")
    state.broadcast_ws = MagicMock()

    await _send(state, "deploy the service")
    if slot.task is not None:
        await slot.task

    assert ran == ["deploy the service"]
    assert all(entry["id"] != held["id"] for entry in slot._queue)
    pops = [c.args[1] for c in state.broadcast_ws.call_args_list if c.args[0] == "queue_pop"]
    mine = [p for p in pops if p.get("queue_id") == held["id"]]
    assert mine and all(p.get("drain_writes_row") is True for p in mine)


@pytest.mark.asyncio
async def test_a_refused_resend_leaves_the_held_prompt_queued(state, slot, ran, monkeypatch):
    from unittest.mock import AsyncMock, MagicMock

    from aiohttp import web

    from kiro_crew.shared_crew import admission_entry

    held = await _held(state, slot, ran, "deploy the service")
    refused = MagicMock(rejected=True)
    refused.refusal_response.return_value = web.json_response({"error": "full"}, status=503)
    monkeypatch.setattr(admission_entry, "admit_queued_send", AsyncMock(return_value=refused))

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post(
            "/api/chat?ws=1", json={"message": "deploy the service", "slot": "s1"}
        )
        assert resp.status == 503

    assert [entry["id"] for entry in slot._queue] == [held["id"]]
    assert ran == []
