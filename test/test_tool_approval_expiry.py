"""A tool approval that expires unanswered is the host's decline, not a person's no.

A cron turn (or any other background source) asks ``_interactive_approval``,
which waits a short window on Slack and the dashboard. When nobody answers, the
window closing answers ``False`` -- the same value a person's Deny gives -- so
``_resolve_permission`` must hear from the approver which one it was. An expiry
is audited ``approval_expired`` and steers the approval-timeout notice before the
reject; a person's Deny keeps its unsteered ``interactive_rejected`` row.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from dashboard_approval_helpers import _make_state

# Imported before any test here patches ``kiro_crew.sel.sel``: the gateway binds
# ``sel`` by name at import (see ``test_llm_helpers_deny_notice``).
import kiro_crew.slack.gateway as gateway_mod
from kiro_crew import llm_helpers
from kiro_crew.llm_helpers import ToolApprovalPolicy, _resolve_permission
from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, LLMEvent
from kiro_crew.testing.wait import async_wait_until, await_until, finished

#: The approval-timeout notice's clause (``deny_notice._DENY_CAUSE_TEXT``).
_EXPIRED_CLAUSE = "its approval prompt expired unanswered"


class _Provider:
    """Permission-answering double recording steer/reject order."""

    def __init__(self) -> None:
        self.supports_steer = True
        self.supports_refusal_steer = True
        self.calls: list[str] = []
        self.steered: list[str] = []

    async def steer(self, message: str) -> bool:
        self.calls.append("steer")
        self.steered.append(message)
        return True

    async def approve_tool(self, request_id) -> None:
        self.calls.append("approve")

    async def reject_tool(self, request_id) -> None:
        self.calls.append("reject")


def _event() -> LLMEvent:
    return LLMEvent(kind=EVENT_PERMISSION_REQUEST, title="Append to File notes.md", request_id="r1")


@pytest.fixture
def rejections(monkeypatch) -> list[str]:
    """The ``reason`` of every ``rejected`` tool row ``_resolve_permission`` writes."""
    reasons: list[str] = []

    def _row(**kw) -> None:
        if kw.get("outcome") == "rejected":
            reasons.append((kw.get("metadata") or {}).get("reason", ""))

    sel = MagicMock()
    sel.log_tool_invocation = MagicMock(side_effect=_row)
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: sel)
    return reasons


async def _resolve(provider: _Provider, approver) -> bool:
    return await finished(
        _resolve_permission(
            provider, _event(), ToolApprovalPolicy.HOOK_BASED, None, on_tool_approval=approver
        ),
        "the permission request to resolve",
    )


class TestTheResolverTellsAnExpiryFromARefusal:
    @pytest.mark.asyncio
    async def test_an_expired_prompt_is_audited_and_steered_as_the_hosts(self, rejections):
        provider = _Provider()

        async def _window_closed(event) -> bool:
            llm_helpers.note_approval_expired()
            return False

        assert await _resolve(provider, _window_closed) is False
        assert rejections == ["approval_expired"]
        assert provider.calls == ["steer", "reject"]
        assert _EXPIRED_CLAUSE in provider.steered[0]

    @pytest.mark.asyncio
    async def test_a_persons_no_stays_an_unsteered_rejection(self, rejections):
        provider = _Provider()

        async def _say_no(event) -> bool:
            return False

        assert await _resolve(provider, _say_no) is False
        assert rejections == ["interactive_rejected"]
        assert provider.calls == ["reject"]

    @pytest.mark.asyncio
    async def test_a_mark_reaches_only_the_request_whose_wait_made_it(self, rejections):
        # Outside any wait the mark has nowhere to go, and one request's expiry
        # must not turn the next request's refusal into an expiry.
        llm_helpers.note_approval_expired()

        async def _window_closed(event) -> bool:
            llm_helpers.note_approval_expired()
            return False

        async def _say_no(event) -> bool:
            return False

        await _resolve(_Provider(), _say_no)
        await _resolve(_Provider(), _window_closed)
        await _resolve(_Provider(), _say_no)
        assert rejections == ["interactive_rejected", "approval_expired", "interactive_rejected"]


def _cron_approver(monkeypatch, state, *, slack=None):
    """The real ``_interactive_approval("cron")`` closure over a fake gateway."""
    monkeypatch.setattr(
        gateway_mod, "safety_override", lambda: SimpleNamespace(is_active=lambda: False)
    )
    fake = SimpleNamespace(
        slack=slack,
        _owner_id="U123" if slack is not None else "",
        dashboard_state=state,
        _cfg=SimpleNamespace(hooks={}),
        _approval_mode="interactive",
        autonudge_svc=None,
        sessions=None,
    )
    return gateway_mod.GatewayOrchestrator._interactive_approval(
        fake,  # type: ignore[arg-type]
        "cron",
        requester="cron job j1, agent notes: nightly notes",
    )


class TestACronPromptNobodyAnswers:
    """End to end: the cron approver's own expiry reaches the resolver's audit.

    Red before: both expiries below were audited ``interactive_rejected`` and
    rejected with no notice, so the model was told the user said no.
    """

    @pytest.mark.asyncio
    async def test_a_dashboard_prompt_that_expires_is_the_hosts(
        self, monkeypatch, tmp_path, rejections
    ):
        state, _client = _make_state(tmp_path)
        monkeypatch.setattr(state, "_BACKGROUND_APPROVAL_TIMEOUT_SECS", 0)
        provider = _Provider()

        assert await _resolve(provider, _cron_approver(monkeypatch, state)) is False
        assert rejections == ["approval_expired"]
        assert provider.calls == ["steer", "reject"]
        assert _EXPIRED_CLAUSE in provider.steered[0]

    @pytest.mark.asyncio
    async def test_a_slack_prompt_that_expires_is_the_hosts(self, monkeypatch, rejections):
        monkeypatch.setattr(gateway_mod.DashboardState, "_BACKGROUND_APPROVAL_TIMEOUT_SECS", 0)
        slack = MagicMock()
        slack.open_dm = AsyncMock(return_value="D123")
        slack.post_blocks = AsyncMock(return_value="1.2")
        slack.update_message = AsyncMock()
        unanswered = asyncio.Event()

        async def _parked(*_args: object, **_kwargs: object) -> bool:
            await unanswered.wait()
            return True

        state = MagicMock()
        state._slots = {}
        state.request_approval = _parked
        provider = _Provider()

        assert await _resolve(provider, _cron_approver(monkeypatch, state, slack=slack)) is False
        assert rejections == ["approval_expired"]
        assert provider.calls == ["steer", "reject"]

    @pytest.mark.asyncio
    async def test_a_persons_deny_on_the_dashboard_stays_a_rejection(
        self, monkeypatch, tmp_path, rejections
    ):
        state, _client = _make_state(tmp_path)
        provider = _Provider()
        task = asyncio.ensure_future(_resolve(provider, _cron_approver(monkeypatch, state)))
        try:
            await async_wait_until(
                lambda: bool(state._pending_approvals),
                describe=lambda: "the cron prompt never reached the dashboard",
            )
            (approval_id,) = state._pending_approvals
            state.resolve_approval(approval_id, False)

            assert await await_until(task, what="the denied request to resolve") is False
        finally:
            task.cancel()
        assert rejections == ["interactive_rejected"]
        assert provider.calls == ["reject"]
