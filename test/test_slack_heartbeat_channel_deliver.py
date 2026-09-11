"""Tests for heartbeat ``deliver:slack:<channel>`` delivery (channel, no thread ts).

A HEARTBEAT.md task tagged ``<!-- deliver:slack:C0123ABC -->`` names a channel and
no thread timestamp. That form posts the report as a NEW message in the named
channel; a tag with an empty channel id falls back to the owner's DM rather than
posting to channel ``""``.

The tag is agent-writable, so the named channel must clear the tracked-channel
allowlist (or be the owner's own DM channel) before an unattended report is
published into it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

_CHANNEL = "C0123ABC"
_OWNER_DM = "D0OWNER"
_NEW_TS = "1700000000.000100"
_THREAD_TS = "1699999999.000200"


@pytest.fixture()
def sel_mock(monkeypatch):
    """Capture the SEL audit trail instead of writing the real one."""
    audit = MagicMock()
    monkeypatch.setattr("kiro_crew.slack.gateway.sel", lambda: audit)
    return audit


@pytest.fixture()
def tracked(monkeypatch):
    """``_CHANNEL`` is the one channel the operator allowed egress to."""
    monkeypatch.setattr("kiro_crew.slack.gateway.is_tracked_channel", lambda c: c == _CHANNEL)


@pytest.fixture()
def orchestrator(sel_mock, tracked):
    """GatewayOrchestrator with a stub Slack client and an owner DM available.

    Bypasses ``__init__`` via ``__new__``, like the sibling heartbeat tests: if
    ``__init__`` grows an attribute ``_deliver_result`` reads, update this.
    """
    from kiro_crew.slack.gateway import GatewayOrchestrator

    orch = GatewayOrchestrator.__new__(GatewayOrchestrator)
    orch.dashboard_state = MagicMock()
    orch.slack = MagicMock()
    orch.slack.post_message = AsyncMock(return_value=_NEW_TS)
    orch.slack.open_dm = AsyncMock(return_value=_OWNER_DM)
    orch._owner_id = "U0OWNER"
    return orch


class TestChannelOnlyDelivery:
    """``slack:<channel>`` posts to the channel instead of DMing the owner."""

    @pytest.mark.asyncio()
    async def test_posts_to_named_channel_not_owner_dm(self, orchestrator):
        await orchestrator._deliver_result(
            "💓 Heartbeat", "pipeline check", "all green", f"slack:{_CHANNEL}"
        )
        orchestrator.slack.open_dm.assert_not_called()
        orchestrator.slack.post_message.assert_awaited_once()
        args = orchestrator.slack.post_message.await_args.args
        assert args[0] == _CHANNEL
        assert "all green" in args[1]
        # New top-level channel message, so no thread timestamp.
        assert args[2] is None

    @pytest.mark.asyncio()
    async def test_continuation_parts_thread_under_the_first_post(self, orchestrator):
        """A split report must not become N top-level channel messages."""
        long_text = "green " * 2000  # > SLACK_MSG_LIMIT, so render_for_slack splits
        await orchestrator._deliver_result(
            "💓 Heartbeat", "pipeline check", long_text, f"slack:{_CHANNEL}"
        )
        calls = orchestrator.slack.post_message.await_args_list
        assert len(calls) > 1, "expected the report to split into several parts"
        assert calls[0].args[2] is None
        assert all(c.args[0] == _CHANNEL for c in calls)
        assert all(c.args[2] == _NEW_TS for c in calls[1:])

    @pytest.mark.asyncio()
    async def test_delivery_is_audited(self, orchestrator, sel_mock):
        await orchestrator._deliver_result(
            "💓 Heartbeat", "pipeline check", "all green", f"slack:{_CHANNEL}"
        )
        sel_mock.log_api_access.assert_called_once()
        kwargs = sel_mock.log_api_access.call_args.kwargs
        assert kwargs["operation"] == "heartbeat_channel_deliver"
        assert kwargs["outcome"] == "approved"
        assert f"channel={_CHANNEL}" in kwargs["resources"]

    @pytest.mark.asyncio()
    async def test_dashboard_notification_still_fires(self, orchestrator):
        await orchestrator._deliver_result(
            "💓 Heartbeat", "pipeline check", "all green", f"slack:{_CHANNEL}"
        )
        orchestrator.dashboard_state.notify.assert_called_once()
        assert orchestrator.dashboard_state.notify.call_args.args[0] == "heartbeat"


class TestThreadDeliveryUnchanged:
    """``slack:<channel>:<ts>`` keeps replying in the thread."""

    @pytest.mark.asyncio()
    async def test_thread_ts_is_used_for_every_part(self, orchestrator):
        await orchestrator._deliver_result(
            "💓 Heartbeat", "pipeline check", "green " * 2000, f"slack:{_CHANNEL}:{_THREAD_TS}"
        )
        calls = orchestrator.slack.post_message.await_args_list
        assert len(calls) > 1
        assert all(c.args == (_CHANNEL, c.args[1], _THREAD_TS) for c in calls)
        orchestrator.slack.open_dm.assert_not_called()


class TestMalformedTagFallsBackToDm:
    """A tag with no channel id must not post to channel ``""``."""

    @pytest.mark.asyncio()
    async def test_empty_channel_dms_the_owner(self, orchestrator, sel_mock):
        await orchestrator._deliver_result("💓 Heartbeat", "pipeline check", "all green", "slack:")
        orchestrator.slack.open_dm.assert_awaited_once()
        orchestrator.slack.post_message.assert_awaited_once()
        assert orchestrator.slack.post_message.await_args.args[0] == _OWNER_DM

    @pytest.mark.asyncio()
    async def test_empty_channel_denial_is_still_audited(self, orchestrator, sel_mock):
        """The empty-channel denial gets a SEL record like every other denial.

        ``deliver="slack:"`` is agent-writable (HEARTBEAT.md is not fenced), so this
        is the one denial shape an agent can author at will — and gating the audit
        on a non-empty channel made it the one shape that left no trace. A routing
        decision that cannot be reconstructed from the audit trail is the gap here,
        not the fallback itself, which was always correct.
        """
        await orchestrator._deliver_result("💓 Heartbeat", "pipeline check", "all green", "slack:")
        kwargs = sel_mock.log_api_access.call_args.kwargs
        assert kwargs["outcome"] == "denied"
        assert kwargs["operation"] == "heartbeat_channel_deliver"
        # `channel=` with nothing after it is the record's "none supplied".
        assert "channel=," in kwargs["resources"]


class TestUngovernedTargetIsRefused:
    """The tag is agent-writable, so the target clears the allowlist or it does not post."""

    @pytest.mark.asyncio()
    async def test_untracked_channel_falls_back_to_owner_dm(self, orchestrator, sel_mock):
        await orchestrator._deliver_result(
            "💓 Heartbeat", "pipeline check", "all green", "slack:C0OTHERTEAM"
        )
        orchestrator.slack.post_message.assert_awaited_once()
        assert orchestrator.slack.post_message.await_args.args[0] == _OWNER_DM
        assert sel_mock.log_api_access.call_args.kwargs["outcome"] == "denied"

    @pytest.mark.asyncio()
    async def test_user_id_target_is_refused(self, orchestrator, sel_mock):
        """A ``U...`` id is not a channel id; Slack would resolve it to an IM."""
        await orchestrator._deliver_result(
            "💓 Heartbeat", "pipeline check", "all green", "slack:U0VICTIM99"
        )
        orchestrator.slack.post_message.assert_awaited_once()
        assert orchestrator.slack.post_message.await_args.args[0] == _OWNER_DM
        assert sel_mock.log_api_access.call_args.kwargs["outcome"] == "denied"

    @pytest.mark.asyncio()
    async def test_owner_dm_origin_thread_still_replies(self, orchestrator, sel_mock):
        """A DM channel is never tracked, so the owner's own DM is admitted by identity."""
        await orchestrator._deliver_result(
            "💓 Heartbeat", "pipeline check", "all green", f"slack:{_OWNER_DM}:{_THREAD_TS}"
        )
        orchestrator.slack.post_message.assert_awaited_once()
        assert orchestrator.slack.post_message.await_args.args == (
            _OWNER_DM,
            orchestrator.slack.post_message.await_args.args[1],
            _THREAD_TS,
        )
        assert sel_mock.log_api_access.call_args.kwargs["outcome"] == "approved"
