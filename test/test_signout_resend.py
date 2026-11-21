"""A send made after a sign-in failure waits behind the prompts the failure held."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard import chat_handlers, chat_runner, chat_turn
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
        data = await resp.json()
    if (task := state.get_slot("s1").task) is not None:
        await task
    return data


async def _held(state, slot, ran, text: str) -> dict:
    await _send(state, "warm up")
    author = slot.messages[-1]["meta"]["author"]
    ran.clear()
    slot._last_turn_auth_required = True
    qid = slot.queue_append(text, meta={"author": author}, directive_user_origin=True)
    assert (entry := slot._queue[0])["id"] == qid
    entry.update({PUT_BACK_KEY: "g1", SIGNOUT_RETAINED_KEY: True})
    return entry


@pytest.mark.asyncio
@pytest.mark.parametrize("cron", [False, True])
async def test_a_new_send_waits_behind_the_held_prompts(state, slot, ran, monkeypatch, cron):
    await _held(state, slot, ran, "held during the outage")
    if cron:
        admit = AsyncMock(return_value=(None, None, None))
        monkeypatch.setattr(chat_handlers, "cron_creator_admission", admit)
        monkeypatch.setattr(chat_handlers, "cron_slot_creator", AsyncMock(return_value="cron:j"))
    data = await _send(state, "sent after signing in")
    assert data.get("queued") is True
    assert ran[:1] == ([] if cron else ["held during the outage"])
    assert not cron or len(slot._queue) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text, waits",
    [("/usage", False), ("/prompts get x", True), ("/var/log/syslog", True), ("/compact", True)],
)
async def test_only_a_no_model_slash_runs_past_held_prompts(state, slot, ran, text, waits):
    await _held(state, slot, ran, "held during the outage")
    data = await _send(state, text)
    assert bool(data.get("queued")) is waits
    assert ran[0] == ("held during the outage" if waits else text)


@pytest.mark.asyncio
@pytest.mark.parametrize("person", [True, False])
async def test_a_composer_send_after_sign_in_passes_a_child(state, slot, ran, monkeypatch, person):
    (await _held(state, slot, ran, "held during the outage"))["_directive_user_origin"] = person
    for module in (chat_handlers, chat_runner, chat_turn.queue_drain):
        monkeypatch.setattr(module, "subagents_hold_user_messages", lambda *_a: True)
    await _send(state, "sent after signing in")
    assert ran[:1] == ["held during the outage" if person else "sent after signing in"]


@pytest.mark.asyncio
async def test_sending_the_held_words_again_runs_both_in_order(state, slot, ran):
    await _held(state, slot, ran, "deploy the service")
    data = await _send(state, "deploy the service")
    assert data.get("queued") is True
    assert ran[:1] == ["deploy the service"]
    assert len(ran) + len(slot._queue) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("raced", [False, True])
async def test_a_refused_or_raced_resend_leaves_the_held_prompt_queued(
    state, slot, ran, monkeypatch, raced
):
    import asyncio

    from kiro_crew.shared_crew import admission_entry

    held = await _held(state, slot, ran, "deploy the service")
    refused = MagicMock(rejected=True)
    refused.refusal_response.return_value = web.json_response({"error": "full"}, status=503)
    real_admit, rival_done, rivals = admission_entry.admit_queued_send, asyncio.Event(), []

    async def admit_while_a_rival_turn_starts(*args, **kwargs):
        slot.task = asyncio.ensure_future(rival_done.wait())
        rivals.append(slot.task)
        return await real_admit(*args, **kwargs)

    admit = admit_while_a_rival_turn_starts if raced else AsyncMock(return_value=refused)
    monkeypatch.setattr(admission_entry, "admit_queued_send", admit)
    async with TestClient(TestServer(_make_app(state))) as client:
        body = {"message": "deploy the service", "slot": "s1"}
        assert (await client.post("/api/chat?ws=1", json=body)).status == (200 if raced else 503)
    assert rivals == ([slot.task] if raced else [])
    rival_done.set()
    for task in rivals:
        await task
    assert [entry["id"] for entry in slot._queue][: 1 if raced else None] == [held["id"]]
    assert ran == []
