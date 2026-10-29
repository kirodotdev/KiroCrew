"""Telegram channel startup: ``maybe_start_telegram`` and the approval-mode resolution.

The gateway calls ``maybe_start_telegram`` once at boot. It must never take the gateway
down, and it must leave the settings badge telling the truth:

* disabled or token-less is a no-op;
* a proven token wires everything, publishes the "/" menu and reports connected;
* an unreachable Telegram still starts polling and reports a token-free reason;
* a rejected token closes the client (no leaked session) and returns ``None``;
* the client's close retires this dispatcher's spawn-approval delivery and queue drain;
* the polling status callback keeps the badge current.

The client, transport and dispatcher are fakes patched into the gateway module, and
the orchestrator is a bare namespace, so nothing touches the network.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.telegram import gateway as gw
from kiro_crew.telegram.client import TelegramAuthError


class _Client:
    instances: list["_Client"] = []
    me: Any = {"username": "crewbot", "id": 42}
    commands_error: Exception | None = None

    def __init__(self, *, token: str, on_callback: Any) -> None:
        self.token, self.on_callback = token, on_callback
        self.message_handler: Any = None
        self.on_close: Any = None
        self.on_status: Any = None
        self._last_status: Any = None
        self.commands: list[Any] = []
        self.closed = 0
        _Client.instances.append(self)

    def set_message_handler(self, handler: Any) -> None:
        self.message_handler = handler

    async def get_me(self) -> dict:
        if isinstance(self.me, BaseException):
            raise self.me
        return self.me

    async def set_my_commands(self, payload: Any) -> None:
        if self.commands_error is not None:
            raise self.commands_error
        self.commands.append(payload)

    async def close(self) -> None:
        self.closed += 1


class _Transport:
    instances: list["_Transport"] = []

    def __init__(self, client: Any, **kwargs: Any) -> None:
        self.client, self.kwargs = client, kwargs
        self.connected = 0
        _Transport.instances.append(self)

    async def receive(self, update: Any) -> None:
        return None

    async def connect(self) -> None:
        self.connected += 1


class _Dispatcher:
    instances: list["_Dispatcher"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.client: Any = None
        self.transport: Any = None
        self.bot_username = ""
        self.bot_id = 0
        _Dispatcher.instances.append(self)

    async def on_callback(self, *a: Any) -> None:
        return None

    async def handle_message(self, *a: Any) -> None:
        return None

    async def deliver_spawn_approval(self, *a: Any) -> None:
        return None

    async def _drain_queue(self, *a: Any) -> None:
        return None


class _State:
    def __init__(self) -> None:
        self.transports: list[Any] = []
        self.telegram_connected: Any = None
        self.telegram_connect_error: Any = None

    def register_channel_transport(self, transport: Any) -> None:
        self.transports.append(transport)


@pytest.fixture()
def registry(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    """Patch the fakes in and record the delivery/drain (un)registrations."""
    for cls in (_Client, _Transport, _Dispatcher):
        monkeypatch.setattr(cls, "instances", [])
    monkeypatch.setattr(_Client, "me", {"username": "crewbot", "id": 42})
    monkeypatch.setattr(_Client, "commands_error", None)
    monkeypatch.setattr(gw, "TelegramClient", _Client)
    monkeypatch.setattr(gw, "TelegramTransport", _Transport)
    monkeypatch.setattr(gw, "TelegramDispatcher", _Dispatcher)
    monkeypatch.setattr(gw, "bot_command_payload", lambda: ["menu"])
    calls: dict[str, list] = {"register": [], "unregister": [], "undrain": []}
    monkeypatch.setattr(
        gw, "register_channel_delivery", lambda ch, h: calls["register"].append((ch, h))
    )
    monkeypatch.setattr(
        gw, "unregister_channel_delivery", lambda ch, h: calls["unregister"].append((ch, h))
    )
    monkeypatch.setattr(gw, "unregister_drain", lambda ch, h: calls["undrain"].append((ch, h)))
    return calls


def _orch(**overrides: Any) -> Any:
    base: dict[str, Any] = {
        "_telegram_enabled": True,
        "_telegram_bot_token": "123:secret",
        "_telegram_allowed_user_ids": [7, 8],
        "_telegram_allow_forum": True,
        "_telegram_allowed_forum_chat_ids": [-100],
        "sessions": object(),
        "ctx_builder": object(),
        "_cfg": SimpleNamespace(agent=SimpleNamespace(approval_mode="interactive")),
        "dashboard_state": _State(),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


async def _start(orch: Any) -> Any:
    """``maybe_start_telegram`` with its result typed loosely, since tests read the fake."""
    return await gw.maybe_start_telegram(orch)


class TestNoOp:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "overrides",
        [{"_telegram_enabled": False}, {"_telegram_bot_token": ""}],
        ids=["disabled", "no-token"],
    )
    async def test_nothing_is_built(self, registry, overrides) -> None:
        assert await _start(_orch(**overrides)) is None
        assert _Client.instances == []


class TestStart:
    @pytest.mark.asyncio
    async def test_a_proven_token_wires_and_reports_connected(self, registry) -> None:
        orch = _orch()
        client = await _start(orch)
        assert client is _Client.instances[0]
        (transport,), (dispatcher,) = _Transport.instances, _Dispatcher.instances
        assert client.token == "123:secret"
        assert client.on_callback == dispatcher.on_callback
        assert client.message_handler == transport.receive
        assert transport.kwargs["allowed_user_ids"] == {7, 8}
        assert transport.kwargs["allow_forum"] is True
        assert transport.kwargs["allowed_forum_chat_ids"] == [-100]
        assert transport.kwargs["dispatch"] == dispatcher.handle_message
        assert (dispatcher.client, dispatcher.transport) == (client, transport)
        assert (dispatcher.bot_username, dispatcher.bot_id) == ("crewbot", 42)
        assert client.commands == [["menu"]]
        assert transport.connected == 1
        assert registry["register"] == [("telegram", dispatcher.deliver_spawn_approval)]
        state = orch.dashboard_state
        assert state.transports == [transport]
        assert (state.telegram_connected, state.telegram_connect_error) == (True, "")
        assert client._last_status is True

    @pytest.mark.asyncio
    async def test_closing_the_client_retires_its_delivery_and_drain(self, registry) -> None:
        client = await _start(_orch())
        (dispatcher,) = _Dispatcher.instances
        client.on_close()
        assert registry["unregister"] == [("telegram", dispatcher.deliver_spawn_approval)]
        assert registry["undrain"] == [(gw._CHANNEL, dispatcher._drain_queue)]

    @pytest.mark.asyncio
    async def test_the_badge_follows_the_polling_status(self, registry) -> None:
        orch = _orch()
        client = await _start(orch)
        client.on_status(False, "x" * 200)
        assert orch.dashboard_state.telegram_connected is False
        assert orch.dashboard_state.telegram_connect_error == "x" * 120
        client.on_status(True, "ignored")
        assert (
            orch.dashboard_state.telegram_connected,
            orch.dashboard_state.telegram_connect_error,
        ) == (
            True,
            "",
        )

    @pytest.mark.asyncio
    async def test_an_unreachable_telegram_still_polls_and_reports_why(self, registry) -> None:
        _Client.me = ConnectionError("https://api.telegram.org/bot123:secret/getMe")
        orch = _orch()
        client = await _start(orch)
        assert client is not None
        assert client.commands == []  # no menu without a proven token
        assert _Transport.instances[0].connected == 1
        state = orch.dashboard_state
        assert state.telegram_connected is False
        assert state.telegram_connect_error == "Telegram unreachable at startup (ConnectionError)"
        assert "secret" not in state.telegram_connect_error

    @pytest.mark.asyncio
    async def test_a_rejected_token_closes_the_client_and_returns_none(self, registry) -> None:
        _Client.me = TelegramAuthError("Telegram rejected the bot token")
        orch = _orch()
        assert await _start(orch) is None
        (client,) = _Client.instances
        assert client.closed == 1  # the session getMe opened is not leaked
        assert _Transport.instances[0].connected == 0
        assert orch.dashboard_state.telegram_connect_error == "Telegram rejected the bot token"

    @pytest.mark.asyncio
    async def test_any_other_failure_reports_only_the_exception_class(
        self, registry, monkeypatch
    ) -> None:
        async def _boom(self: Any) -> None:
            raise RuntimeError("https://api.telegram.org/bot123:secret/x")

        monkeypatch.setattr(_Transport, "connect", _boom)
        orch = _orch()
        assert await _start(orch) is None
        assert orch.dashboard_state.telegram_connect_error == "RuntimeError"
        assert _Client.instances[0].closed == 1

    @pytest.mark.asyncio
    async def test_a_failed_close_after_a_failed_start_is_swallowed(
        self, registry, monkeypatch
    ) -> None:
        _Client.me = TelegramAuthError("rejected")

        async def _bad_close(self: Any) -> None:
            raise OSError("already closed")

        monkeypatch.setattr(_Client, "close", _bad_close)
        assert await _start(_orch()) is None

    @pytest.mark.asyncio
    async def test_a_failed_command_menu_costs_only_autocomplete(self, registry) -> None:
        _Client.commands_error = RuntimeError("setMyCommands 500")
        orch = _orch()
        client = await _start(orch)
        assert client is not None
        assert orch.dashboard_state.telegram_connected is True

    @pytest.mark.asyncio
    async def test_no_dashboard_state_still_starts(self, registry) -> None:
        client = await _start(_orch(dashboard_state=None))
        assert client is not None
        assert client.on_status is None

    @pytest.mark.asyncio
    async def test_an_empty_allowlist_warns_that_every_message_is_rejected(
        self, registry, caplog
    ) -> None:
        caplog.set_level(logging.WARNING, logger=gw.logger.name)
        await _start(_orch(_telegram_allowed_user_ids=[]))
        assert any("REJECT all messages" in r.getMessage() for r in caplog.records)
        assert _Transport.instances[0].kwargs["allowed_user_ids"] == set()


class TestApprovalMode:
    def _cfg(self, mode: str) -> Any:
        return SimpleNamespace(agent=SimpleNamespace(approval_mode=mode))

    @pytest.mark.parametrize(
        ("override", "configured", "expected"),
        [
            ("yolo", "interactive", gw.APPROVAL_AUTO),
            ("auto", "interactive", gw.APPROVAL_AUTO),
            ("interactive", "auto", gw.APPROVAL_INTERACTIVE),  # the CLI override wins
            (None, "auto", gw.APPROVAL_AUTO),
            (None, "interactive", gw.APPROVAL_INTERACTIVE),
            (None, "anything-else", gw.APPROVAL_INTERACTIVE),  # deny-by-default
        ],
    )
    def test_resolution(self, override, configured, expected) -> None:
        orch: Any = SimpleNamespace(_approval_mode=override, _cfg=self._cfg(configured))
        assert gw._resolve_approval_mode(orch) == expected

    @pytest.mark.asyncio
    async def test_the_dispatcher_gets_the_resolved_mode(self, registry) -> None:
        await _start(_orch(_approval_mode="yolo"))
        assert _Dispatcher.instances[0].kwargs["approval_mode"] == gw.APPROVAL_AUTO
