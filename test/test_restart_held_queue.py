"""A queue restored after a restart is HELD: no owner runs it, and nothing says it will.

Handed back as cards (``docs/system-specs/modules/session.md``, "Restored entries
are handed back as queue CARDS, not dispatched"), a restored entry moves only
when the person acts on it: Send now on the card, an edit, or a delete. These
tests pin each reader that could read such a queue as work about to run:

- the drain passes held entries by (a turn ending, a fresh send, a Resume);
- Continue does not refuse on them, so an interrupted chat keeps its Resume;
- the slot projection and the session status report them apart;
- a dispatched send whose row is already written is off the queue on disk
  before the turn marker lands, so a death between the two never restores the
  cut turn's own opener as a held card;
- Resume and Continue skip the rows of sends still held on their cards.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_runner_coverage_helpers import _slot, _state
from chat_test_helpers import _make_state
from test_chat_slot_continue import _make_app, _mock_state

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_delivery import queue_entry_view
from kiro_crew.dashboard.chat_persistence import _rehydrate_slot_from_history, _save_slot_to_history
from kiro_crew.dashboard.chat_turn.steer_queue import _has_user_queued_followup
from kiro_crew.dashboard.chat_utils import (
    CRON_NOTIFICATION_KIND,
    STEER_INTENT_META,
    SYNTHETIC_RECOVERY_KIND,
)
from kiro_crew.dashboard.slot_queue_repository import (
    RESTORED_QUEUE_KEY,
    is_held_queue_entry,
)
from kiro_crew.dashboard.state import _ChatSlot

_RESTART_KIND = "gateway_restart_interruption"
_RESTART_TEXT = (
    "This turn was interrupted when the app restarted. "
    "Review the partial output above, then resume to continue."
)


def _hold(slot: _ChatSlot, text: str) -> str:
    """Queue *text* as a restore would hand it back: a plain prompt, held."""
    qid = slot.queue_append(text)
    slot._queue[-1][RESTORED_QUEUE_KEY] = True
    return qid


def _interrupted(slot: _ChatSlot) -> None:
    slot.append("user", "first request", "msg msg-u")
    slot.append("assistant", "partial", "msg msg-a")
    slot.append("error", _RESTART_TEXT, "msg msg-err", meta={"kind": _RESTART_KIND})


def _restart_rows(messages: list[dict]) -> list[dict]:
    return [m for m in messages if (m.get("meta") or {}).get("kind") == _RESTART_KIND]


# ── what the slot reports ──────────────────────────────────────────────────


class TestHeldQueueProjection:
    def test_held_entries_are_counted_apart_from_ready_ones(self):
        slot = _ChatSlot("s")
        _hold(slot, "restored one")
        slot.queue_append("sent after the restart")

        assert slot.queue_depth == 2
        assert slot.queue_held_depth == 1
        assert slot.queue_ready_depth == 1

    def test_slot_frame_carries_the_held_count(self):
        slot = _ChatSlot("s")
        _interrupted(slot)
        _hold(slot, "restored one")

        frame = slot.to_dict()

        assert frame["running"] is False
        assert frame["interrupted"] is True
        assert frame["queue_depth"] == 1
        assert frame["queue_held_depth"] == 1

    def test_a_held_entry_with_its_row_written_is_marked_so(self):
        """Its row is in the transcript, so the card offers Send now and Discard, no Edit."""
        from kiro_crew.dashboard.chat_delivery import queue_entry_views
        from kiro_crew.dashboard.responsive_chat import QUEUED_ROW_META_KEY

        slot = _ChatSlot("s")
        written = _hold(slot, "sent from a crewmate chat")
        slot.append(
            "user", "sent from a crewmate chat", "msg msg-u", meta={QUEUED_ROW_META_KEY: written}
        )
        _hold(slot, "restored plain")

        first, second = queue_entry_views(slot)

        assert first["meta"] == {"held": True, "rowWritten": True}
        assert second["meta"] == {"held": True}

    def test_queue_entry_view_marks_only_held_entries(self):
        slot = _ChatSlot("s")
        _hold(slot, "restored one")
        slot.queue_append("live one")

        held, live = (queue_entry_view(item) for item in slot._queue)

        assert held["meta"]["held"] is True
        assert "held" not in (live.get("meta") or {})

    def test_a_restore_holds_every_entry_it_hands_back(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        _interrupted(slot)
        slot.queue_append("queued before the restart")
        assert _save_slot_to_history(state, slot, force=True)
        state._slots.pop("chat-1", None)

        restored = _rehydrate_slot_from_history(state, "chat-1")

        assert restored is not None
        assert [item["content"] for item in restored._queue] == ["queued before the restart"]
        assert all(is_held_queue_entry(item) for item in restored._queue)
        assert restored.to_dict()["queue_held_depth"] == 1


# ── the drain passes held entries by ───────────────────────────────────────


class TestDrainSkipsHeldEntries:
    @pytest.mark.asyncio
    async def test_a_queue_of_only_held_entries_starts_nothing(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        state.subagents = None
        qid = _hold(slot, "restored one")

        with patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()) as spawn:
            started = await chat_runner._start_next_queued_turn(state, slot)

        assert started is False
        spawn.assert_not_called()
        assert [item["id"] for item in slot._queue] == [qid]

    @pytest.mark.asyncio
    async def test_a_ready_entry_runs_and_the_held_ones_keep_their_order(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        state.subagents = None
        h1 = _hold(slot, "restored one")
        h2 = _hold(slot, "restored two")
        slot.queue_append("sent after the restart")

        with (
            patch.object(chat_runner.KiroCrewConfig, "load") as load,
            patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()) as spawn,
            patch.object(
                chat_runner, "_run_chat", new=MagicMock(return_value=MagicMock())
            ) as run_chat,
        ):
            load.return_value.dashboard.merge_queued_messages = True
            started = await chat_runner._start_next_queued_turn(state, slot)

        assert started is True
        assert spawn.call_count == 1
        # Alone, never merged with the held cards behind it.
        assert run_chat.call_args.args[2] == "sent after the restart"
        assert [item["id"] for item in slot._queue] == [h1, h2]

    @pytest.mark.asyncio
    async def test_a_held_steer_is_not_released_by_the_sub_agent_hold(self, tmp_path):
        """A restored steer keeps its mark; the hold's system scan must pass it by."""
        state, slot = _state(tmp_path), _slot()
        state.subagents = MagicMock(running_agents_for=MagicMock(return_value=["agent-1"]))
        h1 = _hold(slot, "restored steer")
        slot._queue[0].setdefault("meta", {})[STEER_INTENT_META] = True
        fresh = slot.queue_append("sent while sub-agents run")

        with patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()) as spawn:
            started = await chat_runner._start_next_queued_turn(state, slot)

        assert started is False
        spawn.assert_not_called()
        # Both still queued: the fresh entry was brought ahead, the held one waits.
        assert [item["id"] for item in slot._queue] == [fresh, h1]
        assert is_held_queue_entry(slot._queue[1])

    @pytest.mark.asyncio
    async def test_a_note_is_not_flushed_into_a_cron_turn_behind_a_held_card(self, tmp_path):
        """The deferred note waits for a user turn; the held head is not that turn."""
        state, slot = _state(tmp_path), _slot()
        state.subagents = None
        _hold(slot, "restored one")
        slot.queue_append("cron fired", kind=CRON_NOTIFICATION_KIND)

        with (
            patch.object(_ChatSlot, "flush_deferred_notes") as flush,
            patch.object(chat_runner.KiroCrewConfig, "load") as load,
            patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()),
            patch.object(chat_runner, "_run_chat", new=MagicMock(return_value=MagicMock())),
        ):
            load.return_value.dashboard.merge_queued_messages = False
            assert await chat_runner._start_next_queued_turn(state, slot) is True

        flush.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_now_on_a_held_card_runs_exactly_that_card(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        state.subagents = None
        h1 = _hold(slot, "restored one")
        h2 = _hold(slot, "restored two")

        with (
            patch.object(chat_runner.KiroCrewConfig, "load") as load,
            patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()),
            patch.object(
                chat_runner, "_run_chat", new=MagicMock(return_value=MagicMock())
            ) as run_chat,
        ):
            load.return_value.dashboard.merge_queued_messages = False
            started = await chat_runner._start_next_queued_turn(
                state, slot, allow_user_during_subagents=True, required_queue_id=h2
            )

        assert started is True
        assert run_chat.call_args.args[2] == "restored two"
        assert [item["id"] for item in slot._queue] == [h1]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("marker_landed", [True, False])
    async def test_a_written_send_cut_after_its_marker_is_not_restored_held(
        self, tmp_path, marker_landed
    ):
        """Its row sits above a later reply; only the marker can say it ran."""
        from kiro_crew.dashboard.chat_runner import _begin_local_turn_marker
        from kiro_crew.dashboard.chat_turn.prompt_assembly import _rows_without_held_sends
        from kiro_crew.dashboard.chat_turn.turn_marker import TURN_DISPATCH_ROW
        from kiro_crew.dashboard.responsive_chat import QUEUED_ROW_META_KEY

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        slot.append("user", "first request", "msg msg-u")
        qid = slot.queue_append("sent from a crewmate chat")
        # Written when it was sent, while the first turn still ran.
        row = slot.append(
            "user", "sent from a crewmate chat", "msg msg-u", meta={QUEUED_ROW_META_KEY: qid}
        )
        slot.append("assistant", "answer to the first request", "msg msg-a")
        assert _save_slot_to_history(state, slot, force=True)
        # The drain takes the entry and hands the turn its written row.
        slot.queue_pop(0)
        if marker_landed:
            previous_row = TURN_DISPATCH_ROW.get()
            TURN_DISPATCH_ROW.set(row)
            try:
                await _begin_local_turn_marker(state, slot, 2)
            finally:
                TURN_DISPATCH_ROW.set(previous_row)
        # The process dies here: the queue on disk still holds the entry.
        state._slots.pop("chat-1", None)

        restored = _rehydrate_slot_from_history(state, "chat-1")

        assert restored is not None
        if marker_landed:
            # The marker names the entry, so its stale copy is retired and the
            # row stays the cut turn's opener for Resume.
            assert restored._queue == []
            kept = [m.get("content") for m in _rows_without_held_sends(restored)]
            assert "sent from a crewmate chat" in kept
        else:
            # No marker: the turn never started, and the entry is a held card.
            assert [q["id"] for q in restored._queue] == [qid]
            assert is_held_queue_entry(restored._queue[0])

    @pytest.mark.asyncio
    async def test_a_merged_run_of_written_sends_is_retired_whole(self, tmp_path):
        """A merged turn anchors on its last row; every entry it ran must still be retired."""
        from kiro_crew.dashboard.chat_runner import _begin_local_turn_marker
        from kiro_crew.dashboard.chat_turn.turn_marker import TURN_DISPATCH_ROW, dispatched_entries
        from kiro_crew.dashboard.responsive_chat import QUEUED_ROW_META_KEY

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        slot.append("user", "first request", "msg msg-u")
        first = slot.queue_append("first crewmate send")
        slot.append("user", "first crewmate send", "msg msg-u", meta={QUEUED_ROW_META_KEY: first})
        second = slot.queue_append("second crewmate send")
        anchor = slot.append(
            "user", "second crewmate send", "msg msg-u", meta={QUEUED_ROW_META_KEY: second}
        )
        slot.append("assistant", "answer to the first request", "msg msg-a")
        assert _save_slot_to_history(state, slot, force=True)
        consumed = [slot.queue_pop(0), slot.queue_pop(0)]
        previous_row = TURN_DISPATCH_ROW.get()
        TURN_DISPATCH_ROW.set(anchor)
        try:
            with dispatched_entries(consumed):
                await _begin_local_turn_marker(state, slot, 2)
        finally:
            TURN_DISPATCH_ROW.set(previous_row)
        state._slots.pop("chat-1", None)

        restored = _rehydrate_slot_from_history(state, "chat-1")

        assert restored is not None
        assert restored._queue == []
        assert not any("queuedEntryIds" in (m.get("meta") or {}) for m in restored.messages)

    @pytest.mark.asyncio
    async def test_held_entries_do_not_park_on_memory_preparation(self, tmp_path):
        """Only work the drain owns parks; a held card gets no "will run" notice."""
        state, slot = _state(tmp_path), _slot()
        state.subagents = None
        _hold(slot, "restored one")
        pending = MagicMock()
        pending.done = MagicMock(return_value=False)
        state.memory_startup_task = pending

        started = await chat_runner._start_next_queued_turn(state, slot)

        assert started is False
        assert not any(m.get("role") == "notice" for m in slot.messages)
        pending.add_done_callback.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_purge_that_empties_the_ready_work_never_falls_through_to_a_held_card(
        self, tmp_path
    ):
        state, slot = _state(tmp_path), _slot()
        state.subagents = None
        slot.queue_append("a replay the purge will revoke")
        h1 = _hold(slot, "restored one")

        async def _revoke_the_head(_state, s):
            s._queue.pop(0)
            return False  # the queue is not empty, so the drain carries on

        with (
            patch.object(chat_runner, "_drop_revoked_replays", side_effect=_revoke_the_head),
            patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()) as spawn,
        ):
            started = await chat_runner._start_next_queued_turn(state, slot)

        assert started is False
        spawn.assert_not_called()
        assert [item["id"] for item in slot._queue] == [h1]


class TestHeldCardsAreNotUserIntervention:
    def test_held_cards_do_not_cancel_recovery(self):
        slot = _ChatSlot("s")
        _hold(slot, "restored one")
        assert _has_user_queued_followup(slot) is False

    def test_a_live_send_still_counts(self):
        slot = _ChatSlot("s")
        _hold(slot, "restored one")
        slot.queue_append("sent after the restart")
        assert _has_user_queued_followup(slot) is True


# ── Continue counts only what the runner will pick up ─────────────────────


class TestContinueWithHeldQueue:
    @pytest.mark.asyncio
    async def test_resume_runs_ahead_of_held_cards(self, _patched):
        slot = _ChatSlot("s")
        _interrupted(slot)
        h1 = _hold(slot, "restored one")
        state = _mock_state(slot)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s/continue")
            assert resp.status == 200

        assert slot._queue[0]["kind"] == SYNTHETIC_RECOVERY_KIND
        assert [item["id"] for item in slot._queue[1:]] == [h1]
        assert is_held_queue_entry(slot._queue[1])
        _patched.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_second_press_before_the_recovery_starts_is_refused(self, monkeypatch):
        """Idempotent: the inserted recovery is ready work, so it blocks a duplicate."""
        mock_sel = MagicMock()
        parked = AsyncMock(return_value=False)
        with (
            patch("kiro_crew.dashboard.chat_handlers.sel", return_value=mock_sel),
            patch("kiro_crew.dashboard.chat_handlers._start_next_queued_turn", parked),
        ):
            slot = _ChatSlot("s")
            _interrupted(slot)
            _hold(slot, "restored one")
            state = _mock_state(slot)
            async with TestClient(TestServer(_make_app(state))) as client:
                first = await client.post("/api/chat/slots/s/continue")
                second = await client.post("/api/chat/slots/s/continue")
                assert first.status == 200
                assert second.status == 409
                assert (await second.json())["code"] == "slot_queue_pending"

        assert sum(1 for item in slot._queue if item.get("kind") == SYNTHETIC_RECOVERY_KIND) == 1
        assert slot.queue_held_depth == 1

    @pytest.mark.asyncio
    async def test_a_ready_entry_still_refuses(self, _patched):
        slot = _ChatSlot("s")
        _interrupted(slot)
        _hold(slot, "restored one")
        slot.queue_append("sent after the restart")
        state = _mock_state(slot)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s/continue")
            assert resp.status == 409
            assert (await resp.json())["code"] == "slot_queue_pending"

        assert slot.queue_depth == 2


class TestSendNowBehindARunningTurn:
    @pytest.mark.asyncio
    async def test_send_now_on_a_held_card_is_refused_while_a_turn_runs(self, _patch_interrupt_sel):
        """A held card runs only when started directly; behind a turn it would wait on a drain that skips it."""
        from test_chat_slot_interrupt import _make_app as _interrupt_app
        from test_chat_slot_interrupt import _mock_state as _interrupt_state

        slot = _ChatSlot("test")
        ready = slot.queue_append("sent after the restart")
        held = _hold(slot, "restored one")
        task = MagicMock()
        task.done.return_value = False
        slot.task = task
        state = _interrupt_state(slot)
        generation_before = slot._stop_generation

        async with TestClient(TestServer(_interrupt_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/interrupt", json={"queue_id": held})
            data = await resp.json()

        assert resp.status == 409
        assert data["code"] == "held_turn_running"
        state.sessions.stop_turn.assert_not_awaited()
        assert slot._stop_state == "idle"
        # No stop was claimed: a queued recovery continuation keys on this.
        assert slot._stop_generation == generation_before
        # Nothing moved: the card is still held and still behind the ready one.
        assert [item["id"] for item in slot._queue] == [ready, held]
        assert is_held_queue_entry(slot._queue[1])

    @pytest.mark.asyncio
    async def test_send_now_stands_down_when_the_turn_changes_during_the_body_read(
        self, _patch_interrupt_sel, monkeypatch
    ):
        """The early read holds no claim, so a turn the drain started during it must not be stopped."""
        from test_chat_slot_interrupt import _make_app as _interrupt_app
        from test_chat_slot_interrupt import _mock_state as _interrupt_state

        from kiro_crew.dashboard import chat_handlers

        slot = _ChatSlot("test")
        ready = slot.queue_append("sent after the restart")
        held = _hold(slot, "restored one")
        first = MagicMock()
        first.done.return_value = False
        slot.task = first
        state = _interrupt_state(slot)
        generation_before = slot._stop_generation
        real_read = chat_handlers.read_bounded_json

        async def _read_while_the_turn_hands_off(request, **kwargs):
            result = await real_read(request, **kwargs)
            # Turn A ends and the drain starts turn B before the handler resumes.
            following = MagicMock()
            following.done.return_value = False
            slot.task = following
            return result

        monkeypatch.setattr(chat_handlers, "read_bounded_json", _read_while_the_turn_hands_off)

        async with TestClient(TestServer(_interrupt_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/interrupt", json={"queue_id": ready})

        assert resp.status == 200
        state.sessions.stop_turn.assert_not_awaited()
        assert slot._stop_state == "idle"
        assert slot._stop_generation == generation_before
        assert [item["id"] for item in slot._queue] == [ready, held]

    @pytest.mark.asyncio
    async def test_a_stale_press_does_not_overwrite_a_later_stops_selection(
        self, _patch_interrupt_sel, monkeypatch
    ):
        """A later stop during the early read owns the queue order; the older press stands down."""
        from test_chat_slot_interrupt import _make_app as _interrupt_app
        from test_chat_slot_interrupt import _mock_state as _interrupt_state

        from kiro_crew.dashboard import chat_handlers

        slot = _ChatSlot("test")
        first = slot.queue_append("first ready")
        second = slot.queue_append("second ready")
        held = _hold(slot, "restored one")
        running = MagicMock()
        running.done.return_value = False
        slot.task = running
        state = _interrupt_state(slot)
        real_read = chat_handlers.read_bounded_json

        async def _read_while_a_later_stop_lands(request, **kwargs):
            result = await real_read(request, **kwargs)
            # Press B: picks the second card and stops the turn, which ends.
            slot._stop_generation += 1
            slot.queue_promote_by_id(second)
            slot.task = None
            return result

        monkeypatch.setattr(chat_handlers, "read_bounded_json", _read_while_a_later_stop_lands)

        async with TestClient(TestServer(_interrupt_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/interrupt", json={"queue_id": first})

        assert resp.status == 200
        state.sessions.stop_turn.assert_not_awaited()
        assert [item["id"] for item in slot._queue] == [second, first, held]

    @pytest.mark.asyncio
    async def test_send_now_on_a_held_card_is_refused_behind_a_channel_turn(
        self, _patch_interrupt_sel, monkeypatch
    ):
        """A channel-path turn is not cancelled for a held card the drain would pass by."""
        from test_chat_slot_interrupt import _make_app as _interrupt_app
        from test_chat_slot_interrupt import _mock_state as _interrupt_state

        monkeypatch.setattr(_ChatSlot, "linked_turn_running", property(lambda self: True))
        slot = _ChatSlot("test")
        ready = slot.queue_append("sent after the restart")
        held = _hold(slot, "restored one")
        state = _interrupt_state(slot)

        async with TestClient(TestServer(_interrupt_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/interrupt", json={"queue_id": held})
            data = await resp.json()

        assert resp.status == 409
        assert data["code"] == "held_turn_running"
        state.sessions.stop_turn.assert_not_awaited()
        assert [item["id"] for item in slot._queue] == [ready, held]


class TestResumeNeverContinuesAHeldSend:
    def test_the_interrupted_preamble_skips_a_held_sends_written_row(self):
        """A crewmate send's row is in the transcript, but it is held, not the cut turn."""
        from kiro_crew.context import build_interrupted_turn_preamble
        from kiro_crew.dashboard.chat_turn.prompt_assembly import _rows_without_held_sends
        from kiro_crew.dashboard.responsive_chat import QUEUED_ROW_META_KEY
        from kiro_crew.dashboard.state import TURN_OPENING_INJECT_KINDS

        slot = _ChatSlot("s")
        slot.append("user", "the request the restart cut", "msg msg-u")
        slot.append("assistant", "partial", "msg msg-a")
        held = _hold(slot, "sent while it ran")
        slot.append("user", "sent while it ran", "msg msg-u", meta={QUEUED_ROW_META_KEY: held})
        slot.append("error", _RESTART_TEXT, "msg msg-err", meta={"kind": _RESTART_KIND})

        rows = _rows_without_held_sends(slot)
        preamble = build_interrupted_turn_preamble(
            rows, current=None, opener_inject_kinds=TURN_OPENING_INJECT_KINDS
        )

        assert "the request the restart cut" in preamble
        assert "sent while it ran" not in preamble
        # The transcript itself keeps the row.
        assert any(m.get("content") == "sent while it ran" for m in slot.messages)

    def test_the_cold_replay_skips_held_rows_on_disk_and_live(self, tmp_path):
        """A cold-start Resume replays the transcript, which carries the held row on disk."""
        from kiro_crew.context import build_session_replay
        from kiro_crew.dashboard.chat_turn.prompt_assembly import _unsent_send_filter
        from kiro_crew.dashboard.responsive_chat import QUEUED_ROW_META_KEY
        from kiro_crew.history import ConversationLog

        log = ConversationLog(base_dir=tmp_path)
        slot = _ChatSlot("s")
        slot.append("user", "the request the restart cut", "msg msg-u")
        slot.append("assistant", "partial answer", "msg msg-a")
        held = _hold(slot, "held while it ran")
        slot.append("user", "held while it ran", "msg msg-u", meta={QUEUED_ROW_META_KEY: held})
        log.rewrite_session("dashboard:s", [dict(row) for row in slot.messages])

        unsent = _unsent_send_filter(slot)
        assert unsent is not None
        for pending in ([], list(slot.messages)):
            replay = build_session_replay(
                log, "dashboard:s", pending_messages=pending, current_message=None, skip_row=unsent
            )
            assert replay is not None
            assert "the request the restart cut" in replay
            assert "held while it ran" not in replay

    def test_the_cancelled_preamble_skips_a_held_sends_written_row(self, tmp_path):
        """Continue after a Stop restores the stopped request, not a send still on its card."""
        import json as _json

        from kiro_crew.context import build_cancelled_turn_preamble
        from kiro_crew.dashboard.chat_turn.prompt_assembly import _unsent_send_filter
        from kiro_crew.dashboard.responsive_chat import QUEUED_ROW_META_KEY
        from kiro_crew.history import ConversationLog

        log = ConversationLog(base_dir=tmp_path)
        slot = _ChatSlot("s")
        slot.append("user", "the request that was stopped", "msg msg-u")
        slot.append("assistant", "partial answer", "msg msg-a")
        held = _hold(slot, "held while it ran")
        slot.append("user", "held while it ran", "msg msg-u", meta={QUEUED_ROW_META_KEY: held})
        slot.append("system", _json.dumps({"kind": "stop_event"}), "msg")
        log.rewrite_session("dashboard:s", [dict(row) for row in slot.messages])

        preamble = build_cancelled_turn_preamble(
            log, "dashboard:s", skip_row=_unsent_send_filter(slot)
        )

        assert "the request that was stopped" in preamble
        assert "held while it ran" not in preamble

    @pytest.mark.parametrize("bad_id", [[], {}, 7, None])
    def test_a_row_with_a_non_string_entry_id_is_kept_and_does_not_raise(self, bad_id):
        """Row meta is read from the transcript, so any JSON value can sit there."""
        from kiro_crew.dashboard.chat_turn.prompt_assembly import _unsent_send_filter
        from kiro_crew.dashboard.responsive_chat import QUEUED_ROW_META_KEY

        slot = _ChatSlot("s")
        _hold(slot, "held while it ran")
        row = {"role": "user", "content": "old", "meta": {QUEUED_ROW_META_KEY: bad_id}}

        unsent = _unsent_send_filter(slot)

        assert unsent is not None
        assert unsent(row) is False

    def test_the_filter_is_off_when_nothing_is_held(self):
        from kiro_crew.dashboard.chat_turn.prompt_assembly import _unsent_send_filter

        assert _unsent_send_filter(_ChatSlot("s")) is None


class TestAgentsAreToldHeldIsNotQueued:
    def test_the_session_tools_count_only_entries_that_will_run(self):
        from kiro_crew.mcp_dashboard_session_runs import _queue_phrase

        assert _queue_phrase({"queue_depth": 2, "queue_held_depth": 2}) == ""
        assert _queue_phrase({"queue_depth": 3, "queue_held_depth": 1}, unit=" message(s)") == (
            ", 2 message(s) queued"
        )
        # An older gateway sends no held count: every entry reads as queued.
        assert _queue_phrase({"queue_depth": 1}) == ", 1 queued"


class TestSendNowWhileMemoryPrepares:
    @pytest.mark.asyncio
    async def test_send_now_on_a_held_card_is_refused_as_memory_preparing(
        self, _patch_interrupt_sel
    ):
        """The drain would park it and never run it, so the route says why and keeps it held."""
        from test_chat_slot_interrupt import _make_app as _interrupt_app
        from test_chat_slot_interrupt import _mock_state as _interrupt_state

        slot = _ChatSlot("test")
        qid = _hold(slot, "restored one")
        state = _interrupt_state(slot)
        pending = MagicMock()
        pending.done = MagicMock(return_value=False)
        state.memory_startup_task = pending
        start = AsyncMock(return_value=False)

        with patch("kiro_crew.dashboard.chat_handlers._start_next_queued_turn", new=start):
            async with TestClient(TestServer(_interrupt_app(state))) as client:
                resp = await client.post("/api/chat/slots/test/interrupt", json={"queue_id": qid})
                data = await resp.json()

        assert resp.status == 409
        assert data["code"] == "memory_preparing"
        start.assert_not_called()
        assert is_held_queue_entry(slot._queue[0])


@pytest.fixture
def _patch_interrupt_sel():
    with patch("kiro_crew.dashboard.chat_handlers.sel", return_value=MagicMock()):
        yield


# ── repeated recovery is stable ────────────────────────────────────────────


def test_repeated_restarts_keep_one_notice_and_the_same_held_cards(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot.append("user", "first request", "msg msg-u")
    slot._turn_in_flight_generation = 3
    slot._turn_in_flight_prompt = None
    slot.queue_append("queued one")
    slot.queue_append("queued two")
    assert _save_slot_to_history(state, slot, force=True)
    state._slots.pop("chat-1", None)

    first = _rehydrate_slot_from_history(state, "chat-1")
    assert first is not None
    assert _save_slot_to_history(state, first, force=True)
    state._slots.pop("chat-1", None)
    second = _rehydrate_slot_from_history(state, "chat-1")

    assert second is not None
    assert len(_restart_rows(second.messages)) <= 1
    assert [item["content"] for item in second._queue] == ["queued one", "queued two"]
    assert second.queue_held_depth == 2
    assert second.queue_ready_depth == 0


@pytest.fixture
def _patched():
    mock_sel = MagicMock()
    started = AsyncMock(return_value=True)
    with (
        patch("kiro_crew.dashboard.chat_handlers.sel", return_value=mock_sel),
        patch("kiro_crew.dashboard.chat_handlers._start_next_queued_turn", started),
    ):
        yield started
