"""A stub change applies at once by restarting the broker, never the gateway.

A routed server's target is in the daemon's environment, set at spawn, and the
agent overlays are written with it, so a changed set needs a new daemon. The
apply restarts the BROKER with the configured set and refreshes session defaults
so new chats read the new overlays; the gateway keeps running. Attached stubs
for retained targets reconnect to the new daemon (``stub.py``), except subscribed
stubs. Every broker restart resets open chats when idle so they replace those
stubs. Dropping a target also resets them. An unchanged set restarts only for a
new launch approval.
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
    orch._mcp_gateway_pending = None
    orch.dashboard_state = SimpleNamespace(_mcp_gateway_manager=None)
    orch.sessions = SimpleNamespace(refresh_defaults=AsyncMock())
    return orch


def _pin_broker_calls(monkeypatch, orch: GatewayOrchestrator) -> list[tuple[str, frozenset]]:
    """Record the broker lifecycle calls the apply makes, and act like them."""
    calls: list[tuple[str, frozenset]] = []

    async def _init(stub_servers: frozenset[str] | None = None) -> None:
        calls.append(("init", frozenset(stub_servers or ())))
        orch._mcp_stub_servers_started = frozenset(stub_servers or ())
        orch._mcp_gateway_manager = MagicMock(name="new-broker")

    async def _stop() -> None:
        calls.append(("stop", frozenset()))
        orch._mcp_gateway_manager = None

    monkeypatch.setattr(orch, "_init_mcp_gateway", _init)
    monkeypatch.setattr(orch, "_stop_mcp_broker", _stop)
    monkeypatch.setattr(
        "kiro_crew.config.loader.KiroCrewConfig.load", staticmethod(lambda: orch._cfg)
    )
    return calls


@pytest.mark.asyncio
async def test_stubbing_the_first_server_starts_the_broker_live(monkeypatch) -> None:
    orch = _orch(["alpha-mcp"])
    orch._mcp_stub_servers_started = frozenset()
    calls = _pin_broker_calls(monkeypatch, orch)

    out = await orch._apply_mcp_stub()

    assert calls == [("init", frozenset({"alpha-mcp"}))]
    assert out == {
        "applied": True,
        "restart_required": False,
        "stub_servers": ["alpha-mcp"],
        "sessions_reset": 0,
    }
    assert orch.dashboard_state._mcp_gateway_manager is orch._mcp_gateway_manager
    orch.sessions.refresh_defaults.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_unstubbing_the_last_server_stops_the_broker(monkeypatch) -> None:
    orch = _orch([])
    orch._mcp_gateway_manager = MagicMock()
    orch._mcp_stub_servers_started = frozenset({"alpha-mcp"})
    calls = _pin_broker_calls(monkeypatch, orch)
    reset = AsyncMock(return_value=2)
    monkeypatch.setattr("kiro_crew.dashboard.handlers.sessions.reset_sessions", reset)

    out = await orch._apply_mcp_stub()

    assert calls == [("stop", frozenset())]
    assert orch._mcp_gateway_manager is None
    assert out["applied"] is True and out["restart_required"] is False
    assert out["sessions_reset"] == 2
    reset.assert_awaited_once_with(orch.dashboard_state)
    orch.sessions.refresh_defaults.assert_not_awaited()


@pytest.mark.asyncio
async def test_changing_the_set_restarts_the_broker_with_the_configured_set(monkeypatch) -> None:
    orch = _orch(["alpha-mcp", "beta-mcp"])
    orch._mcp_gateway_manager = MagicMock()
    orch._mcp_stub_servers_started = frozenset({"alpha-mcp"})
    calls = _pin_broker_calls(monkeypatch, orch)
    reset = AsyncMock(return_value=2)
    monkeypatch.setattr("kiro_crew.dashboard.handlers.sessions.reset_sessions", reset)

    out = await orch._apply_mcp_stub()

    reset.assert_awaited_once_with(orch.dashboard_state)
    assert out["sessions_reset"] == 2
    assert calls == [("stop", frozenset()), ("init", frozenset({"alpha-mcp", "beta-mcp"}))]
    assert out["stub_servers"] == ["alpha-mcp", "beta-mcp"]


@pytest.mark.asyncio
async def test_a_served_set_restarts_so_a_new_launch_reaches_the_daemon(monkeypatch) -> None:
    """The press may have approved a new launch for a name already routed."""
    orch = _orch(["alpha-mcp"])
    orch._mcp_gateway_manager = MagicMock()
    orch._mcp_stub_servers_started = frozenset({"alpha-mcp"})
    calls = _pin_broker_calls(monkeypatch, orch)
    reset = AsyncMock(return_value=2)
    monkeypatch.setattr("kiro_crew.dashboard.handlers.sessions.reset_sessions", reset)

    out = await orch._apply_mcp_stub(launch_approved=True)

    reset.assert_awaited_once_with(orch.dashboard_state)
    assert out["sessions_reset"] == 2
    assert [c[0] for c in calls] == ["stop", "init"]


@pytest.mark.asyncio
async def test_nothing_routed_before_or_after_restarts_nothing(monkeypatch) -> None:
    orch = _orch([])
    orch._mcp_stub_servers_started = frozenset()
    calls = _pin_broker_calls(monkeypatch, orch)

    out = await orch._apply_mcp_stub()

    assert calls == []
    assert out["applied"] is True
    orch.sessions.refresh_defaults.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_broker_that_fails_to_start_reports_a_restart_needed(monkeypatch) -> None:
    orch = _orch(["alpha-mcp"])
    orch._mcp_stub_servers_started = frozenset()
    _pin_broker_calls(monkeypatch, orch)

    async def _init_fails(stub_servers: frozenset[str] | None = None) -> None:
        return None  # downgraded: no manager published

    monkeypatch.setattr(orch, "_init_mcp_gateway", _init_fails)

    out = await orch._apply_mcp_stub()

    assert out["applied"] is False and out["restart_required"] is True


@pytest.mark.asyncio
async def test_the_response_names_the_routed_set_not_the_deprecated_key(monkeypatch) -> None:
    orch = _orch(["alpha-mcp"])
    _pin_broker_calls(monkeypatch, orch)

    out = await orch._apply_mcp_stub()

    assert "stub_servers" in out
    assert "poolable_servers" not in out


@pytest.mark.asyncio
async def test_a_missing_sessions_attribute_is_not_required(monkeypatch) -> None:
    """The callback is wired before the session manager exists on some boots."""
    orch = _orch(["alpha-mcp"])
    orch.sessions = None
    _pin_broker_calls(monkeypatch, orch)

    out = await orch._apply_mcp_stub()

    assert out["applied"] is True


@pytest.mark.asyncio
async def test_a_sharing_toggle_does_not_republish_a_pending_unstub(monkeypatch) -> None:
    """The factory's overlay decision is all-or-nothing on the CONFIGURED list.

    ``config.loader`` passes ``mcp_gateway_overlay`` only when ``stub_servers`` is
    non-empty and otherwise passes None, dropping the gateway out of the path. So
    refreshing session defaults right after the last server was unstubbed hands
    new sessions no overlay at all: they bypass the broker that is still serving
    that stub, and the change the operator was told is pending has taken effect.
    """
    orch = _orch([])  # last server unstubbed -- recorded, not applied
    orch._mcp_gateway_manager = MagicMock()
    orch._mcp_stub_servers_started = frozenset({"alpha-mcp"})  # still served

    async def _init(stub_servers: frozenset[str] | None = None) -> None:
        pass

    # Mirrors the real stop, which clears the handle -- without that the manager
    # left behind is a bare MagicMock and the path's `await mgr.ping()` explodes
    # on a non-awaitable, which is a fixture artefact rather than a finding.
    async def _stop() -> None:
        orch._mcp_gateway_manager = None

    monkeypatch.setattr(orch, "_init_mcp_gateway", _init)
    monkeypatch.setattr(orch, "_stop_mcp_broker", _stop)
    monkeypatch.setattr(
        "kiro_crew.config.loader.KiroCrewConfig.load", staticmethod(lambda: orch._cfg)
    )

    await orch._apply_mcp_gateway_enabled(True)

    orch.sessions.refresh_defaults.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_sharing_toggle_still_refreshes_when_config_and_served_agree(monkeypatch) -> None:
    """The guard must suppress only the disagreeing case: with nothing pending the
    refresh is what lets a new session pick up the rebuilt factory."""
    orch = _orch(["alpha-mcp"])
    orch._mcp_gateway_manager = MagicMock()
    orch._mcp_stub_servers_started = frozenset({"alpha-mcp"})

    async def _init(stub_servers: frozenset[str] | None = None) -> None:
        pass

    async def _stop() -> None:
        orch._mcp_gateway_manager = None

    monkeypatch.setattr(orch, "_init_mcp_gateway", _init)
    monkeypatch.setattr(orch, "_stop_mcp_broker", _stop)
    monkeypatch.setattr(
        "kiro_crew.config.loader.KiroCrewConfig.load", staticmethod(lambda: orch._cfg)
    )

    await orch._apply_mcp_gateway_enabled(True)

    orch.sessions.refresh_defaults.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_a_sharing_toggle_does_not_apply_the_pending_stub_set(monkeypatch) -> None:
    """The sharing switch restarts the broker for its own reasons -- the rewriter
    reads the sharing flag at start -- and that restart must re-emit the set the
    broker is ALREADY serving.

    Consuming the configured set here would apply a stub change the operator was
    told is waiting for a restart, and it would do so on the broker cycle that
    cancels the in-flight tool calls of every attached session. The switch that
    was touched is not the one whose change lands, which is the worst version of
    this: the page says the stub change is pending, and it silently is not.
    """
    orch = _orch(["alpha-mcp", "beta-mcp"])  # config carries a PENDING addition
    orch._mcp_gateway_manager = MagicMock()
    orch._mcp_stub_servers_started = frozenset({"alpha-mcp"})  # what is actually served
    started_with: list[frozenset[str]] = []

    async def _init(stub_servers: frozenset[str] | None = None) -> None:
        assert stub_servers is not None, "the sharing restart must name its set"
        started_with.append(stub_servers)

    async def _stop() -> None:
        orch._mcp_gateway_manager = None

    monkeypatch.setattr(orch, "_init_mcp_gateway", _init)
    monkeypatch.setattr(orch, "_stop_mcp_broker", _stop)
    monkeypatch.setattr(
        "kiro_crew.config.loader.KiroCrewConfig.load", staticmethod(lambda: orch._cfg)
    )

    await orch._apply_mcp_gateway_enabled(True)

    assert started_with == [frozenset({"alpha-mcp"})], (
        "the sharing restart re-emitted the configured set, applying a stub "
        "change that was reported as pending"
    )


@pytest.mark.asyncio
async def test_a_sharing_toggle_starts_nothing_when_no_stub_is_served(monkeypatch) -> None:
    """A first stub is recorded, not applied, so there is no broker to share
    between yet. Starting one here would apply that pending stub early."""
    orch = _orch(["alpha-mcp"])
    orch._mcp_gateway_manager = None
    orch._mcp_stub_servers_started = frozenset()
    calls: list[str] = []

    async def _init(stub_servers: frozenset[str] | None = None) -> None:
        calls.append("init")

    monkeypatch.setattr(orch, "_init_mcp_gateway", _init)
    monkeypatch.setattr(orch, "_stop_mcp_broker", AsyncMock())
    monkeypatch.setattr(
        "kiro_crew.config.loader.KiroCrewConfig.load", staticmethod(lambda: orch._cfg)
    )

    await orch._apply_mcp_gateway_enabled(True)

    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "remaining,launch_approved",
    [
        (["beta-mcp"], False),
        ([], False),
        (["alpha-mcp", "beta-mcp", "gamma-mcp"], False),
        (["alpha-mcp", "beta-mcp"], True),
    ],
    ids=["drop", "drop-last", "add", "approve-launch"],
)
async def test_broker_restart_resets_live_sessions_when_idle(
    monkeypatch, remaining, launch_approved
):
    import asyncio

    orch = _orch(remaining)
    orch._mcp_gateway_manager = MagicMock()
    orch._mcp_stub_servers_started = frozenset({"alpha-mcp", "beta-mcp"})
    calls = _pin_broker_calls(monkeypatch, orch)
    provider = SimpleNamespace(shutdown=AsyncMock())
    orch.sessions.count = 2
    orch.sessions.reload_provider_factory = AsyncMock()
    orch.sessions.drain_all_providers = AsyncMock(return_value=[provider])
    orch.sessions.drain_warm_pool = AsyncMock(return_value=[])
    orch.sessions.start_pool = AsyncMock()
    state = orch.dashboard_state
    state.sessions = orch.sessions
    state._background_tasks = set()
    state.broadcast_ws = MagicMock()
    state.push_refresh = MagicMock()
    state.push_slots_update = MagicMock()

    async def reload_factory(**kwargs):
        assert calls == [("stop", frozenset())] + (
            [("init", frozenset(remaining))] if remaining else []
        )
        assert state._mcp_gateway_manager is orch._mcp_gateway_manager

    orch.sessions.reload_provider_factory.side_effect = reload_factory
    out = await orch._apply_mcp_stub(launch_approved=launch_approved)
    if state._background_tasks:
        await asyncio.gather(*list(state._background_tasks))

    orch.sessions.reload_provider_factory.assert_awaited_once_with(defer_busy=True)
    orch.sessions.drain_all_providers.assert_awaited_once_with(keep_busy=True)
    orch.sessions.drain_warm_pool.assert_awaited_once_with()
    provider.shutdown.assert_awaited_once_with()
    assert out["sessions_reset"] == 2
    assert out["applied"] is True
    orch.sessions.refresh_defaults.assert_not_awaited()


@pytest.mark.asyncio
async def test_reapplying_an_unchanged_served_set_does_not_cycle_the_broker(monkeypatch):
    orch = _orch(["alpha-mcp"])
    manager = MagicMock()
    orch._mcp_gateway_manager = manager
    orch._mcp_stub_servers_started = frozenset({"alpha-mcp"})
    calls = _pin_broker_calls(monkeypatch, orch)

    out = await orch._apply_mcp_stub()

    assert calls == []
    assert orch._mcp_gateway_manager is manager
    assert out["applied"] is True
    orch.sessions.refresh_defaults.assert_not_awaited()
