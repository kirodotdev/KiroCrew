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


async def _send(state, text: str) -> dict:
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat?ws=1", json={"message": text, "slot": "s1"})
        assert resp.status == 200
        return await resp.json()


async def _held(state, slot, ran, text: str) -> dict:
    """Hold *text* as a put-back entry carrying the sender's own author."""
    await _send(state, "warm up")
    author = slot.messages[-1]["meta"]["author"]
    ran.clear()
    slot._last_turn_auth_required = True
    qid = slot.queue_append(text, meta={"author": author}, directive_user_origin=True)
    entry = slot._queue[0]
    assert entry["id"] == qid
    entry[PUT_BACK_KEY] = "g1"
    entry[SIGNOUT_RETAINED_KEY] = True
    return entry


@pytest.mark.asyncio
async def test_a_new_send_waits_behind_the_held_prompts(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    ran = _capture(monkeypatch)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    await _held(state, slot, ran, "held during the outage")

    data = await _send(state, "sent after signing in")
    if slot.task is not None:
        await slot.task

    assert data.get("queued") is True
    assert ran[:1] == ["held during the outage"]


@pytest.mark.asyncio
async def test_sending_the_held_words_again_runs_them_once(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    ran = _capture(monkeypatch)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    held = await _held(state, slot, ran, "deploy the service")

    data = await _send(state, "deploy the service")
    if slot.task is not None:
        await slot.task

    assert data.get("queued") is not True
    assert ran == ["deploy the service"]
    assert all(entry["id"] != held["id"] for entry in slot._queue)
