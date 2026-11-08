"""The Crew Mode new-chat counters a dashboard create emits.

``api_chat_slot_create`` stamps a chat born on the Crew Mode conductor at the
mint, and counts ``new_chats`` (plus ``turned_on{via=create}`` for a chat born in
Crew Mode) only once the create has committed: a refused or retracted create
counts nothing, and neither does a named create, which can reopen a saved session.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from dashboard_owner_helpers import as_owner

from kiro_crew import crew_mode
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard import chat_handlers
from kiro_crew.metrics import crew_mode as crew_mode_metrics
from kiro_crew.testing.wait import async_wait_until

CONDUCTOR = "kirocrew-conductor"


def _member_config() -> KiroCrewConfig:
    from kiro_crew.config.loader import KiroCrewAgentConfig
    from kiro_crew.memory_stores import provision_member_memory

    cfg = KiroCrewConfig()
    cfg.agents = {
        "default": KiroCrewAgentConfig(kiro_agent="kirocrew"),
        "worker": KiroCrewAgentConfig(kiro_agent="kirocrew"),
    }
    cfg.default_agent = "default"
    provision_member_memory(cfg, "worker")
    return cfg


@pytest.fixture
def counters(_floor_monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    new_chat = Mock()
    turned_on = Mock()
    _floor_monkeypatch.setattr(crew_mode_metrics, "record_new_chat", new_chat)
    _floor_monkeypatch.setattr(crew_mode_metrics, "record_turned_on", turned_on)
    _floor_monkeypatch.setattr(crew_mode, "known_roots", lambda: frozenset())
    return SimpleNamespace(new_chat=new_chat, turned_on=turned_on)


@pytest.fixture
def state(tmp_path, _floor_monkeypatch: pytest.MonkeyPatch) -> Any:
    state = _make_state(tmp_path)
    cfg = _member_config()
    _floor_monkeypatch.setattr(chat_handlers, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))
    _floor_monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", lambda *a, **k: None)

    async def pin(state_arg, session_key, agent, config, **kwargs):
        return ""

    _floor_monkeypatch.setattr(chat_handlers, "pin_private_agent_store", pin)
    return state


def _born_on_conductor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(crew_mode, "stamp_for_switch", lambda agent, kind: CONDUCTOR)


async def _post_create(state: Any, body: dict) -> int:
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    async with TestClient(TestServer(as_owner(app))) as client:
        response = await client.post("/api/chat/slots", json=body)
        status = response.status
        await async_wait_until(
            lambda: state._slots_broadcast_timer is None,
            describe=lambda: "a trailing slots push is still armed",
        )
    return status


@pytest.mark.asyncio
async def test_a_committed_new_chat_counts_once(state, counters) -> None:
    before = set(state._slots)

    assert await _post_create(state, {"agent": "worker"}) == 200

    assert len(set(state._slots) - before) == 1
    counters.new_chat.assert_called_once_with(crew_mode=False)
    counters.turned_on.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("roots, repeat", [(frozenset(), False), (frozenset({"other"}), True)])
async def test_a_chat_born_on_the_conductor_counts_as_a_crew_mode_new_chat(
    state, counters, _floor_monkeypatch, roots, repeat
) -> None:
    _born_on_conductor(_floor_monkeypatch)
    _floor_monkeypatch.setattr(crew_mode, "known_roots", lambda: roots)
    before = set(state._slots)

    assert await _post_create(state, {"agent": "worker"}) == 200

    (key,) = set(state._slots) - before
    assert state._slots[key].crew_mode_agent == CONDUCTOR
    counters.new_chat.assert_called_once_with(crew_mode=True)
    counters.turned_on.assert_called_once_with("create", repeat=repeat)


@pytest.mark.asyncio
async def test_a_same_name_member_pick_without_a_kind_is_not_a_crew_mode_new_chat(
    state, counters, _floor_monkeypatch
) -> None:
    import asyncio

    from kiro_crew.config.loader import KiroCrewAgentConfig
    from kiro_crew.memory_stores import provision_member_memory

    cfg = chat_handlers.KiroCrewConfig.load()
    cfg.agent.crew_mode_agent = CONDUCTOR
    cfg.agents[CONDUCTOR] = KiroCrewAgentConfig(kiro_agent="kirocrew")
    await asyncio.to_thread(provision_member_memory, cfg, CONDUCTOR)
    _floor_monkeypatch.setattr(crew_mode, "_live_cfg", lambda: cfg)
    before = set(state._slots)

    assert await _post_create(state, {"agent": CONDUCTOR}) == 200

    (key,) = set(state._slots) - before
    assert state._slots[key].agent_kind == "member"
    assert state._slots[key].crew_mode_agent == ""
    counters.new_chat.assert_called_once_with(crew_mode=False)
    counters.turned_on.assert_not_called()


@pytest.mark.asyncio
async def test_a_named_create_that_can_reopen_a_saved_session_counts_nothing(
    state, counters, _floor_monkeypatch
) -> None:
    _born_on_conductor(_floor_monkeypatch)

    assert await _post_create(state, {"name": "chat-saved", "agent": "worker"}) == 200

    # The stamp still lands; only the counters are skipped.
    assert state._slots["chat-saved"].crew_mode_agent == CONDUCTOR
    counters.new_chat.assert_not_called()
    counters.turned_on.assert_not_called()
