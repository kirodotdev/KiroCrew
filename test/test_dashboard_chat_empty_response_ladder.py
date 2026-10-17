"""Dashboard chat: the empty-response ladder — requeue, auto-continue, give-up notice."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from test_dashboard_chat_empty_response import TestEmptyResponseRetry as _EmptyRetrySuite

from kiro_crew.dashboard.state import _ChatSlot


class TestEmptyResponseRetryLadder:
    """TestEmptyResponseRetry: each rung of the empty-response ladder and what resets it."""

    _make_state_and_slot = _EmptyRetrySuite._make_state_and_slot
    _make_empty_stream = _EmptyRetrySuite._make_empty_stream
    _cancel_background_tasks = staticmethod(_EmptyRetrySuite._cancel_background_tasks)

    @pytest.mark.asyncio
    async def test_first_empty_response_requeues_message(self, tmp_path: Path) -> None:
        """First empty response at depth 0 → message re-queued silently."""
        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        self._make_empty_stream(client)

        # Spy on queue_insert to verify the message is ACTUALLY re-queued (the
        # behavior the test name promises) — not merely that the counter ticked.
        calls = []
        callbacks = []
        directive_origins = []
        directive_channel_origins = []
        on_consumed = MagicMock()
        orig = _ChatSlot.queue_insert

        def spy(self_slot, *a, **kw):
            calls.append(a)
            callbacks.append(kw.get("on_consumed"))
            directive_origins.append(kw.get("directive_user_origin"))
            directive_channel_origins.append(kw.get("directive_channel_origin"))
            return orig(self_slot, *a, **kw)

        with (
            patch.object(_ChatSlot, "queue_insert", spy),
            patch("kiro_crew.dashboard.chat_runner.save_slot_off_loop") as mock_save,
            patch("kiro_crew.dashboard.chat_runner._maybe_consolidate") as mock_consolidate,
            patch("kiro_crew.dashboard.chat_runner._flush_file_changes") as mock_flush,
            patch(
                "kiro_crew.dashboard.chat_runner._start_next_queued_turn",
                new=AsyncMock(return_value=False),
            ),
        ):
            # The behavior under test is the re-queue itself. Keep the queued
            # item in the slot, but stop the finally block from immediately
            # launching a second turn. Patching the queue-drain boundary avoids
            # globally replacing asyncio.create_task, which can otherwise let
            # unrelated lifecycle tasks race the assertions under xdist load.
            await _run_chat(
                state,
                slot,
                "test message",
                _directive_user_origin=True,
                _directive_channel_origin=True,
                _on_consumed=on_consumed,
            )
            background_tasks = list(state._background_tasks)
            for _bg_task in background_tasks:
                _bg_task.cancel()
            if background_tasks:
                await asyncio.gather(*background_tasks, return_exceptions=True)

        assert slot._empty_response_retries == 1
        # The message must be re-queued at the front of the queue.
        assert (0, "test message") in calls
        assert callbacks[0] is on_consumed
        assert directive_origins == [True]
        assert directive_channel_origins == [True]
        assert [args.args for args in on_consumed.call_args_list] == [(True,), (False,)]
        # No notice card shown on first attempt — the empty is silently re-queued
        notice_msgs = [m for m in slot.messages if m.get("role") == "notice"]
        assert not any("returned nothing this turn" in m.get("content", "") for m in notice_msgs)
        # Re-queue path must NOT persist/consolidate the spurious empty turn or
        # record success (item 3 of the CR). The only saves are the in-flight
        # marker's: this slot has no transcript yet, so admission falls back to
        # the forced save that creates it, and the finally clears the marker
        # with a second one before the queued retry receives its own generation.
        assert mock_save.await_count == 2
        for marker_save in mock_save.await_args_list:
            assert marker_save.args == (state, slot)
            # Both marker saves are pinned to this slot incarnation so a
            # cancelled runner outliving a same-key replacement writes nothing.
            assert marker_save.kwargs == {
                "force": True,
                "expected_history_key": "dashboard:empty-resp-slot",
                "expected_slot_name": "empty-resp-slot",
            }
        mock_consolidate.assert_not_called()
        state.sessions.record_success.assert_not_called()
        # _flush_file_changes is intentionally NOT skipped: the try-body call (inside
        # the `if not _retrying_empty` guard) is skipped, but the finally block calls
        # it once unconditionally ("ensure file changes always surface, even on
        # cancel/error"). Scope the assertion to this slot because finalization of a
        # task created by another test can run while this module-level function is
        # patched and must not change the behavior observed for this turn.
        own_flushes = [
            mock_call for mock_call in mock_flush.call_args_list if mock_call.args[:1] == (slot,)
        ]
        assert len(own_flushes) == 1

    @pytest.mark.asyncio
    async def test_empty_response_at_depth_gt0_shows_error_immediately(
        self, tmp_path: Path
    ) -> None:
        """At _prompt_depth>0 an empty response is NOT silently retried — it shows the
        terminal empty-response notice card on the FIRST empty. The silent
        re-queue is intentionally depth-0 only (nested tool-use turns must not silently
        re-queue and risk a runaway loop)."""
        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        self._make_empty_stream(client)

        await _run_chat(state, slot, "test message", _prompt_depth=1)

        # depth>0: the `if _prompt_depth == 0 ...` guard is false, so the else fires —
        # terminal card immediately, no silent retry, no increment of the counter.
        notice_msgs = [m for m in slot.messages if m.get("role") == "notice"]
        assert any("returned nothing this turn" in m.get("content", "") for m in notice_msgs)
        assert slot._empty_response_retries == 0

    @pytest.mark.asyncio
    async def test_second_empty_response_auto_continues(self, tmp_path: Path) -> None:
        """Second consecutive empty response → ONE synthetic continue nudge is
        queued (same live session) with a transcript-visible notice. Re-sending
        the identical prompt reproduces the identical empty generation; a
        DIFFERENT message reliably recovers — this automates the user manually
        typing "continue"."""
        from kiro_crew.dashboard.chat_runner import _EMPTY_AUTO_CONTINUE_MSG

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        slot._empty_response_retries = 1  # already silently retried once
        self._make_empty_stream(client)

        calls = []
        orig = _ChatSlot.queue_insert

        def spy(self_slot, *a, **kw):
            calls.append(a)
            return orig(self_slot, *a, **kw)

        with patch.object(_ChatSlot, "queue_insert", spy):
            await _run_chat(state, slot, "test message")
            await self._cancel_background_tasks(state)

        # The nudge (NOT the original message) is queued at the front.
        assert (0, _EMPTY_AUTO_CONTINUE_MSG) in calls
        assert (0, "test message") not in calls
        # Visible recovery notice, counter advanced to the terminal rung, and
        # the recovery turn is excluded from the cycle-complete counter reset.
        notice_msgs = [m for m in slot.messages if m.get("role") == "notice"]
        assert any("auto-continuing once" in m.get("content", "") for m in notice_msgs)
        assert slot._empty_response_retries == 2

    @pytest.mark.asyncio
    async def test_auto_continue_nudge_drains_as_inject_not_user(self, tmp_path: Path) -> None:
        """The queued nudge is runner orchestration, not user speech: when the
        queue drains it, the transcript append MUST use the "inject" recovery
        role (never "user" — a user-role append would persist an internal
        instruction as user-authored history and mirror it to linked
        channels), and it MUST NOT cancel a pending synthesis (the user did
        not take over the conversation)."""
        from kiro_crew.dashboard.chat_runner import _EMPTY_AUTO_CONTINUE_MSG

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        slot._empty_response_retries = 1  # next empty triggers the nudge rung
        slot._pending_synthesis = True
        self._make_empty_stream(client)

        await _run_chat(state, slot, "test message")
        # Let the REAL detached drain task run: it pops the nudge, classifies
        # it, appends it to the transcript, and runs the (also-empty) nudge
        # turn, which terminates the ladder at the give-up notice.
        for _bg_task in list(state._background_tasks):
            try:
                await _bg_task
            except Exception:
                pass

        nudge_msgs = [m for m in slot.messages if m.get("content") == _EMPTY_AUTO_CONTINUE_MSG]
        assert nudge_msgs, "drained nudge never reached the transcript"
        # The nudge must NEVER carry the user role (that would persist an
        # internal instruction as user-authored history and mirror it to
        # linked channels) — the ORIGINAL user message keeps its user role.
        assert all(m.get("role") == "inject" for m in nudge_msgs)
        # Draining a synthetic recovery message is not a user takeover.
        assert slot._pending_synthesis is True

    @pytest.mark.asyncio
    async def test_third_empty_response_shows_notice(self, tmp_path: Path) -> None:
        """Third consecutive empty (the auto-continue nudge ALSO produced
        nothing) → terminal notice card; the ladder is bounded, never loops."""
        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        slot._empty_response_retries = 2  # re-queue + nudge both spent
        self._make_empty_stream(client)

        await _run_chat(state, slot, "test message")

        notice_msgs = [m for m in slot.messages if m.get("role") == "notice"]
        assert any("returned nothing this turn" in m.get("content", "") for m in notice_msgs)
        # After the terminal notice, the counter resets so the NEXT independent
        # user turn gets a fresh budget (not sticky).
        assert slot._empty_response_retries == 0

    @pytest.mark.asyncio
    async def test_productive_giveup_notice_does_not_claim_nothing_happened(
        self, tmp_path: Path
    ) -> None:
        """A PRODUCTIVE turn reaching give-up (its one continuation also ended
        without a closing reply) must not be described as "returned nothing":
        the card is read by the model via the transcript, and telling it the
        completed work produced no output invites a redo of side effects that
        already landed. Same distinction the continue rung draws one rung up."""
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TOOL_CALL,
            EVENT_TOOL_RESULT,
            LLMEvent,
        )

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        slot._empty_response_retries = 2  # the single continuation is spent

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_TOOL_CALL, tool_call_id="tc-1", title="send_message")
            yield LLMEvent(kind=EVENT_TOOL_RESULT, tool_call_id="tc-1", text="sent")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        client.stream = _stream
        client.stream_command = _stream

        await _run_chat(state, slot, "test message")
        await self._cancel_background_tasks(state)

        notice_msgs = [m for m in slot.messages if m.get("role") == "notice"]
        assert notice_msgs, "give-up produced no notice card"
        assert not any("returned nothing" in m.get("content", "") for m in notice_msgs), (
            "the give-up card claims the turn produced nothing, but this turn "
            "ran a tool whose side effects already landed"
        )
        assert any("without a closing reply" in m.get("content", "") for m in notice_msgs)
        # The card reassures rather than inviting a redo: completed steps stay done.
        assert any("will not re-run" in m.get("content", "") for m in notice_msgs)
        # Terminal rung still resets the budget for the next genuine user turn.
        assert slot._empty_response_retries == 0

    @pytest.mark.asyncio
    async def test_productive_episode_giveup_keeps_no_rerun_wording(self, tmp_path: Path) -> None:
        """A give-up whose EPISODE was productive must keep the productive
        wording, even though the final continuation turn itself produced
        nothing.

        The activity snapshot is rebuilt per turn, so the continuation that
        streams nothing arrives at give-up with `productive=False`. The counter
        cannot rescue it either: the productive jump (`= 2`) and the plain
        non-productive ladder (`+= 1` twice) both land on 2. Without
        episode-scoped state the card takes the counter>0 wording, whose "Just
        send your message again to continue." is the same redo invitation the
        continue rung removes one rung up — and the first turn of THIS episode
        already ran a tool whose side effects landed.
        """
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TOOL_CALL,
            EVENT_TOOL_RESULT,
            LLMEvent,
        )

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)

        _turns = {"n": 0}

        async def _stream(msg):
            _turns["n"] += 1
            if _turns["n"] == 1:
                # Productive: a tool ran, no closing reply. Takes the CONTINUE
                # rung and queues the activity continuation.
                yield LLMEvent(kind=EVENT_TOOL_CALL, tool_call_id="tc-1", title="send_message")
                yield LLMEvent(kind=EVENT_TOOL_RESULT, tool_call_id="tc-1", text="sent")
                yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
            else:
                # The continuation returns nothing at all -> give-up.
                yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        client.stream = _stream
        client.stream_command = _stream

        await _run_chat(state, slot, "test message")
        # Let the real drain task run the queued continuation turn, which is
        # the turn that reaches give-up.
        for _bg_task in list(state._background_tasks):
            try:
                await _bg_task
            except Exception:
                pass
        await self._cancel_background_tasks(state)

        assert _turns["n"] >= 2, "the queued continuation never ran"
        notice_msgs = [m for m in slot.messages if m.get("role") == "notice"]
        giveup = [
            m
            for m in notice_msgs
            if "will not re-run" in m.get("content", "")
            or "returned nothing this turn" in m.get("content", "")
        ]
        assert giveup, "give-up produced no notice card"
        # The episode ran a tool: no card may invite the user to re-send.
        assert not any("send your message again" in m.get("content", "") for m in notice_msgs), (
            "the give-up card invites a resend, but the first turn of this "
            "episode ran a tool whose side effects already landed"
        )
        assert any("will not re-run" in m.get("content", "") for m in notice_msgs)
        # Terminal rung clears both the counter and the episode flag.
        assert slot._empty_response_retries == 0
        assert slot._empty_episode_productive is False

    @pytest.mark.asyncio
    async def test_discarded_episode_cannot_speak_for_a_later_turn(self, tmp_path: Path) -> None:
        """An episode whose continuation was thrown away must not lend its
        productive wording to the user's next, unrelated turn.

        Several controls discard a queued entry -- the hard-kill Stop, a plan
        Cancel, a rewind commit -- and none of them resets the recovery counter,
        so the spent counter routes the next empty turn straight to give-up. The
        flag is therefore read only on the ladder's OWN continuation: a discarded
        continuation takes that turn with it, so no per-site bookkeeping is
        needed and a control added later cannot reopen this.
        """
        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        # What a productive CONTINUE rung leaves behind when its continuation
        # never runs: budget spent, flag set, no landed turn to clear either.
        slot._empty_response_retries = 2
        slot._empty_episode_productive = True
        self._make_empty_stream(client)

        # A genuine user turn, NOT the ladder's continuation.
        await _run_chat(state, slot, "a different request")
        await self._cancel_background_tasks(state)

        notice_msgs = [m for m in slot.messages if m.get("role") == "notice"]
        assert notice_msgs, "give-up produced no notice card"
        assert not any("will not re-run" in m.get("content", "") for m in notice_msgs), (
            "a discarded episode's flag told an unrelated turn that its "
            "completed steps will not re-run, but this turn ran nothing"
        )
        assert any("returned nothing this turn" in m.get("content", "") for m in notice_msgs)
        assert slot._empty_response_retries == 0
        assert slot._empty_episode_productive is False

    @pytest.mark.asyncio
    async def test_user_typed_continuation_text_ends_the_episode(self, tmp_path: Path) -> None:
        """The continuation bodies are fixed runner-authored strings, so a user
        can type or paste one. Such a turn is still user speech: it ends the
        episode rather than continuing it.

        User speech carries no structural payload marker, which is what separates
        it from the runner's own recovery dispatches. Keying the episode on the
        bodies instead would hand a user-authored turn the productive wording for
        work it never ran.
        """
        from kiro_crew.dashboard.chat_utils import _ACTIVITY_NO_REPLY_CONTINUE_MSG

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        slot._empty_response_retries = 2
        slot._empty_episode_productive = True
        self._make_empty_stream(client)

        # The exact continuation body, but dispatched as user speech: no
        # synthetic-recovery marker rides with it.
        await _run_chat(state, slot, _ACTIVITY_NO_REPLY_CONTINUE_MSG)
        await self._cancel_background_tasks(state)

        notice_msgs = [m for m in slot.messages if m.get("role") == "notice"]
        assert notice_msgs, "give-up produced no notice card"
        assert not any("will not re-run" in m.get("content", "") for m in notice_msgs), (
            "a user-authored turn matching the continuation body was read as "
            "the ladder's own continuation, so a stale flag spoke for it"
        )
        assert any("returned nothing this turn" in m.get("content", "") for m in notice_msgs)

    @pytest.mark.asyncio
    async def test_a_runner_queued_continuation_keeps_the_productive_wording(
        self, tmp_path: Path
    ) -> None:
        """A runner-authored continuation from ANY family, running inside an
        active productive episode, keeps the no-rerun wording.

        The runner can put another recovery ahead of the ladder's own: a Stop
        hook answering `decision: block` prepends at index 0 in the same teardown
        that just queued the ladder's continuation there, so it dispatches first.
        It is the same episode -- the tools of its productive first turn have
        already landed -- so if it also returns nothing, its give-up must not
        invite a resend. Keying the episode on the two continuation bodies would
        fail exactly here, which is why it is keyed on user origin instead.
        """
        from kiro_crew.dashboard.chat_utils import _PROMISE_ONLY_CONTINUE_MSG

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        # Mid-episode: a productive turn has spent the budget and set the flag.
        slot._empty_response_retries = 2
        slot._empty_episode_productive = True
        self._make_empty_stream(client)

        # Another family's continuation: runner-authored, so it carries the
        # structural payload marker, and its body is not the ladder's.
        await _run_chat(
            state,
            slot,
            _PROMISE_ONLY_CONTINUE_MSG,
            _synthetic_payload=True,
            _synthetic_recovery_turn=True,
        )
        await self._cancel_background_tasks(state)

        notice_msgs = [m for m in slot.messages if m.get("role") == "notice"]
        assert notice_msgs, "give-up produced no notice card"
        assert not any("send your message again" in m.get("content", "") for m in notice_msgs), (
            "a runner-queued continuation inside a productive episode was told "
            "to resend, but that episode's first turn already ran a tool"
        )
        assert any("will not re-run" in m.get("content", "") for m in notice_msgs)

    @pytest.mark.asyncio
    async def test_discarded_episode_cannot_ride_a_later_turns_own_continuation(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """With a continue budget above one, a later turn earns a continuation of
        its own -- and that turn must not inherit a discarded episode's wording.

        This is the case a guard at the give-up rung cannot catch. A productive
        turn leaves the counter at 2 and the flag set; its continuation is
        discarded, so nothing lands and neither resets. With a budget of 2 the
        next unrelated empty turn still passes rung 2 (`2 < 1 + 2`) and queues its
        OWN continuation, which genuinely IS one of the ladder's bodies carrying
        the payload marker. The evidence therefore has to be dropped when the
        episode ends, not merely disbelieved when it is read.
        """
        from kiro_crew.dashboard import chat_runner as cr

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        monkeypatch.setattr(cr, "_empty_max_auto_continues", lambda: 2)
        # What a productive rung-2 turn leaves behind when its continuation is
        # thrown away: budget spent, flag set, no landed turn.
        slot._empty_response_retries = 2
        slot._empty_episode_productive = True
        self._make_empty_stream(client)

        # A genuine user turn, which under this budget gets its own continuation.
        await _run_chat(state, slot, "a different request")
        for _bg_task in list(state._background_tasks):
            try:
                await _bg_task
            except Exception:
                pass
        await self._cancel_background_tasks(state)

        notice_msgs = [m for m in slot.messages if m.get("role") == "notice"]
        assert notice_msgs, "give-up produced no notice card"
        assert not any("will not re-run" in m.get("content", "") for m in notice_msgs), (
            "a discarded episode's flag rode a later turn's own ladder "
            "continuation and claimed its completed steps will not re-run"
        )

    @pytest.mark.asyncio
    async def test_productive_episode_survives_a_second_continuation(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A productive episode keeps its wording across EVERY continuation it is
        budgeted for, not just the first.

        With a budget of 2 the episode runs two continuations, and both carry
        `_ACTIVITY_NO_REPLY_CONTINUE_MSG` under the "from what already ran"
        notice. The second follows a continuation that streamed nothing, so the
        per-turn activity snapshot alone reads it as a plain empty turn: a
        "returned nothing" notice and `_EMPTY_AUTO_CONTINUE_MSG` there would tell
        the model its episode produced no output and invite a redo of tools that
        already ran. Both continuations are this ladder's own, so the episode's
        evidence also reaches give-up, which must not invite a resend.
        """
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard import chat_runner as cr
        from kiro_crew.dashboard.chat_utils import (
            _ACTIVITY_NO_REPLY_CONTINUE_MSG,
            _EMPTY_AUTO_CONTINUE_MSG,
        )
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TOOL_CALL,
            EVENT_TOOL_RESULT,
            LLMEvent,
        )

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        monkeypatch.setattr(cr, "_empty_max_auto_continues", lambda: 2)

        _turns = {"n": 0}

        async def _stream(msg):
            _turns["n"] += 1
            if _turns["n"] == 1:
                # Productive: a tool ran, no closing reply.
                yield LLMEvent(kind=EVENT_TOOL_CALL, tool_call_id="tc-1", title="send_message")
                yield LLMEvent(kind=EVENT_TOOL_RESULT, tool_call_id="tc-1", text="sent")
                yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
            else:
                # Both continuations return nothing, walking the ladder to give-up.
                yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        client.stream = _stream
        client.stream_command = _stream

        calls: list[tuple] = []
        orig = _ChatSlot.queue_insert

        def spy(self_slot, *a, **kw):
            calls.append(a)
            return orig(self_slot, *a, **kw)

        with patch.object(_ChatSlot, "queue_insert", spy):
            await _run_chat(state, slot, "test message")
            # Drain repeatedly: each continuation queues the next one.
            for _round in range(4):
                for _bg_task in list(state._background_tasks):
                    try:
                        await _bg_task
                    except Exception:
                        pass
            await self._cancel_background_tasks(state)

        assert _turns["n"] >= 3, f"the second continuation never ran (turns={_turns['n']})"
        notice_msgs = [m for m in slot.messages if m.get("role") == "notice"]
        assert not any("returned nothing" in m.get("content", "") for m in notice_msgs), (
            "a continuation inside a productive episode was told the model "
            "returned nothing, which invites a redo of tools that already ran"
        )
        assert any(
            "from what already ran (recovery 2 of 2)" in m.get("content", "") for m in notice_msgs
        )
        bodies = [a[1] for a in calls if len(a) > 1]
        assert _EMPTY_AUTO_CONTINUE_MSG not in bodies, (
            "the second continuation queued the empty-response body, which tells "
            "the model its productive episode produced no output"
        )
        assert bodies.count(_ACTIVITY_NO_REPLY_CONTINUE_MSG) == 2
        assert not any("send your message again" in m.get("content", "") for m in notice_msgs), (
            "give-up invited a resend after a productive episode spent its full "
            "continuation budget, so the episode's evidence was lost partway"
        )
        assert any("will not re-run" in m.get("content", "") for m in notice_msgs)

    @pytest.mark.asyncio
    async def test_giveup_without_recoveries_drops_retry_claim(self, tmp_path: Path) -> None:
        """A give-up reached with the recovery counter still at zero (here: a
        nested depth>0 turn) must not assert that a retry and an auto-continue
        ran — neither did. The counter is the evidence, not the rung."""
        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        self._make_empty_stream(client)

        await _run_chat(state, slot, "test message", _prompt_depth=1)

        notice_msgs = [m for m in slot.messages if m.get("role") == "notice"]
        assert any("returned nothing this turn" in m.get("content", "") for m in notice_msgs)
        assert not any("retried" in m.get("content", "") for m in notice_msgs), (
            "the give-up card claims a retry ran, but slot._empty_response_retries "
            "is still 0 on this path"
        )
        assert not any("auto-continued" in m.get("content", "") for m in notice_msgs)
        assert not any("recovery was attempted" in m.get("content", "") for m in notice_msgs)

    @pytest.mark.asyncio
    async def test_giveup_after_spent_recoveries_reports_recovery(self, tmp_path: Path) -> None:
        """When the recovery budget was actually spent (counter exhausted, turn
        not productive), the card reports that automatic recovery was
        attempted. Phrased phase-neutrally on purpose: the counter counts
        budget spent, not which rungs ran."""
        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        slot._empty_response_retries = 2  # re-queue + nudge both spent
        self._make_empty_stream(client)

        await _run_chat(state, slot, "test message")

        notice_msgs = [m for m in slot.messages if m.get("role") == "notice"]
        assert any("automatic recovery was attempted" in m.get("content", "") for m in notice_msgs)
        # The counter alone cannot prove WHICH rungs ran (a productive turn's
        # continuation arrives here at 2 with no verbatim retry), so the card
        # must not name specific recovery phases.
        assert not any("auto-continued automatically" in m.get("content", "") for m in notice_msgs)

    @pytest.mark.asyncio
    async def test_second_empty_flag_off_shows_notice(self, tmp_path: Path) -> None:
        """With session.empty_response_auto_continue disabled, the second empty
        surfaces the terminal notice immediately (pre-feature behavior)."""
        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        slot._empty_response_retries = 1
        self._make_empty_stream(client)

        # Exercise the REAL config path (loader wiring included): persist the
        # flag as false in a config file and point KiroCrewConfig.load() at it,
        # rather than patching the gate function (which would pass even if the
        # loader dropped the field — the exact regression this test guards).
        cfg_file = tmp_path / "flag-off-config.json"
        cfg_file.write_text('{"session": {"empty_response_auto_continue": false}}')
        with patch("kiro_crew.config.loader.config_path", return_value=cfg_file):
            await _run_chat(state, slot, "test message")

        notice_msgs = [m for m in slot.messages if m.get("role") == "notice"]
        assert any("returned nothing this turn" in m.get("content", "") for m in notice_msgs)
        # Counter is 1 here: ONLY the silent verbatim re-queue ran (rung 1's
        # guard ignores the gate); the auto-continue was forbidden by config.
        # The card reports budget spent phase-neutrally and must not assert
        # the auto-continue that provably did not run. This is also the
        # boundary case for the counter gate: budget WAS spent, so the bare
        # zero-recoveries wording would be wrong too.
        assert any("automatic recovery was attempted" in m.get("content", "") for m in notice_msgs)
        assert not any("auto-continued" in m.get("content", "") for m in notice_msgs)
        assert slot._empty_response_retries == 0

    @pytest.mark.asyncio
    async def test_max_continues_config_extends_the_ladder(self, tmp_path: Path) -> None:
        """With session.empty_response_max_continues=3, the second consecutive
        continuation failure keeps continuing — numbered so the transcript
        reads as advancing, not looping — instead of giving up after one.
        Exercises the REAL config path like the flag-off test above, so a
        loader that drops the field fails this test too."""
        from kiro_crew.dashboard.chat_runner import _EMPTY_AUTO_CONTINUE_MSG

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        slot._empty_response_retries = 2  # replay + first continue spent
        self._make_empty_stream(client)

        calls = []
        orig = _ChatSlot.queue_insert

        def spy(self_slot, *a, **kw):
            calls.append(a)
            return orig(self_slot, *a, **kw)

        cfg_file = tmp_path / "max-continues-config.json"
        cfg_file.write_text('{"session": {"empty_response_max_continues": 3}}')
        with (
            patch("kiro_crew.config.loader.config_path", return_value=cfg_file),
            patch.object(_ChatSlot, "queue_insert", spy),
        ):
            await _run_chat(state, slot, "test message")
            await self._cancel_background_tasks(state)

        assert (0, _EMPTY_AUTO_CONTINUE_MSG) in calls
        notice_msgs = [m for m in slot.messages if m.get("role") == "notice"]
        assert any("(recovery 2 of 3)" in m.get("content", "") for m in notice_msgs)
        assert slot._empty_response_retries == 3

    @pytest.mark.asyncio
    async def test_max_continues_budget_still_terminates(self, tmp_path: Path) -> None:
        """The raised budget is still a budget: with max=3 and the replay plus
        all three continues spent, the give-up card fires and the counter
        resets for the next independent user turn."""
        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        slot._empty_response_retries = 4  # replay + 3 continues spent
        self._make_empty_stream(client)

        cfg_file = tmp_path / "max-continues-config.json"
        cfg_file.write_text('{"session": {"empty_response_max_continues": 3}}')
        with patch("kiro_crew.config.loader.config_path", return_value=cfg_file):
            await _run_chat(state, slot, "test message")

        notice_msgs = [m for m in slot.messages if m.get("role") == "notice"]
        assert any("returned nothing this turn" in m.get("content", "") for m in notice_msgs)
        assert slot._empty_response_retries == 0

    @pytest.mark.asyncio
    async def test_successful_response_resets_counter(self, tmp_path: Path) -> None:
        """A successful (non-empty) response resets the retry counter."""
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        slot._empty_response_retries = 1  # had a prior empty

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Hello!")
            yield LLMEvent(kind=EVENT_COMPLETE)

        client.stream = _stream
        client.stream_command = _stream

        await _run_chat(state, slot, "test message")

        assert slot._empty_response_retries == 0

    @pytest.mark.asyncio
    async def test_compaction_turn_no_empty_response_error(self, tmp_path: Path) -> None:
        """Compaction turns set assistant_text='' but should NOT trigger the empty-response notice."""
        from kiro_crew.providers.base import EVENT_COMPACTION_STATUS, EVENT_COMPLETE, LLMEvent

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_COMPACTION_STATUS, text="completed", title="summary")
            yield LLMEvent(kind=EVENT_COMPLETE)

        client.stream = _stream
        client.stream_command = _stream

        await _run_chat(state, slot, "/compact")

        notice_msgs = [m for m in slot.messages if m.get("role") == "notice"]
        assert not any("returned nothing this turn" in m.get("content", "") for m in notice_msgs)

    @pytest.mark.asyncio
    async def test_clear_turn_no_empty_response_error(self, tmp_path: Path) -> None:
        """Clear turns set assistant_text='' but should NOT trigger the empty-response notice."""
        from chat_test_helpers import pin_kiro_cli_harness

        pin_kiro_cli_harness()
        from kiro_crew.providers.base import EVENT_CLEAR_STATUS, EVENT_COMPLETE, LLMEvent

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        state.sessions.provider_switch_replay_pending = MagicMock(return_value=True)
        state.sessions.consume_provider_switch_replay = MagicMock(return_value=True)
        state.sessions.mark_provider_switch_replay = MagicMock(return_value=True)
        state.sessions.commit_provider_switch_replay_sid = MagicMock(return_value=True)

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_CLEAR_STATUS)
            yield LLMEvent(kind=EVENT_COMPLETE)

        client.stream = _stream
        client.stream_command = _stream

        await _run_chat(state, slot, "/clear")

        notice_msgs = [m for m in slot.messages if m.get("role") == "notice"]
        assert not any("returned nothing this turn" in m.get("content", "") for m in notice_msgs)
        state.sessions.consume_provider_switch_replay.assert_called_once_with(
            f"dashboard:{slot.key}"
        )
        state.sessions.commit_provider_switch_replay_sid.assert_called_once_with(
            f"dashboard:{slot.key}"
        )
        state.sessions.mark_provider_switch_replay.assert_not_called()

    @pytest.mark.asyncio
    async def test_agent_switch_turn_no_empty_response_error(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Agent switch turns set assistant_text='' but should NOT trigger the empty-response notice."""
        from kiro_crew.providers.base import EVENT_AGENT_SWITCHED, EVENT_COMPLETE, LLMEvent

        state, slot, client, _run_chat = self._make_state_and_slot(tmp_path)
        monkeypatch.setattr(
            "kiro_crew.config.loader._materialized_kiro_agent",
            lambda name, project_dir=None: name if name == "new-agent" else "",
        )

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_AGENT_SWITCHED, text="new-agent")
            yield LLMEvent(kind=EVENT_COMPLETE)

        client.stream = _stream
        client.stream_command = _stream

        await _run_chat(state, slot, "test message")

        notice_msgs = [m for m in slot.messages if m.get("role") == "notice"]
        assert not any("returned nothing this turn" in m.get("content", "") for m in notice_msgs)
