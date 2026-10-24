"""What a batch rejection, a deny-once and an expired approval leave behind.

Batch rejection auto-rejects the rest of its group and only that group, a host
auto-decline's cascade steers its real cause instead of a user denial, ``rejected_once``
denies one call without cascading, background sources deny on a short window, and an
expired, cancelled or answered approval retires its card through one broadcast. Also here:
tool-completion tracking under trust mode and the state's permission metadata. The state,
stream and answerer helpers come from ``dashboard_approval_helpers.py``.
"""

from __future__ import annotations

import asyncio
import itertools
from unittest.mock import MagicMock, patch

import pytest
from dashboard_approval_helpers import (
    _ANSWER_WAIT_SECS,
    _answer_approval,
    _complete_event,
    _context_builder,
    _drain,
    _make_slot,
    _make_state,
    _patch_stats,
    _permission_event,
    _set_stream,
)

from kiro_crew.dashboard.chat import _run_chat
from kiro_crew.dashboard.handlers.sessions import api_approval_resolve
from kiro_crew.dashboard.state import DashboardState, _ChatSlot, parse_cls_meta
from kiro_crew.providers.base import EVENT_TEXT_CHUNK, EVENT_THINKING_CHUNK, LLMEvent
from kiro_crew.testing.wait import async_wait_until


class TestBatchRejection:
    """Verify batch rejection auto-rejects remaining tools."""

    @pytest.mark.asyncio
    async def test_batch_rejection_auto_rejects_remaining(self, tmp_path):
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot()
        evt1 = _permission_event(title="tool_a")
        evt1.request_id = "req-1"
        evt1.tool_call_id = "tc-1"
        evt2 = _permission_event(title="tool_b")
        evt2.request_id = "req-2"
        evt2.tool_call_id = "tc-2"
        _set_stream(client, [evt1, evt2, _complete_event()])

        async def _reject_first() -> None:
            await _answer_approval(slot, "req-1", "rejected")

        rejecter = asyncio.get_event_loop().create_task(_reject_first())

        with _patch_stats():
            await _run_chat(state, slot, "hello")
        await _drain(rejecter)

        # First tool rejected interactively, second auto-rejected
        client.reject_tool.assert_any_call("req-1")
        client.reject_tool.assert_any_call("req-2")
        assert slot._batch_rejected is False  # reset in finally

    @pytest.mark.asyncio
    async def test_batch_rejected_reset_on_exception(self, tmp_path):
        """_batch_rejected is reset even if event loop raises."""
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot()
        slot._batch_rejected = True

        async def _exploding_stream():
            yield _permission_event()
            raise RuntimeError("boom")

        client.stream = MagicMock(side_effect=lambda *a, **kw: _exploding_stream())

        with _patch_stats():
            try:
                await _run_chat(state, slot, "hello")
            except RuntimeError:
                pass

        assert slot._batch_rejected is False


class TestDenialCascadeLifetime:
    """The batch-rejection suppression must not outlive the group it belongs to.

    Clearing ``_batch_rejected`` only in the turn runner's ``finally`` lets it
    live for the whole TURN. A user who denied one call had
    every later call in that turn auto-denied without ever being shown a card —
    breaking deny → discuss → agent revises → agent retries, and reporting the
    phantom denial to the model as "User denied tool execution".
    """

    @pytest.mark.parametrize("resume_kind", [EVENT_TEXT_CHUNK, EVENT_THINKING_CHUNK])
    @pytest.mark.asyncio
    async def test_later_sequential_call_is_evaluated_independently(self, tmp_path, resume_kind):
        """Call N+1 gets its own prompt once the model resumed after N was denied."""
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot()
        denied = _permission_event(title="tool_a")
        denied.request_id = "req-1"
        denied.tool_call_id = "tc-1"
        # Model-authored output between the two calls: the denied group is over
        # and req-2 is a NEW decision, not a remaining member of that group.
        resumed = LLMEvent(kind=resume_kind, text="Understood - trying a different edit.")
        revised = _permission_event(title="tool_b")
        revised.request_id = "req-2"
        revised.tool_call_id = "tc-2"
        _set_stream(client, [denied, resumed, revised, _complete_event()])

        async def _answer_both() -> None:
            await _answer_approval(slot, "req-1", "rejected")
            await _answer_approval(slot, "req-2", "approved")

        answerer = asyncio.get_event_loop().create_task(_answer_both())

        with _patch_stats():
            await _run_chat(state, slot, "hello")
        await _drain(answerer)

        # The denial itself still denies.
        client.reject_tool.assert_any_call("req-1")
        # The revised call was PROMPTED and could be approved — never swallowed.
        client.approve_tool.assert_any_call("req-2")
        assert ("req-2",) not in [c.args for c in client.reject_tool.call_args_list]

    @pytest.mark.asyncio
    async def test_same_group_still_cascades_after_the_fix(self, tmp_path):
        """No model output between the calls → the remaining member stays denied.

        Pins the blast-radius reduction as a LIFETIME change only: suppression
        of the group the user actually refused is preserved.
        """
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot()
        evt1 = _permission_event(title="tool_a")
        evt1.request_id = "req-1"
        evt1.tool_call_id = "tc-1"
        evt2 = _permission_event(title="tool_b")
        evt2.request_id = "req-2"
        evt2.tool_call_id = "tc-2"
        _set_stream(client, [evt1, evt2, _complete_event()])

        async def _reject_first() -> None:
            await _answer_approval(slot, "req-1", "rejected")

        rejecter = asyncio.get_event_loop().create_task(_reject_first())

        with _patch_stats():
            await _run_chat(state, slot, "hello")
        await _drain(rejecter)

        client.reject_tool.assert_any_call("req-1")
        client.reject_tool.assert_any_call("req-2")
        client.approve_tool.assert_not_called()


class TestBatchCascadeAttribution:
    """A cascade behind a HOST auto-decline must not inherit user attribution.

    ``_batch_rejected`` is also set by the host-side auto-declines
    (approval timeout, no turn budget, Slack delivery failure), and the cascade
    would otherwise answer every remaining batch member with nothing but kiro-cli's
    generic "User denied tool execution" — a decline no user made. These pin the
    provenance split: host-caused cascades steer one cause-specific in-band
    notice for the whole remainder, user-refused batches keep the generic
    message, which is true there.
    """

    @pytest.mark.asyncio
    async def test_timeout_originated_cascade_steers_the_real_cause(self, tmp_path):
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot()
        # Shrink only the per-slot bound; the config ceiling stays at its
        # default, and the product takes the MINIMUM of the two.
        state.approval_timeout_for = MagicMock(return_value=0.05)
        evt1 = _permission_event(title="tool_a")
        evt1.request_id = "req-1"
        evt1.tool_call_id = "tc-1"
        evt2 = _permission_event(title="tool_b")
        evt2.request_id = "req-2"
        evt2.tool_call_id = "tc-2"
        _set_stream(client, [evt1, evt2, _complete_event()])

        with _patch_stats():
            await _run_chat(state, slot, "hello")

        # Nobody answered: the first tool was declined by the host, the second
        # by the cascade — and each decline corrected its own attribution
        # in-band. Two notices, two distinct facts: the expired prompt covers
        # tool_a, the cascade notice covers the remainder.
        client.reject_tool.assert_any_call("req-1")
        client.reject_tool.assert_any_call("req-2")
        assert client.steer.call_count == 2
        timeout_notice = client.steer.call_args_list[0][0][0]
        assert "approval prompt expired" in timeout_notice
        assert "every remaining call in its batch" not in timeout_notice
        notice = client.steer.call_args_list[1][0][0]
        assert "every remaining call in its batch" in notice
        assert "unanswered" in notice
        assert "User denied tool execution" in notice
        assert "NOT a user action" in notice

    @pytest.mark.asyncio
    async def test_no_budget_originated_cascade_steers_the_real_cause(self, tmp_path):
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot()
        # Zero window is the no-budget branch: the host declines without
        # waiting at all, so no answerer could ever race this decline.
        state.approval_timeout_for = MagicMock(return_value=0)
        evt1 = _permission_event(title="tool_a")
        evt1.request_id = "req-1"
        evt1.tool_call_id = "tc-1"
        evt2 = _permission_event(title="tool_b")
        evt2.request_id = "req-2"
        evt2.tool_call_id = "tc-2"
        _set_stream(client, [evt1, evt2, _complete_event()])

        with _patch_stats():
            await _run_chat(state, slot, "hello")

        client.reject_tool.assert_any_call("req-1")
        client.reject_tool.assert_any_call("req-2")
        # The declined tool itself is corrected first — its own no-budget
        # notice — then exactly ONE cascade notice covers the remainder.
        assert client.steer.call_count == 2
        no_budget_notice = client.steer.call_args_list[0][0][0]
        assert "no budget left" in no_budget_notice
        assert "every remaining call in its batch" not in no_budget_notice
        assert "NOT a user action" in no_budget_notice
        notice = client.steer.call_args_list[1][0][0]
        assert "every remaining call in its batch" in notice
        assert "no budget left" in notice

    @pytest.mark.asyncio
    async def test_user_refused_batch_cascades_without_a_steer(self, tmp_path):
        # The exemption half: the person clicked Reject themselves, so the
        # generic message is the TRUE attribution for the remainder and a host
        # notice here would re-attribute the user's own decision to the host.
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot()
        evt1 = _permission_event(title="tool_a")
        evt1.request_id = "req-1"
        evt1.tool_call_id = "tc-1"
        evt2 = _permission_event(title="tool_b")
        evt2.request_id = "req-2"
        evt2.tool_call_id = "tc-2"
        _set_stream(client, [evt1, evt2, _complete_event()])

        async def _reject_first() -> None:
            await _answer_approval(slot, "req-1", "rejected")

        rejecter = asyncio.get_event_loop().create_task(_reject_first())

        with _patch_stats():
            await _run_chat(state, slot, "hello")
        await _drain(rejecter)

        client.reject_tool.assert_any_call("req-1")
        client.reject_tool.assert_any_call("req-2")
        client.steer.assert_not_called()

    @pytest.mark.asyncio
    async def test_one_notice_covers_the_whole_cascaded_remainder(self, tmp_path):
        # The notice speaks for the group, so a three-member batch must not
        # steer three times — repeated notices are noise the model has to spend
        # the very turn being corrected on.
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot()
        state.approval_timeout_for = MagicMock(return_value=0.05)
        events = []
        for i in (1, 2, 3):
            evt = _permission_event(title=f"tool_{i}")
            evt.request_id = f"req-{i}"
            evt.tool_call_id = f"tc-{i}"
            events.append(evt)
        _set_stream(client, [*events, _complete_event()])

        with _patch_stats():
            await _run_chat(state, slot, "hello")

        assert client.reject_tool.call_count == 3
        # One notice for the expired prompt on tool_1, then exactly ONE for the
        # cascaded remainder — never one per cascaded member.
        assert client.steer.call_count == 2
        cascade_notices = [
            c[0][0]
            for c in client.steer.call_args_list
            if "every remaining call in its batch" in c[0][0]
        ]
        assert len(cascade_notices) == 1

    @pytest.mark.asyncio
    async def test_provenance_dies_with_the_group_it_belongs_to(self, tmp_path):
        # Model output ends the denied group. What is OBSERVABLE here:
        # the revised later call is prompted — not cascaded, not steered — and
        # a host-recorded cause never survives past the turn. The paired
        # cause-clear at each flag-clear site is pinned at source level in
        # test_refusal_inband_notice.py (a stale cause is unreadable while the
        # flag is down, so only a source guard can hold that pairing).
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot()
        # Only the FIRST prompt is meant to expire. The revised call must be
        # answered by the approver below, which polls for its future; a 50 ms
        # window on that second prompt races the poll on a slow runner (the
        # runner declines tool_b as unanswered before the approver sees it),
        # and a declined tool_b is the very outcome this test says never
        # happens. So the shrunken bound applies to one prompt; the next gets
        # a window the approver cannot miss.
        state.approval_timeout_for = MagicMock(
            side_effect=itertools.chain([0.05], itertools.repeat(_ANSWER_WAIT_SECS))
        )
        timed_out = _permission_event(title="tool_a")
        timed_out.request_id = "req-1"
        timed_out.tool_call_id = "tc-1"
        resumed = LLMEvent(kind=EVENT_TEXT_CHUNK, text="Retrying with a narrower edit.")
        revised = _permission_event(title="tool_b")
        revised.request_id = "req-2"
        revised.tool_call_id = "tc-2"
        _set_stream(client, [timed_out, resumed, revised, _complete_event()])

        async def _approve_revised() -> None:
            await _answer_approval(slot, "req-2", "approved")

        approver = asyncio.get_event_loop().create_task(_approve_revised())

        with _patch_stats():
            await _run_chat(state, slot, "hello")
        await _drain(approver)

        # The revised call was prompted and approved — never cascaded, so the
        # cascade notice was never sent. The expired prompt on tool_a still
        # steers its own notice; what must be absent is the cascade one.
        client.approve_tool.assert_any_call("req-2")
        assert client.steer.call_count == 1
        assert "every remaining call in its batch" not in client.steer.call_args[0][0]
        assert slot._batch_rejected_cause == ""


class TestDenyOnce:
    """Verify 'rejected_once' denies a single tool without cascading."""

    @pytest.mark.asyncio
    async def test_deny_once_rejects_tool_but_does_not_cascade(self, tmp_path):
        """reject_once rejects the first tool but lets the second get its own prompt."""
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot()
        evt1 = _permission_event(title="tool_a")
        evt1.request_id = "req-1"
        evt1.tool_call_id = "tc-1"
        evt2 = _permission_event(title="tool_b")
        evt2.request_id = "req-2"
        evt2.tool_call_id = "tc-2"
        _set_stream(client, [evt1, evt2, _complete_event()])

        async def _answer_both() -> None:
            await _answer_approval(slot, "req-1", "rejected_once")
            await _answer_approval(slot, "req-2", "approved")

        answerer = asyncio.get_event_loop().create_task(_answer_both())

        with _patch_stats():
            await _run_chat(state, slot, "hello")
        await _drain(answerer)

        # First tool rejected, second approved
        client.reject_tool.assert_any_call("req-1")
        client.approve_tool.assert_any_call("req-2")
        # Batch rejection flag must NOT be set
        assert slot._batch_rejected is False

    @pytest.mark.asyncio
    async def test_decision_override_reaches_slot_future(self, tmp_path):
        """``rejected_once=True`` is what the slot future receives, not "rejected"."""
        state, _ = _make_state(tmp_path)
        slot = _make_slot()
        state._slots[slot.key] = slot

        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        slot._approval_futures["req-once"] = fut

        assert state.resolve_approval("req-once", False, rejected_once=True) is True
        assert fut.result() == "rejected_once"

    @pytest.mark.asyncio
    async def test_both_audit_event_types_agree_on_the_denial(self, tmp_path):
        """The `approval_decision` event records the token too, not just `rejected`.

        `log_tool_invocation` already distinguishes the two denials on the tool
        event. If this second event type flattened both to "rejected", the audit
        trail would distinguish them in one place and not the other, which
        distinguishes nothing.
        """
        state, _ = _make_state(tmp_path)
        slot = _make_slot()
        state._slots[slot.key] = slot

        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        slot._approval_futures["req-audit"] = fut

        with patch("kiro_crew.dashboard.state.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            state.resolve_approval("req-audit", False, rejected_once=True)
            outcomes = [
                c.kwargs.get("outcome")
                for c in mock_sel.return_value.log_tool_invocation.call_args_list
                if c.kwargs.get("tool_name") == "approval_decision"
            ]
        assert outcomes == ["rejected_once"]

    @pytest.mark.asyncio
    async def test_state_level_override_drop_is_logged_not_silent(self, tmp_path):
        """A state-level future cannot carry the token, so the drop must be audible.

        State futures take priority by design (id-collision safety), and they
        hold a bool — so an override addressed at one is discarded. Harmless
        today (a background approval has no batch to cascade to), but silence is
        what makes the next decision token repeat the discovery.
        """
        state, _ = _make_state(tmp_path)
        slot = _make_slot()
        state._slots[slot.key] = slot

        loop = asyncio.get_running_loop()
        state_fut: asyncio.Future[bool] = loop.create_future()
        state._approval_futures["req-bg"] = state_fut

        with patch.object(state, "_log") as log:
            assert state.resolve_approval("req-bg", False, rejected_once=True) is True
        assert state_fut.result() is False
        assert log.warning.called
        assert "rejected_once" in repr(log.warning.call_args)

    @pytest.mark.asyncio
    async def test_api_reject_once_maps_to_decision_override(self):
        """The HTTP seam: ``reject_once`` is a valid action and sets the flag.

        Pinned at the handler, not in redux: a frontend-only test passes even when
        the action never reaches ``resolve_approval``, leaving Deny once wired to
        the plain batch reject in production.
        """
        state = MagicMock()
        state.resolve_approval.return_value = True

        async def _call(action: str):
            request = MagicMock()
            request.get = {"app": ""}.get  # the dashboard user: an empty app claim
            request.app = {"state": state}
            request.match_info = {"id": "req-9", "action": action}
            return await api_approval_resolve(request)

        assert (await _call("reject_once")).status == 200
        assert state.resolve_approval.call_args.args == ("req-9", False)
        assert state.resolve_approval.call_args.kwargs["rejected_once"] is True

        # Plain reject must keep the cascading behavior (no override).
        assert (await _call("reject")).status == 200
        assert state.resolve_approval.call_args.kwargs["rejected_once"] is False

        # Unknown actions are still refused.
        assert (await _call("reject_twice")).status == 400

    @pytest.mark.asyncio
    async def test_deny_once_is_distinguishable_in_the_audit_log(self, tmp_path):
        """SEL records ``rejected_once``.

        A hard-coded ``"rejected"`` would make a single-tool denial and a
        whole-batch denial identical in the audit trail, which is the one place
        the distinction has to survive.
        """
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot()
        evt = _permission_event(title="tool_a")
        evt.request_id = "req-1"
        _set_stream(client, [evt, _complete_event()])

        answerer = asyncio.get_event_loop().create_task(
            _answer_approval(slot, "req-1", "rejected_once")
        )
        with patch("kiro_crew.dashboard.chat_runner.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            with _patch_stats():
                await _run_chat(state, slot, "hello")
            await _drain(answerer)
            outcomes = [
                c.kwargs.get("outcome")
                for c in mock_sel.return_value.log_tool_invocation.call_args_list
                if c.kwargs.get("tool_name") == "tool_a"
            ]
        assert outcomes == ["rejected_once"]


class TestToolCompletionTracking:
    """Verify tool completion state tracking."""

    @pytest.mark.asyncio
    async def test_trust_mode_with_tool_call_id(self, tmp_path):
        """Trust mode auto-approve broadcasts tool_call_id in WS."""
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot(trust=True)
        evt = _permission_event()
        evt.tool_call_id = "tc-42"
        _set_stream(client, [evt, _complete_event()])

        with _patch_stats():
            await _run_chat(state, slot, "hello")

        calls = [c for c in state.broadcast_ws.call_args_list if c[0][0] == "tool_call"]
        assert len(calls) > 0
        assert calls[0][0][1]["tool_call_id"] == "tc-42"


class TestBackgroundApprovalDenyFast:
    """Background sources (cron/heartbeat/taskrunner) deny-fast on a short window
    instead of burning the full 2h human window (F4).

    No human is present for unattended turns, so request_approval must wait only
    _BACKGROUND_APPROVAL_TIMEOUT_SECS and then deny. Interactive sources keep the
    long _APPROVAL_TIMEOUT window.
    """

    @pytest.mark.asyncio
    async def test_background_uses_short_window_and_denies(self, tmp_path, monkeypatch):
        state, _ = _make_state(tmp_path)
        captured = {}

        async def _fake_wait_for(fut, timeout):
            captured["timeout"] = timeout
            fut.cancel()  # don't leave a dangling future
            raise asyncio.TimeoutError

        monkeypatch.setattr("kiro_crew.dashboard.state.asyncio.wait_for", _fake_wait_for)

        result = await state.request_approval("req-bg", "heartbeat", "fs_write", is_background=True)

        assert result is False  # deny-fast on expiry
        assert captured["timeout"] == DashboardState._BACKGROUND_APPROVAL_TIMEOUT_SECS
        # The short window is far below the 2h human window.
        assert captured["timeout"] < DashboardState._APPROVAL_TIMEOUT
        # Pending state cleaned up.
        assert "req-bg" not in state._pending_approvals
        assert "req-bg" not in state._approval_futures

    @pytest.mark.asyncio
    async def test_interactive_uses_long_window(self, tmp_path, monkeypatch):
        state, _ = _make_state(tmp_path)
        captured = {}

        async def _fake_wait_for(fut, timeout):
            captured["timeout"] = timeout
            fut.cancel()
            raise asyncio.TimeoutError

        monkeypatch.setattr("kiro_crew.dashboard.state.asyncio.wait_for", _fake_wait_for)

        # Default is_background=False — interactive dashboard/slack path.
        result = await state.request_approval("req-ui", "dashboard", "fs_write")

        assert result is False  # timeout still denies (pauses) for interactive
        assert captured["timeout"] == DashboardState._APPROVAL_TIMEOUT

    @pytest.mark.asyncio
    async def test_background_approve_before_timeout_returns_true(self, tmp_path):
        """A background approval that IS answered in time still approves."""
        state, _ = _make_state(tmp_path)

        async def _approve_soon():
            # flake-ok: pre-existing 10 ms delay before the answer lands inside the approval window -- moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing 10 ms delay before the answer lands inside the approval window moved verbatim
            await asyncio.sleep(0.01)
            state.resolve_approval("req-bg2", True)

        asyncio.get_event_loop().create_task(_approve_soon())
        result = await state.request_approval("req-bg2", "cron", "fs_write", is_background=True)
        assert result is True


class TestExpiredApprovalRetiresTheCard:
    """An expired or cancelled coordinator wait retires its rendered card.

    The card is client-injected from the WS ``approval`` frame and retires
    through one ``approval_resolved`` broadcast. Slot permission rows belong
    to the chat-runner registry and stay untouched when per-connection request
    ids collide. A normally resolved approval broadcasts exactly once.
    """

    @staticmethod
    def _expire_fast(state, monkeypatch, *, background: bool = False) -> None:
        """Shrink the real approval window so the genuine ``wait_for`` expires.

        Shadows the class default on the instance rather than patching
        ``asyncio.wait_for`` process-wide, so these tests exercise the
        coordinator's own timeout/cancellation path — and cannot pass because
        an unrelated ``wait_for`` on the same path was force-cancelled.
        """
        attr = "_BACKGROUND_APPROVAL_TIMEOUT_SECS" if background else "_APPROVAL_TIMEOUT"
        monkeypatch.setattr(state, attr, 0.01)

    @staticmethod
    def _slot_with_permission(state, request_id: str) -> _ChatSlot:
        """A registered slot whose transcript renders one pending approval bar."""
        import json

        slot = _make_slot()
        slot.append(
            "permission", "fs_write", json.dumps({"request_id": request_id}), broadcast=False
        )
        slot._dirty = False
        state._slots[slot.key] = slot
        return slot

    @staticmethod
    def _resolved_broadcasts(state) -> list:
        return [c for c in state.broadcast_ws.call_args_list if c[0][0] == "approval_resolved"]

    @staticmethod
    async def _register_request(state, request_id: str, slot_key: str) -> asyncio.Task:
        """Start a real (unexpired) approval wait and return once it registered.

        Polls ``_pending_approvals`` — registered in the same synchronous block
        as the future — so this waits on registration itself, never on the
        clock relative to the future (see TestApprovalAnswerersDoNotRaceTheStream).
        """
        task = asyncio.get_running_loop().create_task(
            state.request_approval(request_id, "dashboard", "fs_write", slot=slot_key)
        )
        await async_wait_until(
            lambda: request_id in state._pending_approvals,
            describe=lambda: (
                f"approval {request_id!r} was never registered — the wait "
                f"never started, so there is nothing to expire or resolve"
            ),
        )
        return task

    @pytest.mark.asyncio
    async def test_slot_scoped_expiry_retires_the_card(self, tmp_path, monkeypatch):
        """Timeout broadcasts retirement without mutating a foreign permission row."""
        import json

        state, _ = _make_state(tmp_path)
        slot = self._slot_with_permission(state, "req-exp")
        mock_sel = MagicMock()
        monkeypatch.setattr("kiro_crew.dashboard.state.sel", lambda: mock_sel)
        self._expire_fast(state, monkeypatch)

        result = await state.request_approval("req-exp", "dashboard", "fs_write", slot=slot.key)

        assert result is False
        cls = json.loads(slot.messages[-1]["cls"])
        assert "resolved" not in cls
        assert slot._dirty is False
        resolved = self._resolved_broadcasts(state)
        assert len(resolved) == 1
        # The client cannot derive "expired" from ``approved``; without the
        # decision in the frame the card would render as a rejection.
        assert resolved[0][0][1] == {
            "id": "req-exp",
            "approved": False,
            "slot": slot.key,
            "decision": "expired",
        }
        mock_sel.log_tool_invocation.assert_called_once_with(
            session_key=slot.key,
            tool_name="approval_decision",
            outcome="expired",
            request_id="req-exp",
            source="dashboard",
        )
        assert "req-exp" not in state._pending_approvals
        assert "req-exp" not in state._approval_futures

    @pytest.mark.asyncio
    async def test_expiry_keeps_the_slot_key_after_the_slot_is_gone(self, tmp_path, monkeypatch):
        """Timeout frames retain the owning slot key after slot removal."""
        state, _ = _make_state(tmp_path)
        slot_key = "slot-removed-during-wait"
        assert slot_key not in state._slots
        self._expire_fast(state, monkeypatch)

        result = await state.request_approval(
            "req-removed-slot", "dashboard", "fs_write", slot=slot_key
        )

        assert result is False
        resolved = self._resolved_broadcasts(state)
        assert len(resolved) == 1
        assert resolved[0][0][1] == {
            "id": "req-removed-slot",
            "approved": False,
            "slot": slot_key,
            "decision": "expired",
        }

    @pytest.mark.asyncio
    async def test_state_level_expiry_broadcasts_only(self, tmp_path, monkeypatch):
        """A background approval has no slot messages: broadcast, no marker."""
        state, _ = _make_state(tmp_path)
        marker = MagicMock(return_value=True)
        monkeypatch.setattr("kiro_crew.dashboard.state._mark_permission_resolved", marker)
        self._expire_fast(state, monkeypatch, background=True)

        result = await state.request_approval("req-bg", "cron", "fs_write", is_background=True)

        assert result is False
        marker.assert_not_called()
        resolved = self._resolved_broadcasts(state)
        assert len(resolved) == 1
        # Session key "state" carries no slot in the payload.
        assert resolved[0][0][1] == {"id": "req-bg", "approved": False, "decision": "expired"}
        assert "req-bg" not in state._pending_approvals
        assert "req-bg" not in state._approval_futures

    @pytest.mark.asyncio
    async def test_cancellation_retires_like_timeout(self, tmp_path):
        """A cancelled wait retires through the same broadcast as a timeout."""
        state, _ = _make_state(tmp_path)
        slot = self._slot_with_permission(state, "req-can")
        task = await self._register_request(state, "req-can", slot.key)

        task.cancel()
        # flake-ok: pre-existing await of the approval wait the test has just cancelled or answered -- moved verbatim; tracked for a de-flake pass
        result = await task

        assert result is False
        assert len(self._resolved_broadcasts(state)) == 1
        assert "req-can" not in state._pending_approvals
        assert "req-can" not in state._approval_futures

    @pytest.mark.asyncio
    async def test_resolved_approval_retires_exactly_once(self, tmp_path):
        """The healthy path emits one broadcast and leaves foreign rows untouched."""
        import json

        state, _ = _make_state(tmp_path)
        slot = self._slot_with_permission(state, "req-ok")
        task = await self._register_request(state, "req-ok", slot.key)

        assert state.resolve_approval("req-ok", True) is True
        # flake-ok: pre-existing await of the approval wait the test has just cancelled or answered -- moved verbatim; tracked for a de-flake pass
        result = await task

        assert result is True
        resolved = self._resolved_broadcasts(state)
        assert len(resolved) == 1
        # A decided approval carries no decision key: the client derives it.
        # ``resolve_state`` keys the broadcast "state", so no slot rides along.
        assert resolved[0][0][1] == {"id": "req-ok", "approved": True}
        cls = json.loads(slot.messages[-1]["cls"])
        assert "resolved" not in cls
        assert "req-ok" not in state._pending_approvals
        assert "req-ok" not in state._approval_futures

    @pytest.mark.asyncio
    async def test_rejected_approval_payload_carries_no_decision(self, tmp_path):
        """A real rejection shares ``approved=False`` with expiry but stays bare."""
        state, _ = _make_state(tmp_path)
        slot = self._slot_with_permission(state, "req-no")
        task = await self._register_request(state, "req-no", slot.key)

        assert state.resolve_approval("req-no", False) is True
        # flake-ok: pre-existing await of the approval wait the test has just cancelled or answered -- moved verbatim; tracked for a de-flake pass
        result = await task

        assert result is False
        resolved = self._resolved_broadcasts(state)
        assert len(resolved) == 1
        assert resolved[0][0][1] == {"id": "req-no", "approved": False}
        assert "req-no" not in state._pending_approvals
        assert "req-no" not in state._approval_futures

    @pytest.mark.asyncio
    async def test_broadcast_failure_still_pops(self, tmp_path, monkeypatch):
        """A raising broadcast does not leak coordinator registry entries."""
        state, _ = _make_state(tmp_path)
        slot = self._slot_with_permission(state, "req-ws")
        monkeypatch.setattr(
            state,
            "_audit_and_broadcast_approval",
            MagicMock(side_effect=RuntimeError("ws boom")),
        )
        self._expire_fast(state, monkeypatch)

        result = await state.request_approval("req-ws", "dashboard", "fs_write", slot=slot.key)

        assert result is False
        assert "req-ws" not in state._pending_approvals
        assert "req-ws" not in state._approval_futures

    @pytest.mark.asyncio
    async def test_expiry_does_not_clobber_an_already_decided_row(self, tmp_path, monkeypatch):
        """A colliding chat-runner row keeps its resolved trust decision."""
        import json

        state, _ = _make_state(tmp_path)
        slot = _make_slot()
        slot.append(
            "permission",
            "fs_write",
            json.dumps({"request_id": "req-keep", "resolved": "trust"}),
            broadcast=False,
        )
        slot._dirty = False
        state._slots[slot.key] = slot
        self._expire_fast(state, monkeypatch)

        result = await state.request_approval("req-keep", "dashboard", "fs_write", slot=slot.key)

        assert result is False
        cls = json.loads(slot.messages[-1]["cls"])
        assert cls["resolved"] == "trust"
        assert slot._dirty is False
        assert len(self._resolved_broadcasts(state)) == 1
        assert "req-keep" not in state._pending_approvals
        assert "req-keep" not in state._approval_futures


class TestStateMetaAndPermissions:
    """Cover state.py meta handling and permission resolution."""

    def test_append_with_meta(self):
        slot = _make_slot()
        slot.append(
            "tool", "test", meta={"tool_call_id": "tc-1", "purpose": "testing"}, broadcast=False
        )
        assert slot.messages[-1]["meta"]["tool_call_id"] == "tc-1"

    def test_mark_permission_resolved(self):
        import json

        slot = _make_slot()
        cls_data = json.dumps({"request_id": "req-42"})
        slot.append("permission", "tool_x", cls_data, broadcast=False)
        slot.mark_permission_resolved("req-42", "rejected")
        updated = json.loads(slot.messages[-1]["cls"])
        assert updated["resolved"] == "rejected"

    def test_mark_permission_resolved_not_found(self):
        slot = _make_slot()
        slot.append("permission", "tool_x", '{"request_id": "req-42"}', broadcast=False)
        slot.mark_permission_resolved("nonexistent", "approved")
        assert slot.messages[-1]["cls"] == '{"request_id": "req-42"}'

    def test_parse_cls_meta_normalizes_request_id(self):
        meta = parse_cls_meta('{"request_id": "req-1", "tool_input": "x"}')
        assert "approval_id" in meta
        assert "request_id" not in meta

    def test_meta_stored_on_message(self):
        slot = _make_slot()
        slot.append("tool", "test", meta={"tool_call_id": "tc-1"}, broadcast=False)
        assert slot.messages[-1].get("meta", {}).get("tool_call_id") == "tc-1"
