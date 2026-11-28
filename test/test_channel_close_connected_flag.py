"""A restartable channel whose client was closed reads as not connected.

``GatewayOrchestrator._close_channel_locked`` closes a channel's live client for every
restart and every disable. The ``<channel>_connected`` flag that client's start published
on the dashboard state describes that client, and most clients never report their own
shutdown. So the close itself has to clear the flag: ``GET /api/<channel>/config`` and
``DashboardState.channel_status`` (System > Services) read it, and would keep answering
"connected" for a channel that is off.

Faked: the client handle (only ``close()`` is asked of it) and the readiness badge, which
reads the credential store. Real: the orchestrator, every channel's hoist and descriptor,
the restart lock and generation, the config applier, and ``DashboardState.channel_status``.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest
from _hot_reload_helpers import cfg_with as _cfg_with
from _hot_reload_helpers import change as _change

from kiro_crew.channels import builtin_channel_descriptors
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.messaging import registry
from kiro_crew.slack import gateway as gw
from kiro_crew.slack.gateway import GatewayOrchestrator

_BOOTABLE = tuple(d.channel_type for d in registry.bootable(builtin_channel_descriptors()))


class _Client:
    """A live handle: ``close()`` is all the lifecycle asks of it."""

    def __init__(self, *, fail: bool = False) -> None:
        self.closed = False
        self._fail = fail

    async def close(self) -> None:
        self.closed = True
        if self._fail:
            raise RuntimeError("close failed")


@pytest.fixture(autouse=True)
def _no_readiness_badge(_floor_monkeypatch) -> None:
    """The badge reads the credential store; nothing here is about it."""
    _floor_monkeypatch.setattr(
        GatewayOrchestrator, "_badge_unready_channels", lambda self, descs: None
    )


def _running(
    channels: tuple[str, ...], *, fail_close: bool = False
) -> tuple[GatewayOrchestrator, Any, dict[str, _Client]]:
    """An orchestrator whose dashboard state reports every one of *channels* connected."""
    cfg = KiroCrewConfig()
    with patch.object(cfg, "load_credentials", return_value={"KIROCREW_OWNER_ID": "U_OWNER"}):
        orch = GatewayOrchestrator(cfg, no_dashboard=True, no_crons=True, no_open=True)
    orch._channel_transports_started = True
    # ``__init__`` needs a session manager and a cron service; the status read and the
    # lifecycle only touch these attributes.
    state = object.__new__(DashboardState)
    state.slack_client = None
    state.slack_socket_connected = False
    state.channel_transports = {}
    clients: dict[str, _Client] = {}
    for channel in channels:
        setattr(state, f"{channel}_connected", True)
        setattr(state, f"{channel}_connect_error", "")
        clients[channel] = _Client(fail=fail_close)
    orch.dashboard_state = state
    orch._channel_handles.update(clients)
    return orch, state, clients


@pytest.mark.parametrize("channel", _BOOTABLE)
def test_a_restart_that_ends_with_the_channel_off_reads_as_not_connected(channel):
    other = next(c for c in _BOOTABLE if c != channel)
    orch, state, clients = _running((channel, other))
    cfg = _cfg_with(channel, enabled=False)
    with patch.object(cfg, "load_credentials", return_value={}):
        assert asyncio.run(orch.restart_channel(channel, cfg=cfg)) is None

    assert clients[channel].closed is True
    assert getattr(state, f"{channel}_connected") is False
    assert state.channel_status()[channel]["connected"] is False
    # Only the restarted channel: its neighbour is still up.
    assert getattr(state, f"{other}_connected") is True
    assert state.channel_status()[other]["connected"] is True


@pytest.mark.parametrize("channel", _BOOTABLE)
def test_the_inline_close_of_a_boot_key_change_clears_the_flag_before_the_reconnect(channel):
    """A save handler answers once the applier returns; the reconnect runs after that."""
    orch, state, clients = _running((channel,))
    release = asyncio.Event()

    async def hanging_restart(channel_type, *, cfg=None):
        await release.wait()

    orch.restart_channel = hanging_restart  # type: ignore[method-assign]

    async def run():
        cfg = _cfg_with(channel, enabled=False)
        await orch._on_channel_config_change(_change(cfg, f"{channel}.enabled"))
        assert clients[channel].closed is True
        assert getattr(state, f"{channel}_connected") is False
        assert state.channel_status()[channel]["connected"] is False
        release.set()
        await orch.channel_restarts_settled()

    asyncio.run(run())


def test_a_close_that_raises_still_leaves_the_channel_not_connected():
    orch, state, clients = _running(("telegram",), fail_close=True)
    cfg = _cfg_with("telegram", enabled=False)
    with patch.object(cfg, "load_credentials", return_value={}):
        assert asyncio.run(orch.restart_channel("telegram", cfg=cfg)) is None

    assert clients["telegram"].closed is True
    assert state.telegram_connected is False


def test_a_restart_that_reconnects_reports_what_the_new_start_publishes(monkeypatch):
    """The reset lands before the factory runs, so the new start's own value stands."""
    seen_by_the_factory: list[bool] = []

    async def start(orch):
        seen_by_the_factory.append(orch.dashboard_state.whatsapp_connected)
        orch.dashboard_state.whatsapp_connected = True
        return _Client()

    desc = registry.ChannelDescriptor(
        channel_type="whatsapp", start=start, boot_keys=frozenset({"enabled"})
    )
    monkeypatch.setattr(gw, "builtin_channel_descriptors", lambda: (desc,))
    monkeypatch.setattr(gw, "_channel_transport_permitted", lambda member: True)
    orch, state, clients = _running(("whatsapp",))
    cfg = _cfg_with("whatsapp", enabled=True)
    with patch.object(cfg, "load_credentials", return_value={}):
        new = asyncio.run(orch.restart_channel("whatsapp", cfg=cfg))

    assert clients["whatsapp"].closed is True
    assert new is not None and new is not clients["whatsapp"]
    assert seen_by_the_factory == [False]
    assert state.whatsapp_connected is True
    assert state.channel_status()["whatsapp"]["connected"] is True
