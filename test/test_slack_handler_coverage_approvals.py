"""Slack approval prompts in :mod:`kiro_crew.slack.handler`, from the request to the click.

Pins ``handle_interaction``'s guards and late-trust paths, the claimed region that always
answers the wire and releases the waiter, dashboard-linked approvals, the approval block
builder, and ``_request_approval``'s post, resolve and timeout arms, including the in-band
notice an expired prompt steers before its reject. The commands live in
``test_slack_handler_coverage.py``, privacy modifiers and linked-thread routing in
``test_slack_handler_coverage_inbound.py``, status reactions, review drafts and the safe
update paths in ``test_slack_handler_coverage_delivery.py``, and the small pure helpers in
``test_slack_handler_coverage_pure_helpers.py``.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from slack_handler_coverage_helpers import (  # noqa: F401 -- fixtures are requested by name
    _clean_handler_state,
    _reply,
    _texts,
    owner_fixture,
    sessions_fixture,
    slack_fixture,
)

from conftest import MockSlackClient
from kiro_crew.providers.base import LLMEvent
from kiro_crew.slack import handler as h

#: Lost-run ceiling for a wait the test itself must end (a park, a click it
#: delivers). These paths make no executor hop. Timed wait by wait: at most 0.06 s,
#: both at ``-n 4`` on a loaded 32-CPU host and with every executor hop delayed by
#: 1.2 s (a starved-runner model). 30 s is far over ten times that and a quarter of
#: the module's ``--timeout=120``, so only a regression that never gets there
#: reaches it.
_LOST_RUN_SECS = 30.0


async def _within_lost_run(awaitable, what: str):
    """Await *awaitable* under ``_LOST_RUN_SECS``; on expiry fail naming *what*.

    A bare ``wait_for`` raises an empty ``TimeoutError``. This one says what never
    finished, the ceiling and the elapsed time. A ``TimeoutError`` the awaited code
    raises on its own, before the ceiling, propagates unchanged.
    """
    # flake-ok: pre-existing elapsed-time check that only names a wait which never ends, moved verbatim; tracked for a de-flake pass
    started = time.monotonic()
    try:
        return await asyncio.wait_for(awaitable, _LOST_RUN_SECS)
    except asyncio.TimeoutError:
        # flake-ok: pre-existing elapsed-time check that only names a wait which never ends, moved verbatim; tracked for a de-flake pass
        elapsed = time.monotonic() - started
        # A loop timer can fire up to one clock tick early (15.6 ms on Windows 3.12).
        if elapsed < _LOST_RUN_SECS - 1.0:
            raise
        pytest.fail(
            f"{what} did not finish within the {_LOST_RUN_SECS:.0f}s lost-run ceiling "
            f"({elapsed:.1f}s elapsed)"
        )


# ──────────────────────────────────────────────────────────────────────
# handle_interaction guards
# ──────────────────────────────────────────────────────────────────────
class TestHandleInteractionGuards:
    @pytest.mark.asyncio
    async def test_missing_user_is_rejected(self, owner):
        assert await h.handle_interaction("C1", "m1", h._ACTION_APPROVE, "") is None

    @pytest.mark.asyncio
    async def test_stranger_is_rejected(self, owner):
        assert await h.handle_interaction("C1", "m1", h._ACTION_APPROVE, "UZZZ") is None

    @pytest.mark.asyncio
    async def test_no_pending_approval(self, owner):
        assert await h.handle_interaction("C1", "m1", h._ACTION_APPROVE, "U1") is None

    @pytest.mark.asyncio
    async def test_late_trust_without_slack_client(self, owner):
        assert await h.handle_interaction("C1", "m1", h._ACTION_TRUST, "U1", "t1") is None

    @pytest.mark.asyncio
    async def test_late_trust_when_thread_fetch_fails(self, owner, slack):
        slack.fetch_thread_replies = AsyncMock(side_effect=RuntimeError("api down"))
        out = await h.handle_interaction("C1", "m1", h._ACTION_TRUST, "U1", "t1", slack=slack)
        assert out is None

    @pytest.mark.asyncio
    async def test_late_trust_rejects_non_thread_owner(self, owner, slack):
        slack.fetch_thread_replies = AsyncMock(return_value=[{"user": "UOTHER"}])
        out = await h.handle_interaction("C1", "m1", h._ACTION_TRUST, "U1", "t1", slack=slack)
        assert out is None
        assert not h.is_slack_session_trusted("t1")

    @pytest.mark.asyncio
    async def test_late_trust_grants_when_owner_matches(self, owner, slack, sessions, monkeypatch):
        slack.fetch_thread_replies = AsyncMock(return_value=[{"user": "U1"}])
        fake_map = MagicMock()
        fake_map.get_session_for_thread.return_value = ""
        monkeypatch.setattr("kiro_crew.session.SessionMap", lambda: fake_map)
        out = await h.handle_interaction(
            "C1", "m1", h._ACTION_TRUST, "U1", "t1", slack=slack, sessions=sessions
        )
        assert out == h._ACTION_TRUST
        assert h.is_slack_session_trusted("t1")
        sessions.set_approval_policy.assert_called_once_with("t1", "auto")

    @pytest.mark.asyncio
    async def test_late_trust_refuses_when_session_map_fails(self, owner, slack, monkeypatch):
        slack.fetch_thread_replies = AsyncMock(return_value=[{"user": "U1"}])

        def _boom():
            raise RuntimeError("no map")

        monkeypatch.setattr("kiro_crew.session.SessionMap", _boom)
        out = await h.handle_interaction("C1", "m1", h._ACTION_TRUST, "U1", "t1", slack=slack)
        assert out is None
        assert not h.is_slack_session_trusted("t1")

    @pytest.mark.asyncio
    async def test_pending_approve_resolves_future(self, owner):
        provider = MagicMock()
        provider.approve_tool = AsyncMock()
        pending = h._PendingApproval(provider, "r1", "t1")
        h._pending_approvals["C1:m1"] = pending
        out = await h.handle_interaction("C1", "m1", h._ACTION_APPROVE, "U1")
        assert out == h._ACTION_APPROVE
        assert pending.future.result() == h._OUTCOME_APPROVED
        provider.approve_tool.assert_awaited_once_with("r1")
        assert "C1:m1" not in h._pending_approvals

    @pytest.mark.asyncio
    async def test_pending_reject_resolves_future(self, owner):
        provider = MagicMock()
        provider.reject_tool = AsyncMock()
        pending = h._PendingApproval(provider, "r1", "t1")
        h._pending_approvals["C1:m1"] = pending
        out = await h.handle_interaction("C1", "m1", h._ACTION_REJECT, "U1")
        assert out == h._ACTION_REJECT
        assert pending.future.result() == h._OUTCOME_REJECTED
        provider.reject_tool.assert_awaited_once_with("r1")

    @pytest.mark.asyncio
    async def test_pending_trust_grants_session(self, owner, sessions):
        provider = MagicMock()
        provider.approve_tool = AsyncMock()
        h._pending_approvals["C1:m1"] = h._PendingApproval(provider, "r1", "sess-9")
        out = await h.handle_interaction("C1", "m1", h._ACTION_TRUST, "U1", sessions=sessions)
        assert out == h._ACTION_TRUST
        assert h.is_slack_session_trusted("sess-9")
        sessions.set_approval_policy.assert_called_once_with("sess-9", "auto")

    @pytest.mark.asyncio
    async def test_pending_trust_without_session_key_still_approves(self, owner):
        provider = MagicMock()
        provider.approve_tool = AsyncMock()
        h._pending_approvals["C1:m1"] = h._PendingApproval(provider, "r1", "")
        out = await h.handle_interaction("C1", "m1", h._ACTION_TRUST, "U1")
        assert out == h._ACTION_TRUST
        provider.approve_tool.assert_awaited_once()


# ──────────────────────────────────────────────────────────────────────
# the claimed region always answers the wire and releases the waiter
# ──────────────────────────────────────────────────────────────────────
class TestHandleInteractionClaimedRegion:
    """A raise between the claim (``_pending_approvals.pop``) and ``set_result``
    must still answer the wire and release the waiter.

    ``_request_approval``'s timeout arm that lost its claim awaits
    ``pending.future`` unbounded, justified by "every way the click can end
    resolves this future". These tests pin that invariant for the synchronous
    bookkeeping between the claim and the resolution (trust bookkeeping, audit,
    stats), not just the two wire calls the old per-arm handlers covered.
    """

    @pytest.mark.asyncio
    async def test_a_raise_inside_the_claimed_region_answers_wire_and_waiter(
        self, owner, sessions, monkeypatch
    ):
        provider = MagicMock()
        provider.approve_tool = AsyncMock()
        provider.reject_tool = AsyncMock()
        pending = h._PendingApproval(provider, "r1", "sess-9")
        h._pending_approvals["C1:m1"] = pending

        def _boom(*a, **k):
            raise RuntimeError("trust bookkeeping failed")

        monkeypatch.setattr(h, "add_trusted_session", _boom)
        with pytest.raises(RuntimeError, match="trust bookkeeping failed"):
            await h.handle_interaction("C1", "m1", h._ACTION_TRUST, "U1", sessions=sessions)
        # The waiter is released with the real (rejected) outcome...
        assert pending.future.done()
        assert pending.future.result() == h._OUTCOME_REJECTED
        # ...and the wire is answered, so the ACP request is not stranded.
        provider.reject_tool.assert_awaited_once_with("r1")
        provider.approve_tool.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reject_orphaned_tool_swallows_audit_failure(self, owner, monkeypatch):
        provider = MagicMock()
        provider.reject_tool = AsyncMock()
        monkeypatch.setattr(h, "sel", MagicMock(side_effect=RuntimeError("audit failed")))

        assert await h._reject_orphaned_tool(provider, "r1") is True

        provider.reject_tool.assert_awaited_once_with("r1")

    @pytest.mark.asyncio
    async def test_an_approve_wire_failure_still_rejects_and_releases(self, owner, monkeypatch):
        provider = MagicMock()
        provider.approve_tool = AsyncMock(side_effect=RuntimeError("pipe closed"))
        provider.reject_tool = AsyncMock()
        pending = h._PendingApproval(provider, "r1", "t1")
        h._pending_approvals["C1:m1"] = pending
        monkeypatch.setattr(h, "sel", MagicMock(side_effect=RuntimeError("audit failed")))

        with pytest.raises(RuntimeError, match="pipe closed"):
            await h.handle_interaction("C1", "m1", h._ACTION_APPROVE, "U1")

        assert pending.future.done()
        assert pending.future.result() == h._OUTCOME_REJECTED
        provider.reject_tool.assert_awaited_once_with("r1")


# ──────────────────────────────────────────────────────────────────────
# linked (dashboard-owned) approvals
# ──────────────────────────────────────────────────────────────────────
class TestLinkedApprovals:
    @pytest.mark.asyncio
    async def test_post_registers_entry(self, slack):
        ts = _reply(
            await h.post_linked_approval(slack, "C1", "t1", "r1", "slot-1", "Run tests", "pytest")
        )
        assert f"C1:{ts}" in h._linked_approvals
        h.resolve_linked_approval("C1", ts)
        assert f"C1:{ts}" not in h._linked_approvals

    @pytest.mark.asyncio
    async def test_post_failure_returns_none(self):
        broken = MagicMock()
        broken.post_blocks = AsyncMock(side_effect=RuntimeError("slack down"))
        assert await h.post_linked_approval(broken, "C1", "t1", "r1", "s", "T") is None
        assert not h._linked_approvals

    @pytest.mark.asyncio
    async def test_click_resolves_dashboard_future(self, slack, owner, monkeypatch):
        state = MagicMock()
        state.resolve_approval.return_value = True
        monkeypatch.setattr(h, "_dashboard_state", state)
        ts = _reply(await h.post_linked_approval(slack, "C1", "t1", 7, "slot-1", "Run tests"))
        out = await h.handle_interaction("C1", ts, h._ACTION_APPROVE, "U1")
        assert out == h._ACTION_APPROVE
        state.resolve_approval.assert_called_once_with("7", True)
        assert f"C1:{ts}" not in h._linked_approvals

    @pytest.mark.asyncio
    async def test_reject_click_passes_false(self, slack, owner, monkeypatch):
        state = MagicMock()
        state.resolve_approval.return_value = False
        monkeypatch.setattr(h, "_dashboard_state", state)
        ts = _reply(await h.post_linked_approval(slack, "C1", "t1", "r9", "slot-1", "Delete prod"))
        out = await h.handle_interaction("C1", ts, h._ACTION_REJECT, "U1")
        assert out == h._ACTION_REJECT
        state.resolve_approval.assert_called_once_with("r9", False)

    @pytest.mark.asyncio
    async def test_resolve_failure_is_swallowed(self, slack, owner, monkeypatch):
        state = MagicMock()
        state.resolve_approval.side_effect = RuntimeError("gone")
        monkeypatch.setattr(h, "_dashboard_state", state)
        ts = _reply(await h.post_linked_approval(slack, "C1", "t1", "r1", "slot-1", "T"))
        assert await h.handle_interaction("C1", ts, h._ACTION_APPROVE, "U1") == h._ACTION_APPROVE


# ──────────────────────────────────────────────────────────────────────
# approval block builder
# ──────────────────────────────────────────────────────────────────────
def _perm_event(
    *,
    request_id: str | int = "r1",
    title: str = "Bash",
    tool_input: str = "",
    tool_purpose: str = "",
) -> LLMEvent:
    return LLMEvent(
        kind="permission_request",
        request_id=request_id,
        title=title,
        tool_input=tool_input,
        tool_purpose=tool_purpose,
    )


class TestApprovalBlocks:
    def test_dm_offers_trust(self):
        blocks = h._build_approval_blocks(_perm_event(), is_dm=True)
        actions = [b for b in blocks if b["type"] == "actions"][0]
        ids = [e["action_id"] for e in actions["elements"]]
        assert ids == [h._ACTION_APPROVE, h._ACTION_TRUST, h._ACTION_REJECT]

    def test_channel_omits_trust(self):
        blocks = h._build_approval_blocks(_perm_event(), is_dm=False)
        actions = [b for b in blocks if b["type"] == "actions"][0]
        ids = [e["action_id"] for e in actions["elements"]]
        assert h._ACTION_TRUST not in ids

    def test_integer_request_id_is_stringified(self):
        blocks = h._build_approval_blocks(_perm_event(request_id=42))
        actions = [b for b in blocks if b["type"] == "actions"][0]
        assert all(e["value"] == "42" for e in actions["elements"])

    def test_source_tag_and_purpose_in_footer(self):
        blocks = h._build_approval_blocks(
            _perm_event(tool_purpose="run the suite"), source="subagent"
        )
        footer = blocks[-1]["elements"][0]["text"]
        assert "[subagent]" in footer and "run the suite" in footer

    def test_long_tool_input_is_truncated(self):
        blocks = h._build_approval_blocks(_perm_event(tool_input="x" * 5000))
        detail = blocks[1]["text"]["text"]
        assert h._TRUNCATION_MARKER in detail
        assert len(detail) < 5000

    def test_short_tool_input_is_verbatim(self):
        blocks = h._build_approval_blocks(_perm_event(tool_input="ls -la"))
        assert "```ls -la```" == blocks[1]["text"]["text"]


# ──────────────────────────────────────────────────────────────────────
# _request_approval
# ──────────────────────────────────────────────────────────────────────
class TestRequestApproval:
    @pytest.mark.asyncio
    async def test_post_failure_rejects_the_orphaned_tool(self):
        """A prompt that cannot be posted must not leave the ACP request unanswered."""
        broken = MagicMock()
        broken.post_blocks = AsyncMock(side_effect=RuntimeError("slack down"))
        provider = MagicMock()
        provider.reject_tool = AsyncMock()
        with pytest.raises(RuntimeError):
            await h._request_approval(broken, provider, "C1", "t1", _perm_event())
        provider.reject_tool.assert_awaited_once_with("r1")
        assert not h._pending_approvals

    @pytest.mark.asyncio
    async def test_resolved_future_returns_outcome_and_deletes_prompt(self, slack):
        provider = MagicMock()

        async def _post(channel, blocks, text, thread_ts=None, **kw):
            ts = "9001.0"
            # Resolve as soon as the prompt exists, so no timer is involved.
            asyncio.get_running_loop().call_soon(
                lambda: h._pending_approvals[f"{channel}:{ts}"].future.set_result(
                    h._OUTCOME_APPROVED
                )
            )
            slack.actions.append(("blocks", {"channel": channel, "text": text, "ts": ts}))
            return ts

        slack.post_blocks = _post
        # Bounded: the only other bound is the 120 s production approval budget.
        outcome = await _within_lost_run(
            h._request_approval(slack, provider, "C1", "t1", _perm_event()),
            "the approval request after its prompt resolved",
        )
        assert outcome == h._OUTCOME_APPROVED
        assert [a for a in slack.actions if a[0] == "delete"]
        assert not h._pending_approvals

    @pytest.mark.asyncio
    async def test_timeout_rejects_the_tool(self, slack, monkeypatch):
        # timeout=0 makes wait_for fail its first check — deterministic, no sleep.
        monkeypatch.setattr(h, "_APPROVAL_TIMEOUT", 0)
        provider = MagicMock()
        # Keep this test on the no-steer baseline; the steer arms are owned by
        # TestApprovalTimeoutInbandNotice (a bare MagicMock auto-attribute would
        # be truthy and route through the steer-failure path instead).
        provider.supports_steer = False
        provider.supports_refusal_steer = False
        provider.reject_tool = AsyncMock()
        outcome = await h._request_approval(slack, provider, "C1", "t1", _perm_event())
        assert outcome == h._OUTCOME_REJECTED
        provider.reject_tool.assert_awaited_once_with("r1")
        assert not h._pending_approvals

    @pytest.mark.asyncio
    async def test_delete_failure_falls_back_to_an_edit(self, monkeypatch):
        monkeypatch.setattr(h, "_APPROVAL_TIMEOUT", 0)
        slack_client = MockSlackClient()
        monkeypatch.setattr(
            slack_client, "delete_message", AsyncMock(side_effect=RuntimeError("too old"))
        )
        provider = MagicMock()
        provider.supports_steer = False
        provider.supports_refusal_steer = False
        provider.reject_tool = AsyncMock()
        await h._request_approval(slack_client, provider, "C1", "t1", _perm_event())
        assert "🚫 Rejected" in _texts(slack_client)


class _SteerRecordingProvider:
    """Fake provider recording the order of steer() and reject_tool() calls."""

    def __init__(self, *, supports_steer: bool = True, steer_exc: BaseException | None = None):
        self.supports_steer = supports_steer
        # The deny paths read the refusal answer; a steer-capable double has both.
        self.supports_refusal_steer = supports_steer
        self.calls: list[tuple[str, str]] = []
        self._steer_exc = steer_exc
        self.pending_during_steer: dict[str, object] = {}

    async def steer(self, message: str) -> bool:
        # The decision must already be claimed when the steer runs: a pending
        # entry left registered here would let a concurrent Slack click answer
        # the same permission request a second time.
        self.pending_during_steer = dict(h._pending_approvals)
        self.calls.append(("steer", message))
        if self._steer_exc is not None:
            raise self._steer_exc
        return True

    async def reject_tool(self, request_id) -> None:
        # Suspend once before recording, like a real pipe write: a scheduled-
        # but-never-stepped reject task then fails the assertion instead of
        # passing on scheduling alone.
        await asyncio.sleep(0)
        self.calls.append(("reject_tool", str(request_id)))


class TestApprovalTimeoutInbandNotice:
    """An expired Slack approval prompt must correct the model's attribution.

    Mirrors the dashboard-side ordering contract pinned in
    test_refusal_inband_notice.py: the notice is steered while the permission
    request is still unanswered (queued, not dropped), and the reject follows
    unconditionally.
    """

    @pytest.mark.asyncio
    async def test_timeout_steers_cause_notice_before_reject(self, slack, monkeypatch):
        monkeypatch.setattr(h, "_APPROVAL_TIMEOUT", 0)
        provider = _SteerRecordingProvider()
        outcome = await h._request_approval(slack, provider, "C1", "t1", _perm_event())
        assert outcome == h._OUTCOME_REJECTED
        assert [c[0] for c in provider.calls] == ["steer", "reject_tool"]
        notice = provider.calls[0][1]
        # The pending entry was claimed before the steer awaited, so a click
        # landing mid-steer cannot double-answer the permission request.
        assert provider.pending_during_steer == {}
        # The approval-timeout cause wording, not the generic policy one.
        assert "expired unanswered" in notice
        assert "Slack approval prompt went unanswered" in notice
        assert provider.calls[1][1] == "r1"
        assert not h._pending_approvals

    @pytest.mark.asyncio
    async def test_reject_still_sent_when_steer_raises(self, slack, monkeypatch):
        monkeypatch.setattr(h, "_APPROVAL_TIMEOUT", 0)
        provider = _SteerRecordingProvider(steer_exc=RuntimeError("wire down"))
        outcome = await h._request_approval(slack, provider, "C1", "t1", _perm_event())
        assert outcome == h._OUTCOME_REJECTED
        assert ("reject_tool", "r1") in provider.calls

    @pytest.mark.asyncio
    async def test_reject_still_sent_when_steer_times_out(self, slack, monkeypatch):
        monkeypatch.setattr(h, "_APPROVAL_TIMEOUT", 0)
        # A steer that hangs forever: only the wait_for bound can get past it,
        # so this pins the bound itself, not merely the swallow.
        monkeypatch.setattr(h, "_STEER_NOTICE_BOUND_SECS", 0.05)
        provider = _SteerRecordingProvider()

        async def _hanging_steer(message: str) -> bool:
            provider.calls.append(("steer", message))
            # flake-ok: pre-existing park on an Event nothing sets, ended by the 0.05 s steer bound the test sets; moved verbatim and tracked for a de-flake pass
            await asyncio.Event().wait()
            return True  # pragma: no cover - unreachable

        provider.steer = _hanging_steer  # type: ignore[method-assign]
        outcome = await _within_lost_run(
            h._request_approval(slack, provider, "C1", "t1", _perm_event()),
            "the approval request after its steer timed out",
        )
        assert outcome == h._OUTCOME_REJECTED
        assert ("reject_tool", "r1") in provider.calls

    @pytest.mark.asyncio
    async def test_no_steer_when_a_click_already_claimed_the_entry(self, slack, monkeypatch):
        """A click that won the claim is answering the request itself: steering
        "expired unanswered" then would be false. The reject still goes out as
        the wire safety net."""
        monkeypatch.setattr(h, "_APPROVAL_TIMEOUT", 0)
        provider = _SteerRecordingProvider()

        async def _post(channel, blocks, text, thread_ts=None, **kw):
            ts = "9002.0"

            def _click() -> None:
                # Simulate a click claiming the entry between registration and
                # the timeout arm, then delivering its decision, as
                # handle_interaction does after its wire answer.
                pending = h._pending_approvals.pop(f"{channel}:{ts}", None)
                if pending is not None and not pending.future.done():
                    pending.future.set_result(h._OUTCOME_APPROVED)

            asyncio.get_running_loop().call_soon(_click)
            return ts

        slack.post_blocks = _post
        # Bounded: the lost-claim await has no bound of its own by design.
        outcome = await _within_lost_run(
            h._request_approval(slack, provider, "C1", "t1", _perm_event()),
            "the approval request whose click claimed the entry",
        )
        # The claim winner answers the wire; the loser stays entirely off it —
        # a second answer would land in the ACP client's cancelled-outcome
        # fallback and cancel the whole turn. And the returned outcome is the
        # CLICK's real decision, delivered through the shielded future.
        assert outcome == h._OUTCOME_APPROVED
        assert provider.calls == []

    @pytest.mark.asyncio
    async def test_lost_claim_waits_for_the_click_to_resolve_and_reports_it(
        self, slack, monkeypatch
    ):
        """A click that claimed the entry but is slow to finish its wire write
        (a backpressured stdin) owns the answer: the timeout arm keeps
        awaiting the click-owned future -- no bound, no fabricated rejection --
        and reports the click's real decision once it lands, without ever
        touching the wire itself."""
        monkeypatch.setattr(h, "_APPROVAL_TIMEOUT", 0)
        provider = _SteerRecordingProvider()
        claimed: list[h._PendingApproval] = []

        async def _post(channel, blocks, text, thread_ts=None, **kw):
            ts = "9003.0"

            def _claim() -> None:
                pending = h._pending_approvals.pop(f"{channel}:{ts}", None)
                if pending is not None:
                    claimed.append(pending)

            asyncio.get_running_loop().call_soon(_claim)
            return ts

        # "No bound" is checked structurally, not by outwaiting a guessed bound:
        # the arm's second shield is the lost-claim await itself, and any bound
        # on it (wait_for, asyncio.timeout, sleep, call_later, a helper task's
        # sleep) arms a loop timer that this test did not arm itself. Timers are
        # recorded from the FIRST shield on: the timeout-0 wait around it arms
        # none, so anything armed from there to the park is a bound on the arm.
        loop = asyncio.get_running_loop()
        test_task = asyncio.current_task()
        shields: list[object] = []
        parked = asyncio.Event()
        timers_while_parked: list[object] = []

        class _RecordingAsyncio:
            def __getattr__(self, name):
                return getattr(asyncio, name)

            @staticmethod
            def shield(aw):
                shields.append(aw)
                if len(shields) == 2:
                    parked.set()
                return asyncio.shield(aw)

        real_call_at = loop.call_at

        def _call_at(when, callback, *args, **kwargs):
            if shields and asyncio.current_task() is not test_task:
                timers_while_parked.append(callback)
            return real_call_at(when, callback, *args, **kwargs)

        monkeypatch.setattr(h, "asyncio", _RecordingAsyncio())
        monkeypatch.setattr(loop, "call_at", _call_at)
        slack.post_blocks = _post
        task = asyncio.create_task(h._request_approval(slack, provider, "C1", "t1", _perm_event()))
        try:
            await _within_lost_run(parked.wait(), "the lost-claim park")
            # One more turn: a bound armed by a helper task the arm started runs
            # its first step now, and would arm its timer here.
            await asyncio.sleep(0)
            # Parked on the click-owned future: the click is still "writing".
            assert claimed and not claimed[0].future.done()
            assert timers_while_parked == [], "the lost-claim await must carry no bound"
            assert not task.done()
            assert provider.calls == []
            # The click finishes its answer -- Approve -- and resolves the waiter.
            claimed[0].future.set_result(h._OUTCOME_APPROVED)
            outcome = await _within_lost_run(task, "the request after the click resolved")
        finally:
            task.cancel()
        assert outcome == h._OUTCOME_APPROVED
        # The click's approve is the only answer this request ever gets.
        assert provider.calls == []

    @pytest.mark.asyncio
    async def test_cancellation_during_steer_still_answers_the_wire(self, slack, monkeypatch):
        """A REAL task cancel while parked in the steer: the shielded orphan
        reject must still reach the wire before the cancellation re-raises."""
        monkeypatch.setattr(h, "_APPROVAL_TIMEOUT", 0)
        provider = _SteerRecordingProvider()
        steer_parked = asyncio.Event()

        async def _hanging_steer(message: str) -> bool:
            provider.calls.append(("steer", message))
            steer_parked.set()
            # flake-ok: pre-existing park on an Event nothing sets, ended by the cancel the test delivers; moved verbatim and tracked for a de-flake pass
            await asyncio.Event().wait()
            return True  # pragma: no cover - unreachable

        provider.steer = _hanging_steer  # type: ignore[method-assign]
        task = asyncio.create_task(h._request_approval(slack, provider, "C1", "t1", _perm_event()))
        await _within_lost_run(steer_parked.wait(), "the park in the steer")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await _within_lost_run(task, "the cancelled request")
        assert ("reject_tool", "r1") in provider.calls
        assert not h._pending_approvals

    def test_steer_bound_mirrors_the_dashboard_runner(self):
        from kiro_crew.dashboard import chat_runner

        assert h._STEER_NOTICE_BOUND_SECS == chat_runner._STEER_NOTICE_BOUND_SECS

    @pytest.mark.asyncio
    async def test_no_steer_attempted_when_unsupported(self, slack, monkeypatch):
        monkeypatch.setattr(h, "_APPROVAL_TIMEOUT", 0)
        provider = _SteerRecordingProvider(supports_steer=False)
        outcome = await h._request_approval(slack, provider, "C1", "t1", _perm_event())
        assert outcome == h._OUTCOME_REJECTED
        assert [c[0] for c in provider.calls] == ["reject_tool"]
