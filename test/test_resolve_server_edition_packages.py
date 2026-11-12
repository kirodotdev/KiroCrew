"""A server an agent asks for by registry id that only the edition's package registry holds.

An edition that withholds the official MCP registry lists its servers through
the capability provider (``mcp_providers.capability``), whose entries carry no
install plan: the edition's manager installs them (``install_mcp``), outside any
server entry a card records. So ``resolve_server`` builds no entry for one, and
its refusal (``edition_package``) names the card that installs it
(``mcp.install`` with provider ``capability``). An id no reachable registry
holds keeps the ``registry_unavailable`` refusal.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew.dashboard.handlers import mcp_discover
from kiro_crew.dashboard.handlers.member_capability_catalog import resolve_server
from kiro_crew.mate_capability_catalog import AdditionError
from kiro_crew.mcp_providers.base import (
    McpInstallPlan,
    McpServerDetail,
    ProviderRegistry,
    ProviderUnavailableError,
)
from kiro_crew.mcp_providers.capability import CapabilityProvider


class _EditionManager:
    """The part of an edition ``CapabilityManager`` the capability provider reads."""

    def __init__(self, ids: tuple[str, ...], *, fail: bool = False) -> None:
        self._rows = [{"id": i, "title": i, "description": "", "installed": False} for i in ids]
        self._fail = fail

    def available(self) -> bool:
        return True

    async def registry(self, query: str | None = None) -> list[dict[str, Any]]:
        if self._fail:
            raise ProviderUnavailableError("package registry unreachable")
        return list(self._rows)


class _Official:
    """An official registry that knows *known* and nothing else."""

    name = "official"
    display_name = "MCP Registry"

    def __init__(self, known: tuple[str, ...] = ()) -> None:
        self._known = known

    def is_available(self) -> bool:
        return True

    async def search(self, query: str, *, limit: int = 20) -> list[Any]:
        return []

    async def fetch_detail(self, server_id: str) -> McpServerDetail | None:
        if server_id not in self._known:
            return None
        plan = McpInstallPlan(method="uvx", spec={"command": "uvx", "args": [server_id]})
        return McpServerDetail(
            id=server_id, name=server_id, description="", provider="official", install_plan=plan
        )


def _providers(monkeypatch, *providers: Any) -> None:
    registry = ProviderRegistry()
    for provider in providers:
        registry.register(provider)
    monkeypatch.setattr(mcp_discover, "_registry", registry)


def _edition(*ids: str, fail: bool = False) -> CapabilityProvider:
    manager = _EditionManager(ids, fail=fail)
    return CapabilityProvider(lambda: manager)


async def _refusal(registry_id: str) -> AdditionError:
    with pytest.raises(AdditionError) as caught:
        await resolve_server(None, "registry", registry_id, settings_hint="ask on the MCP page")
    return caught.value


@pytest.mark.parametrize("official", [False, True], ids=["edition-only", "official-lacks-it"])
@pytest.mark.asyncio
async def test_an_edition_package_is_pointed_at_the_card_that_installs_it(monkeypatch, official):
    if official:
        _providers(monkeypatch, _Official(), _edition("example-mcp"))
    else:
        _providers(monkeypatch, _edition("example-mcp"))

    exc = await _refusal("example-mcp")

    assert exc.code == "edition_package"
    assert "mcp.install" in exc.message
    assert '"provider": "capability"' in exc.message
    assert '"id": "example-mcp"' in exc.message
    assert "source 'gateway'" in exc.message


@pytest.mark.parametrize(
    "providers",
    [
        pytest.param(lambda: [_edition("other-mcp")], id="edition-lacks-it"),
        pytest.param(lambda: [_Official(), _edition("other-mcp")], id="neither-has-it"),
        pytest.param(lambda: [_Official()], id="no-edition"),
        pytest.param(lambda: [_edition("example-mcp", fail=True)], id="edition-unreachable"),
    ],
)
@pytest.mark.asyncio
async def test_an_id_no_reachable_registry_holds_keeps_the_registry_refusal(monkeypatch, providers):
    _providers(monkeypatch, *providers())

    exc = await _refusal("example-mcp")

    assert exc.code == "registry_unavailable"
    assert "mcp.install" not in exc.message


@pytest.mark.asyncio
async def test_an_official_entry_still_resolves_to_its_install_plan(monkeypatch):
    _providers(monkeypatch, _Official(known=("example-mcp",)), _edition("example-mcp"))
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.mcp._find_server_spec_anywhere", lambda name: None
    )

    name, transport, origin = await resolve_server(
        None, "registry", "example-mcp", settings_hint="ask on the MCP page"
    )

    assert name == "example-mcp"
    assert transport == {"command": "uvx", "args": ["example-mcp"]}
    assert origin == {"source": "registry", "registry_id": "example-mcp"}
