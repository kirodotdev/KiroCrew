"""Regression tests for the MCP-gateway control-plane wiring.

These guard the exact seam a handler-only unit test missed: the dashboard
handlers read ``_mcp_gateway_manager`` / ``_mcp_gateway_apply`` /
``_mcp_gateway_apply_stub`` off ``DashboardState``, and
``GatewayOrchestrator`` must publish them there after dashboard init (the
broker starts earlier, before ``dashboard_state`` exists). If the wiring
regresses, ``/api/mcp-gateway/enable`` 503s and status always reports down.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from body_stream_helpers import attach_body

from kiro_crew.dashboard.handlers import mcp as mcp_mod
from kiro_crew.slack.gateway import GatewayOrchestrator


def _make_request(state: object, body: dict) -> web.Request:
    req = MagicMock(spec=web.Request)
    attach_body(req, body)
    req.app = {"state": state}
    req.get = lambda key, default=None: default
    return req


def test_wire_publishes_manager_and_callbacks_onto_dashboard_state() -> None:
    """The wiring must land all three attrs on the DashboardState the handlers
    read from — not leave the manager on the orchestrator."""
    ds = SimpleNamespace()
    orch = SimpleNamespace(
        dashboard_state=ds,
        _mcp_gateway_manager="MGR",
        _apply_mcp_gateway_enabled="ENABLE_CB",
        _apply_mcp_stub="POOLABLE_CB",
        _refresh_mcp_resolutions="RESOLVE_CB",
    )
    GatewayOrchestrator._wire_mcp_gateway_dashboard(orch)  # type: ignore[arg-type]
    assert ds._mcp_gateway_manager == "MGR"
    assert ds._mcp_gateway_apply == "ENABLE_CB"
    assert ds._mcp_gateway_apply_stub == "POOLABLE_CB"
    # The pre-resolve refresh is wired the same way: the handler reads it off
    # DashboardState, so leaving it on the orchestrator makes the endpoint 503.
    assert ds._mcp_resolve_refresh == "RESOLVE_CB"


def test_wire_is_noop_when_dashboard_absent() -> None:
    orch = SimpleNamespace(dashboard_state=None)
    GatewayOrchestrator._wire_mcp_gateway_dashboard(orch)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_pre_resolve_pass_runs_on_a_clock_not_only_at_boot(monkeypatch) -> None:
    """``resolve_once_refresh_hours`` needs something to tick it.

    Staleness is consulted only when a pass runs, and the launch path ignores it
    by design, so a single startup pass would freeze an unpinned ``@latest`` spec
    at whatever it resolved to on boot -- on a gateway that stays up for weeks,
    the config key would silently mean nothing.
    """
    import asyncio as _asyncio

    passes: list[dict[str, str]] = []
    slept: list[float] = []

    async def fake_pass(target_env):
        passes.append(target_env)
        return {}

    class _Stop(Exception):
        pass

    async def fake_sleep(secs):
        slept.append(secs)
        if len(slept) >= 3:
            raise _Stop
        return None

    monkeypatch.setattr(_asyncio, "sleep", fake_sleep)
    orch = SimpleNamespace(
        _cfg=SimpleNamespace(mcp_gateway=SimpleNamespace(resolve_once_refresh_hours=24)),
        _prefetch_mcp_resolutions=fake_pass,
        _MCP_RESOLVE_MIN_SLEEP_SECS=GatewayOrchestrator._MCP_RESOLVE_MIN_SLEEP_SECS,
    )
    with pytest.raises(_Stop):
        await GatewayOrchestrator._mcp_resolve_prefetch_loop(orch, {"A": "b"})  # type: ignore[arg-type]
    # More than once is the whole point; one pass is the bug being fixed.
    assert len(passes) == 3
    assert slept == [86400.0, 86400.0, 86400.0]


@pytest.mark.asyncio
async def test_a_zero_refresh_window_does_not_spin_the_pre_resolve_loop(monkeypatch) -> None:
    """``0`` legitimately means "always stale" -- but must not mean "reinstall forever"."""
    import asyncio as _asyncio

    slept: list[float] = []

    async def fake_pass(_target_env):
        return {}

    class _Stop(Exception):
        pass

    async def fake_sleep(secs):
        slept.append(secs)
        raise _Stop

    monkeypatch.setattr(_asyncio, "sleep", fake_sleep)
    orch = SimpleNamespace(
        _cfg=SimpleNamespace(mcp_gateway=SimpleNamespace(resolve_once_refresh_hours=0)),
        _prefetch_mcp_resolutions=fake_pass,
        _MCP_RESOLVE_MIN_SLEEP_SECS=GatewayOrchestrator._MCP_RESOLVE_MIN_SLEEP_SECS,
    )
    with pytest.raises(_Stop):
        await GatewayOrchestrator._mcp_resolve_prefetch_loop(orch, {})  # type: ignore[arg-type]
    assert slept == [GatewayOrchestrator._MCP_RESOLVE_MIN_SLEEP_SECS]
    assert slept[0] > 0


@pytest.mark.asyncio
async def test_enable_503_when_apply_unwired(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: with the apply callback never assigned (the shipped bug),
    the handler must 503 — this is the test that would have caught the dead
    control plane."""
    monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
    monkeypatch.setattr(mcp_mod, "is_gateway_supported", lambda: True)
    state = SimpleNamespace()  # no _mcp_gateway_apply attribute
    resp = await mcp_mod.api_mcp_gateway_enable(
        _make_request(state, {"enabled": True})
    )
    assert resp.status == 503


@pytest.mark.asyncio
async def test_enable_invokes_wired_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    """When wired, the handler persists the flag and returns the apply
    result (200, no 503)."""
    monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
    # Enabling is gated on platform support; pin it True so this wiring test is
    # deterministic on the Windows CI shard (where the broker is unsupported).
    monkeypatch.setattr(mcp_mod, "is_gateway_supported", lambda: True)
    apply_cb = AsyncMock(
        return_value={"enabled": True, "running": True, "ping_ok": True}
    )
    state = SimpleNamespace(_mcp_gateway_apply=apply_cb)
    resp = await mcp_mod.api_mcp_gateway_enable(
        _make_request(state, {"enabled": True})
    )
    assert resp.status == 200
    payload = json.loads(resp.body)
    assert payload["ok"] is True
    assert payload["running"] is True
    apply_cb.assert_awaited_once_with(True)
    from kiro_crew.config.loader import config_path

    saved = json.loads(config_path().read_text(encoding="utf-8"))
    assert saved["mcp_gateway"]["enabled"] is True


@pytest.mark.asyncio
async def test_enable_rejects_non_bool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
    state = SimpleNamespace(_mcp_gateway_apply=AsyncMock())
    resp = await mcp_mod.api_mcp_gateway_enable(
        _make_request(state, {"enabled": "yes"})
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_status_reports_supported_true_when_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The status payload mirrors the platform-support helper so the UI knows
    whether to offer the toggle. Pin the helper True so this is deterministic
    on every CI OS (incl. the Windows shard)."""
    monkeypatch.setattr(mcp_mod, "is_gateway_supported", lambda: True)
    state = SimpleNamespace()  # no manager attr -> running/ping_ok false
    resp = await mcp_mod.api_mcp_gateway_status(_make_request(state, {}))
    assert resp.status == 200
    payload = json.loads(resp.body)
    assert payload["supported"] is True
    assert payload["running"] is False
    assert payload["ping_ok"] is False


@pytest.mark.asyncio
async def test_status_reports_supported_false_when_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On an unsupported platform (Windows) the status flag flips to false so
    the UI can disable the control instead of letting it fail on apply."""
    monkeypatch.setattr(mcp_mod, "is_gateway_supported", lambda: False)
    state = SimpleNamespace()
    resp = await mcp_mod.api_mcp_gateway_status(_make_request(state, {}))
    assert resp.status == 200
    assert json.loads(resp.body)["supported"] is False


@pytest.mark.asyncio
async def test_enable_rejected_on_unsupported_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enabling where the broker can't run (Windows) must fail closed with a
    clear error and MUST NOT invoke apply or persist enabled=true."""
    monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
    monkeypatch.setattr(mcp_mod, "is_gateway_supported", lambda: False)
    apply_cb = AsyncMock()
    state = SimpleNamespace(_mcp_gateway_apply=apply_cb)
    resp = await mcp_mod.api_mcp_gateway_enable(
        _make_request(state, {"enabled": True})
    )
    assert resp.status == 400
    assert "not supported" in json.loads(resp.body)["error"].lower()
    apply_cb.assert_not_awaited()


@pytest.mark.asyncio
async def test_disable_allowed_on_unsupported_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disabling is always permitted, even on an unsupported platform, so a
    stale enabled=true from a config restore can always be turned off."""
    monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
    monkeypatch.setattr(mcp_mod, "is_gateway_supported", lambda: False)
    apply_cb = AsyncMock(
        return_value={"enabled": False, "running": False, "ping_ok": False}
    )
    state = SimpleNamespace(_mcp_gateway_apply=apply_cb)
    resp = await mcp_mod.api_mcp_gateway_enable(
        _make_request(state, {"enabled": False})
    )
    assert resp.status == 200
    apply_cb.assert_awaited_once_with(False)


@pytest.mark.parametrize(
    "platform,expected",
    [("linux", True), ("darwin", True), ("win32", True), ("cygwin", False)],
)
def test_is_gateway_supported_platform_matrix(
    monkeypatch: pytest.MonkeyPatch, platform: str, expected: bool
) -> None:
    """The single source of truth: every platform the transport layer covers.

    win32 is supported via the named-pipe transport. cygwin is not: it reports
    its own ``sys.platform`` and has neither the POSIX nor the proactor path.
    """
    import kiro_crew.mcp_gateway as gw

    monkeypatch.setattr(gw.sys, "platform", platform)
    assert gw.is_gateway_supported() is expected
