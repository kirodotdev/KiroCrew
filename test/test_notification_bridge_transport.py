"""The notification bridge's ``MessagingTransport`` sink (RFC phase B2).

Each transport in ``TRANSPORT_SINKS`` is driven end to end through the real bus,
the dispatcher and the proactive owner-DM leg on its REAL transport class, with
only the platform client faked: a routed critical note lands as a redacted owner
DM with a ``completed`` record. A transport that is not connected, cannot send
proactively, or names no single owner is an audited skip; a refused send is an
audited error.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from real_dashboard_state import (  # noqa: F401
    _settle_real_dashboard_states,
    real_dashboard_state,
)

from kiro_crew.constants import CHANNEL_OWNER_DM_NAMESPACES
from kiro_crew.dashboard.handlers import messaging
from kiro_crew.notifications import bridge_transport
from kiro_crew.notifications.bridge_transport import (
    TRANSPORT_SINKS,
    TransportBridgeSink,
    render_transport_text,
)
from kiro_crew.notifications.bus import NotificationPayload

_SECRET = "AKIAIOSFODNN7EXAMPLE"


def _discord(owners: list[str]) -> tuple[Any, AsyncMock]:
    from kiro_crew.discord.transport import DiscordTransport

    client = MagicMock()
    client.create_dm_channel = AsyncMock(return_value="999000111222333444")
    client.send_message = AsyncMock(return_value="1")
    return DiscordTransport(client, allowed_user_ids=owners), client.send_message


def _telegram(owners: list[str]) -> tuple[Any, AsyncMock]:
    from kiro_crew.telegram.transport import TelegramTransport

    client = MagicMock()
    client.send_message = AsyncMock(return_value=7)
    return TelegramTransport(client, allowed_user_ids=[int(o) for o in owners]), client.send_message


def _webex(owners: list[str]) -> tuple[Any, AsyncMock]:
    from kiro_crew.webex.transport import WebexTransport

    client = MagicMock()
    client.send_message = AsyncMock(return_value="m-1")
    return WebexTransport(client, allowed_emails=owners), client.send_message


#: transport id -> (build the real transport with these owners, one owner id).
_TRANSPORTS: dict[str, tuple[Callable[[list[str]], tuple[Any, AsyncMock]], str]] = {
    "discord": (_discord, "424242424242424242"),
    "telegram": (_telegram, "123"),
    "webex": (_webex, "owner@example.invalid"),
}


class _Sel:
    def __init__(self) -> None:
        self.deliveries: list[dict[str, Any]] = []

    def log_tool_invocation(self, **kw: Any) -> None:
        self.deliveries.append(kw)

    def __getattr__(self, name: str) -> Callable[..., None]:
        return lambda **kw: None


@pytest.fixture()
def sel_log(monkeypatch) -> _Sel:
    log = _Sel()
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: log)
    monkeypatch.setattr("kiro_crew.dashboard.chat_runner.sel", lambda: log)
    return log


def _routed_state(tmp_path, monkeypatch, transport_id: str, transport: Any = None):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.notifications.settings.config_dir", lambda: tmp_path)
    state = real_dashboard_state(tmp_path)
    if transport is not None:
        state.register_channel_transport(transport)
    state.notification_channel_settings.update("system.cron", deliver_to=[transport_id])
    return state


def _push(state, **fields: Any) -> None:
    state.notification_bus.push(
        NotificationPayload(
            source="system",
            channel="system.cron",
            title=fields.get("title", "Job failed"),
            body=fields.get("body", "details"),
            priority="critical",
        )
    )


def test_every_bridge_transport_but_slack_has_a_test_row():
    assert set(TRANSPORT_SINKS) == set(_TRANSPORTS)


def test_every_registered_transport_can_name_its_owner():
    """A channel that folds learned peers into its targets would DM a stranger."""
    assert set(TRANSPORT_SINKS) <= set(CHANNEL_OWNER_DM_NAMESPACES)


@pytest.mark.asyncio
@pytest.mark.parametrize("transport_id", sorted(_TRANSPORTS))
async def test_a_channel_that_cannot_name_its_owner_is_an_audited_skip(
    tmp_path, monkeypatch, sel_log, transport_id
):
    build, owner = _TRANSPORTS[transport_id]
    transport, send = build([owner])
    monkeypatch.setattr(bridge_transport, "CHANNEL_OWNER_DM_NAMESPACES", ())
    state = _routed_state(tmp_path, monkeypatch, transport_id, transport)
    _push(state)
    await state.notification_bridge.drain()
    send.assert_not_awaited()
    bridged = [d for d in sel_log.deliveries if d.get("tool_kind") == "notification_bridge"]
    assert [(d["outcome"], d["error"]) for d in bridged] == [
        ("skipped", f"{transport_id} cannot name its owner")
    ]


@pytest.mark.parametrize("transport_id", sorted(_TRANSPORTS))
def test_the_state_registers_a_sink_for_each_transport(tmp_path, monkeypatch, transport_id):
    state = _routed_state(tmp_path, monkeypatch, transport_id)
    sink = state.notification_bridge._sinks[transport_id]
    assert isinstance(sink, TransportBridgeSink)


@pytest.mark.asyncio
@pytest.mark.parametrize("transport_id", sorted(_TRANSPORTS))
async def test_a_routed_critical_note_lands_as_a_redacted_owner_dm(
    tmp_path, monkeypatch, sel_log, transport_id
):
    build, owner = _TRANSPORTS[transport_id]
    transport, send = build([owner])
    state = _routed_state(tmp_path, monkeypatch, transport_id, transport)
    _push(state, title=f"key {_SECRET}")
    await state.notification_bridge.drain()

    send.assert_awaited_once()
    text = " ".join(str(arg) for arg in send.await_args.args)
    assert _SECRET not in text and "[REDACTED" in text
    assert "details" in text
    bridged = [d for d in sel_log.deliveries if d.get("tool_kind") == "notification_bridge"]
    assert [d["outcome"] for d in bridged] == ["completed"]
    assert bridged[0]["downstream_service"] == transport_id


@pytest.mark.asyncio
@pytest.mark.parametrize("transport_id", sorted(_TRANSPORTS))
async def test_a_transport_that_is_not_connected_is_an_audited_skip(
    tmp_path, monkeypatch, sel_log, transport_id
):
    state = _routed_state(tmp_path, monkeypatch, transport_id)
    _push(state)
    await state.notification_bridge.drain()
    bridged = [d for d in sel_log.deliveries if d.get("tool_kind") == "notification_bridge"]
    assert [d["outcome"] for d in bridged] == ["skipped"]


@pytest.mark.asyncio
@pytest.mark.parametrize("transport_id", sorted(_TRANSPORTS))
async def test_no_single_owner_is_an_audited_skip(tmp_path, monkeypatch, sel_log, transport_id):
    build, owner = _TRANSPORTS[transport_id]
    # Two allow-listed people: the leg refuses to guess which one is the owner.
    transport, send = build([owner, owner[:-1] + "9"])
    state = _routed_state(tmp_path, monkeypatch, transport_id, transport)
    _push(state)
    await state.notification_bridge.drain()
    send.assert_not_awaited()
    bridged = [d for d in sel_log.deliveries if d.get("tool_kind") == "notification_bridge"]
    assert [d["outcome"] for d in bridged] == ["skipped"]


@pytest.mark.asyncio
@pytest.mark.parametrize("transport_id", sorted(_TRANSPORTS))
async def test_no_proactive_send_is_an_audited_skip(tmp_path, monkeypatch, sel_log, transport_id):
    build, owner = _TRANSPORTS[transport_id]
    transport, send = build([owner])
    monkeypatch.setattr(transport.capabilities, "supports_proactive_send", False)
    state = _routed_state(tmp_path, monkeypatch, transport_id, transport)
    _push(state)
    await state.notification_bridge.drain()
    send.assert_not_awaited()
    bridged = [d for d in sel_log.deliveries if d.get("tool_kind") == "notification_bridge"]
    assert [d["outcome"] for d in bridged] == ["skipped"]


@pytest.mark.asyncio
@pytest.mark.parametrize("transport_id", sorted(_TRANSPORTS))
async def test_a_refused_send_is_an_audited_error(tmp_path, monkeypatch, sel_log, transport_id):
    build, owner = _TRANSPORTS[transport_id]
    transport, _ = build([owner])
    monkeypatch.setattr(
        messaging,
        "_deliver_channel_dm",
        AsyncMock(return_value=(False, "channel_not_permitted", "denied")),
    )
    state = _routed_state(tmp_path, monkeypatch, transport_id, transport)
    _push(state)
    await state.notification_bridge.drain()
    bridged = [d for d in sel_log.deliveries if d.get("tool_kind") == "notification_bridge"]
    assert [(d["outcome"], d["error"]) for d in bridged] == [("error", "RuntimeError")]


class TestRender:
    def test_critical_note_is_marked_and_carries_its_link(self):
        text = render_transport_text(
            {"priority": "critical", "title": "Job failed", "body": "details", "url": "/chat/a"}
        )
        assert text.split("\n") == ["🚨 **Job failed**", "details", "/chat/a"]

    def test_default_note_has_no_marker_and_skips_empty_fields(self):
        assert render_transport_text({"priority": "default", "title": "t"}) == "**t**"
