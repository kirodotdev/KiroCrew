"""Discord channel startup: ``maybe_start_discord``, the slash-menu task, the approval mode.

The gateway calls ``maybe_start_discord`` once at boot. It must never take the gateway
down, must not block boot on the slash-command registration, and must leave the
settings badge telling the truth:

* disabled or token-less is a no-op;
* the client, transport and dispatcher are wired with the allowlists passed through;
* the dashboard says Connected only once the Gateway reaches READY;
* the badge follows the client's state callback;
* any failure reports only the exception class and returns ``None``;
* the client's close retires this dispatcher's spawn-approval delivery and queue drain;
* the "/" menu is published in a background task, only after READY.

The client, transport and dispatcher are fakes patched into the gateway module, and
the orchestrator is a bare namespace, so nothing touches the network.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.discord import gateway as gw
from kiro_crew.testing.wait import await_until


class _Client:
    instances: list["_Client"] = []
    ready: bool = True
    register_error: Exception | None = None
    fatal_error: str = ""

    def __init__(self, *, token: str, on_interaction: Any, enable_guild_threads: bool) -> None:
        self.token, self.on_interaction = token, on_interaction
        self.enable_guild_threads = enable_guild_threads
        self.message_handler: Any = None
        self.on_close: Any = None
        self.on_state_change: Any = None
        self.ready_waits: list[float] = []
        self.registered: list[Any] = []
        _Client.instances.append(self)

    def set_message_handler(self, handler: Any) -> None:
        self.message_handler = handler

    async def wait_ready(self, *, timeout: float) -> bool:
        self.ready_waits.append(timeout)
        return self.ready

    async def register_application_commands(self, payload: Any) -> bool:
        if self.register_error is not None:
            raise self.register_error
        self.registered.append(payload)
        return True


class _Transport:
    instances: list["_Transport"] = []
    connect_error: Exception | None = None

    def __init__(self, client: Any, **kwargs: Any) -> None:
        self.client, self.kwargs = client, kwargs
        self.connected = 0
        _Transport.instances.append(self)

    async def receive(self, event: Any) -> None:
        return None

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
        self.handled: list[Any] = []
        self._session_resume = SimpleNamespace(dashboard_state=None)
        _Dispatcher.instances.append(self)

    async def on_interaction(self, *a: Any) -> None:
        return None

    async def handle_message(self, message: Any) -> None:
        self.handled.append(message)

    def register_allowed_thread(self, thread_id: str) -> None:
        return None

    async def deliver_spawn_approval(self, *a: Any) -> None:
        return None

    async def _drain_queue(self, *a: Any) -> None:
        return None


class _State:
    def __init__(self) -> None:
        self.transports: list[Any] = []
        self.discord_connected: Any = None
        self.discord_connect_error: Any = None

    def register_channel_transport(self, transport: Any) -> None:
        self.transports.append(transport)


@pytest.fixture()
def registry(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    """Patch the fakes in and record the delivery/drain (un)registrations."""
    for cls in (_Client, _Transport, _Dispatcher):
        monkeypatch.setattr(cls, "instances", [])
    monkeypatch.setattr(_Client, "ready", True)
    monkeypatch.setattr(_Client, "register_error", None)
    monkeypatch.setattr(_Transport, "connect_error", None)
    monkeypatch.setattr(gw, "DiscordClient", _Client)
    monkeypatch.setattr(gw, "DiscordTransport", _Transport)
    monkeypatch.setattr(gw, "DiscordDispatcher", _Dispatcher)
    monkeypatch.setattr(gw, "application_command_payload", lambda: ["menu"])
    calls: dict[str, list] = {"register": [], "unregister": [], "undrain": []}

    def _register(ch: str, hook: Any) -> None:
        calls["register"].append((ch, hook))

    def _unregister(ch: str, hook: Any) -> None:
        calls["unregister"].append((ch, hook))

    def _undrain(ch: str, hook: Any) -> None:
        calls["undrain"].append((ch, hook))

    monkeypatch.setattr(gw, "register_channel_delivery", _register)
    monkeypatch.setattr(gw, "unregister_channel_delivery", _unregister)
    monkeypatch.setattr(gw, "unregister_drain", _undrain)
    return calls


def _orch(**overrides: Any) -> Any:
    base: dict[str, Any] = {
        "_discord_enabled": True,
        "_discord_bot_token": "bot-secret",
        "_discord_allowed_user_ids": ["u1", "u2"],
        "_discord_allowed_thread_ids": [],
        "_discord_allowed_channel_ids": [],
        "sessions": object(),
        "ctx_builder": object(),
        "_cfg": SimpleNamespace(agent=SimpleNamespace(approval_mode="interactive")),
        "dashboard_state": _State(),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


async def _start(orch: Any) -> Any:
    """``maybe_start_discord`` with its result typed loosely, since tests read the fake."""
    return await gw.maybe_start_discord(orch)


async def _finish_registrations() -> None:
    """Await every background slash-menu task the start scheduled."""
    for task in list(gw._PENDING_REGISTRATIONS):
        await await_until(task, what="the slash-menu registration")


class TestNoOp:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "overrides",
        [{"_discord_enabled": False}, {"_discord_bot_token": ""}],
        ids=["disabled", "no-token"],
    )
    async def test_nothing_is_built(self, registry, overrides) -> None:
        assert await _start(_orch(**overrides)) is None
        assert _Client.instances == []


class TestWiring:
    @pytest.mark.asyncio
    async def test_the_parts_are_wired_with_the_allowlists(self, registry) -> None:
        orch = _orch(_discord_allowed_thread_ids=["t1"], _discord_auto_thread=False)
        client = await _start(orch)
        await _finish_registrations()
        (transport,), (dispatcher,) = _Transport.instances, _Dispatcher.instances
        assert client is _Client.instances[0]
        assert client.token == "bot-secret"
        assert client.on_interaction == dispatcher.on_interaction
        assert client.enable_guild_threads is True  # a thread allowlist needs guild threads
        assert client.message_handler == transport.receive
        assert dispatcher.kwargs["allowed_user_ids"] == {"u1", "u2"}
        assert dispatcher.kwargs["allowed_thread_ids"] == {"t1"}
        assert transport.kwargs["allowed_channel_ids"] == set()
        assert transport.kwargs["auto_thread"] is False
        assert transport.kwargs["on_thread_created"] == dispatcher.register_allowed_thread
        assert (dispatcher.client, dispatcher.transport) == (client, transport)
        assert transport.connected == 1
        assert registry["register"] == [("discord", dispatcher.deliver_spawn_approval)]

    @pytest.mark.asyncio
    async def test_guild_threads_follow_a_channel_allowlist_too(self, registry) -> None:
        client = await _start(_orch(_discord_allowed_channel_ids=["c1"]))
        await _finish_registrations()
        assert client.enable_guild_threads is True

    @pytest.mark.asyncio
    async def test_dm_only_needs_no_guild_threads_and_auto_threads_by_default(
        self, registry
    ) -> None:
        client = await _start(_orch())
        await _finish_registrations()
        assert client.enable_guild_threads is False
        assert _Transport.instances[0].kwargs["auto_thread"] is True

    @pytest.mark.asyncio
    async def test_inbound_messages_reach_the_dispatcher(self, registry) -> None:
        await _start(_orch())
        await _finish_registrations()
        message = object()
        await _Transport.instances[0].kwargs["dispatch"](message)
        assert _Dispatcher.instances[0].handled == [message]

    @pytest.mark.asyncio
    async def test_closing_the_client_retires_its_delivery_and_drain(self, registry) -> None:
        client = await _start(_orch())
        await _finish_registrations()
        (dispatcher,) = _Dispatcher.instances
        client.on_close()
        assert registry["unregister"] == [("discord", dispatcher.deliver_spawn_approval)]
        assert registry["undrain"] == [(gw._CHANNEL, dispatcher._drain_queue)]


class TestBadge:
    @pytest.mark.asyncio
    async def test_connected_only_once_the_gateway_is_ready(self, registry) -> None:
        orch = _orch()
        client = await _start(orch)
        await _finish_registrations()
        state = orch.dashboard_state
        assert state.transports == [_Transport.instances[0]]
        assert _Dispatcher.instances[0]._session_resume.dashboard_state is state
        assert client.ready_waits[0] == 15.0
        assert (state.discord_connected, state.discord_connect_error) == (True, "")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("fatal", "shown"),
        [
            ("Authentication failed (4004)", "Authentication failed (4004)"),
            ("", "gateway not READY within 15s (check bot token/network)"),
        ],
        ids=["fatal-error", "plain-timeout"],
    )
    async def test_a_handshake_that_never_lands_says_why(
        self, registry, monkeypatch, fatal, shown
    ) -> None:
        monkeypatch.setattr(_Client, "ready", False)
        monkeypatch.setattr(_Client, "fatal_error", fatal)
        orch = _orch()
        client = await _start(orch)
        await _finish_registrations()
        assert client is not None  # still running, so it can recover
        state = orch.dashboard_state
        assert (state.discord_connected, state.discord_connect_error) == (False, shown)

    @pytest.mark.asyncio
    async def test_the_badge_follows_the_state_callback(self, registry) -> None:
        orch = _orch()
        client = await _start(orch)
        await _finish_registrations()
        client.on_state_change(False, "x" * 200)
        assert orch.dashboard_state.discord_connected is False
        assert orch.dashboard_state.discord_connect_error == "x" * 120
        client.on_state_change(True, "")
        assert orch.dashboard_state.discord_connected is True

    @pytest.mark.asyncio
    async def test_no_dashboard_state_still_starts(self, registry) -> None:
        client = await _start(_orch(dashboard_state=None))
        await _finish_registrations()
        assert client is not None
        assert client.on_state_change is None


class TestFailure:
    @pytest.mark.asyncio
    async def test_a_failed_start_reports_only_the_exception_class(
        self, registry, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            _Transport, "connect_error", RuntimeError("wss://gateway?token=bot-secret")
        )
        orch = _orch()
        assert await _start(orch) is None
        assert orch.dashboard_state.discord_connect_error == "RuntimeError"
        assert gw._PENDING_REGISTRATIONS == set()  # no menu task for a failed start

    @pytest.mark.asyncio
    async def test_a_failed_start_without_a_dashboard_returns_none(
        self, registry, monkeypatch
    ) -> None:
        monkeypatch.setattr(_Transport, "connect_error", OSError("down"))
        assert await _start(_orch(dashboard_state=None)) is None

    @pytest.mark.asyncio
    async def test_an_empty_allowlist_warns_that_every_message_is_rejected(
        self, registry, caplog
    ) -> None:
        caplog.set_level(logging.WARNING, logger=gw.logger.name)
        await _start(_orch(_discord_allowed_user_ids=[]))
        await _finish_registrations()
        assert any("REJECT all messages" in r.getMessage() for r in caplog.records)


class TestSlashMenu:
    @pytest.mark.asyncio
    async def test_the_menu_is_published_after_ready_in_the_background(self, registry) -> None:
        client = await _start(_orch(dashboard_state=None))
        assert len(gw._PENDING_REGISTRATIONS) == 1  # scheduled, not awaited on boot
        await _finish_registrations()
        assert client.ready_waits == [gw._REGISTER_READY_TIMEOUT_SECS]
        assert client.registered == [["menu"]]
        assert gw._PENDING_REGISTRATIONS == set()  # dropped once done

    @pytest.mark.asyncio
    async def test_no_ready_skips_the_menu_with_a_warning(
        self, registry, monkeypatch, caplog
    ) -> None:
        monkeypatch.setattr(_Client, "ready", False)
        caplog.set_level(logging.WARNING, logger=gw.logger.name)
        client = await _start(_orch(dashboard_state=None))
        await _finish_registrations()
        assert client.registered == []
        assert any("slash-command menu was not published" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_a_failed_registration_costs_only_the_menu(
        self, registry, monkeypatch, caplog
    ) -> None:
        monkeypatch.setattr(_Client, "register_error", RuntimeError("429"))
        caplog.set_level(logging.WARNING, logger=gw.logger.name)
        client = await _start(_orch(dashboard_state=None))
        await _finish_registrations()
        assert client is not None
        assert any(
            "publishing the slash-command menu failed" in r.getMessage() for r in caplog.records
        )


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
    async def test_the_dispatcher_gets_the_resolved_mode(self, registry) -> None:
        await _start(_orch(_approval_mode="yolo"))
        await _finish_registrations()
        assert _Dispatcher.instances[0].kwargs["approval_mode"] == gw.APPROVAL_AUTO
