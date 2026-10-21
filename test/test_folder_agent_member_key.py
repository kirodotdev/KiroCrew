"""A folder Default agent binds what it stores: a member key, or a template name.

The folder picker stores a crewmate's ``config.agents`` key for a crewmate row
and a template's name for a template row. These pin what the create route does
with each, for the shape where a crewmate keyed ``Jira ticket`` is labelled
``jira-ticket`` and a template of that name also exists: the key binds the
crewmate with its model pin, and the template name binds the template.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from dashboard_owner_helpers import as_owner

from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig, resolve_agent_bindings
from kiro_crew.dashboard import chat_handlers

MEMBER_KEY = "Jira ticket"
SPEC = "jira-ticket"
MEMBER_MODEL = "claude-sonnet-5"
#: Templates kiro-cli declares in this test's world.
TEMPLATES = {SPEC, "kirocrew"}


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    config = KiroCrewConfig()
    config.agents = {
        "default": KiroCrewAgentConfig(kiro_agent="kirocrew"),
        MEMBER_KEY: KiroCrewAgentConfig(kiro_agent=SPEC, display_name=SPEC, model=MEMBER_MODEL),
    }
    config.default_agent = "default"

    def materialized(name, project_dir=None):
        return name if name in TEMPLATES else ""

    for target in ("kiro_crew.config.loader", "kiro_crew.config.loader_agents"):
        monkeypatch.setattr(f"{target}._materialized_kiro_agent", materialized)
    monkeypatch.setattr(chat_handlers, "KiroCrewConfig", SimpleNamespace(load=lambda: config))
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", lambda *a, **k: None)
    default_dir = tmp_path / "default-project"
    default_dir.mkdir()
    monkeypatch.setattr(chat_handlers, "default_project_dir", lambda _ws: str(default_dir))
    return config


def _state(tmp_path, default_agent: str):
    state = _make_state(tmp_path)
    state._folders = [
        {
            "id": "jira",
            "name": "Jira Tickets",
            "order": 0,
            "parent_id": "",
            "default_agent": default_agent,
        }
    ]
    return state


async def _create(state: Any, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    async with TestClient(TestServer(as_owner(app))) as client:
        resp = await client.post("/api/chat/slots", json=payload)
        return resp.status, await resp.json()


@pytest.mark.asyncio
async def test_a_member_key_default_binds_the_member_and_keeps_its_model(tmp_path, cfg):
    state = _state(tmp_path, MEMBER_KEY)
    status, data = await _create(state, {"folder_id": "jira", "agent": MEMBER_KEY})
    assert status == 200, data
    slot = state._slots[data["key"]]
    assert slot.agent == MEMBER_KEY
    bindings = resolve_agent_bindings(cfg, slot.agent, validate_memory_files=False)
    assert bindings.selection_kind == "member"
    assert bindings.model == MEMBER_MODEL


@pytest.mark.asyncio
async def test_a_member_key_on_the_default_folder_binds_the_member(tmp_path, cfg):
    cfg.dashboard.default_session_folder = "jira"
    state = _state(tmp_path, MEMBER_KEY)
    status, data = await _create(state, {"default_folder": True})
    assert status == 200, data
    assert data["folder_id"] == "jira"
    assert state._slots[data["key"]].agent == MEMBER_KEY


@pytest.mark.asyncio
async def test_a_template_name_default_binds_the_template(tmp_path, cfg):
    state = _state(tmp_path, SPEC)
    status, data = await _create(state, {"folder_id": "jira", "agent": SPEC})
    assert status == 200, data
    slot = state._slots[data["key"]]
    assert slot.agent == SPEC
    bindings = resolve_agent_bindings(cfg, slot.agent, validate_memory_files=False)
    assert bindings.selection_kind == "template"
    assert bindings.model != MEMBER_MODEL
