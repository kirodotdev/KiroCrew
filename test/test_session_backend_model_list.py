"""A session's model picker lists the models of the harness that session runs on.

``GET /api/models`` answers for the configured backend (``agent.acp_backend``),
and every picker read that one list. A session can run on another harness: a
crewmate DM thread routes through ``agent.member_acp_backend``, and a live
session keeps the harness it started on after the default changes. With the
default on kiro-cli and the member route on claude, a crewmate's thread ran Claude
Code while its picker offered kiro-cli's credit-priced catalog, ticked a kiro id
the thread was not running, and greyed out the Claude model it was running.

``selection-capabilities`` (the composer) and ``resolved-model`` (the crew
editor) now name the backend whose list applies when it is not the configured
one, and ``GET /api/models?backend=`` serves it.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard.chat_handlers import api_chat_slot_selection_capabilities
from kiro_crew.dashboard.handlers import agents
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.providers.acp import AcpProvider

# ── GET /api/models?backend= ──


def _models_app(monkeypatch, *, configured: str) -> web.Application:
    monkeypatch.setattr(
        agents.KiroCrewConfig,
        "load",
        staticmethod(
            lambda: SimpleNamespace(agent=SimpleNamespace(acp_backend=configured, model=""))
        ),
    )

    async def _never_spawn(*_a, **_k):  # pragma: no cover - reaching it is the failure
        raise AssertionError("a claude list must not spawn kiro-cli --list-models")

    monkeypatch.setattr(agents, "reject_if_kiro_unverified", _never_spawn)
    app = web.Application()
    app["state"] = SimpleNamespace(sessions=SimpleNamespace(active_providers=lambda: []))
    app.router.add_get("/api/models", agents.api_models)
    return app


@pytest.mark.asyncio
async def test_backend_param_serves_that_backends_list_under_a_kiro_default(monkeypatch):
    claude_rows = [{"model_name": "auto"}, {"model_name": "global.anthropic.claude-opus-5-5[1m]"}]
    monkeypatch.setattr(agents, "_cc_models", lambda _request, configured_default="": claude_rows)
    async with TestClient(TestServer(_models_app(monkeypatch, configured=""))) as client:
        resp = await client.get("/api/models", params={"backend": "claude"})
        body = await resp.json()

    assert resp.status == 200
    assert body == claude_rows


@pytest.mark.asyncio
async def test_backend_param_refuses_a_backend_this_build_cannot_select(monkeypatch):
    async with TestClient(TestServer(_models_app(monkeypatch, configured=""))) as client:
        resp = await client.get("/api/models", params={"backend": "../claude"})
        body = await resp.json()

    assert resp.status == 400
    assert body["code"] == "invalid_backend"


# ── selection-capabilities: which backend's list the composer reads ──


def _caps_app(state: DashboardState) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_get(
        "/api/chat/slots/{slot}/selection-capabilities", api_chat_slot_selection_capabilities
    )
    return app


def _state(slot: _ChatSlot, provider: object = None) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._slots = {slot.key: slot}
    state.sessions = MagicMock()
    state.sessions.reset = AsyncMock()
    state.sessions.get_provider = MagicMock(return_value=provider)
    return state


def _config(monkeypatch, *, default: str, member: str) -> None:
    monkeypatch.setattr(
        chat_handlers.KiroCrewConfig,
        "load",
        lambda: SimpleNamespace(
            agent=SimpleNamespace(acp_backend=default, member_acp_backend=member)
        ),
    )
    monkeypatch.setattr(chat_handlers, "resolve_effective_model", lambda _cfg, _agent: "")


async def _caps(state: DashboardState, slot: str) -> dict:
    async with TestClient(TestServer(_caps_app(state))) as client:
        resp = await client.get(f"/api/chat/slots/{slot}/selection-capabilities")
        assert resp.status == 200
        return await resp.json()


@pytest.mark.asyncio
async def test_a_cold_claude_member_thread_names_claudes_list(monkeypatch):
    """The reported case: crew slots are never eager-spawned, so the thread is cold."""
    _config(monkeypatch, default="", member="claude")

    data = await _caps(_state(_ChatSlot("member-helper")), "member-helper")

    assert data["models_backend"] == "claude"


@pytest.mark.asyncio
async def test_a_live_claude_session_names_claudes_list_under_a_kiro_default(monkeypatch):
    _config(monkeypatch, default="", member="kas")
    provider = MagicMock(spec=AcpProvider)
    provider.capabilities = SimpleNamespace(backend="claude")
    provider.supports_effort.return_value = False

    data = await _caps(_state(_ChatSlot("test"), provider), "test")

    assert data["models_backend"] == "claude"


@pytest.mark.asyncio
async def test_a_kas_thread_keeps_the_configured_kiro_list(monkeypatch):
    """kas and kiro-cli read one catalog: a second copy would lose the last-good cache."""
    _config(monkeypatch, default="", member="kas")

    data = await _caps(_state(_ChatSlot("member-helper")), "member-helper")

    assert "models_backend" not in data


# ── resolved-model: which backend's list the crew editor reads ──


@pytest.fixture()
def crew():
    """One stored crew, through the real config API (the conftest redirects the home)."""
    from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig

    cfg = KiroCrewConfig.load()
    cfg.agents["writer"] = KiroCrewAgentConfig(
        kiro_agent="kirocrew", workspace="default", memory_store="default"
    )
    cfg.save()
    return "writer"


async def _resolved(crew_name: str, *, default: str, member: str, state: object = None) -> dict:
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.dashboard.handlers import api_kirocrew_agent_resolved_model

    cfg = KiroCrewConfig.load()
    cfg.agent.acp_backend = default
    cfg.agent.member_acp_backend = member
    cfg.save()
    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/agents/resolved-model", api_kirocrew_agent_resolved_model)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/agents/resolved-model", params={"agent": crew_name})
        assert resp.status == 200
        return json.loads(await resp.text())


@pytest.mark.asyncio
async def test_the_crew_editor_reads_the_list_its_pin_is_judged_by(crew):
    """A crew's pin is judged by the member route's catalog (``_pin_entitlement_backend``)."""
    body = await _resolved(crew, default="", member="claude")

    assert body["models_backend"] == "claude"


@pytest.mark.asyncio
async def test_the_crew_editor_keeps_the_configured_list_on_one_namespace(crew):
    body = await _resolved(crew, default="", member="kas")

    assert "models_backend" not in body


def _claude_session(*advertised: str) -> SimpleNamespace:
    return SimpleNamespace(
        client=SimpleNamespace(backend="claude"),
        available_models=lambda: [{"modelId": m} for m in advertised],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("live", "served"),
    [
        # The pin keeps the kiro-cli spelling; the gateway sends Claude Code's own id for it.
        (
            [_claude_session("claude-opus-5-5", "global.anthropic.claude-opus-5-5[1m]")],
            "global.anthropic.claude-opus-5-5[1m]",
        ),
        ([_claude_session("claude-sonnet-5-5")], ""),
        # No session has advertised a list: nothing says the pin is not offered.
        ([], None),
    ],
    ids=["served-as-another-id", "not-offered", "unknown"],
)
async def test_the_crew_editor_learns_what_the_harness_serves_its_pin_as(crew, live, served):
    from kiro_crew.config.loader import KiroCrewConfig

    cfg = KiroCrewConfig.load()
    cfg.agents[crew].model = "claude-opus-5.5"
    cfg.save()
    state = SimpleNamespace(sessions=SimpleNamespace(active_providers=lambda: live))

    body = await _resolved(crew, default="", member="claude", state=state)

    assert body.get("pin_served_as") == served
