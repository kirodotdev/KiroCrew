"""An app's session never runs as a Bot.

The owner approves the paths an app may call, never a Bot, so owning a session
gives an app no claim on a Bot's memory store or on its chat. Each route that
takes an agent name answers an app that names a Bot with the 404 it gets for
anything it may not reach, and turn admission refuses the same selection whichever
way it reached the session: a name stamped on the slot, or an execution record the
session already carries. A refused turn starts no provider, attaches no store and
writes no record. The owner still opens a session as a Bot, and an app still runs
an agent of its own: a template that shares a Bot's name, and a ``config.agents``
row on the shared store, which has nothing of its own to give.
The shared turn state and waits live in ``chat_agent_selection_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_agent_selection_helpers import _finished, _turn_state
from chat_test_helpers import drain_background_tasks

from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.config.sections import MemoryStoreConfig
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_utils import APP_AGENT_REFUSED_KIND
from kiro_crew.dashboard.slot_ownership import slot_ownership_middleware
from kiro_crew.execution_context import (
    bind_session_execution,
    read_session_execution,
    resolve_member_execution,
)
from kiro_crew.mate_worker import MATE_CONDUCTOR_AGENT
from kiro_crew.member_memory_auth import read_private_session_store
from kiro_crew.memory_stores import persist_member_config, provision_member_memory

APP = "shop"
#: A Bot with private memory, and one bound to a named store of its own.
PRIVATE_BOT = "reviewer"
NAMED_STORE_BOT = "archivist"
NAMED_STORE = "archive"
#: A ``config.agents`` row on the shared store: a template with a workspace and a
#: model pin, as the reserved ``default`` row is.
SHARED_ROW = "plain"
SHARED_ROW_MODEL = "pinned-model"
#: An agent the app ships: a template only.
APP_AGENT = "shop-helper"


def _add_bots(monkeypatch) -> str:
    """Two Bots, a shared-store row and the app's own template. Returns the private store."""
    cfg = KiroCrewConfig.load()
    cfg.agents[PRIVATE_BOT] = KiroCrewAgentConfig(kiro_agent="kirocrew")
    store = provision_member_memory(cfg, PRIVATE_BOT)
    persist_member_config(cfg, PRIVATE_BOT, create=True)
    cfg = KiroCrewConfig.load()
    cfg.agents[NAMED_STORE_BOT] = KiroCrewAgentConfig(
        kiro_agent="kirocrew", memory_store=NAMED_STORE
    )
    cfg.memory_stores[NAMED_STORE] = MemoryStoreConfig()
    cfg.agents[SHARED_ROW] = KiroCrewAgentConfig(kiro_agent="kirocrew", model=SHARED_ROW_MODEL)
    cfg.save()
    # The installed template catalog is the external boundary: the app's agent,
    # and a template that shares the private Bot's name.
    monkeypatch.setattr(
        "kiro_crew.config.loader._materialized_kiro_agent",
        lambda name, project_dir=None: name if name in (APP_AGENT, PRIVATE_BOT) else "",
    )
    return store


@web.middleware
async def _identity(request: web.Request, handler):
    """Token auth's stand-in: ``X-Test-App`` names an app token, else the owner."""
    request["app"] = request.headers.get("X-Test-App", "")
    request["user"] = "" if request["app"] else "local-app"
    request["is_dashboard_user"] = request["app"] == ""
    request["internal_auth"] = False
    return await handler(request)


def _client(state) -> TestClient:
    from kiro_crew.dashboard.chat import api_chat, api_chat_slot_agent, api_chat_slot_create
    from kiro_crew.dashboard.openai_compat import api_completions

    app = web.Application(middlewares=[_identity, slot_ownership_middleware])
    app["state"] = state
    app.router.add_post("/api/chat", api_chat)
    app.router.add_post("/api/chat/slots", api_chat_slot_create)
    app.router.add_post("/api/chat/slots/{slot}/agent", api_chat_slot_agent)
    app.router.add_post("/v1/chat/completions", api_completions)
    return TestClient(TestServer(app))


def _session_key(slot) -> str:
    return f"dashboard:{slot.key}"


async def _assert_nothing_of_a_bot(state, slot) -> None:
    """No provider was started, no store attached and no record written for *slot*."""
    state.sessions.get_or_create.assert_not_awaited()
    state.context_builder.ensure_store.assert_not_awaited()
    assert not slot.memory_store
    key = _session_key(slot)
    assert await asyncio.to_thread(read_session_execution, key) is None
    assert await asyncio.to_thread(read_private_session_store, key) is None


async def _send(client, route: str, bot: str):
    """One app request that names *bot* on *route*, for the app's own ``shop-1``."""
    headers = {"X-Test-App": APP}
    if route == "slot_create":
        return await client.post(
            "/api/chat/slots", json={"name": "shop-new", "agent": bot}, headers=headers
        )
    if route == "chat_send":
        return await client.post(
            "/api/chat", json={"slot": "shop-1", "message": "hi", "agent": bot}, headers=headers
        )
    if route == "chat_send_new_slot":
        return await client.post(
            "/api/chat", json={"slot": "shop-new", "message": "hi", "agent": bot}, headers=headers
        )
    if route == "slot_agent":
        return await client.post(
            "/api/chat/slots/shop-1/agent", json={"agent": bot}, headers=headers
        )
    assert route == "completions"
    return await client.post(
        "/v1/chat/completions",
        json={"id": "shop-1", "model": bot, "messages": [{"role": "user", "content": "hi"}]},
        headers=headers,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "route", ["slot_create", "chat_send", "chat_send_new_slot", "slot_agent", "completions"]
)
@pytest.mark.parametrize("bot", [PRIVATE_BOT, NAMED_STORE_BOT])
async def test_an_app_naming_a_bot_gets_the_not_found_answer_and_no_bot_turn(
    tmp_path, monkeypatch, route, bot
):
    await asyncio.to_thread(_add_bots, monkeypatch)
    state = _turn_state(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("shop-1", app=APP)
    async with _client(state) as client:
        response = await _finished(_send(client, route, bot), "the request's response")
        text = await response.text()
    await _finished(drain_background_tasks(state), "the background tasks draining")

    assert response.status == 404, text
    # The body an app gets for anything it may not reach, on each route's own shape.
    assert json.loads(text)["code"] == ("not_found" if route == "completions" else "slot_not_found")
    assert "shop-new" not in state._slots
    assert not slot.agent
    assert not slot.messages
    await _assert_nothing_of_a_bot(state, slot)


@pytest.mark.asyncio
@pytest.mark.parametrize("bot", [PRIVATE_BOT, NAMED_STORE_BOT])
async def test_a_bot_name_stamped_on_an_app_session_is_refused_at_turn_admission(
    tmp_path, monkeypatch, bot
):
    """The floor under every route: the name is on the slot, and no record exists yet."""
    await asyncio.to_thread(_add_bots, monkeypatch)
    state = _turn_state(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("shop-1", agent=bot, app=APP)

    await _finished(chat_runner._run_chat(state, slot, "hi"), "the chat turn")
    await _finished(drain_background_tasks(state), "the background tasks draining")

    await _assert_nothing_of_a_bot(state, slot)
    assert not [m for m in slot.messages if m["role"] == "assistant"]
    refusals = [m for m in slot.messages if m["role"] == "error"]
    assert [m["content"] for m in refusals] == [chat_runner.APP_BOT_TURN_REFUSAL]
    assert refusals[0]["meta"]["kind"] == APP_AGENT_REFUSED_KIND


@pytest.mark.asyncio
async def test_an_app_session_that_already_carries_a_bot_record_is_refused(tmp_path, monkeypatch):
    """A record the session already carries does not keep it a Bot's session."""
    store = await asyncio.to_thread(_add_bots, monkeypatch)
    state = _turn_state(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("shop-1", agent=PRIVATE_BOT, app=APP)
    key = _session_key(slot)
    carried = await asyncio.to_thread(
        resolve_member_execution, KiroCrewConfig.load(), PRIVATE_BOT, app=APP
    )
    await asyncio.to_thread(bind_session_execution, key, carried)
    slot.memory_store = store

    await _finished(chat_runner._run_chat(state, slot, "hi"), "the chat turn")
    await _finished(drain_background_tasks(state), "the background tasks draining")

    state.sessions.get_or_create.assert_not_awaited()
    state.context_builder.ensure_store.assert_not_awaited()
    assert [m["content"] for m in slot.messages if m["role"] == "error"] == [
        chat_runner.APP_BOT_TURN_REFUSAL
    ]
    # The record is left as it was: the refusal writes nothing.
    assert await asyncio.to_thread(read_session_execution, key) == carried


@pytest.mark.asyncio
async def test_the_owners_bot_pick_on_an_apps_session_ends_at_the_turn_and_the_log_says_why(
    tmp_path, monkeypatch, caplog
):
    """The switch route takes the owner's pick as it always did. The turn is where
    it ends, on the same refusal row, and the log names the app and the agent."""
    await asyncio.to_thread(_add_bots, monkeypatch)
    state = _turn_state(tmp_path, monkeypatch)
    state.sessions.reset = AsyncMock(return_value=True)
    slot = state.get_or_create_slot("shop-1", app=APP)
    async with _client(state) as client:
        response = await _finished(
            client.post("/api/chat/slots/shop-1/agent", json={"agent": PRIVATE_BOT}),
            "the request's response",
        )
        assert response.status == 200, await response.text()
    assert slot.agent == PRIVATE_BOT

    with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.chat_runner"):
        await _finished(chat_runner._run_chat(state, slot, "hi"), "the chat turn")
    await _finished(drain_background_tasks(state), "the background tasks draining")

    state.sessions.get_or_create.assert_not_awaited()
    refusals = [m for m in slot.messages if m["role"] == "error"]
    assert [m["content"] for m in refusals] == [chat_runner.APP_BOT_TURN_REFUSAL]
    assert refusals[0]["meta"]["kind"] == APP_AGENT_REFUSED_KIND
    reasons = [r.getMessage() for r in caplog.records if "refusing the turn" in r.getMessage()]
    assert len(reasons) == 1
    assert f"belongs to app '{APP}'" in reasons[0]
    assert f"agent '{PRIVATE_BOT}'" in reasons[0]
    assert "whoever picked the agent" in reasons[0]


@pytest.mark.asyncio
async def test_the_owner_still_opens_a_session_as_a_bot(tmp_path, monkeypatch):
    store = await asyncio.to_thread(_add_bots, monkeypatch)
    state = _turn_state(tmp_path, monkeypatch)
    async with _client(state) as client:
        response = await _finished(
            client.post("/api/chat/slots", json={"name": "mine", "agent": PRIVATE_BOT}),
            "the request's response",
        )
        assert response.status == 200, await response.text()
    slot = state._slots["mine"]
    assert slot.memory_store == store

    await _finished(chat_runner._run_chat(state, slot, "hi"), "the chat turn")
    await _finished(drain_background_tasks(state), "the background tasks draining")

    state.sessions.get_or_create.assert_awaited_once()
    assert not [m for m in slot.messages if m["role"] == "error"]
    execution = await asyncio.to_thread(read_session_execution, _session_key(slot))
    assert execution.selection_kind == "member"
    assert execution.store.store_id == store


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", [APP_AGENT, PRIVATE_BOT])
async def test_an_app_still_runs_an_agent_of_its_own(tmp_path, monkeypatch, agent):
    """A template the app picks runs, also when a Bot shares its name."""
    await asyncio.to_thread(_add_bots, monkeypatch)
    state = _turn_state(tmp_path, monkeypatch)
    async with _client(state) as client:
        response = await _finished(
            client.post(
                "/api/chat/slots",
                json={"name": "shop-1", "agent": agent, "agent_kind": "template"},
                headers={"X-Test-App": APP},
            ),
            "the request's response",
        )
        assert response.status == 200, await response.text()
    slot = state._slots["shop-1"]
    assert (slot._app, slot.agent, slot.agent_kind) == (APP, agent, "template")

    await _finished(chat_runner._run_chat(state, slot, "hi"), "the chat turn")
    await _finished(drain_background_tasks(state), "the background tasks draining")

    state.sessions.get_or_create.assert_awaited_once()
    assert not [m for m in slot.messages if m["role"] == "error"]
    execution = await asyncio.to_thread(read_session_execution, _session_key(slot))
    assert (execution.selection_kind, execution.member_id) == ("template", None)
    assert execution.store.store_id == "default"
    assert not slot.memory_store


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["slot_create", "completions"])
async def test_a_shared_store_row_still_names_an_agent_for_an_app(tmp_path, monkeypatch, route):
    """A row with nothing of its own (as the reserved ``default`` row) keeps working."""
    await asyncio.to_thread(_add_bots, monkeypatch)
    state = _turn_state(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("shop-1", app=APP)
    async with _client(state) as client:
        if route == "slot_create":
            response = await _finished(
                client.post(
                    "/api/chat/slots",
                    json={"name": "shop-new", "agent": SHARED_ROW},
                    headers={"X-Test-App": APP},
                ),
                "the request's response",
            )
            assert response.status == 200, await response.text()
            slot = state._slots["shop-new"]
            await _finished(chat_runner._run_chat(state, slot, "hi"), "the chat turn")
        else:
            response = await _finished(_send(client, route, SHARED_ROW), "the request's response")
            assert response.status == 200, await response.text()
    await _finished(drain_background_tasks(state), "the background tasks draining")

    state.sessions.get_or_create.assert_awaited_once()
    assert not [m for m in slot.messages if m["role"] == "error"]
    assert not slot.memory_store
    execution = await asyncio.to_thread(read_session_execution, _session_key(slot))
    assert (execution.member_id, execution.store.store_id) == (None, "default")
    assert execution.app == APP


def test_only_an_app_session_is_judged_and_only_on_what_is_a_bots_own():
    """The predicate turn admission and the eager start share."""

    def bindings(kind, *, store="default", agent="kirocrew", record=None):
        return SimpleNamespace(
            selection_kind=kind, memory_store_name=store, kiro_agent=agent, execution_context=record
        )

    own_store = bindings("member", store="reviewer-store")
    own_chat = bindings("member", agent=MATE_CONDUCTOR_AGENT)
    # A record that runs a template on a Bot's store: the store is still the Bot's.
    template_on_bot_store = bindings(
        "template",
        store="reviewer-store",
        record=SimpleNamespace(selection_kind="template", member_id="m-1"),
    )
    # A record whose Bot is gone from the config resolves as a template.
    removed_bot = bindings(
        "template",
        store="reviewer-store",
        record=SimpleNamespace(selection_kind="member", member_id=None),
    )
    shared_row = bindings("member")
    template = bindings("template")
    conductor_template = bindings("template", agent=MATE_CONDUCTOR_AGENT)
    app_slot = SimpleNamespace(_app=APP)
    owner_slot = SimpleNamespace(_app="")

    for refused in (own_store, own_chat, template_on_bot_store, removed_bot):
        assert chat_runner.app_session_selects_bot(app_slot, refused)
        assert not chat_runner.app_session_selects_bot(owner_slot, refused)
    for allowed in (shared_row, template, conductor_template, None):
        assert not chat_runner.app_session_selects_bot(app_slot, allowed)
