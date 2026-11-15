"""``POST /api/chat/slots`` checks the ``agent`` type before it makes a slot.

A non-string agent was stored on the new slot as sent. A list or object then
raised while the create serialised its reply (a 500 after the slot was made, and
every later slot listing failed the same way); a number or ``true`` cannot form
the execution context every turn runs under, so it failed the create with a
misleading 503 or was saved on a chat that could not start. Such an agent is now
400 ``invalid_field_type`` and no slot is made.
A string and every falsy value (missing, ``null``, ``""``) keep their meaning.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from dashboard_owner_helpers import as_owner

from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.dashboard import chat_handlers


def _state(tmp_path, monkeypatch) -> Any:
    cfg = KiroCrewConfig()
    cfg.agents = {"default": KiroCrewAgentConfig(kiro_agent="kirocrew")}
    cfg.default_agent = "default"
    state = _make_state(tmp_path)
    monkeypatch.setattr(chat_handlers, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", lambda *args, **kwargs: None)
    return state


async def _post(state: Any, body: dict) -> tuple[int, dict]:
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    async with TestClient(TestServer(as_owner(app))) as client:
        response = await client.post("/api/chat/slots", json=body)
        if response.content_type != "application/json":
            return response.status, {"text": await response.text()}
        return response.status, await response.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", [123, True, ["kirocrew"], {"a": 1}], ids=repr)
async def test_a_non_string_agent_is_refused_before_any_slot_is_made(
    tmp_path, monkeypatch, agent
) -> None:
    state = _state(tmp_path, monkeypatch)
    status, body = await _post(state, {"name": "agent-chat", "agent": agent})
    assert status == 400, body
    assert body["code"] == "invalid_field_type"
    assert "agent-chat" not in state._slots


@pytest.mark.parametrize("agent", [123, True], ids=repr)
def test_a_non_string_agent_cannot_form_the_execution_context_a_chat_runs_under(agent) -> None:
    """Why a number or ``true`` is refused rather than saved: every turn runs
    under an ``ExecutionContext`` whose selection name is the slot's agent, and
    that refuses a non-string, so such a chat could never start. (On main the
    create itself failed later with a 503 "memory store unavailable" for the same
    reason, or saved the value where no store was resolved.)"""
    from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef
    from kiro_crew.memory_stores import UnknownMemoryStore

    with pytest.raises(UnknownMemoryStore, match="invalid execution attribution"):
        ExecutionContext(
            member_id=None,
            store=MemoryStoreRef("default"),
            selection_kind="template",
            template_id="kirocrew",
            selection_name=agent,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", [None, "", 0], ids=repr)
async def test_a_falsy_agent_still_means_the_default(tmp_path, monkeypatch, agent) -> None:
    state = _state(tmp_path, monkeypatch)
    status, body = await _post(state, {"name": "default-chat", "agent": agent})
    assert status == 200, body
    assert isinstance(state._slots["default-chat"].agent, str)
