"""Weixin channel startup: the badge, failure and policy paths of ``maybe_start_weixin``.

``test_weixin.py`` pins that the transport is wired into the dispatcher before
``connect()``. This file pins the rest of the boot path:

* disabled is a no-op;
* the iLink base URL and the data home fall back to their defaults;
* the dashboard says connected after ``connect()``, then follows the transport's
  state callback, so a poll loop that dies later flips the badge off;
* a failed start reports not-connected with the reason and returns ``None``;
* the empty-allowlist warning fires only under ``dm_policy=allowlist``;
* the approval-mode resolution.

The client, transport, dispatcher and stores are fakes patched into the gateway
module, and the orchestrator is a bare namespace, so nothing touches the network.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.weixin import gateway as gw


class _Client:
    instances: list["_Client"] = []

    def __init__(self, *, token: str, base_url: str, account_id: str) -> None:
        self.token, self.base_url, self.account_id = token, base_url, account_id
        _Client.instances.append(self)


class _Transport:
    instances: list["_Transport"] = []
    connect_error: Exception | None = None

    def __init__(self, client: Any, **kwargs: Any) -> None:
        self.client, self.kwargs = client, kwargs
        self.on_state_change: Any = None
        self.connected = 0
        _Transport.instances.append(self)

    async def connect(self) -> None:
        if self.connect_error is not None:
            raise self.connect_error
        self.connected += 1


class _Dispatcher:
    instances: list["_Dispatcher"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.client: Any = None
        self.transport: Any = None
        _Dispatcher.instances.append(self)

    async def handle_message(self, *a: Any) -> None:
        return None


class _Store:
    homes: list[str] = []

    def __init__(self, home: str) -> None:
        _Store.homes.append(home)


class _State:
    def __init__(self) -> None:
        self.transports: list[Any] = []
        self.weixin_connected: Any = None
        self.weixin_connect_error: Any = None

    def register_channel_transport(self, transport: Any) -> None:
        self.transports.append(transport)


@pytest.fixture(autouse=True)
def _fakes(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    for cls in (_Client, _Transport, _Dispatcher):
        _floor_monkeypatch.setattr(cls, "instances", [])
    _floor_monkeypatch.setattr(_Store, "homes", [])
    _floor_monkeypatch.setattr(_Transport, "connect_error", None)
    _floor_monkeypatch.setattr(gw, "WeixinClient", _Client)
    _floor_monkeypatch.setattr(gw, "WeixinTransport", _Transport)
    _floor_monkeypatch.setattr(gw, "WeixinDispatcher", _Dispatcher)
    _floor_monkeypatch.setattr(gw, "ContextTokenStore", _Store)
    _floor_monkeypatch.setattr(gw, "TypingTicketCache", lambda: "typing-cache")
    _floor_monkeypatch.setattr(gw, "data_home", lambda: "/data-home")


def _orch(**overrides: Any) -> Any:
    base: dict[str, Any] = {
        "_weixin_enabled": True,
        "_weixin_token": "tok",
        "_weixin_account_id": "acct1",
        "_weixin_allowed_user_ids": ["u1"],
        "sessions": object(),
        "ctx_builder": object(),
        "_cfg": SimpleNamespace(agent=SimpleNamespace(approval_mode="interactive")),
        "dashboard_state": _State(),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


async def _start(orch: Any) -> Any:
    """``maybe_start_weixin`` with its result typed loosely, since tests read the fake."""
    return await gw.maybe_start_weixin(orch)


class TestStart:
    @pytest.mark.asyncio
    async def test_disabled_builds_nothing(self) -> None:
        assert await _start(_orch(_weixin_enabled=False)) is None
        assert _Client.instances == []

    @pytest.mark.asyncio
    async def test_defaults_for_the_base_url_and_data_home(self) -> None:
        client = await _start(_orch())
        assert client.base_url == gw.ILINK_BASE_URL
        assert (client.token, client.account_id) == ("tok", "acct1")
        assert _Store.homes == ["/data-home"]
        transport = _Transport.instances[0]
        assert transport.kwargs["dm_policy"] == "allowlist"
        assert transport.kwargs["allowed_user_ids"] == {"u1"}
        assert _Dispatcher.instances[0].kwargs["typing_cache"] == "typing-cache"

    @pytest.mark.asyncio
    async def test_configured_base_url_and_home_are_used(self) -> None:
        client = await _start(_orch(_weixin_base_url="https://ilink.example", _weixin_home="/h"))
        assert client.base_url == "https://ilink.example"
        assert _Store.homes == ["/h"]


class TestBadge:
    @pytest.mark.asyncio
    async def test_connected_after_connect_and_the_transport_is_registered(self) -> None:
        orch = _orch()
        await _start(orch)
        (transport,) = _Transport.instances
        assert transport.connected == 1
        state = orch.dashboard_state
        assert state.transports == [transport]
        assert (state.weixin_connected, state.weixin_connect_error) == (True, "")

    @pytest.mark.asyncio
    async def test_the_badge_follows_the_transport_state(self) -> None:
        orch = _orch()
        await _start(orch)
        transport = _Transport.instances[0]
        transport.on_state_change(False, "x" * 200)
        assert orch.dashboard_state.weixin_connected is False
        assert orch.dashboard_state.weixin_connect_error == "x" * 120
        transport.on_state_change(True, "")
        assert (
            orch.dashboard_state.weixin_connected,
            orch.dashboard_state.weixin_connect_error,
        ) == (
            True,
            "",
        )

    @pytest.mark.asyncio
    async def test_no_dashboard_state_still_starts(self) -> None:
        client = await _start(_orch(dashboard_state=None))
        assert client is not None
        assert _Transport.instances[0].on_state_change is None


class TestFailure:
    @pytest.mark.asyncio
    async def test_a_failed_start_reports_not_connected_and_returns_none(
        self, monkeypatch, caplog
    ) -> None:
        monkeypatch.setattr(_Transport, "connect_error", RuntimeError("y" * 200))
        caplog.set_level(logging.ERROR, logger=gw.logger.name)
        orch = _orch()
        assert await _start(orch) is None
        state = orch.dashboard_state
        assert (state.weixin_connected, state.weixin_connect_error) == (False, "y" * 120)
        assert any("Failed to start Weixin" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_a_failed_start_without_a_dashboard_returns_none(self, monkeypatch) -> None:
        monkeypatch.setattr(_Transport, "connect_error", OSError("down"))
        assert await _start(_orch(dashboard_state=None)) is None


class TestAllowlistWarning:
    @pytest.mark.asyncio
    async def test_an_empty_allowlist_under_allowlist_policy_warns(self, caplog) -> None:
        caplog.set_level(logging.WARNING, logger=gw.logger.name)
        await _start(_orch(_weixin_allowed_user_ids=[]))
        assert any("REJECT all DMs" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_an_open_policy_does_not_warn(self, caplog) -> None:
        caplog.set_level(logging.WARNING, logger=gw.logger.name)
        await _start(_orch(_weixin_allowed_user_ids=[], _weixin_dm_policy="open"))
        assert not any("REJECT all DMs" in r.getMessage() for r in caplog.records)
        assert _Transport.instances[0].kwargs["dm_policy"] == "open"


class TestApprovalMode:
    @pytest.mark.parametrize(
        ("override", "configured", "expected"),
        [
            ("yolo", "interactive", gw.APPROVAL_AUTO),
            ("auto", "interactive", gw.APPROVAL_AUTO),
            ("interactive", "auto", gw.APPROVAL_INTERACTIVE),  # the CLI override wins
            (None, "auto", gw.APPROVAL_AUTO),
            (None, "anything-else", gw.APPROVAL_INTERACTIVE),  # deny-by-default
        ],
    )
    def test_resolution(self, override, configured, expected) -> None:
        orch: Any = SimpleNamespace(
            _approval_mode=override,
            _cfg=SimpleNamespace(agent=SimpleNamespace(approval_mode=configured)),
        )
        assert gw._resolve_approval_mode(orch) == expected

    @pytest.mark.asyncio
    async def test_the_dispatcher_gets_the_resolved_mode(self) -> None:
        await _start(_orch(_approval_mode="yolo"))
        assert _Dispatcher.instances[0].kwargs["approval_mode"] == gw.APPROVAL_AUTO
