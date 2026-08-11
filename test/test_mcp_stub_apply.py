"""The broker's existence follows the stub set, in all three directions.

Found on a real pod: stubbing the FIRST server reported ok but started nothing,
because the apply path only ever restarted an already-running broker. That state
— no manager yet, because nothing had been stubbed — is created by making the stub
opt-in, so it had no prior coverage.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.slack.gateway import GatewayOrchestrator


def _orch(stub: list[str]) -> GatewayOrchestrator:
    orch = GatewayOrchestrator.__new__(GatewayOrchestrator)
    orch._cfg = SimpleNamespace(mcp_gateway=SimpleNamespace(stub_servers=list(stub)))
    orch._mcp_gateway_manager = None
    orch.dashboard_state = SimpleNamespace(_mcp_gateway_manager=None)
    # Real interface, not a bare attribute: the apply path must refresh the
    # session defaults so the NEXT session is launched with the new routing
    # instead of the provider factory's boot-time capture.
    orch.sessions = SimpleNamespace(refresh_defaults=AsyncMock())
    return orch


@pytest.mark.asyncio
async def test_stubbing_the_first_server_starts_the_broker(monkeypatch) -> None:
    orch = _orch(["alpha-mcp"])
    manager = MagicMock()
    calls: list[str] = []

    async def _init() -> None:
        calls.append("init")
        orch._mcp_gateway_manager = manager

    async def _stop() -> None:  # pragma: no cover - must not run
        calls.append("stop")

    monkeypatch.setattr(orch, "_init_mcp_gateway", _init)
    monkeypatch.setattr(orch, "_stop_mcp_broker", _stop)
    monkeypatch.setattr(
        "kiro_crew.config.loader.KiroCrewConfig.load", staticmethod(lambda: orch._cfg)
    )

    out = await orch._apply_mcp_stub()

    assert calls == ["init"], "the first stubbed server must START a broker, not skip"
    assert out["applied"] is True
    assert out["stub_servers"] == ["alpha-mcp"]
    # The dashboard reads the manager off state; a start nobody published is invisible.
    assert orch.dashboard_state._mcp_gateway_manager is manager
    orch.sessions.refresh_defaults.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_unstubbing_the_last_server_stops_the_broker(monkeypatch) -> None:
    orch = _orch([])
    orch._mcp_gateway_manager = MagicMock()
    calls: list[str] = []

    async def _init() -> None:  # pragma: no cover - must not run
        calls.append("init")

    async def _stop() -> None:
        calls.append("stop")
        orch._mcp_gateway_manager = None

    monkeypatch.setattr(orch, "_init_mcp_gateway", _init)
    monkeypatch.setattr(orch, "_stop_mcp_broker", _stop)
    monkeypatch.setattr(
        "kiro_crew.config.loader.KiroCrewConfig.load", staticmethod(lambda: orch._cfg)
    )

    out = await orch._apply_mcp_stub()

    assert calls == ["stop"], "an empty stub set has nothing for a broker to serve"
    assert out["applied"] is True
    assert orch.dashboard_state._mcp_gateway_manager is None


@pytest.mark.asyncio
async def test_changing_the_set_restarts_so_the_rewriter_reruns(monkeypatch) -> None:
    """Restart, not no-op: the rewriter reads the stub set at broker start, so
    re-running it is what actually re-emits the stubs."""
    orch = _orch(["alpha-mcp", "beta-mcp"])
    first, second = MagicMock(name="old"), MagicMock(name="new")
    orch._mcp_gateway_manager = first
    calls: list[str] = []

    async def _init() -> None:
        calls.append("init")
        orch._mcp_gateway_manager = second

    async def _stop() -> None:
        calls.append("stop")
        orch._mcp_gateway_manager = None

    monkeypatch.setattr(orch, "_init_mcp_gateway", _init)
    monkeypatch.setattr(orch, "_stop_mcp_broker", _stop)
    monkeypatch.setattr(
        "kiro_crew.config.loader.KiroCrewConfig.load", staticmethod(lambda: orch._cfg)
    )

    out = await orch._apply_mcp_stub()

    assert calls == ["stop", "init"]
    assert orch.dashboard_state._mcp_gateway_manager is second
    assert out["stub_servers"] == ["alpha-mcp", "beta-mcp"]


@pytest.mark.asyncio
async def test_the_response_names_the_routed_set_not_the_deprecated_key(monkeypatch) -> None:
    """The dashboard reads this payload back; echoing `poolable_servers` would
    report a key the config no longer drives."""
    orch = _orch(["alpha-mcp"])

    async def _init() -> None:
        orch._mcp_gateway_manager = MagicMock()

    monkeypatch.setattr(orch, "_init_mcp_gateway", _init)
    monkeypatch.setattr(orch, "_stop_mcp_broker", AsyncMock())
    monkeypatch.setattr(
        "kiro_crew.config.loader.KiroCrewConfig.load", staticmethod(lambda: orch._cfg)
    )

    out = await orch._apply_mcp_stub()

    assert "stub_servers" in out
    assert "poolable_servers" not in out
