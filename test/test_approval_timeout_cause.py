"""Every interactive approval decider tells the TurnDriver WHY it denied.

The driver steers ``DENY_CAUSE_APPROVAL_TIMEOUT`` into the running turn before
it rejects (``test_messaging_driver.TestApprovalTimeoutInbandNotice``); that only
corrects the model's attribution if the channel deciders actually record the
cause when their prompt expires -- and record NOTHING when a person answered,
because steering "expired" over a real Deny would be the opposite lie. Each
shipped decider is pinned here on both sides of that contract.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from kiro_crew import constants
from kiro_crew.constants import DENY_CAUSE_APPROVAL_TIMEOUT
from kiro_crew.messaging import approval
from kiro_crew.messaging.approval import (
    DENY,
    PendingApprovals,
    SessionApprovalDecider,
    TextReplyApprovalDecider,
    deliver_verdict,
    open_approval,
    reset_for_tests,
)

SESSION = "whatsapp:kirocrew:direct:447700900000"


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_for_tests()
    yield
    reset_for_tests()


def _event(request_id: str = "req-1") -> SimpleNamespace:
    return SimpleNamespace(request_id=request_id, title="bash", options=[])


class TestTheCauseNamesAreShared:
    def test_the_dashboard_and_the_messaging_core_agree_on_the_cause_name(self):
        """The driver compares the decider's cause against the constants leaf
        and the dashboard builds the wording from the same name; a drift between
        them would silently steer nothing."""
        from kiro_crew.dashboard import state

        assert state.DENY_CAUSE_APPROVAL_TIMEOUT == constants.DENY_CAUSE_APPROVAL_TIMEOUT
        assert state.DENY_CAUSE_POLICY == constants.DENY_CAUSE_POLICY
        assert DENY_CAUSE_APPROVAL_TIMEOUT in state._DENY_CAUSE_TEXT


class TestTextReplyDecider:
    @pytest.mark.asyncio
    async def test_silence_records_the_timeout_cause(self):
        open_approval(SESSION, "req-1")
        decider = TextReplyApprovalDecider(SESSION, timeout_s=0.05)
        assert decider.last_deny_cause == ""
        assert await decider(_event()) is False
        assert decider.last_deny_cause == DENY_CAUSE_APPROVAL_TIMEOUT

    @pytest.mark.asyncio
    async def test_a_typed_denial_records_no_cause(self):
        open_approval(SESSION, "req-1")
        decider = TextReplyApprovalDecider(SESSION, timeout_s=5.0)
        task = asyncio.create_task(decider(_event()))
        await asyncio.sleep(0)
        deliver_verdict(SESSION, DENY)
        assert await task is False
        assert decider.last_deny_cause == ""

    @pytest.mark.asyncio
    async def test_the_cause_is_reset_on_the_next_call(self):
        """A stale cause from an earlier expiry must not label the next
        prompt's real Deny as a timeout."""
        open_approval(SESSION, "req-1")
        decider = TextReplyApprovalDecider(SESSION, timeout_s=0.05)
        assert await decider(_event("req-1")) is False
        assert decider.last_deny_cause == DENY_CAUSE_APPROVAL_TIMEOUT
        open_approval(SESSION, "req-2")
        decider._timeout_s = 5.0
        task = asyncio.create_task(decider(_event("req-2")))
        await asyncio.sleep(0)
        deliver_verdict(SESSION, DENY)
        assert await task is False
        assert decider.last_deny_cause == ""

    @pytest.mark.asyncio
    async def test_an_undelivered_prompt_is_an_immediate_deny_with_no_cause(self):
        """No renderer opened the request (muted conversation): denied at once,
        and NOT as a timeout -- nothing expired."""
        decider = TextReplyApprovalDecider(SESSION, timeout_s=5.0)
        assert await decider(_event()) is False
        assert decider.last_deny_cause == ""


class TestSessionDecider:
    @pytest.mark.asyncio
    async def test_the_window_closing_is_reported_with_its_cause(self, monkeypatch):
        monkeypatch.setattr(approval, "APPROVAL_TIMEOUT_S", 0.01)
        pending = PendingApprovals("webex")
        decider = SessionApprovalDecider(pending, session_key="webex:a@b.com")
        assert await decider(_event(1)) is False
        assert decider.last_deny_cause == DENY_CAUSE_APPROVAL_TIMEOUT
        # The registry's own API keeps its bool contract and exposes the cause.
        assert await pending.decide_with_cause("webex:a@b.com", _event(2)) == (
            False,
            DENY_CAUSE_APPROVAL_TIMEOUT,
        )
        assert await pending.decide("webex:a@b.com", _event(3)) is False

    @pytest.mark.asyncio
    async def test_a_click_records_no_cause(self):
        pending = PendingApprovals("webex")
        decider = SessionApprovalDecider(pending, session_key="webex:a@b.com")
        task = asyncio.create_task(decider(_event(1)))
        await asyncio.sleep(0)
        assert pending.resolve("webex:a@b.com", False, request_id=1) is True
        assert await task is False
        assert decider.last_deny_cause == ""

    @pytest.mark.asyncio
    async def test_an_answer_that_arrived_before_the_wait_carries_no_cause(self):
        """The renderer's reservation was resolved before decide() ran: that is
        the human's decision, adopted as-is."""
        pending = PendingApprovals("webex")
        pending.reserve("webex:a@b.com", 1)
        assert pending.resolve("webex:a@b.com", False, request_id=1) is True
        assert await pending.decide_with_cause("webex:a@b.com", _event(1)) == (False, "")


class TestButtonDeciders:
    """Discord, Slack (transport), Telegram and Teams: one registry-backed
    button decider each, all denying by default when the window closes."""

    @pytest.mark.asyncio
    async def test_discord(self, monkeypatch):
        from kiro_crew.discord import renderer as discord_renderer

        monkeypatch.setattr(discord_renderer, "_APPROVAL_TIMEOUT_S", 0.01)
        decider = discord_renderer.DiscordApprovalDecider(session_key="discord:1")
        assert await decider(_event("r1")) is False
        assert decider.last_deny_cause == DENY_CAUSE_APPROVAL_TIMEOUT
        # A press records no cause.
        monkeypatch.setattr(discord_renderer, "_APPROVAL_TIMEOUT_S", 5.0)
        task = asyncio.create_task(decider(_event("r2")))
        await asyncio.sleep(0)
        discord_renderer.DiscordApprovalDecider._REGISTRY[
            decider.key("discord:1", "r2")
        ].set_result(False)
        assert await task is False
        assert decider.last_deny_cause == ""

    @pytest.mark.asyncio
    async def test_slack_transport(self, monkeypatch):
        from kiro_crew.slack import renderer as slack_renderer

        monkeypatch.setattr(slack_renderer, "_APPROVAL_TIMEOUT", 0.01)
        decider = slack_renderer.SlackApprovalDecider(session_key="slack:C1:t1")
        assert await decider(_event("r1")) is False
        assert decider.last_deny_cause == DENY_CAUSE_APPROVAL_TIMEOUT
        monkeypatch.setattr(slack_renderer, "_APPROVAL_TIMEOUT", 5.0)
        task = asyncio.create_task(decider(_event("r2")))
        await asyncio.sleep(0)
        assert decider.resolve("r2", False) is True
        assert await task is False
        assert decider.last_deny_cause == ""

    @pytest.mark.asyncio
    async def test_telegram(self, monkeypatch):
        from kiro_crew.telegram import renderer as telegram_renderer

        monkeypatch.setattr(telegram_renderer, "_APPROVAL_TIMEOUT_S", 0.01)
        decider = telegram_renderer.TelegramApprovalDecider(session_key="telegram:1")
        assert await decider(_event("r1")) is False
        assert decider.last_deny_cause == DENY_CAUSE_APPROVAL_TIMEOUT
        monkeypatch.setattr(telegram_renderer, "_APPROVAL_TIMEOUT_S", 5.0)
        task = asyncio.create_task(decider(_event("r2")))
        await asyncio.sleep(0)
        telegram_renderer.TelegramApprovalDecider._REGISTRY[
            decider.key("telegram:1", "r2")
        ].set_result(False)
        assert await task is False
        assert decider.last_deny_cause == ""

    @pytest.mark.asyncio
    async def test_teams(self, monkeypatch):
        from kiro_crew.teams import approvals as teams_approvals

        monkeypatch.setattr(teams_approvals, "APPROVAL_TIMEOUT_SECS", 0.01)
        decider = teams_approvals.TeamsApprovalDecider(session_key="teams:1")
        assert await decider(_event("r1")) is False
        assert decider.last_deny_cause == DENY_CAUSE_APPROVAL_TIMEOUT
        monkeypatch.setattr(teams_approvals, "APPROVAL_TIMEOUT_SECS", 5.0)
        task = asyncio.create_task(decider(_event("r2")))
        await asyncio.sleep(0)
        decider._futures["r2"].set_result(False)
        assert await task is False
        assert decider.last_deny_cause == ""
