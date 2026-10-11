"""Telegram owner-DM fallback in the send_message proactive delivery path.

When Slack is absent (no slack_client) and a cron-originated send arrives,
the fallback must deliver to the owner's Telegram DM via the registered
TelegramTransport rather than degrading to notification-only.

Covered cases:
- Telegram fires when Slack is absent and send_to_slack is True (cron default).
- Telegram fires when Slack was attempted but failed.
- Telegram does NOT fire when Slack already succeeded.
- delivered_to reports "telegram" and response body carries "telegram: True".
- When Telegram also fails the send, it is logged but does not raise (soft miss).
- When no TelegramTransport is registered, falls through to notification-only.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.dashboard.messaging_api.proactive_send import (
    _SendMessageOutcome,
    _deliver_send_message_fallback,
    _send_message_response,
)
from kiro_crew.telegram.transport import TelegramTransport


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_tg_transport(owner_id: int = 123456789) -> TelegramTransport:
    """A TelegramTransport with a fake client and one allowed (owner) user."""
    client = MagicMock()
    client.send_message = AsyncMock(return_value=42)
    return TelegramTransport(client, allowed_user_ids=[owner_id])


def _make_state(
    *,
    slack_client=None,
    owner_id: str = "U_OWNER",
    tg_transport: TelegramTransport | None = None,
) -> MagicMock:
    state = MagicMock()
    state.slack_client = slack_client
    state.owner_id = owner_id
    if tg_transport is not None:
        state.get_channel_transport.return_value = tg_transport
    else:
        state.get_channel_transport.return_value = None
    state.notify = MagicMock()
    return state


def _fallback_kwargs(**overrides) -> dict:
    """Default kwargs for _deliver_send_message_fallback — override as needed."""
    defaults = dict(
        body={},
        text="Morning summary ready.",
        title="morning-summary",
        blocks=None,
        options=[],
        target_channel="",
        target_user="",
        thread_ts=None,
        reply_broadcast=None,
        target_session="",
        job_name="morning-summary",
        channel_target="",
        channel_type="",
        caller_session="cron:abc123",
        declared_session="",
        is_cron_caller=True,
        send_to_slack=True,
    )
    defaults.update(overrides)
    return defaults


# ── Telegram fires when Slack is absent ──────────────────────────────────────


class TestTelegramFallbackNoSlack:
    @pytest.mark.asyncio
    async def test_delivers_to_telegram_when_no_slack_client(self):
        tg = _make_tg_transport()
        state = _make_state(slack_client=None, tg_transport=tg)
        outcome = _SendMessageOutcome()

        with patch("kiro_crew.sel.sel"):
            await _deliver_send_message_fallback(
                state, outcome, **_fallback_kwargs()
            )

        assert outcome.sent_telegram is True
        tg.client.send_message.assert_awaited_once()
        call_args = tg.client.send_message.call_args
        # chat_id is the owner's Telegram user_id (== private DM chat_id)
        assert call_args[0][0] == 123456789
        # plain text: title + "\n\n" + text
        assert "morning-summary" in call_args[0][1]
        assert "Morning summary ready." in call_args[0][1]

    @pytest.mark.asyncio
    async def test_delivered_to_is_telegram_in_response(self):
        tg = _make_tg_transport()
        state = _make_state(slack_client=None, tg_transport=tg)
        outcome = _SendMessageOutcome()

        with patch("kiro_crew.sel.sel"):
            await _deliver_send_message_fallback(
                state, outcome, **_fallback_kwargs()
            )

        resp = _send_message_response(
            outcome,
            sent_session=False,
            channel_target="",
            channel_type="",
        )
        data = await resp.json()
        assert data["ok"] is True
        assert data["telegram"] is True
        assert data["delivered_to"] == "telegram"

    @pytest.mark.asyncio
    async def test_default_title_not_prepended(self):
        """When title is the default 'Agent Message', just send the text."""
        tg = _make_tg_transport()
        state = _make_state(slack_client=None, tg_transport=tg)
        outcome = _SendMessageOutcome()

        with patch("kiro_crew.sel.sel"):
            await _deliver_send_message_fallback(
                state, outcome, **_fallback_kwargs(title="Agent Message")
            )

        call_args = tg.client.send_message.call_args
        sent_text = call_args[0][1]
        assert "Agent Message" not in sent_text
        assert "Morning summary ready." in sent_text


# ── Telegram fires when Slack was attempted and failed ───────────────────────


class TestTelegramFallbackAfterSlackFailure:
    @pytest.mark.asyncio
    async def test_telegram_fires_when_slack_attempted_and_failed(self):
        tg = _make_tg_transport()
        state = _make_state(slack_client=MagicMock(), tg_transport=tg)
        # Simulate Slack was attempted but failed
        outcome = _SendMessageOutcome()
        outcome.slack_attempted = True
        outcome.sent_slack = False

        with patch("kiro_crew.sel.sel"):
            await _deliver_send_message_fallback(
                state, outcome, **_fallback_kwargs()
            )

        assert outcome.sent_telegram is True
        tg.client.send_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_502_not_returned_when_telegram_succeeded_after_slack_failure(self):
        tg = _make_tg_transport()
        state = _make_state(slack_client=MagicMock(), tg_transport=tg)
        outcome = _SendMessageOutcome()
        outcome.slack_attempted = True
        outcome.sent_slack = False

        with patch("kiro_crew.sel.sel"):
            await _deliver_send_message_fallback(
                state, outcome, **_fallback_kwargs()
            )

        # Should NOT return 502 because Telegram succeeded
        resp = _send_message_response(
            outcome,
            sent_session=False,
            channel_target="",
            channel_type="",
        )
        data = await resp.json()
        assert resp.status == 200
        assert data["ok"] is True
        assert data["telegram"] is True


# ── Telegram does NOT fire when Slack already succeeded ──────────────────────


class TestTelegramSkippedWhenSlackSucceeded:
    @pytest.mark.asyncio
    async def test_telegram_not_called_when_slack_succeeded(self):
        tg = _make_tg_transport()
        slack = MagicMock()
        slack.open_dm = AsyncMock(return_value="DM_CHANNEL")
        slack.post_message = AsyncMock(return_value="1712793600.0")
        state = _make_state(slack_client=slack, tg_transport=tg)
        outcome = _SendMessageOutcome()

        with patch("kiro_crew.sel.sel"):
            await _deliver_send_message_fallback(
                state, outcome, **_fallback_kwargs()
            )

        # Slack sent successfully
        assert outcome.sent_slack is True
        # Telegram must not be called
        assert outcome.sent_telegram is False
        tg.client.send_message.assert_not_awaited()


# ── Soft miss: Telegram failure does not raise ───────────────────────────────


class TestTelegramSoftMiss:
    @pytest.mark.asyncio
    async def test_telegram_failure_is_logged_not_raised(self):
        tg = _make_tg_transport()
        tg.client.send_message = AsyncMock(side_effect=Exception("network error"))
        state = _make_state(slack_client=None, tg_transport=tg)
        outcome = _SendMessageOutcome()

        with patch("kiro_crew.sel.sel"):
            # Must not raise
            await _deliver_send_message_fallback(
                state, outcome, **_fallback_kwargs()
            )

        assert outcome.sent_telegram is False


# ── No Telegram transport registered ─────────────────────────────────────────


class TestNoTelegramTransport:
    @pytest.mark.asyncio
    async def test_falls_through_to_notification_when_no_telegram(self):
        state = _make_state(slack_client=None, tg_transport=None)
        outcome = _SendMessageOutcome()

        with patch("kiro_crew.sel.sel"):
            await _deliver_send_message_fallback(
                state, outcome, **_fallback_kwargs()
            )

        assert outcome.sent_telegram is False
        assert outcome.sent_slack is False
        state.notify.assert_called_once()

        resp = _send_message_response(
            outcome,
            sent_session=False,
            channel_target="",
            channel_type="",
        )
        data = await resp.json()
        assert data["delivered_to"] == "notification"
        assert data["telegram"] is False
