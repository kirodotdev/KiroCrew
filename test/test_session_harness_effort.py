"""A session's effort control offers what its harness takes, before and after it reports.

Until a session reported, the composer judged it by the displayed model's name over the
levels whichever session reported last. A crew on Claude Code showed no control before
its first turn, and a codex session showed one its build could not take. On a live codex
session a build that advertises no ``reasoning_effort`` option drops every pick
(``change_effort`` skips it), yet a GPT-5.x model still read as effort-capable.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.acp_backends import ACP_BACKEND_CODEX, ACP_BACKEND_PI
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard.chat_handlers import api_chat_slot_selection_capabilities
from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.effort import EFFORT_LEVELS
from kiro_crew.providers.acp import AcpProvider

# ── selection-capabilities before the session reports ──


def _state(slot: _ChatSlot, live: tuple = ()) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._slots = {slot.key: slot}
    state.sessions = MagicMock()
    state.sessions.reset = AsyncMock()
    state.sessions.get_provider = MagicMock(return_value=None)
    state.sessions.active_providers = MagicMock(return_value=list(live))
    return state


def _config(monkeypatch, *, member: str, resolves_to: str = "") -> None:
    monkeypatch.setattr(
        chat_handlers.KiroCrewConfig,
        "load",
        lambda: SimpleNamespace(agent=SimpleNamespace(acp_backend="", member_acp_backend=member)),
    )
    monkeypatch.setattr(chat_handlers, "resolve_effective_model", lambda _cfg, _agent: resolves_to)


async def _caps(state: DashboardState, slot: str) -> dict:
    app = web.Application()
    app["state"] = state
    app.router.add_get(
        "/api/chat/slots/{slot}/selection-capabilities", api_chat_slot_selection_capabilities
    )
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(f"/api/chat/slots/{slot}/selection-capabilities")
        assert resp.status == 200
        return await resp.json()


@pytest.mark.asyncio
async def test_a_cold_claude_thread_offers_effort_for_the_model_its_crew_runs(monkeypatch):
    """The reported case: the crew pins an effort-capable model, the slot shows ``auto``."""
    _config(monkeypatch, member="claude", resolves_to="claude-opus-5.5")

    data = await _caps(_state(_ChatSlot("member-helper")), "member-helper")

    assert data["effort_supported"] is True
    assert data["effort_levels"] == list(EFFORT_LEVELS)


@pytest.mark.asyncio
async def test_a_cold_codex_thread_reads_unknown_until_its_harness_says(monkeypatch):
    """Unknown, not unsupported: no live codex session has said what its build takes."""
    _config(monkeypatch, member="codex", resolves_to="claude-fable-5.1")

    data = await _caps(_state(_ChatSlot("member-helper")), "member-helper")

    assert data["effort_supported"] is None
    assert data["effort_levels"] == []


@pytest.mark.asyncio
async def test_a_cold_codex_thread_takes_a_live_codex_sessions_answer(monkeypatch):
    _config(monkeypatch, member="codex", resolves_to="openai.gpt-6.1-sol")
    live = MagicMock()
    live.capabilities = capabilities_for(ACP_BACKEND_CODEX)
    live.supports_effort.return_value = True
    live.get_valid_effort_levels.return_value = ["low", "medium", "high"]

    data = await _caps(_state(_ChatSlot("member-helper"), (live,)), "member-helper")

    assert data["effort_supported"] is True
    assert data["effort_levels"] == ["low", "medium", "high"]


def _codex(*, effort: bool) -> MagicMock:
    live = MagicMock()
    live.capabilities = capabilities_for(ACP_BACKEND_CODEX)
    live.supports_effort.return_value = effort
    live.get_valid_effort_levels.return_value = ["low", "high"] if effort else []
    return live


@pytest.mark.asyncio
async def test_a_cold_codex_thread_takes_its_own_crews_session_over_another(monkeypatch):
    """Another crew's codex session can run a build that takes no effort."""
    _config(monkeypatch, member="codex", resolves_to="openai.gpt-6.1-sol")
    cold, sibling = _ChatSlot("member-helper", agent="helper"), _ChatSlot("side", agent="helper")
    own = _codex(effort=True)
    state = _state(cold, (own, _codex(effort=False)))
    state._slots[sibling.key] = sibling
    state.sessions.get_provider = lambda key: own if key == effective_session_key(sibling) else None

    data = await _caps(state, "member-helper")

    assert data["effort_supported"] is True
    assert data["effort_levels"] == ["low", "high"]


@pytest.mark.asyncio
async def test_a_cold_codex_thread_takes_the_newest_codex_sessions_answer(monkeypatch):
    """An older session holds what its build said at its own session/new."""
    _config(monkeypatch, member="codex", resolves_to="openai.gpt-6.1-sol")
    state = _state(_ChatSlot("member-helper"), (_codex(effort=False), _codex(effort=True)))

    data = await _caps(state, "member-helper")

    assert data["effort_supported"] is True
    assert data["effort_levels"] == ["low", "high"]


# ── a live codex session ──


def test_a_codex_build_without_its_effort_option_takes_no_effort(tmp_path):
    provider = AcpProvider(acp_backend=ACP_BACKEND_CODEX, work_dir=tmp_path)
    provider._client._model = "openai.gpt-5.6-sol"
    provider._client._acp_config_options = [{"id": "model", "options": []}]

    assert provider.supports_effort() is False

    provider._client._acp_config_options.append(
        {"id": "reasoning_effort", "options": [{"value": "low"}, {"value": "high"}]}
    )
    assert provider.supports_effort() is True


# ── resolved-model: the crew editor's Effort field ──


async def _resolved(*, member: str, state: object = None, model: str = "") -> dict:
    from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
    from kiro_crew.dashboard.handlers import api_kirocrew_agent_resolved_model

    cfg = KiroCrewConfig.load()
    cfg.agents["writer"] = KiroCrewAgentConfig(
        kiro_agent="kirocrew", workspace="default", memory_store="default", model=model
    )
    cfg.agent.acp_backend = ""
    cfg.agent.member_acp_backend = member
    cfg.save()
    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/agents/resolved-model", api_kirocrew_agent_resolved_model)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/agents/resolved-model", params={"agent": "writer"})
        assert resp.status == 200
        return json.loads(await resp.text())


@pytest.mark.asyncio
async def test_the_crew_editor_reads_a_cold_codex_route_as_unknown_not_unsupported():
    """With no codex session live, nothing says the pin is ignored, so the editor must not."""
    body = await _resolved(member="codex")

    assert body["effort_supported"] is None


@pytest.mark.asyncio
async def test_the_crew_editor_offers_no_effort_pin_a_live_codex_build_cannot_take():
    state = SimpleNamespace(
        sessions=SimpleNamespace(active_providers=lambda: [_codex(effort=False)])
    )

    body = await _resolved(member="codex", state=state)

    assert body["effort_supported"] is False


@pytest.mark.asyncio
async def test_the_crew_editor_takes_the_crews_own_codex_session_over_another():
    own = _codex(effort=True)
    thread = _ChatSlot("member-writer", agent="writer")
    sessions = SimpleNamespace(
        active_providers=lambda: [own, _codex(effort=False)],
        get_provider=lambda key: own if key == effective_session_key(thread) else None,
    )
    state = SimpleNamespace(sessions=sessions, _slots={thread.key: thread})

    body = await _resolved(member="codex", state=state, model="openai.gpt-6.1-sol")

    assert body["effort_supported"] is True
    assert body["effort_levels"] == ["low", "high"]


@pytest.mark.asyncio
@pytest.mark.parametrize("crew", ["writer", "other"], ids=["its-own-crews", "another-crews"])
async def test_no_codex_sessions_model_answers_for_a_crews_pin(tmp_path, crew):
    """Its build says the option exists; the model it runs can be a slot pick, not the pin."""
    live = AcpProvider(acp_backend=ACP_BACKEND_CODEX, work_dir=tmp_path)
    live._client._model = "deepseek-chat"  # takes no effort, and is not the crew's pin
    live._client._acp_config_options = [
        {"id": "reasoning_effort", "options": [{"value": "low"}, {"value": "high"}]}
    ]
    assert live.supports_effort() is False
    thread = _ChatSlot("member-x", agent=crew)
    sessions = SimpleNamespace(active_providers=lambda: [live], get_provider=lambda _key: live)
    state = SimpleNamespace(sessions=sessions, _slots={thread.key: thread})

    body = await _resolved(member="codex", state=state, model="openai.gpt-6.1-sol")

    assert body["effort_supported"] is True
    assert body["effort_levels"] == ["low", "high"]


@pytest.mark.asyncio
async def test_the_crew_editor_keeps_judging_the_model_on_a_claude_route():
    """Claude's level rides the model, which the editor judges as the user edits it."""
    body = await _resolved(member="claude")

    assert "effort_supported" not in body


@pytest.mark.asyncio
async def test_the_crew_editor_offers_only_the_levels_a_crew_pin_can_save():
    """pi's own levels include off and minimal, which the crew-pin write refuses."""
    live = MagicMock()
    live.capabilities = capabilities_for(ACP_BACKEND_PI)
    live.supports_effort.return_value = True
    live.get_valid_effort_levels.return_value = ["off", "minimal", "low", "medium", "high", "xhigh"]
    state = SimpleNamespace(sessions=SimpleNamespace(active_providers=lambda: [live]))

    body = await _resolved(member="pi", state=state)

    assert body["effort_levels"] == ["low", "medium", "high", "xhigh"]
