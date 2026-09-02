"""A stub change is recorded for the next start and never applied in place.

The apply path used to cycle the broker in whichever direction the stub set
moved. That cost the request up to the daemon's whole shutdown budget, and it
bought nothing it could keep: a session's MCP toolset is fixed at
``session/new``, so no running session adopts a new stub set however the broker
is cycled. What the restart did reach was the sessions already attached to the
broker -- their in-flight tool calls are drained and cancelled, and the stub
never re-handshakes, so they lose those servers for the rest of their lives.

These tests pin the three directions as NOT touching the broker, and pin the
report that tells the operator why the switch is not live yet. Rewriting the
agent specs without restarting is not a middle ground and has no test here
because the apply path must not reach the rewriter at all: a new session would
route a server through the stub while the running daemon has no target for it,
which ``stub.py`` treats as a terminal rejection rather than a fallback.
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
    # Real interface, so a call to it would be observable: the apply path must
    # NOT refresh session defaults, because it no longer rewrites the overlay
    # those defaults capture. Refreshing would advertise routing nobody wrote.
    orch.sessions = SimpleNamespace(refresh_defaults=AsyncMock())
    return orch


def _pin_broker_calls(monkeypatch, orch: GatewayOrchestrator) -> list[str]:
    """Record any broker lifecycle call the apply path makes."""
    calls: list[str] = []

    async def _init() -> None:
        calls.append("init")

    async def _stop() -> None:
        calls.append("stop")

    monkeypatch.setattr(orch, "_init_mcp_gateway", _init)
    monkeypatch.setattr(orch, "_stop_mcp_broker", _stop)
    monkeypatch.setattr(
        "kiro_crew.config.loader.KiroCrewConfig.load", staticmethod(lambda: orch._cfg)
    )
    return calls


@pytest.mark.asyncio
async def test_stubbing_the_first_server_does_not_start_the_broker(monkeypatch) -> None:
    """No broker is running, so starting one breaks nobody -- but it would make
    this one transition behave differently from every other, and the operator
    would learn a rule ('it applies immediately') that the next toggle breaks."""
    orch = _orch(["alpha-mcp"])
    calls = _pin_broker_calls(monkeypatch, orch)

    out = await orch._apply_mcp_stub()

    assert calls == []
    assert out["applied"] is False
    assert out["restart_required"] is True
    assert out["stub_servers"] == ["alpha-mcp"]


@pytest.mark.asyncio
async def test_unstubbing_the_last_server_does_not_stop_the_broker(monkeypatch) -> None:
    """Stopping is the destructive direction: the sessions attached to the broker
    lose their tool calls to the drain and never re-handshake."""
    orch = _orch([])
    manager = MagicMock()
    orch._mcp_gateway_manager = manager
    calls = _pin_broker_calls(monkeypatch, orch)

    out = await orch._apply_mcp_stub()

    assert calls == []
    assert orch._mcp_gateway_manager is manager, "a live broker must survive the edit"
    assert out["restart_required"] is True
    assert out["stub_servers"] == []


@pytest.mark.asyncio
async def test_changing_the_set_does_not_cycle_the_broker(monkeypatch) -> None:
    orch = _orch(["alpha-mcp", "beta-mcp"])
    manager = MagicMock()
    orch._mcp_gateway_manager = manager
    calls = _pin_broker_calls(monkeypatch, orch)

    out = await orch._apply_mcp_stub()

    assert calls == []
    assert orch._mcp_gateway_manager is manager
    assert out["stub_servers"] == ["alpha-mcp", "beta-mcp"]


@pytest.mark.asyncio
async def test_the_apply_does_not_refresh_session_defaults(monkeypatch) -> None:
    """The refresh existed to publish a freshly rewritten overlay. Nothing is
    rewritten now, so refreshing would re-capture the same paths and imply to
    the next session that its routing changed when it did not."""
    orch = _orch(["alpha-mcp"])
    _pin_broker_calls(monkeypatch, orch)

    await orch._apply_mcp_stub()

    orch.sessions.refresh_defaults.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_response_names_the_routed_set_not_the_deprecated_key(monkeypatch) -> None:
    """The dashboard reads this payload back; echoing `poolable_servers` would
    report a key the config no longer drives."""
    orch = _orch(["alpha-mcp"])
    _pin_broker_calls(monkeypatch, orch)

    out = await orch._apply_mcp_stub()

    assert "stub_servers" in out
    assert "poolable_servers" not in out


@pytest.mark.asyncio
async def test_a_missing_sessions_attribute_is_not_required(monkeypatch) -> None:
    """The callback is wired before the session manager exists on some boots, and
    it no longer has any reason to reach for it."""
    orch = _orch(["alpha-mcp"])
    orch.sessions = None
    _pin_broker_calls(monkeypatch, orch)

    out = await orch._apply_mcp_stub()

    assert out["restart_required"] is True


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
