"""Tests for the multi-provider MCP server discovery system.

All provider I/O is fixture-based — outbound network to the registry is
blocked in this environment, and the tests must stay hermetic anyway.
"""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew.mcp_providers import official as official_mod
from kiro_crew.mcp_providers.base import (
    McpSearchResult,
    ProviderRegistry,
    ProviderUnavailableError,
)
from kiro_crew.mcp_providers.capability import CapabilityProvider
from kiro_crew.mcp_providers.official import (
    OfficialRegistryProvider,
    translate_install_plan,
)

# ---------------------------------------------------------------------------
# Fixture documents — official registry response shapes
# ---------------------------------------------------------------------------

# Newer v0.1 shape: each item wraps the server doc + registry _meta.
_WRAPPED_ENTRY = {
    "server": {
        "name": "io.github.acme/weather",
        "title": "Weather MCP",
        "description": "Forecasts via ACME",
        "version": "1.2.3",
        "repository": {"url": "https://github.com/acme/weather", "source": "github"},
        "packages": [
            {
                "registryType": "npm",
                "identifier": "@acme/weather-mcp",
                "version": "1.2.3",
                "environmentVariables": [
                    {"name": "WEATHER_API_KEY", "isRequired": True, "isSecret": True},
                    {"name": "WEATHER_UNITS", "isRequired": False},
                ],
            }
        ],
    },
    "_meta": {"io.modelcontextprotocol.registry/official": {"status": "active", "isLatest": True}},
}

# Flat / snake_case shape from earlier registry drafts.
_FLAT_ENTRY = {
    "name": "io.github.acme/files",
    "description": "File tools",
    "status": "active",
    "version_detail": {"version": "0.9.0"},
    "repository": {"url": "https://github.com/acme/files"},
    "packages": [
        {
            "registry_type": "pypi",
            "identifier": "acme-files-mcp",
            "environment_variables": [{"name": "FILES_TOKEN", "is_required": True}],
        }
    ],
}

_DEPRECATED_ENTRY = {
    "server": {"name": "io.github.acme/old", "description": "old", "version": "0.1.0"},
    "_meta": {"io.modelcontextprotocol.registry/official": {"status": "deprecated"}},
}

_DELETED_ENTRY = {
    "name": "io.github.acme/gone",
    "description": "tombstone",
    "status": "deleted",
}

_REMOTE_ENTRY = {
    "name": "io.github.acme/hosted",
    "description": "Hosted server",
    "version": "2.0.0",
    "remotes": [
        {"type": "sse", "url": "https://mcp.acme.dev/sse"},
        {"type": "streamable-http", "url": "https://mcp.acme.dev/mcp"},
    ],
}


def _patch_fetch(monkeypatch, data, urls: list[str] | None = None):
    """Replace the module-level HTTP fetch with a canned document."""

    async def fake(url: str):
        if urls is not None:
            urls.append(url)
        return data

    monkeypatch.setattr(official_mod, "_fetch_json", fake)


# ---------------------------------------------------------------------------
# Official provider — search parsing
# ---------------------------------------------------------------------------


class TestOfficialSearch:
    def test_name_and_display(self):
        p = OfficialRegistryProvider()
        assert p.name == "official"
        assert p.display_name == "MCP Registry"
        assert p.is_available()

    @pytest.mark.asyncio
    async def test_empty_query_returns_empty_without_fetch(self, monkeypatch):
        urls: list[str] = []
        _patch_fetch(monkeypatch, {"servers": [_FLAT_ENTRY]}, urls)
        assert await OfficialRegistryProvider().search("  ") == []
        assert urls == []

    @pytest.mark.asyncio
    async def test_parses_wrapped_and_flat_entries(self, monkeypatch):
        _patch_fetch(monkeypatch, {"servers": [_WRAPPED_ENTRY, _FLAT_ENTRY]})
        results = await OfficialRegistryProvider().search("acme")
        assert [r.id for r in results] == ["io.github.acme/weather", "io.github.acme/files"]
        weather, files = results
        assert weather.name == "weather"
        assert weather.title == "Weather MCP"
        assert weather.version == "1.2.3"
        assert weather.repo_url == "https://github.com/acme/weather"
        assert weather.methods == ["npx"]
        assert weather.deprecated is False
        # snake_case + version_detail fallback path
        assert files.name == "files"
        assert files.version == "0.9.0"
        assert files.methods == ["uvx"]

    @pytest.mark.asyncio
    async def test_search_url_shape(self, monkeypatch):
        urls: list[str] = []
        _patch_fetch(monkeypatch, {"servers": []}, urls)
        await OfficialRegistryProvider().search("file tools", limit=7)
        assert len(urls) == 1
        assert urls[0].startswith("https://registry.modelcontextprotocol.io/v0.1/servers?")
        assert "search=file+tools" in urls[0]
        assert "version=latest" in urls[0]
        assert "limit=7" in urls[0]

    @pytest.mark.asyncio
    async def test_deleted_skipped_deprecated_badged(self, monkeypatch):
        _patch_fetch(monkeypatch, {"servers": [_DELETED_ENTRY, _DEPRECATED_ENTRY, _FLAT_ENTRY]})
        results = await OfficialRegistryProvider().search("acme")
        ids = [r.id for r in results]
        assert "io.github.acme/gone" not in ids
        old = next(r for r in results if r.id == "io.github.acme/old")
        assert old.deprecated is True

    @pytest.mark.asyncio
    async def test_malformed_entries_skipped(self, monkeypatch):
        _patch_fetch(
            monkeypatch,
            {
                "servers": [
                    "not-a-dict",
                    {"description": "no name"},
                    {"server": {"name": 42}},
                    _FLAT_ENTRY,
                ]
            },
        )
        results = await OfficialRegistryProvider().search("acme")
        assert [r.id for r in results] == ["io.github.acme/files"]

    @pytest.mark.asyncio
    async def test_non_dict_response_returns_empty(self, monkeypatch):
        _patch_fetch(monkeypatch, ["totally", "wrong"])
        assert await OfficialRegistryProvider().search("acme") == []

    @pytest.mark.asyncio
    async def test_remote_entry_methods(self, monkeypatch):
        _patch_fetch(monkeypatch, {"servers": [_REMOTE_ENTRY]})
        results = await OfficialRegistryProvider().search("hosted")
        assert results[0].methods == ["url"]

    @pytest.mark.asyncio
    async def test_limit_caps_results(self, monkeypatch):
        many = [dict(_FLAT_ENTRY, name=f"io.github.acme/s{i}") for i in range(10)]
        _patch_fetch(monkeypatch, {"servers": many})
        results = await OfficialRegistryProvider().search("acme", limit=3)
        assert len(results) == 3


# ---------------------------------------------------------------------------
# Official provider — detail + spec translation
# ---------------------------------------------------------------------------


class TestOfficialDetail:
    @pytest.mark.asyncio
    async def test_detail_urlencodes_name(self, monkeypatch):
        urls: list[str] = []
        _patch_fetch(monkeypatch, _WRAPPED_ENTRY, urls)
        detail = await OfficialRegistryProvider().fetch_detail("io.github.acme/weather")
        assert detail is not None
        assert urls[0].endswith("/v0.1/servers/io.github.acme%2Fweather/versions/latest")

    @pytest.mark.asyncio
    async def test_detail_not_found(self, monkeypatch):
        _patch_fetch(monkeypatch, None)
        assert await OfficialRegistryProvider().fetch_detail("io.github.x/y") is None

    @pytest.mark.asyncio
    async def test_detail_deleted_is_none(self, monkeypatch):
        _patch_fetch(monkeypatch, _DELETED_ENTRY)
        assert await OfficialRegistryProvider().fetch_detail("io.github.acme/gone") is None

    @pytest.mark.asyncio
    async def test_detail_translates_npm_plan(self, monkeypatch):
        _patch_fetch(monkeypatch, _WRAPPED_ENTRY)
        detail = await OfficialRegistryProvider().fetch_detail("io.github.acme/weather")
        assert detail is not None
        assert detail.install_plan is not None
        assert detail.install_plan.method == "npx"
        assert detail.install_plan.spec == {
            "command": "npx",
            "args": ["-y", "@acme/weather-mcp@1.2.3"],
            "env": {"WEATHER_API_KEY": "", "WEATHER_UNITS": ""},
        }
        assert detail.required_env == ["WEATHER_API_KEY"]


class TestSpecTranslation:
    def test_npm_priority_over_pypi_oci_remotes(self):
        server = {
            "name": "io.github.acme/multi",
            "packages": [
                {"registryType": "oci", "identifier": "acme/multi"},
                {"registryType": "pypi", "identifier": "acme-multi"},
                {"registryType": "npm", "identifier": "@acme/multi", "version": "2.0.0"},
            ],
            "remotes": [{"type": "streamable-http", "url": "https://x.dev/mcp"}],
        }
        plan, required = translate_install_plan(server)
        assert plan is not None
        assert plan.method == "npx"
        assert plan.spec["args"] == ["-y", "@acme/multi@2.0.0"]
        assert required == []

    def test_pypi_bare_identifier_without_version(self):
        plan, required = translate_install_plan(
            {"packages": [{"registry_type": "pypi", "identifier": "acme-files-mcp"}]}
        )
        assert plan is not None
        assert plan.method == "uvx"
        assert plan.spec == {"command": "uvx", "args": ["acme-files-mcp"]}
        assert required == []

    def test_pypi_with_version_pins(self):
        plan, _ = translate_install_plan(
            {
                "packages": [
                    {"registryType": "pypi", "identifier": "acme-files-mcp", "version": "0.9.0"}
                ]
            }
        )
        assert plan is not None
        assert plan.spec["args"] == ["acme-files-mcp==0.9.0"]

    def test_oci_translation(self):
        plan, _ = translate_install_plan(
            {"packages": [{"registryType": "oci", "identifier": "docker.io/acme/srv"}]}
        )
        assert plan is not None
        assert plan.method == "docker"
        assert plan.spec == {
            "command": "docker",
            "args": ["run", "-i", "--rm", "docker.io/acme/srv"],
        }

    def test_remote_prefers_streamable_http_over_sse(self):
        plan, required = translate_install_plan(_REMOTE_ENTRY)
        assert plan is not None
        assert plan.method == "url"
        assert plan.spec == {"url": "https://mcp.acme.dev/mcp"}
        assert required == []

    def test_remote_falls_back_to_sse(self):
        plan, _ = translate_install_plan(
            {"remotes": [{"type": "sse", "url": "https://mcp.acme.dev/sse"}]}
        )
        assert plan is not None
        assert plan.spec == {"url": "https://mcp.acme.dev/sse"}

    def test_nothing_installable_returns_none(self):
        assert translate_install_plan({"name": "io.github.acme/x"}) == (None, [])
        assert translate_install_plan(
            {"packages": [{"registryType": "nuget", "identifier": "x"}]}
        ) == (
            None,
            [],
        )

    def test_runtime_args_precede_target_package_args_follow(self):
        """Regression (review finding): runtime args fed to npx/uvx must come
        BEFORE the package target and package args AFTER it — the v0 merge
        passed runtime flags to the package and persisted the broken spec."""
        server = {
            "packages": [
                {
                    "registryType": "npm",
                    "identifier": "@acme/srv",
                    "version": "1.0.0",
                    "runtimeArguments": [
                        {"type": "positional", "value": "run"},
                        {"type": "positional", "valueHint": "user-supplied"},
                    ],
                    "packageArguments": [
                        {"type": "named", "name": "--port", "value": "8080"},
                        {"type": "named", "name": "--token", "valueHint": "fill me"},
                    ],
                }
            ]
        }
        plan, _ = translate_install_plan(server)
        assert plan is not None
        assert plan.spec["args"] == ["-y", "run", "@acme/srv@1.0.0", "--port", "8080"]

    def test_oci_runtime_args_dropped_package_args_follow_image(self):
        """SECURITY regression: docker runtimeArguments are host-level flags
        (`--privileged`, `--volume /:/host`) under publisher control — the
        translator must never emit them. Package args (server argv) follow
        the image."""
        server = {
            "packages": [
                {
                    "registryType": "oci",
                    "identifier": "acme/srv:1",
                    "runtimeArguments": [
                        {"type": "named", "name": "--network", "value": "host"},
                        {"type": "positional", "value": "--privileged"},
                    ],
                    "packageArguments": [{"type": "positional", "value": "serve"}],
                }
            ]
        }
        plan, _ = translate_install_plan(server)
        assert plan is not None
        assert plan.spec["args"] == ["run", "-i", "--rm", "acme/srv:1", "serve"]

    def test_env_var_matrix_camel_and_snake(self):
        server = {
            "packages": [
                {
                    "registry_type": "npm",
                    "identifier": "@acme/env",
                    "environment_variables": [
                        {"name": "REQ_SNAKE", "is_required": True},
                        {"name": "OPT_SNAKE"},
                        {"name": ""},
                        "not-a-dict",
                    ],
                }
            ]
        }
        plan, required = translate_install_plan(server)
        assert plan is not None
        assert plan.spec["env"] == {"REQ_SNAKE": "", "OPT_SNAKE": ""}
        assert required == ["REQ_SNAKE"]


# ---------------------------------------------------------------------------
# Transport failures
# ---------------------------------------------------------------------------


class TestOfficialTransport:
    @pytest.mark.asyncio
    async def test_fetch_failure_propagates_provider_unavailable(self, monkeypatch):
        async def boom(url: str):
            raise ProviderUnavailableError("registry returned HTTP 503")

        monkeypatch.setattr(official_mod, "_fetch_json", boom)
        with pytest.raises(ProviderUnavailableError):
            await OfficialRegistryProvider().fetch_detail("io.github.acme/weather")


# ---------------------------------------------------------------------------
# Registry fan-out
# ---------------------------------------------------------------------------


class _StubProvider:
    def __init__(self, name: str, results=None, delay: float = 0.0, available: bool = True):
        self._name = name
        self._results = results or []
        self._delay = delay
        self._available = available

    @property
    def name(self) -> str:
        return self._name

    @property
    def display_name(self) -> str:
        return self._name.upper()

    def is_available(self) -> bool:
        return self._available

    async def search(self, query: str, *, limit: int = 20):
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._results[:limit]

    async def fetch_detail(self, server_id: str):
        return None


def _result(provider: str, ident: str) -> McpSearchResult:
    return McpSearchResult(id=ident, name=ident, description="", provider=provider)


class TestRegistryFanOut:
    @pytest.mark.asyncio
    async def test_slow_provider_dropped_fast_provider_survives(self, monkeypatch):
        from kiro_crew.mcp_providers import base as base_mod

        monkeypatch.setattr(base_mod, "_SEARCH_TIMEOUT_SECS", 0.05)
        registry = ProviderRegistry()
        registry.register(_StubProvider("slow", [_result("slow", "s1")], delay=1.0))
        registry.register(_StubProvider("fast", [_result("fast", "f1")]))
        results = await registry.search("query")
        assert [r.id for r in results] == ["f1"]

    @pytest.mark.asyncio
    async def test_unavailable_provider_skipped(self):
        registry = ProviderRegistry()
        registry.register(_StubProvider("off", [_result("off", "x")], available=False))
        registry.register(_StubProvider("on", [_result("on", "y")]))
        assert [p.name for p in registry.available_providers] == ["on"]
        results = await registry.search("query")
        assert [r.id for r in results] == ["y"]

    @pytest.mark.asyncio
    async def test_provider_filter_targets_one(self):
        registry = ProviderRegistry()
        registry.register(_StubProvider("a", [_result("a", "a1")]))
        registry.register(_StubProvider("b", [_result("b", "b1")]))
        results = await registry.search("query", provider="b")
        assert [r.id for r in results] == ["b1"]

    @pytest.mark.asyncio
    async def test_provider_exception_dropped(self):
        class _Boom(_StubProvider):
            async def search(self, query: str, *, limit: int = 20):
                raise RuntimeError("kaput")

        registry = ProviderRegistry()
        registry.register(_Boom("boom"))
        registry.register(_StubProvider("ok", [_result("ok", "k1")]))
        results = await registry.search("query")
        assert [r.id for r in results] == ["k1"]


# ---------------------------------------------------------------------------
# Capability-seam provider
# ─────────────────────────────────────────────────────────────────────────────


class _FakeManager:
    """Minimal CapabilityManager double for the seam provider."""

    def __init__(self, rows, available=True):
        self._rows = rows
        self._available = available

    def available(self):
        return self._available

    async def registry(self):
        return self._rows


_ROWS = [
    {
        "id": "InternalMCP",
        "installed": "yes",
        "title": "Internal MCP",
        "description": "An internal registry server.",
    },
    {"id": "OtherMCP", "installed": "", "title": "Other MCP", "description": "Another one."},
    {"title": "No id — skipped"},
    "not-a-dict",
]


class TestCapabilityProvider:
    def test_availability_follows_manager(self):
        assert CapabilityProvider(lambda: _FakeManager([], available=False)).is_available() is False
        assert CapabilityProvider(lambda: _FakeManager([], available=True)).is_available() is True

    def test_availability_exception_safe(self):
        def boom():
            raise RuntimeError("no context installed")

        assert CapabilityProvider(boom).is_available() is False

    @pytest.mark.asyncio
    async def test_search_filters_registry_rows(self):
        provider = CapabilityProvider(lambda: _FakeManager(_ROWS))
        results = await provider.search("internal")
        assert [r.id for r in results] == ["InternalMCP"]
        r = results[0]
        assert r.provider == "capability"
        assert r.title == "Internal MCP"
        assert r.installed is True
        assert r.methods == ["capability"]

    @pytest.mark.asyncio
    async def test_search_matches_all_and_skips_malformed(self):
        provider = CapabilityProvider(lambda: _FakeManager(_ROWS))
        results = await provider.search("mcp")
        # The idless and non-dict rows never surface.
        assert {r.id for r in results} == {"InternalMCP", "OtherMCP"}

    @pytest.mark.asyncio
    async def test_unavailable_manager_raises(self):
        provider = CapabilityProvider(lambda: _FakeManager([], available=False))
        with pytest.raises(ProviderUnavailableError):
            await provider.search("anything")

    @pytest.mark.asyncio
    async def test_detail_finds_by_id_with_null_plan(self):
        provider = CapabilityProvider(lambda: _FakeManager(_ROWS))
        detail = await provider.fetch_detail("OtherMCP")
        assert detail is not None
        assert detail.title == "Other MCP"
        assert detail.install_plan is None
        assert await provider.fetch_detail("Nope") is None

    @pytest.mark.asyncio
    async def test_non_list_registry_yields_empty(self):
        provider = CapabilityProvider(lambda: _FakeManager({"weird": True}))
        assert await provider.search("x") == []
