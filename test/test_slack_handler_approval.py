"""Slack handler tests: who may answer an approval prompt, and Trust.

Allowed-user gating, the transport floor on an approval click, Trust propagating to
subagents, and per-session trust.
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import MagicMock

import pytest
from slack_handler_helpers import _clean_approval_state  # noqa: F401
from slack_handler_helpers import (
    FakeProvider,
    FakeSessionManager,
    _approval_prompt_ts,
    _within_lost_run,
)

from conftest import MockSlackClient
from kiro_crew.providers.base import LLMEvent
from kiro_crew.slack.handler import (
    _pending_approvals,
    _trusted_sessions,
    add_trusted_session,
    handle_interaction,
    handle_message,
    is_slack_session_trusted,
    set_allowed_users,
    set_owner_id,
)


class TestAllowedUsers:
    """Tests for allowed-user authorization in handle_interaction."""

    @pytest.fixture(autouse=True)
    def _reset_globals(self):
        from kiro_crew.slack.handler import _trusted_sessions

        _trusted_sessions.clear()
        yield
        _trusted_sessions.clear()

    @pytest.mark.asyncio
    async def test_allowed_user_can_approve(self):
        """Allowed user's approve action is accepted."""
        set_owner_id("U1")
        set_allowed_users({"U1"})
        slack = MockSlackClient()
        provider = FakeProvider(
            [
                LLMEvent(
                    kind="permission_request",
                    request_id="req-1",
                    title="Tool",
                    options=[{"id": "allow_once", "label": "Allow once"}],
                ),
                LLMEvent(kind="text_chunk", text="done"),
            ]
        )
        sessions = FakeSessionManager(provider)

        async def _click():
            approval_ts = await _approval_prompt_ts(slack)
            await handle_interaction("C1", approval_ts, "approve_tool", user_id="U1")

        await _within_lost_run(
            asyncio.gather(
                handle_message(
                    slack, sessions, "C1", "go", None, "msg1", "U1", approval_mode="interactive"
                ),
                _click(),
            ),
            "the turn and its click",
        )
        assert "req-1" in provider.approved

    @pytest.mark.asyncio
    async def test_unauthorized_user_rejected(self, monkeypatch):
        """Non-allowed user's approve action is silently rejected."""
        set_owner_id("U1")
        set_allowed_users({"U1"})
        import kiro_crew.slack.handler as _h

        monkeypatch.setattr(_h, "_trusted_sessions", type(_h._trusted_sessions)())
        slack = MockSlackClient()
        gate = asyncio.Event()

        class GatedProvider(FakeProvider):
            async def stream(self, message, timeout=120.0):
                yield LLMEvent(
                    kind="permission_request",
                    request_id="req-2",
                    title="Tool",
                    options=[{"id": "allow_once", "label": "Allow once"}],
                )
                await gate.wait()
                yield LLMEvent(kind="text_chunk", text="done")
                yield LLMEvent(kind="complete")

        provider = GatedProvider()
        sessions = FakeSessionManager(provider)

        task = asyncio.ensure_future(
            handle_message(
                slack, sessions, "C1", "go", None, "msg2", "U1", approval_mode="interactive"
            )
        )
        try:
            # The intruder clicks a prompt that is live and registered, so a
            # refusal is the gate's verdict and not a click that arrived early.
            approval_ts = await _approval_prompt_ts(slack)
            # U999 is not in allowed set — should be rejected
            await handle_interaction("C1", approval_ts, "approve_tool", user_id="U999")
            gate.set()
            assert "req-2" not in provider.approved
            # Still the turn's: the refused click claimed nothing.
            assert f"C1:{approval_ts}" in _pending_approvals
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await _within_lost_run(task, "the cancelled turn")

    @pytest.mark.asyncio
    async def test_empty_allowed_users_rejects_all(self, monkeypatch):
        """With no allowed users only the owner passes the gate; a stale click claims nothing."""
        import kiro_crew.slack.handler as _h

        set_allowed_users(set())
        rec = MagicMock()
        monkeypatch.setattr(_h, "sel", lambda: rec)
        _pending_approvals["C1:fake_ts"] = MagicMock()

        def errors() -> list[str]:
            return [c.kwargs["error"] for c in rec.log_api_access.call_args_list]

        # No owner configured: nobody passes.
        monkeypatch.setattr(_h, "_owner_id", "")
        assert await handle_interaction("C1", "fake_ts", "approve_tool", user_id="U1") is None
        # An owner is configured, and the click is someone else's.
        monkeypatch.setattr(_h, "_owner_id", "UOWNER")
        assert await handle_interaction("C1", "fake_ts", "approve_tool", user_id="U1") is None
        assert errors() == ["unauthorized user", "unauthorized user"]
        assert "C1:fake_ts" in _pending_approvals
        # The owner passes the gate, but a click on a prompt with no pending entry
        # is refused rather than approving anything.
        assert await handle_interaction("C1", "gone", "approve_tool", user_id="UOWNER") is None
        assert errors()[-1] == "no_pending_approval"
        assert "C1:fake_ts" in _pending_approvals
        assert [c.kwargs["outcome"] for c in rec.log_api_access.call_args_list] == ["denied"] * 3
        assert [c.kwargs["caller"] for c in rec.log_api_access.call_args_list][:2] == ["U1"] * 2

    @pytest.mark.asyncio
    async def test_w_u_prefix_cross_match(self):
        """User with W-prefix matches U-prefix owner via is_owner cross-match."""
        set_owner_id("U1234")
        set_allowed_users({"U1234"})
        slack = MockSlackClient()
        provider = FakeProvider(
            [
                LLMEvent(
                    kind="permission_request",
                    request_id="req-4",
                    title="Tool",
                    options=[{"id": "allow_once", "label": "Allow once"}],
                ),
                LLMEvent(kind="text_chunk", text="done"),
            ]
        )
        sessions = FakeSessionManager(provider)

        async def _click():
            approval_ts = await _approval_prompt_ts(slack)
            await handle_interaction("C1", approval_ts, "approve_tool", user_id="W1234")

        await _within_lost_run(
            asyncio.gather(
                handle_message(
                    slack,
                    sessions,
                    "C1",
                    "go",
                    None,
                    "msg3",
                    "U1234",
                    approval_mode="interactive",
                ),
                _click(),
            ),
            "the turn and its click",
        )
        assert "req-4" in provider.approved


class _RecordingSessions:
    """Minimal session store recording approval-policy writes."""

    def __init__(self) -> None:
        self.policies: dict[str, str] = {}

    def set_approval_policy(self, key: str, policy: str) -> None:
        self.policies[key] = policy

    def get_approval_policy(self, key: str) -> str:
        return self.policies.get(key, "")


class _ApprovingProvider:
    def __init__(self) -> None:
        self.approved: list[str] = []

    async def approve_tool(self, request_id: str) -> None:
        self.approved.append(request_id)


class _FloorRefusingProvider:
    """approve_tool answers False: the transport's gate refused the call."""

    def __init__(self) -> None:
        self.approved: list[str] = []

    async def approve_tool(self, request_id: str) -> bool:
        self.approved.append(request_id)
        return False


class TestSlackApproveTransportFloor:
    @pytest.mark.asyncio
    async def test_approve_click_refused_by_floor_returns_reject(self):
        """The card must not be relabelled as approved when approve_tool refused."""
        from kiro_crew.slack.handler import _ACTION_REJECT, _OUTCOME_REJECTED, _PendingApproval

        set_owner_id("U1")
        set_allowed_users({"U1"})
        prov = _FloorRefusingProvider()
        pending = _PendingApproval(prov, "req-floor", session_key="chat-floor")
        _pending_approvals["C1:tsf"] = pending

        result = await handle_interaction("C1", "tsf", "approve_tool", user_id="U1")

        assert result == _ACTION_REJECT
        assert prov.approved == ["req-floor"]
        assert pending.future.result() == _OUTCOME_REJECTED


class TestSlackTrustSubagentPropagation:
    """Slack Trust must set the session approval policy so subagents inherit it."""

    @pytest.mark.asyncio
    async def test_trust_sets_session_approval_policy_auto(self):
        from kiro_crew.slack.handler import _PendingApproval

        set_owner_id("U1")
        set_allowed_users({"U1"})
        prov = _ApprovingProvider()
        sessions = _RecordingSessions()
        _pending_approvals["C1:ts1"] = _PendingApproval(prov, "req-1", session_key="chat-1-trust")

        result = await handle_interaction(
            "C1", "ts1", "trust_tool", user_id="U1", sessions=sessions
        )

        assert result == "trust_tool"
        # The fix: subagents read get_approval_policy(parent)=="auto".
        assert sessions.get_approval_policy("chat-1-trust") == "auto"
        assert "chat-1-trust" in _trusted_sessions
        assert "req-1" in prov.approved

    @pytest.mark.asyncio
    async def test_trust_without_sessions_does_not_raise(self):
        from kiro_crew.slack.handler import _PendingApproval

        set_owner_id("U1")
        set_allowed_users({"U1"})
        prov = _ApprovingProvider()
        _pending_approvals["C2:ts2"] = _PendingApproval(prov, "req-2", session_key="chat-2-trust")

        # sessions omitted (e.g. orchestrator not ready) — must stay safe.
        result = await handle_interaction("C2", "ts2", "trust_tool", user_id="U1")

        assert result == "trust_tool"
        assert "chat-2-trust" in _trusted_sessions

    @pytest.mark.asyncio
    async def test_late_trust_click_sets_session_approval_policy_auto(self):
        """Late-click trust path (no pending approval) also propagates the
        policy so subagents inherit it (covers the late-click site)."""
        from unittest.mock import AsyncMock, MagicMock, patch

        set_owner_id("U1")
        set_allowed_users({"U1"})
        sessions = _RecordingSessions()

        slack = MagicMock()
        slack.fetch_thread_replies = AsyncMock(return_value=[{"user": "U1"}])

        fake_map = MagicMock()
        fake_map.get_session_for_thread.return_value = ""  # no override → key is thread_ts

        with patch("kiro_crew.session.SessionMap", return_value=fake_map):
            result = await handle_interaction(
                "C9",
                "ts9",
                "trust_tool",
                user_id="U1",
                thread_ts="thread-9",
                slack=slack,
                sessions=sessions,
            )

        assert result == "trust_tool"
        assert sessions.get_approval_policy("thread-9") == "auto"
        assert "thread-9" in _trusted_sessions


class TestPerSessionTrust:
    """Per-session Trust helpers shared by native + transport approval paths."""

    def test_untrusted_session_is_false(self):
        assert is_slack_session_trusted("thread-1") is False
        assert is_slack_session_trusted("") is False

    def test_add_trusted_session_marks_only_that_session(self):
        add_trusted_session("thread-1")
        assert is_slack_session_trusted("thread-1") is True
        # Trust is scoped to the one session — others stay untrusted.
        assert is_slack_session_trusted("thread-2") is False

    def test_add_trusted_session_propagates_policy_to_subagents(self):
        class _FakeSessions:
            def __init__(self):
                self.policies = {}

            def set_approval_policy(self, key, policy):
                self.policies[key] = policy

        sessions = _FakeSessions()
        add_trusted_session("thread-9", sessions)
        assert is_slack_session_trusted("thread-9") is True
        # Subagents read the parent's approval policy, so trust must propagate.
        assert sessions.policies == {"thread-9": "auto"}

    def test_add_trusted_session_empty_key_is_noop(self):
        add_trusted_session("")
        assert "" not in _trusted_sessions
