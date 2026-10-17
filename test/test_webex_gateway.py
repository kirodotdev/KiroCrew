"""Tests for kiro_crew.webex.gateway (maybe_start_webex).

This file owns three things every other Webex change leans on: the guarded
no-op paths (a Webex problem must never take down the gateway), the approval-mode
resolution that decides whether tools can be approved at all, and the
connected-badge contract — the dashboard's only honest signal that the channel
is live.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from kiro_crew.messaging.driver import APPROVAL_AUTO, APPROVAL_INTERACTIVE
from kiro_crew.webex import gateway as webex_gateway
from kiro_crew.webex.gateway import _resolve_approval_mode, maybe_start_webex


class FakeClient:
    """A WebexClient stand-in whose readiness and identity are settable."""

    instances: list["FakeClient"] = []

    def __init__(self, *, token: str = "", device_base: str = "", **kw) -> None:
        self.token = token
        self.device_base = device_base
        self.on_state_change = None
        self.last_error = ""
        self.handler = None
        self.started = False
        self.closed = False
        self._ready = True
        FakeClient.instances.append(self)

    def set_message_handler(self, handler) -> None:
        self.handler = handler

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True

    async def wait_ready(self, timeout: float = 15.0) -> bool:
        return self._ready


class FakeState:
    def __init__(self) -> None:
        self.webex_connected = False
        self.webex_connect_error = ""
        self.registered: list = []

    def register_channel_transport(self, transport) -> None:
        self.registered.append(transport)


def _orch(
    *,
    enabled: bool = True,
    token: str = "tok",
    emails: list[str] | None = None,
    approval_mode: str | None = None,
    cfg_approval: str = "interactive",
    state: FakeState | None = None,
    allow_group_rooms: bool = False,
    allowed_room_ids: list[str] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        _webex_enabled=enabled,
        _webex_bot_token=token,
        _webex_allowed_emails=emails if emails is not None else ["Kyle@Example.com"],
        _approval_mode=approval_mode,
        _cfg=SimpleNamespace(
            agent=SimpleNamespace(approval_mode=cfg_approval, default_agent=""),
            messaging=SimpleNamespace(
                dm_scope="per-channel-peer", idle_reset_minutes=0, daily_reset_hour=-1
            ),
            webex=SimpleNamespace(
                soft_threshold_pct=80.0,
                hard_threshold_pct=95.0,
                allowed_emails=emails if emails is not None else ["Kyle@Example.com"],
                allow_group_rooms=allow_group_rooms,
                allowed_room_ids=allowed_room_ids or [],
                reply_in_thread=True,
                wdm_base="",
            ),
        ),
        sessions=object(),
        ctx_builder=object(),
        conv_log=None,
        dashboard_state=state,
    )


@pytest.fixture(autouse=True)
def _fake_client(monkeypatch: pytest.MonkeyPatch):
    FakeClient.instances = []
    monkeypatch.setattr(webex_gateway, "WebexClient", FakeClient)
    yield
    FakeClient.instances = []


class TestGuardedNoOps:
    @pytest.mark.asyncio
    async def test_disabled_channel_does_not_construct_a_client(self) -> None:
        assert await maybe_start_webex(_orch(enabled=False)) is None
        assert FakeClient.instances == []

    @pytest.mark.asyncio
    async def test_missing_token_does_not_construct_a_client(self) -> None:
        # A configured-but-uncredentialed channel is a normal state (the operator
        # has not pasted the token yet), so it is a silent no-op, not an error.
        assert await maybe_start_webex(_orch(token="")) is None
        assert FakeClient.instances == []

    @pytest.mark.asyncio
    async def test_an_empty_allowlist_still_starts_but_warns(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Fail closed, loudly.

        Anyone in the org can message a Webex bot, so an empty allowlist rejects
        every message. Starting anyway is right — the operator sees a connected
        badge and can add themselves — but it MUST warn, or the channel looks
        broken with no explanation.
        """
        with caplog.at_level("WARNING"):
            client = await maybe_start_webex(_orch(emails=[]))
        assert client is not None
        assert any("allowed_emails" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_a_start_failure_is_badged_and_swallowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A Webex problem must never take down the gateway.

        The badge carries the exception TYPE name, never its message: the
        message can embed a URL or response body that is token-adjacent.
        """

        class Boom(FakeClient):
            async def start(self) -> None:
                raise RuntimeError("https://webexapis.com/v1?secret=abc")

        monkeypatch.setattr(webex_gateway, "WebexClient", Boom)
        state = FakeState()
        assert await maybe_start_webex(_orch(state=state)) is None
        assert state.webex_connect_error == "RuntimeError"
        assert state.webex_connected is False


class TestApprovalMode:
    def test_yolo_resolves_to_auto(self) -> None:
        assert _resolve_approval_mode(_orch(approval_mode="yolo")) == APPROVAL_AUTO

    def test_auto_resolves_to_auto(self) -> None:
        assert _resolve_approval_mode(_orch(approval_mode="auto")) == APPROVAL_AUTO

    @pytest.mark.parametrize("mode", ["interactive", "trust", "trust-reads"])
    def test_everything_else_collapses_to_interactive(self, mode: str) -> None:
        """Anything that is not auto becomes interactive, i.e. it ASKS.

        Collapsing trust / trust-reads here is deliberate: the driver's
        finer-grained rungs need a channel that can express them, and asking is
        the safe direction to collapse toward.
        """
        assert _resolve_approval_mode(_orch(approval_mode=mode)) == APPROVAL_INTERACTIVE

    def test_an_unset_cli_override_falls_back_to_config(self) -> None:
        assert (
            _resolve_approval_mode(_orch(approval_mode=None, cfg_approval="auto")) == APPROVAL_AUTO
        )
        assert (
            _resolve_approval_mode(_orch(approval_mode=None, cfg_approval="interactive"))
            == APPROVAL_INTERACTIVE
        )


class TestWiring:
    @pytest.mark.asyncio
    async def test_the_inbound_path_is_wired_without_a_construction_cycle(self) -> None:
        """The client gets its handler AFTER construction, on purpose.

        The transport needs the client and the client needs the transport's
        ``receive``; ``set_message_handler`` is what breaks that cycle. If it
        stopped being called, inbound messages would silently go nowhere.
        """
        state = FakeState()
        client = await maybe_start_webex(_orch(state=state))
        assert client is not None
        assert client.started is True
        assert client.handler is not None
        transport = state.registered[0]
        assert client.handler == transport.receive

    @pytest.mark.asyncio
    async def test_the_allowlist_is_lowercased_for_matching(self) -> None:
        # Webex emails are case-insensitive, and authorize() compares lowercased.
        state = FakeState()
        await maybe_start_webex(_orch(emails=["Kyle@Example.COM"], state=state))
        transport = state.registered[0]
        assert transport.authorize(
            SimpleNamespace(user_id="kyle@example.com", channel_type="webex")
        )

    @pytest.mark.asyncio
    async def test_no_dashboard_state_still_starts(self) -> None:
        # The gateway can boot without a dashboard (headless CLI runs), so every
        # state write must be behind the None check.
        orch = _orch(state=None)
        client = await maybe_start_webex(orch)
        assert client is not None and client.started is True


class TestConnectedBadge:
    @pytest.mark.asyncio
    async def test_connected_is_reported_only_after_the_handshake(self) -> None:
        state = FakeState()
        await maybe_start_webex(_orch(state=state))
        assert state.webex_connected is True
        assert state.webex_connect_error == ""

    @pytest.mark.asyncio
    async def test_a_handshake_timeout_leaves_the_badge_off_with_a_reason(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``connect()`` only SCHEDULES the serve loop.

        Reporting success off the back of it would show a green badge on a bad
        token — the failure mode with the longest tail, because nothing else
        contradicts it.
        """

        class NeverReady(FakeClient):
            async def wait_ready(self, timeout: float = 15.0) -> bool:
                return False

        monkeypatch.setattr(webex_gateway, "WebexClient", NeverReady)
        state = FakeState()
        await maybe_start_webex(_orch(state=state))
        assert state.webex_connected is False
        assert "bot token" in state.webex_connect_error

    @pytest.mark.asyncio
    async def test_a_timeout_prefers_the_client_reason_over_the_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Refused(FakeClient):
            async def wait_ready(self, timeout: float = 15.0) -> bool:
                self.last_error = "ClientConnectorError"
                return False

        monkeypatch.setattr(webex_gateway, "WebexClient", Refused)
        state = FakeState()
        await maybe_start_webex(_orch(state=state))
        assert state.webex_connect_error == "ClientConnectorError"

    @pytest.mark.asyncio
    async def test_the_observer_keeps_the_badge_truthful_after_boot(self) -> None:
        """A disconnect hours later must flip the badge back.

        Without the observer the badge records "was connected once at boot",
        which is exactly the lie the truthful-status work exists to prevent.
        """
        state = FakeState()
        client = await maybe_start_webex(_orch(state=state))
        assert client is not None and client.on_state_change is not None

        client.on_state_change(False, "server closed connection immediately")
        assert state.webex_connected is False
        assert state.webex_connect_error == "server closed connection immediately"

        client.on_state_change(True, "")
        assert state.webex_connected is True
        assert state.webex_connect_error == ""

    @pytest.mark.asyncio
    async def test_a_long_disconnect_reason_is_truncated(self) -> None:
        # The reason is rendered in a dashboard badge, and it is externally
        # derived, so it is bounded at the write rather than at every reader.
        state = FakeState()
        client = await maybe_start_webex(_orch(state=state))
        assert client is not None and client.on_state_change is not None

        client.on_state_change(False, "x" * 500)
        assert len(state.webex_connect_error) == 120


class TestDispatcherConstruction:
    @pytest.mark.asyncio
    async def test_the_dispatcher_receives_the_resolved_approval_mode(self) -> None:
        state = FakeState()
        await maybe_start_webex(_orch(approval_mode="yolo", state=state))
        transport = state.registered[0]
        # The dispatcher is reachable through the handler the client was given.
        dispatcher = transport._dispatch.__self__
        assert dispatcher.approval_mode == APPROVAL_AUTO
        assert dispatcher.client is not None

    @pytest.mark.asyncio
    async def test_interactive_mode_reaches_the_dispatcher(self) -> None:
        state = FakeState()
        await maybe_start_webex(_orch(approval_mode="interactive", state=state))
        dispatcher = state.registered[0]._dispatch.__self__
        assert dispatcher.approval_mode == APPROVAL_INTERACTIVE


class TestConcurrencySafety:
    @pytest.mark.asyncio
    async def test_two_boots_do_not_share_a_client(self) -> None:
        # Each start owns its own client, so a second gateway instance in the
        # same process cannot close the first one's socket.
        a = await maybe_start_webex(_orch())
        b = await maybe_start_webex(_orch())
        assert a is not b
        await asyncio.gather(a.close(), b.close())
        assert a.closed and b.closed
