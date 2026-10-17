"""The owner-DM leg authorizes the recipient against the RESOLVED conversation.

``_deliver_channel_dm`` addresses a ``configured_targets()`` entry, whose id is the
configured-target spelling (``user:<id>``), not a conversation id. ``may_send_to`` is
a recipient predicate over conversation ids, so a transport that judges the
conversation id itself (Telegram: a private ``chat_id`` IS the peer's user id) can
never match ``user:<id>`` against a roster of bare ids. The leg therefore runs the
ladder for governance and capability only, resolves the target, and then decides the
recipient on the id the transport actually sends to.

Driven on the REAL Telegram transport and the real ladder: a permissive double would
answer ``may_send_to`` with True for any spelling and could not tell the two apart.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard.handlers import messaging as mod
from kiro_crew.telegram.transport import TelegramTransport


def _state(tmp_path, monkeypatch, transport: Any):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.register_channel_transport(transport)
    return state


def _recorded_sel(monkeypatch) -> list[dict[str, Any]]:
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_runner.sel",
        lambda: type("S", (), {"log_api_access": lambda self, **kw: recorded.append(kw)})(),
    )
    return recorded


def _telegram(allowed: list[int]) -> tuple[TelegramTransport, MagicMock]:
    client = MagicMock()
    client.send_message = AsyncMock(return_value=7)
    return TelegramTransport(client, allowed_user_ids=allowed), client


@pytest.mark.asyncio
async def test_an_allow_listed_telegram_owner_receives_the_dm(tmp_path, monkeypatch):
    recorded = _recorded_sel(monkeypatch)
    transport, client = _telegram([123])
    state = _state(tmp_path, monkeypatch, transport)

    sent, code, detail = await mod._deliver_channel_dm(
        state, "telegram", "report ready", caller_session=""
    )

    assert (sent, code, detail) == (True, "", "")
    client.send_message.assert_awaited_once()
    assert client.send_message.await_args.args[:2] == (123, "report ready")
    assert ("channel.proactive_send_authorize", "denied") not in [
        (kw["operation"], kw["outcome"]) for kw in recorded
    ]


@pytest.mark.asyncio
async def test_a_resolved_conversation_off_the_roster_is_refused_and_audited(tmp_path, monkeypatch):
    """The recipient check still runs: it judges the resolved id, and refuses it.

    A resolver that yields a conversation the roster does not hold (a roster
    narrowed between the two reads, or a resolver that does not consult it) must
    not be sent to, and the denial lands on the SEL trail.
    """
    recorded = _recorded_sel(monkeypatch)
    transport, client = _telegram([123])
    transport.resolve_configured_target = AsyncMock(return_value=("999", None))
    state = _state(tmp_path, monkeypatch, transport)

    sent, code, _ = await mod._deliver_channel_dm(
        state, "telegram", "report ready", caller_session=""
    )

    assert (sent, code) == (False, "channel_not_permitted")
    client.send_message.assert_not_awaited()
    transport.resolve_configured_target.assert_awaited_once_with("user:123")
    assert ("channel.proactive_send_authorize", "denied") in [
        (kw["operation"], kw["outcome"]) for kw in recorded
    ]


@pytest.mark.asyncio
async def test_the_recipient_check_gets_the_resolved_id_and_the_target_principal(
    tmp_path, monkeypatch
):
    """Both inputs a transport may judge on: the conversation it sends to, and the
    peer the configured target names (Discord and Webex test the principal)."""
    transport, _ = _telegram([123])
    transport.resolve_configured_target = AsyncMock(return_value=("C-123", "T-1"))
    seen: list[tuple[str, str | None, str]] = []

    def _may_send_to(conversation_id, thread_id=None, *, principal=""):
        seen.append((conversation_id, thread_id, principal))
        return True

    transport.may_send_to = _may_send_to
    transport.send_message = AsyncMock(return_value="7")
    state = _state(tmp_path, monkeypatch, transport)

    sent, code, _ = await mod._deliver_channel_dm(
        state, "telegram", "report ready", caller_session=""
    )

    assert (sent, code) == (True, "")
    assert seen == [("C-123", "T-1", "123")]


@pytest.mark.asyncio
async def test_a_discord_owner_receives_the_dm_on_the_opened_channel(tmp_path, monkeypatch):
    """Discord judges the PEER: the resolved id is a DM channel unrelated to the
    user snowflake on the roster, so the check passes on the target's principal."""
    from kiro_crew.discord.transport import DiscordTransport

    client = MagicMock()
    client.create_dm_channel = AsyncMock(return_value="999000111222333444")
    client.send_message = AsyncMock(return_value="1")
    transport = DiscordTransport(client, allowed_user_ids=["424242424242424242"])
    state = _state(tmp_path, monkeypatch, transport)

    sent, code, _ = await mod._deliver_channel_dm(
        state, "discord", "report ready", caller_session=""
    )

    assert (sent, code) == (True, "")
    client.create_dm_channel.assert_awaited_once_with("424242424242424242")
    client.send_message.assert_awaited_once_with("999000111222333444", "report ready")


@pytest.mark.asyncio
async def test_a_webex_owner_receives_the_dm(tmp_path, monkeypatch):
    """Webex resolves a DM target to the email itself and judges the principal
    against the same lowercased roster ``authorize`` uses."""
    from kiro_crew.webex.transport import WebexTransport

    client = MagicMock()
    client.send_message = AsyncMock(return_value="m-1")
    transport = WebexTransport(client, allowed_emails=["Owner@example.invalid"])
    state = _state(tmp_path, monkeypatch, transport)

    sent, code, _ = await mod._deliver_channel_dm(state, "webex", "report ready", caller_session="")

    assert (sent, code) == (True, "")
    client.send_message.assert_awaited_once_with(
        "owner@example.invalid", "report ready", parent_id=None
    )


@pytest.mark.asyncio
async def test_a_raising_recipient_check_fails_closed(tmp_path, monkeypatch):
    transport, client = _telegram([123])

    def _boom(conversation_id, thread_id=None, *, principal=""):
        raise RuntimeError("roster unavailable")

    transport.may_send_to = _boom
    state = _state(tmp_path, monkeypatch, transport)

    sent, code, _ = await mod._deliver_channel_dm(
        state, "telegram", "report ready", caller_session=""
    )

    assert (sent, code) == (False, "channel_not_permitted")
    client.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_governance_denial_still_stops_before_resolution(tmp_path, monkeypatch):
    """Governance is vetted BEFORE the target resolves, as resolution can itself be
    a visible side effect (Discord opens a DM channel over REST)."""
    monkeypatch.setattr(
        "kiro_crew.platform.governance_profiles.governance_permits",
        lambda *a, **kw: SimpleNamespace(permitted=False),
    )
    transport, client = _telegram([123])
    transport.resolve_configured_target = AsyncMock(return_value=("123", None))
    state = _state(tmp_path, monkeypatch, transport)

    sent, code, _ = await mod._deliver_channel_dm(
        state, "telegram", "report ready", caller_session=""
    )

    assert (sent, code) == (False, "channel_not_permitted")
    transport.resolve_configured_target.assert_not_awaited()
    client.send_message.assert_not_awaited()
