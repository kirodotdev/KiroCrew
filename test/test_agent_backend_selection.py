"""Profile defaults survive API writes and select the actual provider independently."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.agent_backend_selection import agent_backend_pin, session_backend
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.dashboard.chat_backend import chat_session_selection
from kiro_crew.dashboard.handlers import agents
from kiro_crew.dashboard.state import _ChatSlot


@pytest.fixture
def config():
    cfg = KiroCrewConfig()
    cfg.agent.acp_backend = ""
    cfg.agent.member_acp_backend = "kas"
    cfg.agents["one"] = KiroCrewAgentConfig(kiro_agent="kirocrew", acp_backend="codex")
    cfg.agents["two"] = KiroCrewAgentConfig(kiro_agent="kirocrew", acp_backend="pi")
    cfg.agents["legacy"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
    return cfg


@pytest.mark.parametrize("name,backend", [("one", "codex"), ("two", "pi"), ("legacy", "")])
def test_factory_routes_independent_members(config, tmp_path, name, backend):
    provider = config.create_provider_factory()(
        f"cron:{name}", agent="kirocrew", crew_agent=name, cwd=str(tmp_path)
    )
    assert provider.client.backend == backend
    assert config.agent.acp_backend == ""


def test_member_legacy_route_and_explicit_template_identity(config):
    assert session_backend(config, "legacy", session_key="member-legacy") == "kas"
    assert session_backend(config, "one", "", session_key="ordinary") == ""
    assert agent_backend_pin(config, "one") == "codex"


def test_older_records_inherit_without_backend_migration(config):
    from kiro_crew.config.loader import config_path

    config.save()
    path = config_path()
    document = json.loads(path.read_text(encoding="utf-8"))
    document["agents"]["legacy"].pop("acp_backend", None)
    path.write_text(json.dumps(document), encoding="utf-8")
    loaded = KiroCrewConfig.load()
    assert loaded.agents["legacy"].acp_backend is None
    assert "acp_backend" not in json.loads(path.read_text(encoding="utf-8"))["agents"]["legacy"]


@pytest.mark.parametrize("override", ["", "pi"])
def test_explicit_chat_and_spawn_override_win(config, tmp_path, override):
    slot = _ChatSlot("ordinary", agent="one")
    slot.acp_backend = override
    selected = chat_session_selection(slot, config, "auto")
    selected.pop("model")
    provider = config.create_provider_factory()(
        "dashboard:ordinary", agent="kirocrew", crew_agent="one", cwd=str(tmp_path), **selected
    )
    assert provider.client.backend == override
    assert (
        config.create_provider_factory()(
            "spawn:one", agent="one", backend_override=override, cwd=str(tmp_path)
        ).client.backend
        == override
    )
    slot.acp_backend = None
    assert chat_session_selection(slot, config, "auto")["acp_backend_override"] == "codex"


@pytest.mark.parametrize("bad", ["unknown", "vibe-unregistered", 1, False, [], {}])
def test_invalid_persisted_pin_refuses_start_but_explicit_override_can_recover(config, bad):
    config.agents["one"].acp_backend = bad
    with pytest.raises(ValueError, match="not selectable"):
        config.create_provider_factory()("cron:one", agent="one")
    assert (
        config.create_provider_factory()(
            "cron:one", agent="one", acp_backend_override="pi"
        ).client.backend
        == "pi"
    )


def test_revoked_pin_cannot_silently_fall_back(config, monkeypatch):
    monkeypatch.setattr("kiro_crew.agent_sdk.backends.selectable_backends", lambda: frozenset({""}))
    with pytest.raises(ValueError, match="not selectable"):
        session_backend(config, "one")


@pytest.fixture
def crud(config, monkeypatch):
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request", lambda r: True
    )
    config.save()
    app = web.Application()
    app.router.add_post("/api/agents", agents.api_kirocrew_agents_create)
    app.router.add_put("/api/agents/{name}", agents.api_kirocrew_agent_update)
    return app


@pytest.mark.asyncio
async def test_api_update_persists_and_clears_only_selected_member(crud):
    async with TestClient(TestServer(crud)) as client:
        assert (await client.put("/api/agents/one", json={"acp_backend": "pi"})).status == 200
        loaded = KiroCrewConfig.load()
        assert loaded.agents["one"].acp_backend == "pi"
        assert loaded.agents["two"].acp_backend == "pi"
        assert loaded.agents["legacy"].acp_backend is None
        assert (await client.put("/api/agents/one", json={"acp_backend": None})).status == 200
    assert KiroCrewConfig.load().agents["one"].acp_backend is None


@pytest.mark.asyncio
async def test_profile_accepts_real_probe_kiro_id_and_persists_explicit_kiro(crud):
    async with TestClient(TestServer(crud)) as client:
        assert (await client.put("/api/agents/one", json={"acp_backend": "kiro"})).status == 200
    cfg = KiroCrewConfig.load()
    assert cfg.agents["one"].acp_backend == ""
    assert cfg.create_provider_factory()("member-one", agent="one").client.backend == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["unknown", "vibe-unregistered", 2, True, [], {}])
@pytest.mark.parametrize("method", ["put", "post"])
async def test_api_invalid_backend_is_atomic(crud, bad, method):
    async with TestClient(TestServer(crud)) as client:
        body = {
            "name": "new-member",
            "kiro_agent": "kirocrew",
            "acp_backend": bad,
            "description": "changed",
        }
        response = await getattr(client, method)(
            "/api/agents/one" if method == "put" else "/api/agents", json=body
        )
        assert response.status == 400
        assert (await response.json())["code"] == "invalid_backend"
    cfg = KiroCrewConfig.load()
    assert cfg.agents["one"].acp_backend == "codex"
    assert cfg.agents["one"].description != "changed"
    assert "new-member" not in cfg.agents


@pytest.mark.asyncio
async def test_api_create_and_reload_routes_selected_backend(crud, tmp_path):
    async with TestClient(TestServer(crud)) as client:
        response = await client.post(
            "/api/agents",
            json={"name": "new-member", "kiro_agent": "kirocrew", "acp_backend": "codex"},
        )
        assert response.status == 200, await response.text()
    cfg = KiroCrewConfig.load()
    assert cfg.agents["new-member"].acp_backend == "codex"
    provider = cfg.create_provider_factory()(
        "member-new-member", agent="new-member", cwd=str(tmp_path)
    )
    assert provider.client.backend == "codex"


@pytest.mark.asyncio
async def test_update_refreshes_factory_without_resetting_live_sessions(crud):
    sessions = MagicMock()
    sessions.refresh_defaults = AsyncMock()
    crud["state"] = MagicMock(sessions=sessions)
    async with TestClient(TestServer(crud)) as client:
        assert (await client.put("/api/agents/one", json={"acp_backend": "pi"})).status == 200
    sessions.refresh_defaults.assert_awaited_once()
    sessions.reset.assert_not_called()


@pytest.mark.asyncio
async def test_update_remains_owner_only(crud, monkeypatch):
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request", lambda r: False
    )
    async with TestClient(TestServer(crud)) as client:
        assert (await client.put("/api/agents/one", json={"acp_backend": "pi"})).status == 403
    assert KiroCrewConfig.load().agents["one"].acp_backend == "codex"


@pytest.mark.asyncio
async def test_profile_backend_bypasses_pool_and_reaches_allocation(config, tmp_path):
    from kiro_crew.providers.acp import AcpProvider
    from kiro_crew.session import SessionManager
    from kiro_crew.session_capabilities import CapabilityPreparation

    def factory(key, **kwargs):
        provider = MagicMock(spec=AcpProvider)
        provider.client = MagicMock(backend=kwargs["acp_backend_override"])
        provider.client.resumed = False
        provider.client._session_id = "new-session"
        provider.client._pid = None
        provider.cwd = str(tmp_path)
        provider.is_process_alive.return_value = True
        provider.start = AsyncMock()
        provider.shutdown = AsyncMock()
        return provider

    manager = SessionManager(config, provider_factory=factory)
    manager._pool_size = 1
    with (
        patch.object(manager, "_drain_and_claim", new=AsyncMock()) as pool,
        patch(
            "kiro_crew.session_capabilities.prepare_runtime", return_value=CapabilityPreparation("")
        ),
    ):
        try:
            provider, is_new, _ = await manager.get_or_create(
                "cron:one", agent="kirocrew", crew_agent="one", model="auto"
            )
            assert is_new and provider.client.backend == "codex"
            pool.assert_not_awaited()
        finally:
            await manager.close_all()
