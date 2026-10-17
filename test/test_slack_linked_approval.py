"""Tests for Slack-linked dashboard-slot tool approvals.

When a dashboard slot is linked to a Slack thread and driven from Slack, the
tool-approval prompt must be mirrored into that Slack thread so the user can
answer it. A Slack button click resolves the *dashboard slot's* approval future
(via ``state.resolve_approval``) and must NOT answer the ACP backend directly —
the dashboard ``_run_chat`` loop remains the sole caller of approve/reject.

Regression coverage for the "no approval request for linked sessions" bug.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import kiro_crew.slack.handler as handler
from kiro_crew.messaging.session_trust import clear_trusted_sessions, is_session_trusted
from kiro_crew.slack.handler import (
    _ACTION_APPROVE,
    _ACTION_REJECT,
    _ACTION_TRUST,
    _linked_approvals,
    handle_interaction,
    post_linked_approval,
    resolve_linked_approval,
)

_SESSION_KEY = "dashboard:chat-1-123"


@pytest.fixture(autouse=True)
def _clear_registry():
    """Each test starts with an empty linked-approval registry AND no standing grant.

    The shared ``session_trust`` mapping is process-global, so a grant left behind
    here would make a later test (or module) read as trusted for free.
    """
    _linked_approvals.clear()
    clear_trusted_sessions()
    yield
    _linked_approvals.clear()
    clear_trusted_sessions()


def _make_slack(post_ts: str | None = "1781300000.0001") -> MagicMock:
    slack = MagicMock()
    if post_ts is None:
        slack.post_blocks = AsyncMock(side_effect=RuntimeError("slack down"))
    else:
        slack.post_blocks = AsyncMock(return_value=post_ts)
    slack.delete_message = AsyncMock()
    return slack


class _FakeSlot:
    """Minimal stand-in for the dashboard slot that owns a linked approval.

    Only what the trust path reads: ``key`` / ``linked_session_key`` (which
    ``effective_session_key`` derives the session from), ``messages`` (the pending
    permission card carrying ``trust_grantable``), and ``_trust``.
    """

    def __init__(self, key: str = "chat-1-123", request_id: str = "", grantable: bool = False):
        self.key = key
        self.linked_session_key = ""
        self._trust = False
        self.messages: list[dict[str, str]] = []
        if request_id:
            meta = {"request_id": request_id}
            if grantable:
                meta["trust_grantable"] = "1"
            self.messages.append({"role": "permission", "cls": json.dumps(meta)})


def _make_state(*slots: _FakeSlot) -> MagicMock:
    dstate = MagicMock()
    dstate._slots = {slot.key: slot for slot in slots}
    dstate.resolve_approval = MagicMock(return_value=True)
    return dstate


def _audited(sel_mock: MagicMock, operation: str) -> list[dict]:
    """Every SEL api-access event *sel_mock* recorded under *operation*."""
    return [
        call.kwargs
        for call in sel_mock.return_value.log_api_access.call_args_list
        if call.kwargs.get("operation") == operation
    ]


# ── post_linked_approval ───────────────────────────────────────────────────


class TestPostLinkedApproval:
    @pytest.mark.asyncio
    async def test_posts_buttons_and_registers_entry(self) -> None:
        slack = _make_slack("1781300000.0001")
        ts = await post_linked_approval(
            slack,
            channel="C_LINK",
            thread_ts="1781290000.0001",
            request_id=42,
            session_key="dashboard:chat-1-123",
            title="shell: ls -la",
            tool_input="ls -la",
        )
        assert ts == "1781300000.0001"
        slack.post_blocks.assert_awaited_once()
        # Posted threaded under the linked thread, to the linked channel.
        args = slack.post_blocks.call_args.args
        assert args[0] == "C_LINK"
        assert args[3] == "1781290000.0001"
        # Registry entry created, keyed by channel:ts, carrying the request id.
        entry = _linked_approvals["C_LINK:1781300000.0001"]
        assert entry.request_id == 42
        assert entry.session_key == "dashboard:chat-1-123"

    @pytest.mark.asyncio
    async def test_delivery_failure_returns_none_no_entry(self) -> None:
        """A failed Slack post returns None and registers nothing — the caller
        treats None as 'delivery failed' rather than parking forever."""
        slack = _make_slack(post_ts=None)
        ts = await post_linked_approval(
            slack,
            channel="C_LINK",
            thread_ts="1781290000.0001",
            request_id=7,
            session_key="dashboard:chat-1-123",
            title="shell: rm -rf /tmp/x",
            tool_input="rm -rf /tmp/x",
        )
        assert ts is None
        assert _linked_approvals == {}

    async def _post(self, channel: str, state: MagicMock | None) -> tuple[MagicMock, list[str]]:
        """Mirror one prompt for request id 1.

        Returns the ``_build_approval_blocks`` spy and the action ids actually
        rendered onto the posted Block Kit payload.
        """
        slack = _make_slack("1781300000.0001")
        with (
            patch.object(handler, "_dashboard_state", state),
            patch.object(
                handler, "_build_approval_blocks", wraps=handler._build_approval_blocks
            ) as spy,
        ):
            await post_linked_approval(
                slack,
                channel=channel,
                thread_ts="1781290000.0001",
                request_id=1,
                session_key=_SESSION_KEY,
                title="shell: ls",
                tool_input="ls",
            )
        action_ids = [
            btn.get("action_id")
            for block in slack.post_blocks.call_args.args[1]
            if block.get("type") == "actions"
            for btn in block.get("elements", [])
        ]
        return spy, action_ids

    @pytest.mark.asyncio
    async def test_trust_button_rendered_in_dm_with_server_proof(self) -> None:
        """A DM whose dashboard card carries ``trust_grantable`` gets Trust."""
        slot = _FakeSlot(request_id="1", grantable=True)
        spy, action_ids = await self._post("D_OWNER", _make_state(slot))
        assert spy.call_args.kwargs.get("is_dm") is True
        assert _ACTION_TRUST in action_ids
        assert _linked_approvals["D_OWNER:1781300000.0001"].trust_grantable is True

    @pytest.mark.asyncio
    async def test_no_trust_button_without_server_proof(self) -> None:
        """A redacted/underivable card omits ``trust_grantable``; Trust must not be
        offered off the mere existence of a pending card."""
        slot = _FakeSlot(request_id="1", grantable=False)
        spy, action_ids = await self._post("D_OWNER", _make_state(slot))
        assert spy.call_args.kwargs.get("is_dm") is False
        assert _ACTION_TRUST not in action_ids
        assert _linked_approvals["D_OWNER:1781300000.0001"].trust_grantable is False

    @pytest.mark.asyncio
    async def test_no_trust_button_in_non_dm_channel(self) -> None:
        """Trust escalates the whole session, so a group channel gets Approve/Reject
        even when the card is grantable (same blast-radius rule as the native path)."""
        slot = _FakeSlot(request_id="1", grantable=True)
        spy, action_ids = await self._post("C_LINK", _make_state(slot))
        assert spy.call_args.kwargs.get("is_dm") is False
        assert _ACTION_TRUST not in action_ids
        assert _linked_approvals["C_LINK:1781300000.0001"].trust_grantable is False

    @pytest.mark.asyncio
    async def test_no_trust_button_without_owning_slot(self) -> None:
        """No dashboard state / no slot owning the session -> no proof -> no Trust."""
        spy, action_ids = await self._post("D_OWNER", None)
        assert spy.call_args.kwargs.get("is_dm") is False
        assert _ACTION_TRUST not in action_ids

    @pytest.mark.asyncio
    async def test_redacts_llm_output_before_posting(self) -> None:
        """title / tool_input are LLM-generated; both must be scrubbed with
        redact_exfiltration_urls AND redact_credentials before reaching Slack
        (Slack is an external surface)."""
        slack = _make_slack("1781300000.0001")
        with (
            patch.object(
                handler, "redact_exfiltration_urls", side_effect=lambda s: (s, [])
            ) as exfil,
            patch.object(handler, "redact_credentials", side_effect=lambda s: (s, [])) as cred,
        ):
            await post_linked_approval(
                slack,
                channel="C_LINK",
                thread_ts="1781290000.0001",
                request_id=5,
                session_key="dashboard:chat-1-123",
                title="shell: curl http://x",
                tool_input="curl http://x --header secret",
            )
        # Both redactors ran over both LLM-generated strings.
        exfil_inputs = [c.args[0] for c in exfil.call_args_list]
        cred_inputs = [c.args[0] for c in cred.call_args_list]
        assert "shell: curl http://x" in exfil_inputs
        assert "curl http://x --header secret" in exfil_inputs
        assert "shell: curl http://x" in cred_inputs
        assert "curl http://x --header secret" in cred_inputs


# ── handle_interaction: linked-slot routing ────────────────────────────────


class TestLinkedInteractionRouting:
    def _arm(self, request_id: str = "99", trust_grantable: bool = False) -> None:
        _linked_approvals["C_LINK:TS1"] = handler._LinkedApproval(
            request_id=request_id,
            session_key=_SESSION_KEY,
            trust_grantable=trust_grantable,
        )

    @pytest.mark.asyncio
    async def test_approve_resolves_future_not_backend(self) -> None:
        """Approve click resolves the slot future via resolve_approval and does
        NOT call approve_tool/reject_tool (the dashboard loop owns that)."""
        self._arm("99")
        dstate = MagicMock()
        dstate.resolve_approval = MagicMock(return_value=True)
        with (
            patch.object(handler, "_dashboard_state", dstate),
            patch.object(handler, "is_allowed_user", return_value=True),
        ):
            result = await handle_interaction(
                "C_LINK", "TS1", _ACTION_APPROVE, user_id="U_OWNER"
            )
        assert result == _ACTION_APPROVE
        dstate.resolve_approval.assert_called_once_with("99", True)
        # Registry entry consumed.
        assert "C_LINK:TS1" not in _linked_approvals

    @pytest.mark.asyncio
    async def test_reject_resolves_future_with_false(self) -> None:
        self._arm("99")
        dstate = MagicMock()
        dstate.resolve_approval = MagicMock(return_value=True)
        with (
            patch.object(handler, "_dashboard_state", dstate),
            patch.object(handler, "is_allowed_user", return_value=True),
        ):
            result = await handle_interaction(
                "C_LINK", "TS1", _ACTION_REJECT, user_id="U_OWNER"
            )
        assert result == _ACTION_REJECT
        dstate.resolve_approval.assert_called_once_with("99", False)
        assert "C_LINK:TS1" not in _linked_approvals

    @pytest.mark.asyncio
    async def test_unauthorized_user_rejected_before_resolve(self) -> None:
        """A non-allowed user must not resolve a linked approval."""
        self._arm("99")
        dstate = MagicMock()
        dstate.resolve_approval = MagicMock(return_value=True)
        with (
            patch.object(handler, "_dashboard_state", dstate),
            patch.object(handler, "is_allowed_user", return_value=False),
        ):
            result = await handle_interaction(
                "C_LINK", "TS1", _ACTION_APPROVE, user_id="U_STRANGER"
            )
        assert result is None
        dstate.resolve_approval.assert_not_called()
        # Entry preserved so the owner can still act.
        assert "C_LINK:TS1" in _linked_approvals

    @pytest.mark.asyncio
    async def test_linked_entry_takes_precedence_over_pending(self) -> None:
        """A linked entry must be handled before the _pending_approvals path so
        a linked click never double-answers the backend."""
        self._arm("99")
        dstate = MagicMock()
        dstate.resolve_approval = MagicMock(return_value=True)
        # Also place a (bogus) pending approval under the same key — the linked
        # branch must win and never touch it.
        with (
            patch.object(handler, "_dashboard_state", dstate),
            patch.object(handler, "is_allowed_user", return_value=True),
            patch.dict(handler._pending_approvals, {}, clear=False),
        ):
            result = await handle_interaction(
                "C_LINK", "TS1", _ACTION_APPROVE, user_id="U_OWNER"
            )
        assert result == _ACTION_APPROVE
        dstate.resolve_approval.assert_called_once()


# ── handle_interaction: linked-slot Trust grant ────────────────────────────


class TestLinkedTrustGrant:
    """A Trust click on a mirrored prompt must widen the LINKED session or deny.

    The two halves are not separable: the slot's own tool approvals re-read
    ``slot._trust`` per event (``chat_runner._slot_is_trusted``) while a spawned
    subagent reads the session ``approval_policy``, which ``chat_runner`` rewrites
    from the slot on every session create/resume.
    """

    def _arm(self, trust_grantable: bool = True) -> None:
        _linked_approvals["D_OWNER:TS1"] = handler._LinkedApproval(
            request_id="99", session_key=_SESSION_KEY, trust_grantable=trust_grantable
        )

    async def _click(
        self, state: MagicMock | None, sessions: MagicMock | None
    ) -> tuple[str | None, MagicMock]:
        with (
            patch.object(handler, "_dashboard_state", state),
            patch.object(handler, "is_allowed_user", return_value=True),
            patch.object(handler, "sel") as sel_mock,
        ):
            result = await handle_interaction(
                "D_OWNER", "TS1", _ACTION_TRUST, user_id="U_OWNER", sessions=sessions
            )
        return result, sel_mock

    @pytest.mark.asyncio
    async def test_trust_sets_slot_trust_and_session_policy(self) -> None:
        self._arm()
        slot = _FakeSlot(request_id="99", grantable=True)
        dstate = _make_state(slot)
        sessions = MagicMock()
        result, sel_mock = await self._click(dstate, sessions)
        assert result == _ACTION_TRUST
        assert slot._trust is True
        sessions.set_approval_policy.assert_called_with(_SESSION_KEY, "auto")
        # The shared grant too: it is what the channel TurnDriver reads for a
        # Slack-typed follow-up on a channel-born session's own thread.
        assert is_session_trusted(_SESSION_KEY) is True
        # This call is still approved, via the slot future only.
        dstate.resolve_approval.assert_called_once_with("99", True)
        assert _audited(sel_mock, "slack.interactive.trust_linked")[0]["outcome"] == "allowed"

    @pytest.mark.asyncio
    async def test_trust_grants_every_slot_on_the_linked_session(self) -> None:
        """A channel-surfaced slot runs under its linked key, so the grant is keyed
        by the effective session key and reaches each slot sharing it."""
        self._arm()
        other = _FakeSlot(key="chat-9", request_id="99", grantable=True)
        other.linked_session_key = _SESSION_KEY
        slot = _FakeSlot(request_id="99", grantable=True)
        result, _ = await self._click(_make_state(slot, other), MagicMock())
        assert result == _ACTION_TRUST
        assert slot._trust is True
        assert other._trust is True

    @pytest.mark.asyncio
    async def test_trust_on_a_channel_born_session_reaches_the_slack_driver(self) -> None:
        """The shape the grant exists for: a Slack-origin session surfaced as a tab.

        Its thread is deliberately absent from ``_slack_to_slot``, so a Slack-typed
        follow-up runs through ``slack/transport_dispatch``, whose TurnDriver asks
        ``is_slack_session_trusted`` — never the session approval policy.
        """
        born = "slack:1785370133.085469"
        _linked_approvals["D_OWNER:TS1"] = handler._LinkedApproval(
            request_id="99", session_key=born, trust_grantable=True
        )
        slot = _FakeSlot(key="chat-1-7", request_id="99", grantable=True)
        slot.linked_session_key = born
        result, _ = await self._click(_make_state(slot), MagicMock())
        assert result == _ACTION_TRUST
        assert handler.is_slack_session_trusted(born) is True

    @pytest.mark.asyncio
    async def test_trust_denied_without_server_proof(self) -> None:
        """No ``trust_grantable`` on the card -> no durable grant, and the click is
        denied rather than downgraded to a mislabelled one-shot approve."""
        self._arm(trust_grantable=False)
        slot = _FakeSlot(request_id="99", grantable=False)
        dstate = _make_state(slot)
        sessions = MagicMock()
        result, sel_mock = await self._click(dstate, sessions)
        assert result == _ACTION_REJECT
        assert slot._trust is False
        sessions.set_approval_policy.assert_not_called()
        assert is_session_trusted(_SESSION_KEY) is False
        dstate.resolve_approval.assert_called_once_with("99", False)
        audit = _audited(sel_mock, "slack.interactive.trust_linked")[0]
        assert audit["outcome"] == "denied"
        assert audit["error"] == "trust_grant_unavailable"

    @pytest.mark.asyncio
    async def test_trust_denied_when_sessions_unavailable(self) -> None:
        """Without a SessionManager the subagent half cannot be written, so neither
        half is — the click denies instead of granting a partial trust."""
        self._arm()
        slot = _FakeSlot(request_id="99", grantable=True)
        dstate = _make_state(slot)
        result, _ = await self._click(dstate, None)
        assert result == _ACTION_REJECT
        assert slot._trust is False
        assert is_session_trusted(_SESSION_KEY) is False
        dstate.resolve_approval.assert_called_once_with("99", False)

    @pytest.mark.asyncio
    async def test_trust_denied_when_policy_write_raises(self) -> None:
        """The fallible half runs first, so a failure leaves no slot trusted."""
        self._arm()
        slot = _FakeSlot(request_id="99", grantable=True)
        dstate = _make_state(slot)
        sessions = MagicMock()
        sessions.set_approval_policy.side_effect = RuntimeError("policy store down")
        result, sel_mock = await self._click(dstate, sessions)
        assert result == _ACTION_REJECT
        assert slot._trust is False
        assert is_session_trusted(_SESSION_KEY) is False
        dstate.resolve_approval.assert_called_once_with("99", False)
        assert _audited(sel_mock, "slack.interactive.trust_linked")[0]["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_trust_denied_when_no_slot_owns_the_session(self) -> None:
        """The session's slot is gone (closed tab, restart) -> nothing to widen."""
        self._arm()
        dstate = _make_state(_FakeSlot(key="chat-other"))
        sessions = MagicMock()
        result, _ = await self._click(dstate, sessions)
        assert result == _ACTION_REJECT
        sessions.set_approval_policy.assert_not_called()

    @pytest.mark.asyncio
    async def test_approve_click_never_grants_trust(self) -> None:
        """A plain Approve on a grantable card stays one-shot."""
        self._arm()
        slot = _FakeSlot(request_id="99", grantable=True)
        sessions = MagicMock()
        with (
            patch.object(handler, "_dashboard_state", _make_state(slot)),
            patch.object(handler, "is_allowed_user", return_value=True),
        ):
            result = await handle_interaction(
                "D_OWNER", "TS1", _ACTION_APPROVE, user_id="U_OWNER", sessions=sessions
            )
        assert result == _ACTION_APPROVE
        assert slot._trust is False
        sessions.set_approval_policy.assert_not_called()


class TestStrictSharedGrant:
    """The seam ``_grant_linked_trust`` relies on to stay fail-closed.

    The default grant logs a failing policy write and keeps the in-memory half, which
    is right for a caller with nobody to tell. A Trust BUTTON has somebody to tell, so
    it must not label that partial state "Trusted".
    """

    def test_strict_undoes_the_mapping_and_re_raises(self) -> None:
        sessions = MagicMock()
        sessions.set_approval_policy.side_effect = RuntimeError("policy store down")
        with pytest.raises(RuntimeError):
            handler.add_trusted_session(_SESSION_KEY, sessions, strict=True)
        assert is_session_trusted(_SESSION_KEY) is False

    def test_the_default_stays_best_effort(self) -> None:
        sessions = MagicMock()
        sessions.set_approval_policy.side_effect = RuntimeError("policy store down")
        handler.add_trusted_session(_SESSION_KEY, sessions)
        assert is_session_trusted(_SESSION_KEY) is True

    def test_a_successful_strict_grant_writes_both_halves(self) -> None:
        sessions = MagicMock()
        handler.add_trusted_session(_SESSION_KEY, sessions, strict=True)
        assert is_session_trusted(_SESSION_KEY) is True
        sessions.set_approval_policy.assert_called_once_with(_SESSION_KEY, "auto")


# ── resolve_linked_approval ────────────────────────────────────────────────


def test_resolve_linked_approval_drops_entry() -> None:
    _linked_approvals["C_LINK:TS1"] = handler._LinkedApproval(
        request_id="1", session_key="dashboard:chat-1-123"
    )
    resolve_linked_approval("C_LINK", "TS1")
    assert "C_LINK:TS1" not in _linked_approvals
    # Idempotent — second call is a no-op.
    resolve_linked_approval("C_LINK", "TS1")
