"""Tests for the MCP discovery dashboard handlers.

Covers the wire contract of ``/api/mcp/discover`` (+ ``/detail`` and
``/install``): the short-query availability probe, installed cross-ref,
provider-string redaction, the install write path (spec lands in the
KiroCrew scope, SEL-logged), and every documented error status.

Providers are faked end-to-end — no network, no edition capability manager.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.mcp_providers.base import (
    McpInstallPlan,
    McpSearchResult,
    McpServerDetail,
    ProviderRegistry,
    ProviderUnavailableError,
)

_WEATHER_ID = "io.github.acme/weather"

_WEATHER_DETAIL = McpServerDetail(
    id=_WEATHER_ID,
    name="weather",
    title="Weather MCP",
    description="Forecasts via ACME",
    provider="official",
    version="1.2.3",
    repo_url="https://github.com/acme/weather",
    install_plan=McpInstallPlan(
        method="npx",
        spec={
            "command": "npx",
            "args": ["-y", "@acme/weather-mcp@1.2.3"],
            "env": {"WEATHER_API_KEY": ""},
        },
    ),
    required_env=["WEATHER_API_KEY"],
)


class FakeOfficialProvider:
    """Minimal McpProvider double registered under the 'official' name."""

    def __init__(
        self, results=None, detail=_WEATHER_DETAIL, available=True, raise_unavailable=False
    ):
        self._results = results or []
        self._detail = detail
        self._available = available
        self._raise = raise_unavailable
        self.search_calls = 0
        self.detail_calls = 0

    @property
    def name(self):
        return "official"

    @property
    def display_name(self):
        return "MCP Registry"

    def is_available(self):
        return self._available

    async def search(self, query, *, limit=20):
        self.search_calls += 1
        if self._raise:
            raise ProviderUnavailableError("down")
        return self._results[:limit]

    async def fetch_detail(self, server_id):
        self.detail_calls += 1
        if self._raise:
            raise ProviderUnavailableError("down")
        return self._detail


def _search_result(**overrides) -> McpSearchResult:
    base = dict(
        id=_WEATHER_ID,
        name="weather",
        title="Weather MCP",
        description="Forecasts via ACME",
        provider="official",
        version="1.2.3",
        repo_url="https://github.com/acme/weather",
        installed=False,
        methods=["npx"],
        deprecated=False,
    )
    base.update(overrides)
    return McpSearchResult(**base)


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Pin $HOME to tmp_path so all config paths resolve into a sandbox."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def sandbox(fake_home, monkeypatch):
    """Sandbox the mcp.py module-level config paths + collaborator seams.

    The path constants are computed at import time from the real home, so
    patching Path.home alone is not enough. Also stubs list_servers (the
    installed cross-ref source) and rebuild_agent_config (post-install).
    """
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    monkeypatch.setattr(mcp_mod, "_KIROCREW_MCP_JSON", fake_home / "kirocrew.mcp.json")
    monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", fake_home / "kiro.mcp.json")
    monkeypatch.setattr(mcp_mod, "_MCP_LOCK_PATH", fake_home / "kiro.mcp.lock")

    import kiro_crew.mcp_discovery as discovery_mod

    monkeypatch.setattr(discovery_mod, "list_servers", lambda: [])

    import kiro_crew.agent as agent_mod

    rebuild = MagicMock()
    monkeypatch.setattr(agent_mod, "rebuild_agent_config", rebuild)

    return SimpleNamespace(
        home=fake_home,
        kirocrew_json=fake_home / "kirocrew.mcp.json",
        discovery_mod=discovery_mod,
        rebuild=rebuild,
        mcp_mod=mcp_mod,
    )


@pytest.fixture
def fake_sel(monkeypatch):
    """Capture SEL calls made by the discover handlers."""
    from kiro_crew.dashboard.handlers import mcp_discover as mod

    instance = MagicMock()
    monkeypatch.setattr(mod, "sel", lambda: instance)
    return instance


def _make_app(provider) -> web.Application:
    from kiro_crew.dashboard.handlers import mcp_discover as mod

    registry = ProviderRegistry()
    registry.register(provider)
    mod._registry = registry  # inject the fake registry

    state = MagicMock()
    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/mcp/discover", mod.api_mcp_discover)
    app.router.add_get("/api/mcp/discover/detail", mod.api_mcp_discover_detail)
    app.router.add_post("/api/mcp/discover/install", mod.api_mcp_discover_install)
    return app


@pytest.fixture
def reset_registry():
    """Restore the module-level registry singleton after each test."""
    from kiro_crew.dashboard.handlers import mcp_discover as mod

    old = mod._registry
    yield
    mod._registry = old


async def _client(provider) -> TestClient:
    client = TestClient(TestServer(_make_app(provider)))
    await client.start_server()
    return client


# ---------------------------------------------------------------------------
# Search + probe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestDiscoverSearch:
    async def test_short_query_probe_skips_providers(self, sandbox, fake_sel, reset_registry):
        provider = FakeOfficialProvider(results=[_search_result()])
        client = await _client(provider)
        try:
            for q in ("", "a"):
                resp = await client.get("/api/mcp/discover", params={"q": q})
                assert resp.status == 200
                data = await resp.json()
                assert data == {"results": [], "providers": ["official"]}
            assert provider.search_calls == 0
        finally:
            await client.close()

    async def test_negative_limit_clamps_to_one(self, sandbox, fake_sel, reset_registry):
        provider = FakeOfficialProvider(results=[_search_result(), _search_result(id="x/y")])
        client = await _client(provider)
        try:
            resp = await client.get("/api/mcp/discover", params={"q": "weather", "limit": "-5"})
            assert resp.status == 200
            assert len((await resp.json())["results"]) == 1
        finally:
            await client.close()

    async def test_probe_omits_unavailable_provider(self, sandbox, fake_sel, reset_registry):
        client = await _client(FakeOfficialProvider(available=False))
        try:
            resp = await client.get("/api/mcp/discover", params={"q": ""})
            assert (await resp.json())["providers"] == []
        finally:
            await client.close()

    async def test_search_returns_contract_shape(self, sandbox, fake_sel, reset_registry):
        client = await _client(FakeOfficialProvider(results=[_search_result()]))
        try:
            resp = await client.get("/api/mcp/discover", params={"q": "weather"})
            assert resp.status == 200
            data = await resp.json()
            assert data["providers"] == ["official"]
            assert data["results"] == [
                {
                    "id": _WEATHER_ID,
                    "name": "weather",
                    "title": "Weather MCP",
                    "description": "Forecasts via ACME",
                    "provider": "official",
                    "display_provider": "MCP Registry",
                    "version": "1.2.3",
                    "repo_url": "https://github.com/acme/weather",
                    "installed": False,
                    "methods": ["npx"],
                    "deprecated": False,
                }
            ]
        finally:
            await client.close()

    async def test_installed_cross_ref_matches_short_name(
        self, sandbox, fake_sel, reset_registry, monkeypatch
    ):
        # The configured server list holds the sanitized short name — the
        # discovered reverse-DNS id must still be badged installed.
        monkeypatch.setattr(
            sandbox.discovery_mod,
            "list_servers",
            lambda: [SimpleNamespace(name="weather")],
        )
        results = [_search_result(), _search_result(id="io.github.acme/other", name="other")]
        client = await _client(FakeOfficialProvider(results=results))
        try:
            resp = await client.get("/api/mcp/discover", params={"q": "acme"})
            data = await resp.json()
            by_id = {r["id"]: r for r in data["results"]}
            assert by_id[_WEATHER_ID]["installed"] is True
            assert by_id["io.github.acme/other"]["installed"] is False
        finally:
            await client.close()

    async def test_provider_strings_are_redacted(self, sandbox, fake_sel, reset_registry):
        leaked = "creds AKIAIOSFODNN7EXAMPLE leaked"
        client = await _client(FakeOfficialProvider(results=[_search_result(description=leaked)]))
        try:
            resp = await client.get("/api/mcp/discover", params={"q": "weather"})
            body = await resp.text()
            assert "AKIAIOSFODNN7EXAMPLE" not in body
        finally:
            await client.close()


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestDiscoverDetail:
    async def test_detail_contract_shape(self, sandbox, fake_sel, reset_registry):
        client = await _client(FakeOfficialProvider())
        try:
            resp = await client.get(
                "/api/mcp/discover/detail",
                params={"provider": "official", "id": _WEATHER_ID},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data == {
                "id": _WEATHER_ID,
                "name": "weather",
                "title": "Weather MCP",
                "description": "Forecasts via ACME",
                "provider": "official",
                "version": "1.2.3",
                "repo_url": "https://github.com/acme/weather",
                "install_plan": {
                    "method": "npx",
                    "spec": {
                        "command": "npx",
                        "args": ["-y", "@acme/weather-mcp@1.2.3"],
                        "env": {"WEATHER_API_KEY": ""},
                    },
                },
                "required_env": ["WEATHER_API_KEY"],
            }
        finally:
            await client.close()

    async def test_detail_null_plan_for_capability_style_detail(
        self, sandbox, fake_sel, reset_registry
    ):
        detail = McpServerDetail(id="SomeBundle", name="Some", description="", provider="official")
        client = await _client(FakeOfficialProvider(detail=detail))
        try:
            resp = await client.get(
                "/api/mcp/discover/detail",
                params={"provider": "official", "id": "SomeBundle"},
            )
            data = await resp.json()
            assert data["install_plan"] is None
            assert data["required_env"] == []
        finally:
            await client.close()

    async def test_detail_missing_params_400(self, sandbox, fake_sel, reset_registry):
        client = await _client(FakeOfficialProvider())
        try:
            resp = await client.get("/api/mcp/discover/detail", params={"provider": "official"})
            assert resp.status == 400
            resp = await client.get("/api/mcp/discover/detail", params={"id": "x"})
            assert resp.status == 400
        finally:
            await client.close()

    async def test_detail_unknown_provider_400(self, sandbox, fake_sel, reset_registry):
        client = await _client(FakeOfficialProvider())
        try:
            resp = await client.get(
                "/api/mcp/discover/detail", params={"provider": "nope", "id": "x"}
            )
            assert resp.status == 400
        finally:
            await client.close()

    async def test_detail_not_found_404(self, sandbox, fake_sel, reset_registry):
        client = await _client(FakeOfficialProvider(detail=None))
        try:
            resp = await client.get(
                "/api/mcp/discover/detail",
                params={"provider": "official", "id": "io.github.x/gone"},
            )
            assert resp.status == 404
        finally:
            await client.close()

    async def test_detail_unavailable_503(self, sandbox, fake_sel, reset_registry):
        client = await _client(FakeOfficialProvider(available=False))
        try:
            resp = await client.get(
                "/api/mcp/discover/detail",
                params={"provider": "official", "id": _WEATHER_ID},
            )
            assert resp.status == 503
        finally:
            await client.close()

    async def test_detail_upstream_failure_503(self, sandbox, fake_sel, reset_registry):
        client = await _client(FakeOfficialProvider(raise_unavailable=True))
        try:
            resp = await client.get(
                "/api/mcp/discover/detail",
                params={"provider": "official", "id": _WEATHER_ID},
            )
            assert resp.status == 503
        finally:
            await client.close()


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestDiscoverInstall:
    async def test_official_install_happy_path(self, sandbox, fake_sel, reset_registry):
        client = await _client(FakeOfficialProvider())
        try:
            resp = await client.post(
                "/api/mcp/discover/install",
                json={"provider": "official", "id": _WEATHER_ID},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data == {
                "ok": True,
                "name": "weather",
                "required_env": ["WEATHER_API_KEY"],
                "method": "npx",
                "enabled": False,
            }
            # The translated spec landed in the KiroCrew scope — DISABLED,
            # because required env is unset: enabling after configuration is
            # the user's informed-consent step (review finding).
            written = json.loads(sandbox.kirocrew_json.read_text(encoding="utf-8"))
            assert written["mcpServers"]["weather"] == {
                "command": "npx",
                "args": ["-y", "@acme/weather-mcp@1.2.3"],
                "env": {"WEATHER_API_KEY": ""},
                "disabled": True,
            }
            # Agent artifacts rebuilt so the server loads on next session.
            assert sandbox.rebuild.called
            # SEL-logged with the install operation.
            ops = [c.kwargs.get("operation") for c in fake_sel.log_api_access.call_args_list]
            assert "mcp_discover_install" in ops
        finally:
            await client.close()

    async def test_install_without_required_env_also_lands_disabled(
        self, sandbox, fake_sel, reset_registry
    ):
        """Consent default: EVERY fresh official install is written disabled,
        even when runnable as-is — enabling from the servers table is the
        explicit consent step."""
        detail = McpServerDetail(
            id=_WEATHER_ID,
            name="weather",
            description="Forecasts via ACME",
            provider="official",
            install_plan=McpInstallPlan(
                method="npx",
                spec={"command": "npx", "args": ["-y", "@acme/weather-mcp@1.2.3"]},
            ),
            required_env=[],
        )
        client = await _client(FakeOfficialProvider(detail=detail))
        try:
            resp = await client.post(
                "/api/mcp/discover/install",
                json={"provider": "official", "id": _WEATHER_ID},
            )
            assert resp.status == 200
            assert (await resp.json())["enabled"] is False
            written = json.loads(sandbox.kirocrew_json.read_text(encoding="utf-8"))
            assert written["mcpServers"]["weather"]["disabled"] is True
        finally:
            await client.close()

    async def test_reinstall_preserves_user_enabled_state(self, sandbox, fake_sel, reset_registry):
        """A user who configured env and enabled the server must not be
        flipped back to disabled by an idempotent reinstall click."""
        sandbox.kirocrew_json.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "weather": {
                            "command": "npx",
                            "args": ["-y", "@acme/weather-mcp@1.2.3"],
                            "env": {"WEATHER_API_KEY": "user-filled-value"},
                        }
                    }
                }
            )
        )
        client = await _client(FakeOfficialProvider())
        try:
            resp = await client.post(
                "/api/mcp/discover/install",
                json={"provider": "official", "id": _WEATHER_ID},
            )
            assert resp.status == 200
            assert (await resp.json())["enabled"] is True
            written = json.loads(sandbox.kirocrew_json.read_text(encoding="utf-8"))
            assert "disabled" not in written["mcpServers"]["weather"]
        finally:
            await client.close()

    async def test_reinstall_same_spec_is_idempotent(self, sandbox, fake_sel, reset_registry):
        sandbox.kirocrew_json.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "weather": {
                            "command": "npx",
                            "args": ["-y", "@acme/weather-mcp@1.2.3"],
                            "env": {"WEATHER_API_KEY": "user-filled-value"},
                        }
                    }
                }
            )
        )
        client = await _client(FakeOfficialProvider())
        try:
            resp = await client.post(
                "/api/mcp/discover/install",
                json={"provider": "official", "id": _WEATHER_ID},
            )
            assert resp.status == 200
            # User's filled env value survives the reinstall no-op.
            written = json.loads(sandbox.kirocrew_json.read_text(encoding="utf-8"))
            assert written["mcpServers"]["weather"]["env"] == {
                "WEATHER_API_KEY": "user-filled-value"
            }
        finally:
            await client.close()

    async def test_name_collision_different_spec_409(self, sandbox, fake_sel, reset_registry):
        sandbox.kirocrew_json.write_text(
            json.dumps({"mcpServers": {"weather": {"command": "uvx", "args": ["other"]}}})
        )
        client = await _client(FakeOfficialProvider())
        try:
            resp = await client.post(
                "/api/mcp/discover/install",
                json={"provider": "official", "id": _WEATHER_ID},
            )
            assert resp.status == 409
            assert (await resp.json())["error"] == "name already in use"
            # The existing spec was NOT clobbered.
            written = json.loads(sandbox.kirocrew_json.read_text(encoding="utf-8"))
            assert written["mcpServers"]["weather"]["command"] == "uvx"
        finally:
            await client.close()

    async def test_install_not_found_404(self, sandbox, fake_sel, reset_registry):
        client = await _client(FakeOfficialProvider(detail=None))
        try:
            resp = await client.post(
                "/api/mcp/discover/install",
                json={"provider": "official", "id": "io.github.x/gone"},
            )
            assert resp.status == 404
        finally:
            await client.close()

    async def test_install_no_plan_400(self, sandbox, fake_sel, reset_registry):
        detail = McpServerDetail(
            id=_WEATHER_ID, name="weather", description="", provider="official"
        )
        client = await _client(FakeOfficialProvider(detail=detail))
        try:
            resp = await client.post(
                "/api/mcp/discover/install",
                json={"provider": "official", "id": _WEATHER_ID},
            )
            assert resp.status == 400
        finally:
            await client.close()

    async def test_install_provider_unavailable_503(self, sandbox, fake_sel, reset_registry):
        client = await _client(FakeOfficialProvider(available=False))
        try:
            resp = await client.post(
                "/api/mcp/discover/install",
                json={"provider": "official", "id": _WEATHER_ID},
            )
            assert resp.status == 503
        finally:
            await client.close()

    async def test_install_upstream_failure_503(self, sandbox, fake_sel, reset_registry):
        client = await _client(FakeOfficialProvider(raise_unavailable=True))
        try:
            resp = await client.post(
                "/api/mcp/discover/install",
                json={"provider": "official", "id": _WEATHER_ID},
            )
            assert resp.status == 503
        finally:
            await client.close()

    async def test_install_bad_bodies_400(self, sandbox, fake_sel, reset_registry):
        client = await _client(FakeOfficialProvider())
        try:
            for body in ([], {"provider": 1, "id": "x"}, {"provider": "official"}, {}):
                resp = await client.post("/api/mcp/discover/install", json=body)
                assert resp.status == 400
            resp = await client.post(
                "/api/mcp/discover/install", json={"provider": "nope", "id": "x"}
            )
            assert resp.status == 400
        finally:
            await client.close()

    async def test_capability_install_delegates_to_manager(
        self, sandbox, fake_sel, reset_registry, monkeypatch
    ):
        from kiro_crew.dashboard.handlers import _shared as shared_mod
        from kiro_crew.platform.interfaces import CapabilityResult

        install_calls: list[str] = []

        class _FakeManager:
            def available(self):
                return True

            async def registry(self):
                return []

            async def install_mcp(self, server_id):
                install_calls.append(server_id)
                return CapabilityResult(ok=True, message="installed")

        monkeypatch.setattr(shared_mod, "_capability_manager", lambda: _FakeManager())
        # The handler resolves the manager through its own lazy import site too.
        from kiro_crew.dashboard.handlers import mcp_discover as mod

        synced: list[tuple] = []
        monkeypatch.setattr(
            sandbox.mcp_mod,
            "_sync_mcp_to_agent",
            lambda name, enabled, **kw: synced.append((name, enabled)),
        )

        provider = FakeOfficialProvider()
        # Register a capability provider double alongside official.
        capability_provider = FakeOfficialProvider()
        capability_provider.__class__ = type(
            "FakeCapability",
            (FakeOfficialProvider,),
            {
                "name": property(lambda self: "capability"),
                "display_name": property(lambda self: "Packages"),
            },
        )

        registry = ProviderRegistry()
        registry.register(provider)
        registry.register(capability_provider)
        mod._registry = registry

        app = web.Application()
        app["state"] = MagicMock()
        app.router.add_post("/api/mcp/discover/install", mod.api_mcp_discover_install)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post(
                "/api/mcp/discover/install",
                json={"provider": "capability", "id": "SomeBundleMCP"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data == {
                "ok": True,
                "name": "SomeBundleMCP",
                "required_env": [],
                "method": "capability",
            }
            assert install_calls == ["SomeBundleMCP"]
            assert synced == [("SomeBundleMCP", True)]
        finally:
            await client.close()

    async def test_capability_install_rejects_unsafe_id_400(
        self, sandbox, fake_sel, reset_registry
    ):
        client = await _client(FakeOfficialProvider())
        try:
            resp = await client.post(
                "/api/mcp/discover/install",
                json={"provider": "capability", "id": "../evil"},
            )
            assert resp.status == 400
        finally:
            await client.close()
