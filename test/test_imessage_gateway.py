"""Tests for kiro_crew.imessage.gateway (the guarded boot entry point).

The module is all branch, no algorithm: two deliberate refusals, a fail-closed
warning, a readiness verdict that drives the status badge, and a catch-all that
must never let an iMessage problem take the gateway down. Each of those is a
separate consequence, so each gets its own test rather than one happy path.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew.imessage import gateway as gw
from kiro_crew.messaging.driver import APPROVAL_AUTO, APPROVAL_INTERACTIVE


class _State:
    """The slice of DashboardState this module touches."""

    def __init__(self) -> None:
        self.imessage_connected = False
        self.imessage_connect_error = ""
        self.registered: list[Any] = []

    def register_channel_transport(self, transport: Any) -> None:
        self.registered.append(transport)


class _Cfg:
    def __init__(self, **imessage: Any) -> None:
        self.imessage = type(
            "IM",
            (),
            {
                "allowed_handles": imessage.get("allowed_handles", ["+15551234567"]),
                "db_path": imessage.get("db_path", ""),
                "service": imessage.get("service", "imessage"),
            },
        )()
        self.agent = type("Agent", (), {"approval_mode": imessage.get("approval_mode", "")})()


class _Orch:
    def __init__(self, *, enabled: bool = True, state: _State | None = None, **cfg: Any) -> None:
        self._imessage_enabled = enabled
        self._cfg = _Cfg(**cfg)
        self.sessions = object()
        self.ctx_builder = object()
        self.dashboard_state = state
        self._approval_mode = cfg.get("orch_approval")


class _FakeClient:
    """Stand-in for IMessageClient: records wiring, answers readiness."""

    def __init__(self, *, ready: bool = True, last_error: str = "", **kwargs: Any) -> None:
        self.kwargs = kwargs
        self._ready = ready
        self.last_error = last_error
        self.handler: Any = None
        self.on_state_change: Any = None

    def set_message_handler(self, handler: Any) -> None:
        self.handler = handler

    async def wait_ready(self, timeout: float = 0.0) -> bool:
        return self._ready


class _FakeTransport:
    def __init__(self, client: Any, *, allowed_handles: Any, dispatch: Any) -> None:
        self.client = client
        self.allowed_handles = allowed_handles
        self.dispatch = dispatch
        self.connected = False

    async def connect(self) -> None:
        self.connected = True

    async def receive(self, inbound: Any) -> None:
        """The seam the gateway wires the client's notifications into."""
        return None


class _FakeDispatcher:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.client: Any = None

    async def handle_message(self, inbound: Any) -> None:
        return None


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace the three collaborators and force the macOS branch."""
    made: dict[str, Any] = {}

    def _client(**kwargs: Any) -> _FakeClient:
        made["client"] = _FakeClient(
            ready=made.get("ready", True),
            last_error=made.get("last_error", ""),
            **kwargs,
        )
        return made["client"]

    def _transport(client: Any, **kwargs: Any) -> _FakeTransport:
        made["transport"] = _FakeTransport(client, **kwargs)
        return made["transport"]

    def _dispatcher(**kwargs: Any) -> _FakeDispatcher:
        made["dispatcher"] = _FakeDispatcher(**kwargs)
        return made["dispatcher"]

    monkeypatch.setattr(gw, "IMessageClient", _client)
    monkeypatch.setattr(gw, "IMessageTransport", _transport)
    monkeypatch.setattr(gw, "IMessageDispatcher", _dispatcher)
    monkeypatch.setattr(gw, "IS_MACOS", True)
    return made


class TestApprovalMode:
    def test_yolo_auto_approves(self) -> None:
        orch = _Orch(orch_approval="yolo")
        assert gw._resolve_approval_mode(orch) == APPROVAL_AUTO

    def test_an_explicit_auto_override_auto_approves(self) -> None:
        orch = _Orch(orch_approval=APPROVAL_AUTO)
        assert gw._resolve_approval_mode(orch) == APPROVAL_AUTO

    def test_anything_else_collapses_to_interactive(self) -> None:
        # Deny-by-default: an unrecognised mode must not widen approval.
        orch = _Orch(orch_approval="reads")
        assert gw._resolve_approval_mode(orch) == APPROVAL_INTERACTIVE

    def test_it_falls_back_to_the_configured_mode(self) -> None:
        orch = _Orch(orch_approval=None, approval_mode=APPROVAL_AUTO)
        assert gw._resolve_approval_mode(orch) == APPROVAL_AUTO


class TestRefusals:
    @pytest.mark.asyncio
    async def test_a_disabled_channel_does_not_start(self, wired: dict[str, Any]) -> None:
        assert await gw.maybe_start_imessage(_Orch(enabled=False)) is None
        assert "client" not in wired

    @pytest.mark.asyncio
    async def test_off_macos_it_refuses_and_says_why(
        self, monkeypatch: pytest.MonkeyPatch, wired: dict[str, Any]
    ) -> None:
        # The refusal must be visible in the UI, not only in a log line: the
        # symptom otherwise looks like a channel that silently never connects.
        monkeypatch.setattr(gw, "IS_MACOS", False)
        state = _State()
        assert await gw.maybe_start_imessage(_Orch(state=state)) is None
        assert "macOS" in state.imessage_connect_error
        assert "client" not in wired

    @pytest.mark.asyncio
    async def test_off_macos_without_dashboard_state_still_refuses(
        self, monkeypatch: pytest.MonkeyPatch, wired: dict[str, Any]
    ) -> None:
        monkeypatch.setattr(gw, "IS_MACOS", False)
        assert await gw.maybe_start_imessage(_Orch(state=None)) is None


class TestStart:
    @pytest.mark.asyncio
    async def test_the_happy_path_connects_and_registers(self, wired: dict[str, Any]) -> None:
        state = _State()
        client = await gw.maybe_start_imessage(_Orch(state=state))
        assert client is wired["client"]
        assert wired["transport"].connected is True
        assert state.registered == [wired["transport"]]
        assert state.imessage_connected is True
        assert state.imessage_connect_error == ""

    @pytest.mark.asyncio
    async def test_inbound_is_wired_through_the_transport(self, wired: dict[str, Any]) -> None:
        # The client must deliver to transport.receive, not straight to the
        # dispatcher: that hop is what suppresses own messages, fails closed on
        # groups, and authorizes the handle.
        await gw.maybe_start_imessage(_Orch(state=_State()))
        assert wired["client"].handler == wired["transport"].receive
        assert wired["dispatcher"].client is wired["client"]

    @pytest.mark.asyncio
    async def test_an_empty_allowlist_still_starts_but_warns(
        self, wired: dict[str, Any], caplog: pytest.LogCaptureFixture
    ) -> None:
        # Fail-closed, not fail-to-start: the operator needs the channel running
        # to see the panel's own empty-allowlist hint.
        with caplog.at_level("WARNING"):
            client = await gw.maybe_start_imessage(_Orch(state=_State(), allowed_handles=[]))
        assert client is not None
        assert wired["transport"].allowed_handles == []
        assert any("REJECT every message" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_blank_handles_are_dropped(self, wired: dict[str, Any]) -> None:
        await gw.maybe_start_imessage(_Orch(state=_State(), allowed_handles=["", "+1555", ""]))
        assert wired["transport"].allowed_handles == ["+1555"]

    @pytest.mark.asyncio
    async def test_a_bridge_that_never_readies_reports_it(self, wired: dict[str, Any]) -> None:
        wired["ready"] = False
        state = _State()
        assert await gw.maybe_start_imessage(_Orch(state=state)) is not None
        assert state.imessage_connected is False
        assert "Full Disk Access" in state.imessage_connect_error

    @pytest.mark.asyncio
    async def test_a_readiness_failure_prefers_the_clients_own_reason(
        self, wired: dict[str, Any]
    ) -> None:
        wired["ready"] = False
        wired["last_error"] = "database unavailable"
        state = _State()
        await gw.maybe_start_imessage(_Orch(state=state))
        assert state.imessage_connect_error == "database unavailable"

    @pytest.mark.asyncio
    async def test_the_state_callback_keeps_the_badge_truthful(self, wired: dict[str, Any]) -> None:
        # A watch that drops later must flip the badge back off with a reason,
        # otherwise the UI claims connected for the rest of the process's life.
        state = _State()
        await gw.maybe_start_imessage(_Orch(state=state))
        wired["client"].on_state_change(False, "bridge exited")
        assert state.imessage_connected is False
        assert state.imessage_connect_error == "bridge exited"

    @pytest.mark.asyncio
    async def test_it_starts_without_dashboard_state(self, wired: dict[str, Any]) -> None:
        assert await gw.maybe_start_imessage(_Orch(state=None)) is not None


class TestFailureIsSwallowed:
    @pytest.mark.asyncio
    async def test_a_connect_failure_never_takes_the_gateway_down(
        self, monkeypatch: pytest.MonkeyPatch, wired: dict[str, Any]
    ) -> None:
        class _Boom(_FakeTransport):
            async def connect(self) -> None:
                raise RuntimeError("bridge missing")

        monkeypatch.setattr(gw, "IMessageTransport", lambda client, **kw: _Boom(client, **kw))
        state = _State()
        assert await gw.maybe_start_imessage(_Orch(state=state)) is None
        # The type name, not the message: the message can carry a handle.
        assert state.imessage_connect_error == "RuntimeError"

    @pytest.mark.asyncio
    async def test_a_failure_without_dashboard_state_is_still_swallowed(
        self, monkeypatch: pytest.MonkeyPatch, wired: dict[str, Any]
    ) -> None:
        def _boom(**_kwargs: Any) -> Any:
            raise RuntimeError("nope")

        monkeypatch.setattr(gw, "IMessageDispatcher", _boom)
        assert await gw.maybe_start_imessage(_Orch(state=None)) is None
