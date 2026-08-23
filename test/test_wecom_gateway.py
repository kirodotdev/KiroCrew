"""WeCom channel startup.

``maybe_start_wecom`` had no coverage at all, which mattered most for one thing:
the ``on_status`` wiring is the **documented compensating control** for not
verifying WeCom credentials at save time (see the WeCom settings API section of
``docs/system-specs/modules/messaging.md``). If that wiring breaks, a wrong bot
secret leaves the settings badge green forever and the operator is told nothing —
and nothing would have failed to tell us.

So these pin the ordering that makes the badge truthful, not merely that startup
returns a client.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.messaging.driver import APPROVAL_AUTO, APPROVAL_INTERACTIVE
from kiro_crew.wecom.gateway import _allowed_userids, _resolve_approval_mode, maybe_start_wecom


class FakeState:
    def __init__(self) -> None:
        self.wecom_connected: Any = "unset"
        self.wecom_connect_error: Any = "unset"
        self.registered: list[Any] = []

    def register_channel_transport(self, transport: Any) -> None:
        self.registered.append(transport)


def _orch(
    *,
    enabled: bool = True,
    approval: str | None = None,
    allowed: list | None = None,
    allow_all: bool = False,
    state: FakeState | None = None,
) -> Any:
    return SimpleNamespace(
        _wecom_enabled=enabled,
        _wecom_bot_id="bot-1",
        _wecom_secret="sec-1",
        _owner_id="Wei",
        _approval_mode=approval,
        sessions=SimpleNamespace(),
        ctx_builder=SimpleNamespace(hooks=None),
        conv_log=None,
        dashboard_state=state,
        _cfg=SimpleNamespace(
            agent=SimpleNamespace(default_agent="", approval_mode="interactive"),
            wecom=SimpleNamespace(
                ws_url="wss://example.invalid/ws",
                allowed_users=allowed if allowed is not None else [{"userid": "Wei", "name": "W"}],
                allow_all_users=allow_all,
                hard_threshold_pct=95.0,
                soft_threshold_pct=80.0,
            ),
            messaging=SimpleNamespace(
                idle_reset_minutes=0, daily_reset_hour=-1, dm_scope="per_user"
            ),
        ),
    )


@pytest.fixture(autouse=True)
def _never_open_a_socket(monkeypatch):
    """``connect()`` must not reach the network from a unit test."""
    started: list[bool] = []

    async def fake_start(self) -> None:
        started.append(True)

    monkeypatch.setattr("kiro_crew.wecom.client.WeComClient.start", fake_start)
    return started


class TestApprovalMode:
    def test_yolo_auto_approves(self) -> None:
        assert _resolve_approval_mode(_orch(approval="yolo")) == APPROVAL_AUTO

    def test_an_explicit_auto_override_auto_approves(self) -> None:
        assert _resolve_approval_mode(_orch(approval="auto")) == APPROVAL_AUTO

    def test_anything_else_collapses_to_interactive(self) -> None:
        # WeCom renders no approve/deny widget, so INTERACTIVE is deny-by-default
        # here. Documented, and shared with Webex's identical resolver.
        assert _resolve_approval_mode(_orch(approval="interactive")) == APPROVAL_INTERACTIVE
        assert _resolve_approval_mode(_orch(approval=None)) == APPROVAL_INTERACTIVE


class TestAllowedUserids:
    def test_userids_are_extracted_from_the_canonical_entries(self) -> None:
        orch = _orch(allowed=[{"userid": "a", "name": "A"}, {"userid": "b"}])
        assert _allowed_userids(orch) == ["a", "b"]

    def test_malformed_entries_are_skipped_rather_than_crashing_startup(self) -> None:
        # config.json is operator-edited, so a stray value must not take the
        # channel down at boot.
        orch = _orch(allowed=[{"userid": "a"}, {"name": "no id"}, "bare string", None, {}])
        assert _allowed_userids(orch) == ["a"]


class TestStartup:
    @pytest.mark.asyncio
    async def test_a_disabled_channel_starts_nothing(self) -> None:
        assert await maybe_start_wecom(_orch(enabled=False)) is None

    @pytest.mark.asyncio
    async def test_startup_returns_the_client_and_registers_the_transport(self) -> None:
        state = FakeState()
        client = await maybe_start_wecom(_orch(state=state))
        assert client is not None
        assert len(state.registered) == 1
        assert state.registered[0].channel_type == "wecom"

    @pytest.mark.asyncio
    async def test_the_badge_starts_NOT_connected(self) -> None:
        # connect() only SCHEDULES the WS loop, so "started" proves nothing about
        # the credentials. Starting green would show a healthy channel that never
        # connected.
        state = FakeState()
        await maybe_start_wecom(_orch(state=state))
        assert state.wecom_connected is False
        assert state.wecom_connect_error == ""

    @pytest.mark.asyncio
    async def test_the_status_callback_is_wired_and_drives_the_badge(self) -> None:
        state = FakeState()
        client = await maybe_start_wecom(_orch(state=state))
        assert client is not None and client.on_status is not None

        client.on_status(True, "")
        assert state.wecom_connected is True
        assert state.wecom_connect_error == ""

        client.on_status(False, "bot credentials rejected by WeCom (check bot ID / secret)")
        assert state.wecom_connected is False
        assert "credentials" in state.wecom_connect_error

    @pytest.mark.asyncio
    async def test_a_long_reason_is_truncated_for_the_badge(self) -> None:
        state = FakeState()
        client = await maybe_start_wecom(_orch(state=state))
        assert client is not None and client.on_status is not None
        client.on_status(False, "x" * 500)
        assert len(state.wecom_connect_error) == 120

    @pytest.mark.asyncio
    async def test_the_callback_is_wired_BEFORE_the_socket_opens(self, monkeypatch) -> None:
        # If connect() ran first, the very first transition could fire into a
        # missing callback -- and the client's dedupe would then swallow the
        # re-report forever, leaving the badge permanently wrong.
        order: list[str] = []

        async def fake_start(self) -> None:
            order.append("connect")

        monkeypatch.setattr("kiro_crew.wecom.client.WeComClient.start", fake_start)

        class RecordingState(FakeState):
            def __setattr__(self, name: str, value: Any) -> None:
                if name == "wecom_connected":
                    order.append("badge")
                object.__setattr__(self, name, value)

        await maybe_start_wecom(_orch(state=RecordingState()))
        assert order and order[0] == "badge", f"connect ran before the badge was wired: {order}"
        assert "connect" in order

    @pytest.mark.asyncio
    async def test_a_startup_failure_is_swallowed_and_reported_on_the_badge(
        self, monkeypatch
    ) -> None:
        # A WeCom problem must never take the gateway down with it.
        async def boom(self) -> None:
            raise RuntimeError("ws refused")

        monkeypatch.setattr("kiro_crew.wecom.client.WeComClient.start", boom)
        state = FakeState()

        assert await maybe_start_wecom(_orch(state=state)) is None

        assert state.wecom_connected is False
        assert "RuntimeError" in state.wecom_connect_error

    @pytest.mark.asyncio
    async def test_startup_works_with_no_dashboard_state(self) -> None:
        # A headless gateway has no dashboard; the channel must still start.
        assert await maybe_start_wecom(_orch(state=None)) is not None

    @pytest.mark.asyncio
    async def test_the_transport_receives_the_configured_allowlist_and_owner(self) -> None:
        state = FakeState()
        await maybe_start_wecom(_orch(state=state, allowed=[{"userid": "Zhang"}], allow_all=False))
        transport = state.registered[0]
        assert transport.authorize(SimpleNamespace(user_id="Zhang", channel_type="wecom"))
        assert transport.authorize(SimpleNamespace(user_id="Wei", channel_type="wecom"))
        assert not transport.authorize(SimpleNamespace(user_id="Stranger", channel_type="wecom"))

    @pytest.mark.asyncio
    async def test_the_inbound_handler_is_wired_client_to_transport_to_dispatcher(self) -> None:
        # set_message_handler exists to break the client<->transport construction
        # cycle; if it is not called the channel connects and then ignores everyone.
        state = FakeState()
        client = await maybe_start_wecom(_orch(state=state))
        assert client is not None
        assert client._on_message is not None
        transport = state.registered[0]
        assert client._on_message == transport.receive
        assert transport.dispatcher is not None
        assert transport.dispatcher.client is client
