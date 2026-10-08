"""The Slack approval window comes from the dashboard's shared resolver.

Slack used a flat 120 s window no matter what the config said. Both Slack waits
now ask ``dashboard.turn_dispatch.tool_approval_timeout_secs()`` -- the same
resolver the dashboard uses, with its config read, turn-ceiling bound and
remaining-budget bound -- unless a test pins ``_APPROVAL_TIMEOUT``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from kiro_crew.slack import handler
from kiro_crew.slack import renderer as slack_renderer


def _resolver_double(window: float):
    """Stand in for ``tool_approval_timeout_secs``: count calls, return *window*."""
    calls: list[None] = []

    def _resolve() -> float:
        calls.append(None)
        return window

    return _resolve, calls


def _expiring_wait_for(seen: list[float]):
    """Honour the wait_for contract for an unanswered prompt without sleeping."""

    async def _wait_for(fut, timeout):
        seen.append(timeout)
        fut.cancel()
        raise asyncio.TimeoutError

    return _wait_for


def test_slack_modules_bind_the_dashboard_resolver():
    from kiro_crew.dashboard import turn_dispatch

    assert handler.tool_approval_timeout_secs is turn_dispatch.tool_approval_timeout_secs
    assert slack_renderer.tool_approval_timeout_secs is turn_dispatch.tool_approval_timeout_secs


def test_unpinned_default_is_no_fixed_window():
    assert handler._APPROVAL_TIMEOUT is None


class TestDecider:
    @pytest.mark.asyncio
    async def test_waits_for_the_resolved_window(self, monkeypatch):
        resolve, calls = _resolver_double(900.0)
        monkeypatch.setattr(slack_renderer, "tool_approval_timeout_secs", resolve)
        monkeypatch.setattr(slack_renderer, "_APPROVAL_TIMEOUT", None)
        seen: list[float] = []
        monkeypatch.setattr(slack_renderer.asyncio, "wait_for", _expiring_wait_for(seen))
        decider = slack_renderer.SlackApprovalDecider(session_key="slack:C1:t1")
        event = SimpleNamespace(request_id="r1", title="bash", options=[])
        assert await decider(event) is False
        assert seen == [900.0]
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_pin_wins_over_the_resolver(self, monkeypatch):
        resolve, calls = _resolver_double(900.0)
        monkeypatch.setattr(slack_renderer, "tool_approval_timeout_secs", resolve)
        monkeypatch.setattr(slack_renderer, "_APPROVAL_TIMEOUT", 0.25)
        seen: list[float] = []
        monkeypatch.setattr(slack_renderer.asyncio, "wait_for", _expiring_wait_for(seen))
        decider = slack_renderer.SlackApprovalDecider(session_key="slack:C1:t2")
        event = SimpleNamespace(request_id="r2", title="bash", options=[])
        assert await decider(event) is False
        assert seen == [0.25]
        assert calls == []


class TestNativeArm:
    @pytest.mark.asyncio
    async def test_waits_and_reports_the_resolved_window(self, monkeypatch):
        resolve, calls = _resolver_double(600.0)
        monkeypatch.setattr(handler, "tool_approval_timeout_secs", resolve)
        monkeypatch.setattr(handler, "_APPROVAL_TIMEOUT", None)
        monkeypatch.setattr(handler, "_build_approval_blocks", lambda *_a, **_k: [])
        monkeypatch.setattr(handler, "Stats", lambda: SimpleNamespace(inc_tool_denial=lambda: None))
        seen: list[float] = []
        monkeypatch.setattr(handler.asyncio, "wait_for", _expiring_wait_for(seen))
        reasons: list[str] = []
        rejected: list[str] = []

        async def _steer(_provider, _event, reason, **_kwargs):
            reasons.append(reason)

        async def _reject(request_id):
            rejected.append(request_id)

        async def _post_blocks(*_args, **_kwargs):
            return "1000.0001"

        async def _delete(*_args, **_kwargs):
            return None

        monkeypatch.setattr(handler, "_steer_host_deny", _steer)
        slack = SimpleNamespace(post_blocks=_post_blocks, delete_message=_delete)
        provider = SimpleNamespace(reject_tool=_reject)
        event = SimpleNamespace(request_id="r3", title="bash")
        outcome = await handler._request_approval(slack, provider, "D1", "1.0", event, "s")
        assert outcome == handler._OUTCOME_REJECTED
        assert seen == [600.0]
        assert len(calls) == 1
        assert reasons == ["the Slack approval prompt went unanswered for 600s"]
        assert rejected == ["r3"]
