"""Merging a fork back into its parent: the merge route, second half.

Continued from ``test_chat_merge_back_merge.py``; the shared fixtures and
helpers are in ``chat_merge_back_helpers.py``.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest
from chat_merge_back_helpers import (  # noqa: F401
    _CHANNEL_KEY,
    _CHANNEL_STEM,
    _card,
    _cards,
    _channel_parent_and_fork,
    _client,
    _draft,
    _ends_at,
    _merge,
    _move_parent_rows_to_rotated_archive,
    _place_merge_card,
    _queued_merge_context,
    _run_turn,
    _seed,
    audit_fixture,
    chats_fixture,
    model_fixture,
)

from kiro_crew.dashboard import chat_handlers, chat_merge_back, chat_persistence
from kiro_crew.dashboard.chat_merge_back import MERGE_NOTE_SOURCE
from kiro_crew.dashboard.chat_persistence import (
    _apply_recent_session,
    _rehydrate_slot_from_history,
    _save_slot_to_history,
)
from kiro_crew.dashboard.chat_utils import effective_session_key, slot_history_key
from kiro_crew.dashboard.slot_buffers import (
    MAX_DEFERRED_NOTE_CHARS,
    MERGED_FROM_MAX_KEY_CHARS,
    MERGED_FROM_META_KEY,
)
from kiro_crew.dashboard.state import (
    _MAX_CONSUMED_MERGE_CARDS,
    _MAX_CONTEXT_PER_SOURCE,
    _MAX_PENDING_CONTEXT,
)
from kiro_crew.testing.wait import async_wait_until, await_until


class TestMerge:
    @staticmethod
    def _full_line(slot, existing_meta, window):
        from kiro_crew.dashboard.slot_persistence import metadata_line

        meta_line, _mode, _queue, retired, _tab_id = metadata_line.build_full_line(
            slot,
            existing_meta,
            live_session="",
            closed=False,
            closed_at=None,
            window=window,
            queue_snapshot=[],
            queue_candidates=0,
            rewrite=False,
            rows_only=False,
        )
        return [entry.get("id") for entry in meta_line.get("deferred_notes") or []], retired

    @staticmethod
    async def _merged_card(chats, summary: str) -> str:
        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            status, body = await _merge(client, summary, draft)
        assert status == 200, body
        [context] = chats.parent._pending_context
        return context["noteId"]

    @staticmethod
    def _kept_reply(*events_before_end):
        """A reply kiro-cli keeps: *events_before_end*, then a normal ``end_turn``."""
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        async def events():
            for event in events_before_end:
                yield event
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        return events

    @staticmethod
    def _text(text: str):
        from kiro_crew.providers.base import EVENT_TEXT_CHUNK, LLMEvent

        return LLMEvent(kind=EVENT_TEXT_CHUNK, text=text)

    async def _assert_card_reached_the_model_once(
        self, chats, summary: str, note_id: str, first_turn: list[str]
    ) -> None:
        """The failed turn sent the frame; the next turn does not send it again."""
        state = chats.state
        assert first_turn[0].count(summary) == 1
        assert chats.parent._pending_context == []
        following = await _run_turn(state, chats.parent, "and then")
        assert summary not in following[0]
        state.flush_slot_now(chats.parent)
        key = slot_history_key(chats.parent)
        held = state.conversation_log.get_metadata(key).get("deferred_notes") or []
        held_ids = {entry.get("id") for entry in held}
        assert note_id not in held_ids
        assert not held_ids

    @staticmethod
    def _stamp_failing_once(chat_runner, monkeypatch) -> list[Any]:
        """Make the first durable stamp raise, then let the real one run.

        Returns the list of attempts, so a test can tell one try from a retry.
        """
        real_stamp = chat_runner._stamp_consumed_merge_contexts
        attempts: list[Any] = []

        async def failing_then_real(*args):
            attempts.append(args[-1])
            if len(attempts) == 1:
                raise OSError("the transcript refused the consumed stamp")
            return await real_stamp(*args)

        monkeypatch.setattr(chat_runner, "_stamp_consumed_merge_contexts", failing_then_real)
        monkeypatch.setattr(chat_runner, "_MERGE_STAMP_RETRY_DELAY_SECS", 0)
        return attempts

    @staticmethod
    def _tracked_stamp_tasks(state: Any) -> list[Any]:
        """The one tracked consumption-stamp task on *state*, asserted before use.

        Filtered by coroutine name rather than unpacked off ``_background_tasks``,
        which carries whatever else the turn tracked.
        """
        tasks = [
            entry
            for entry in state._background_tasks
            if entry.get_coro().__name__ == "_stamp_and_reconcile_consumed_merge_contexts"
        ]
        assert len(tasks) == 1, tasks
        return tasks

    @pytest.mark.asyncio
    async def test_a_stop_inside_the_consumption_stamp_puts_no_card_back(
        self, chats, model, monkeypatch
    ):
        """A landed turn cancelled inside the stamp keeps the card consumed.

        ``asyncio.to_thread`` is cancellable, so a Stop during
        ``_record_consumed_merge_contexts`` raises out of it, leaves
        ``_merge_contexts_completed`` false and re-enters the finally's
        merge-card branch; ``_turn_lands`` is what keeps the card off the
        queue there. The cards were registered before the await, so the
        unstamped durable entry replays no context either.
        """
        from kiro_crew.dashboard import chat_runner

        state = chats.state
        key = slot_history_key(chats.parent)
        note_id = await self._merged_card(chats, "stamp cancel summary")
        stamp_started = asyncio.Event()

        async def held_stamp(*_args):
            stamp_started.set()
            await await_until(asyncio.Future(), what="the cancel that ends this stub")

        monkeypatch.setattr(chat_runner, "_stamp_consumed_merge_contexts", held_stamp)

        prompts = await _run_turn(state, chats.parent, "continue", cancel_when=stamp_started)

        assert prompts[0].count("stamp cancel summary") == 1
        assert stamp_started.is_set()
        assert chats.parent._pending_context == []
        assert chats.parent._inflight_merge_contexts == []
        assert state._consumed_merge_cards[note_id] == (key, False)
        state.flush_slot_now(chats.parent)
        hold = state.conversation_log._read_metadata(key).get("deferred_notes") or []
        assert [entry.get("id") for entry in hold] == [note_id]
        assert [entry.get("consumed") for entry in hold] == [None]
        del state._slots["parent"]

        restored = _rehydrate_slot_from_history(state, "parent")

        assert restored is not None
        assert all(entry.get("noteId") != note_id for entry in restored._pending_context)

    @pytest.mark.asyncio
    async def test_a_twin_that_queued_context_before_consumption_discards_it_at_drain(
        self, chats, model
    ):
        from kiro_crew.dashboard import chat_runner

        state = chats.state
        note_id = await self._merged_card(chats, "queued twin summary")
        [context] = chats.parent._pending_context
        twin = state.get_or_create_slot("twin")
        twin.linked_session_key = effective_session_key(chats.parent)
        twin._disk_meta_created_at = chats.parent._disk_meta_created_at

        first = chat_runner.take_turn_context(
            state, chats.parent, transcript_identity=chats.parent._disk_meta_created_at
        )
        twin._pending_context.append(dict(context))
        await chat_runner._record_consumed_merge_contexts(state, first.consumed)
        assert twin._pending_context == []
        second = chat_runner.take_turn_context(
            state, twin, transcript_identity=twin._disk_meta_created_at
        )

        assert "queued twin summary" in first.prefix
        assert state._consumed_merge_cards[note_id] == (slot_history_key(chats.parent), True)
        assert "queued twin summary" not in second.prefix
        assert second.consumed == []

    def test_the_consumed_merge_registry_has_a_hard_count_bound(self, chats):
        """The registry is count-bounded whether or not its guards ever resolve."""
        from kiro_crew.dashboard import chat_runner

        contexts = [_queued_merge_context(index) for index in range(_MAX_CONSUMED_MERGE_CARDS + 3)]

        chat_runner._register_consumed_merge_contexts(chats.state, contexts)

        assert len(chats.state._consumed_merge_cards) == _MAX_CONSUMED_MERGE_CARDS
        assert contexts[0]["noteId"] not in chats.state._consumed_merge_cards
        assert contexts[-1]["noteId"] in chats.state._consumed_merge_cards

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stamp_lands", [True, False], ids=["landed", "failed"])
    async def test_a_landed_turn_hands_an_unfinished_stamp_to_the_finally(
        self, chats, model, monkeypatch, stamp_lands
    ):
        """The turn completes its cards from the stamp's result, not from reaching the save.

        A landing stamp that never landed leaves ``_merge_contexts_completed``
        false, so the finally re-registers the cards and owes their stamp to its
        tracked task. A landed stamp finishes them there and the finally has
        nothing to schedule.
        """
        from kiro_crew.dashboard import chat_runner

        state = chats.state
        key = slot_history_key(chats.parent)
        note_id = await self._merged_card(chats, "unfinished landing summary")
        state.flush_slot_now(chats.parent)
        scheduled: list[Any] = []

        async def always_failing(*_args):
            raise OSError("the transcript refused the consumed stamp")

        if not stamp_lands:
            monkeypatch.setattr(chat_runner, "_stamp_consumed_merge_contexts", always_failing)
        monkeypatch.setattr(chat_runner, "_MERGE_STAMP_RETRY_DELAY_SECS", 0)
        monkeypatch.setattr(
            chat_runner,
            "_schedule_consumed_merge_context_stamp",
            lambda _state, identities: scheduled.append(identities),
        )

        prompts = await _run_turn(state, chats.parent, "continue")

        assert prompts[0].count("unfinished landing summary") == 1
        assert scheduled == ([] if stamp_lands else [[(note_id, key)]])
        assert state._consumed_merge_cards[note_id] == (key, stamp_lands)

    @pytest.mark.asyncio
    async def test_a_failed_landing_stamp_leaves_the_card_unfinished_and_retries_it(
        self, chats, monkeypatch
    ):
        """A failed landing stamp completes nothing, and the finally's task lands it.

        The in-memory entry is a stamp-pending guard, so a write that failed
        leaves the card unconsumed. Logging the failure and reporting the card
        consumed would leave the hold without its ``consumed`` stamp, and the
        next restart would re-queue the card's context into the parent's prompt.
        The landing path takes ONE attempt -- the turn is past its reply, so it
        must not sit through a backoff for a write nobody awaits -- and the
        retry is the tracked task's.
        """
        from kiro_crew.dashboard import chat_runner

        state = chats.state
        key = slot_history_key(chats.parent)
        await _place_merge_card(state, chats.parent, chats.fork, "retried landing summary")
        [context] = chats.parent._pending_context
        note_id = context["noteId"]
        state.flush_slot_now(chats.parent)
        attempts = self._stamp_failing_once(chat_runner, monkeypatch)

        completed = await chat_runner._record_consumed_merge_contexts(state, [context])

        assert completed is False
        assert len(attempts) == 1
        assert state._consumed_merge_cards[note_id] == (key, False)
        [held] = state.conversation_log._read_metadata(key)["deferred_notes"]
        assert held.get("consumed") is not True

        chat_runner._schedule_consumed_merge_context_stamp(
            state, chat_runner._register_consumed_merge_contexts(state, [context])
        )
        await await_until(
            asyncio.gather(*self._tracked_stamp_tasks(state)),
            what="the finally's retry of the failed landing stamp",
        )

        assert len(attempts) == 2
        assert state._consumed_merge_cards[note_id] == (key, True)
        [held] = state.conversation_log._read_metadata(key)["deferred_notes"]
        assert held["consumed"] is True

    @pytest.mark.asyncio
    async def test_a_landing_stamp_that_never_lands_completes_no_card(self, chats, monkeypatch):
        """A landing stamp that failed leaves the card for the finally's retry.

        One attempt on the landing path, not a retry loop: the turn has already
        replied, so the bounded retry belongs to the tracked task the finally
        schedules.
        """
        from kiro_crew.dashboard import chat_runner

        state = chats.state
        key = slot_history_key(chats.parent)
        await _place_merge_card(state, chats.parent, chats.fork, "never lands summary")
        [context] = chats.parent._pending_context
        note_id = context["noteId"]
        state.flush_slot_now(chats.parent)
        attempts: list[Any] = []

        async def always_failing(*args):
            attempts.append(args[-1])
            raise OSError("the transcript refused the consumed stamp")

        monkeypatch.setattr(chat_runner, "_stamp_consumed_merge_contexts", always_failing)
        monkeypatch.setattr(chat_runner, "_MERGE_STAMP_RETRY_DELAY_SECS", 0)

        completed = await chat_runner._record_consumed_merge_contexts(state, [context])

        assert completed is False
        assert len(attempts) == 1
        assert state._consumed_merge_cards[note_id] == (key, False)
        [held] = state.conversation_log._read_metadata(key)["deferred_notes"]
        assert held.get("consumed") is not True

    @pytest.mark.asyncio
    async def test_the_finally_paths_tracked_stamp_retries_a_failed_write(self, chats, monkeypatch):
        """The finally's stamp is retried inside its tracked task, not fire-and-forget.

        A turn that errors after its output streamed never reaches the landing
        write, so the finally owes the stamp. Without a retry, one failed write
        there would leave a hold whose card the next restart replays.
        """
        from kiro_crew.dashboard import chat_runner

        state = chats.state
        key = slot_history_key(chats.parent)
        await _place_merge_card(state, chats.parent, chats.fork, "finally retry summary")
        [context] = chats.parent._pending_context
        note_id = context["noteId"]
        state.flush_slot_now(chats.parent)
        attempts = self._stamp_failing_once(chat_runner, monkeypatch)
        identities = chat_runner._register_consumed_merge_contexts(state, [context])

        chat_runner._schedule_consumed_merge_context_stamp(state, identities)
        [task] = self._tracked_stamp_tasks(state)
        await await_until(asyncio.gather(task), what="the tracked consumption stamp")

        assert len(attempts) == 2
        assert state._consumed_merge_cards[note_id] == (key, True)
        [held] = state.conversation_log._read_metadata(key)["deferred_notes"]
        assert held["consumed"] is True

    @pytest.mark.asyncio
    async def test_the_finally_paths_stamp_marks_a_guard_with_no_conversation_log(
        self, chats, monkeypatch
    ):
        """With no transcript log the tracked task still reconciles and marks the guards.

        Nothing durable can be written and nothing durable can be replayed from,
        so those guards are as resolved as they can get and the task's own
        no-log branch marks them. The scheduler must therefore not short-circuit
        on a missing log: that left the error path's guards unstamped for the
        life of the process, which inverts the eviction order the registry
        relies on -- a guard that can never resolve would be kept ahead of one
        whose write is still owed -- and left an already-queued twin copy
        holding its pending-context seat.
        """
        from kiro_crew.dashboard import chat_runner

        state = chats.state
        key = slot_history_key(chats.parent)
        await _place_merge_card(state, chats.parent, chats.fork, "no log summary")
        [context] = chats.parent._pending_context
        note_id = context["noteId"]
        identities = chat_runner._register_consumed_merge_contexts(state, [context])
        assert state._consumed_merge_cards[note_id] == (key, False)
        monkeypatch.setattr(state, "conversation_log", None)

        chat_runner._schedule_consumed_merge_context_stamp(state, identities)
        [task] = self._tracked_stamp_tasks(state)
        await await_until(asyncio.gather(task), what="the no-log consumption stamp")

        assert state._consumed_merge_cards[note_id] == (key, True)
        assert chats.parent._pending_context == []

    @pytest.mark.asyncio
    async def test_a_stamp_that_proves_nothing_durable_reports_no_consumption(
        self, chats, monkeypatch
    ):
        """Consumption is read as done only after the durable stamp's result is checked.

        The reconciliation -- the one step that treats consumption as final --
        runs strictly after the stamp returns, and a stamp that proved nothing
        durable leaves the guard unstamped and reports the card unconsumed.
        """
        from kiro_crew.dashboard import chat_runner

        state = chats.state
        key = slot_history_key(chats.parent)
        await _place_merge_card(state, chats.parent, chats.fork, "unproven summary")
        [context] = chats.parent._pending_context
        note_id = context["noteId"]
        state.flush_slot_now(chats.parent)
        order: list[str] = []
        real_reconcile = chat_runner._reconcile_stamped_merge_contexts

        async def stamped_nothing(*_args):
            order.append("stamp")
            return []

        def recording_reconcile(*args):
            order.append("reconcile")
            return real_reconcile(*args)

        monkeypatch.setattr(chat_runner, "_stamp_consumed_merge_contexts", stamped_nothing)
        monkeypatch.setattr(chat_runner, "_reconcile_stamped_merge_contexts", recording_reconcile)
        monkeypatch.setattr(chat_runner, "_MERGE_STAMP_RETRY_DELAY_SECS", 0)

        completed = await chat_runner._record_consumed_merge_contexts(state, [context])

        assert completed is False
        assert order == ["stamp", "reconcile"]
        assert state._consumed_merge_cards[note_id] == (key, False)
        [held] = state.conversation_log._read_metadata(key)["deferred_notes"]
        assert held.get("consumed") is not True

    @pytest.mark.asyncio
    async def test_a_slot_restored_while_the_stamp_is_pending_queues_no_consumed_context(
        self, chats, monkeypatch
    ):
        from kiro_crew.dashboard import chat_runner

        state = chats.state
        await _place_merge_card(state, chats.parent, chats.fork, "pending stamp summary")
        [context] = chats.parent._pending_context
        note_id = context["noteId"]
        state.flush_slot_now(chats.parent)
        stamp_started = asyncio.Event()
        release_stamp = asyncio.Event()

        async def held_stamp(*_args):
            stamp_started.set()
            await await_until(release_stamp, what="the stamp release")

        monkeypatch.setattr(chat_runner, "_stamp_consumed_merge_contexts", held_stamp)
        record = asyncio.create_task(chat_runner._record_consumed_merge_contexts(state, [context]))
        try:
            await await_until(stamp_started, what="the stamp starting")
            state._slots.pop("parent")
            restored = _rehydrate_slot_from_history(state, "parent")
        finally:
            release_stamp.set()
            await await_until(record, what="the consumption record finishing")

        assert restored is not None
        assert all(entry.get("noteId") != note_id for entry in restored._pending_context)

    @pytest.mark.asyncio
    async def test_a_stamp_whose_write_was_a_no_op_vouches_for_nothing(
        self, chats, model, monkeypatch
    ):
        """The stamp vouches for what the FILE says after its write, not for what it sent.

        ``_update_metadata_locked`` returns silently on a first line that is not
        a metadata object, so a card could be reported durable by a write that
        never happened. A returned identity is read as proof the card can never
        replay, so the proof is read back off disk.
        """
        from kiro_crew.dashboard.slot_buffers import stamp_consumed_merge_cards_sync

        state = chats.state
        key = slot_history_key(chats.parent)
        # Held, so the card has no committed row: the row-proof fallback must
        # not stand in for the hold stamp this test is about.
        chats.parent.task = asyncio.get_running_loop().create_future()
        async with _client(state) as client:
            _, draft = await _draft(client)
            status, body = await _merge(client, "no-op write summary", draft)
        assert (status, body["deferred"]) == (200, True)
        chats.parent.task = None
        state.flush_slot_now(chats.parent)
        [held] = state.conversation_log._read_metadata(key)["deferred_notes"]
        note_id = held["id"]
        with monkeypatch.context() as mp:
            mp.setattr(state.conversation_log, "_update_metadata_locked", lambda *_a, **_k: None)

            vouched = await asyncio.to_thread(
                stamp_consumed_merge_cards_sync, state.conversation_log, [(note_id, key)]
            )

        assert vouched == []
        assert (
            state.conversation_log._read_metadata(key)["deferred_notes"][0].get("consumed")
            is not True
        )

        landed = await asyncio.to_thread(
            stamp_consumed_merge_cards_sync, state.conversation_log, [(note_id, key)]
        )

        assert landed == [(note_id, key)]
        assert state.conversation_log._read_metadata(key)["deferred_notes"][0]["consumed"] is True

    @pytest.mark.asyncio
    async def test_a_hydration_holding_a_pre_stamp_snapshot_queues_no_consumed_context(
        self, chats, monkeypatch
    ):
        """A hydration that read the hold BEFORE the stamp still queues nothing.

        Every hydration carrier reads its metadata off the loop and applies it
        on the loop, so a snapshot taken before the stamp is applied after it.
        The hydrating slot is not in ``state._slots`` while the stamp lands
        (``get_or_create_slot`` registers it at apply time), so the twin
        reconciliation cannot reach its hold: the process guard is the only
        thing ``restore_deferred_note_hold`` and the drain can consult, which is
        why a proven stamp keeps its guard instead of dropping it.
        """
        from kiro_crew.dashboard import chat_runner

        state = chats.state
        key = slot_history_key(chats.parent)
        await _place_merge_card(state, chats.parent, chats.fork, "pre-stamp snapshot summary")
        [context] = chats.parent._pending_context
        note_id = context["noteId"]
        state.flush_slot_now(chats.parent)
        # The read the real hydration does off the loop, taken while the hold
        # still says nothing about consumption.
        snapshot = await asyncio.to_thread(
            chat_persistence._prefetch_rehydrate_inputs, state.conversation_log, key
        )
        assert [entry.get("consumed") for entry in snapshot[0]["deferred_notes"]] == [None]

        state._slots.pop("parent")
        assert await chat_runner._record_consumed_merge_contexts(state, [context])
        [stamped] = state.conversation_log._read_metadata(key)["deferred_notes"]
        assert stamped["consumed"] is True
        monkeypatch.setattr(
            chat_persistence, "_prefetch_rehydrate_inputs", lambda *args, **kwargs: snapshot
        )

        restored = await chat_persistence.rehydrate_slot_from_history_async(state, "parent")

        assert restored is not None
        assert all(entry.get("noteId") != note_id for entry in restored._pending_context)
        assert (
            chat_runner.take_turn_context(
                state, restored, transcript_identity=restored._disk_meta_created_at
            ).prefix
            == ""
        )

    def test_an_unstamped_guard_outlives_a_stamped_one_at_the_cap(self, chats):
        """Eviction takes a stamped guard first: an unstamped one is the live protection.

        A stamped guard only answers a hydration that read the hold before its
        stamp; an unstamped one is the whole suppression until the write lands,
        so filling the registry must not drop it.
        """
        from kiro_crew.dashboard import chat_runner

        state = chats.state
        contexts = [_queued_merge_context(index) for index in range(_MAX_CONSUMED_MERGE_CARDS)]
        identities = chat_runner._register_consumed_merge_contexts(state, contexts)
        # The oldest entry is the one plain FIFO eviction would take; leave it
        # unstamped and stamp every other.
        unstamped, *rest = identities
        chat_runner._reconcile_stamped_merge_contexts(state, rest)

        chat_runner._register_consumed_merge_contexts(state, [_queued_merge_context(10_000)])

        assert len(state._consumed_merge_cards) == _MAX_CONSUMED_MERGE_CARDS
        assert state._consumed_merge_cards[unstamped[0]] == (unstamped[1], False)
        assert rest[0][0] not in state._consumed_merge_cards

    @pytest.mark.asyncio
    async def test_a_consumed_card_whose_row_never_committed_flushes_its_row_without_context(
        self, chats, model
    ):
        state = chats.state
        key = slot_history_key(chats.parent)
        chats.parent.task = asyncio.get_running_loop().create_future()
        async with _client(state) as client:
            _, draft = await _draft(client)
            status, body = await _merge(client, "consumed held summary", draft)
        assert (status, body["deferred"]) == (200, True)
        chats.parent.task = None
        state.flush_slot_now(chats.parent)
        hold = state.conversation_log._read_metadata(key)["deferred_notes"]
        state.conversation_log.update_metadata(
            key, {"deferred_notes": [{**entry, "consumed": True} for entry in hold]}
        )
        del state._slots["parent"]

        restored = _rehydrate_slot_from_history(state, "parent")

        assert restored is not None
        assert [entry.get("consumed") for entry in restored._deferred_notes] == [True]
        assert restored.flush_deferred_notes() == 1
        assert len(_cards(restored)) == 1
        assert restored._pending_context == []

    @pytest.mark.asyncio
    async def test_a_rows_only_handover_keeps_the_consumed_stamp_for_the_restore(self, chats):
        from kiro_crew.dashboard import chat_runner

        state = chats.state
        original = chats.parent
        original_key = slot_history_key(original)
        await _place_merge_card(state, original, chats.fork, "handover summary")
        [context] = original._pending_context
        note_id = context["noteId"]
        state.flush_slot_now(original)
        original._pending_context.clear()
        await chat_runner._record_consumed_merge_contexts(state, [context])
        original.append("assistant", "turn finished during close")
        state._slots.pop("parent")
        replacement = state.get_or_create_slot("parent")
        replacement._tab_id = "replacement-writer"
        replacement._disk_meta_created_at = original._disk_meta_created_at
        replacement._disk_meta_observed = True
        assert await chat_persistence.save_slot_off_loop(state, replacement, force=True)
        before = state.conversation_log._read_metadata(original_key)
        assert before.get("tab_id") == replacement._tab_id
        assert note_id in [entry.get("id") for entry in before.get("deferred_notes", [])]

        result = await chat_handlers._persist_handover_tail(state, "parent", original)

        assert result.rows_committed is True
        after = state.conversation_log._read_metadata(original_key).get("deferred_notes", [])
        assert [entry.get("consumed") for entry in after if entry.get("id") == note_id] == [True]
        state._consumed_merge_cards.clear()
        state._slots.pop("parent")
        restored = _rehydrate_slot_from_history(state, "parent")
        assert restored is not None
        assert all(entry.get("noteId") != note_id for entry in restored._pending_context)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("row_in_window", [True, False], ids=["held", "rowless"])
    async def test_the_finally_path_tracks_the_stamp_and_retires_only_with_row_proof(
        self, chats, model, monkeypatch, row_in_window
    ):
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard import state as dashboard_state

        state = chats.state
        parent = chats.parent
        key = slot_history_key(parent)
        note_id = await self._merged_card(chats, "error summary")
        [context] = parent._pending_context
        state.flush_slot_now(parent)
        if not row_in_window:
            monkeypatch.setattr(dashboard_state, "_MAX_SLOT_MESSAGES", 3)
            for index in range(3):
                parent.append("chunk", f"chunk-{index}")

        stamp_started = asyncio.Event()
        stamp_release = asyncio.Event()
        stamp_observed = asyncio.Event()
        real_stamp = chat_runner._stamp_consumed_merge_contexts
        real_summary = chat_runner.generate_session_summary
        stamp_task = None
        stamped_entry = None

        async def blocked_stamp(*args):
            nonlocal stamp_task, stamped_entry
            stamp_task = asyncio.current_task()
            assert stamp_task is not None
            stamp_started.set()
            await await_until(stamp_release, what="the consumed-card stamp release")
            stamped = await real_stamp(*args)
            [stamped_entry] = state.conversation_log._read_metadata(key)["deferred_notes"]
            stamp_observed.set()
            return stamped

        async def summary_after_stamp(*args):
            await await_until(stamp_observed, what="the consumed-card stamp observation")
            return await real_summary(*args)

        monkeypatch.setattr(chat_runner, "_stamp_consumed_merge_contexts", blocked_stamp)
        monkeypatch.setattr(chat_runner, "generate_session_summary", summary_after_stamp)
        real_run_chat = chat_runner._run_chat
        turn_release = asyncio.Event()

        async def held_run_chat(*args, **kwargs):
            try:
                return await real_run_chat(*args, **kwargs)
            finally:
                await await_until(turn_release, what="the finally-path turn release")

        monkeypatch.setattr(chat_runner, "_run_chat", held_run_chat)

        async def events():
            yield self._text("partial")
            raise RuntimeError("provider failed after output streamed")

        turn = asyncio.create_task(_run_turn(state, parent, "continue", events=events))
        await await_until(stamp_started, what="the consumed-card stamp starting")
        assert stamp_task is not None
        assert stamp_task in state._background_tasks
        assert state._consumed_merge_cards[note_id] == (key, False)

        twin = state.get_or_create_slot("stamp-race-twin")
        twin.linked_session_key = effective_session_key(parent)
        twin._disk_meta_created_at = parent._disk_meta_created_at
        twin._pending_context.append(dict(context))
        raced = chat_runner.take_turn_context(
            state, twin, transcript_identity=twin._disk_meta_created_at
        )
        assert "error summary" not in raced.prefix
        assert raced.consumed == []

        stamp_release.set()
        await await_until(asyncio.gather(stamp_task), what="the consumed-card stamp")
        assert stamped_entry is not None
        assert stamped_entry["consumed"] is True
        assert state._consumed_merge_cards[note_id] == (key, True)
        turn_release.set()
        [failed] = await await_until(asyncio.gather(turn), what="the finally-path turn cleanup")
        assert failed[0].count("error summary") == 1

        assert _save_slot_to_history(state, parent, force=True)
        held = [
            entry.get("id")
            for entry in state.conversation_log._read_metadata(key).get("deferred_notes") or []
        ]
        assert held == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("death", ["typed", "pipe"])
    async def test_a_process_death_after_output_returns_the_card_for_the_recovery_turn(
        self, chats, model, death
    ):
        """kiro-cli logs a prompt only once the model answered it (``replay.py``).

        The reloaded session lacks the prompt that carried the frame, the
        recovery requeue is a bare continuation, so the card must go back on the
        queue and out again exactly once, with its durable hold intact meanwhile.
        """
        from kiro_crew.acp.transport_errors import AcpError, AcpProcessDied

        state = chats.state
        parent = chats.parent
        key = slot_history_key(parent)
        note_id = await self._merged_card(chats, "death summary")
        state.flush_slot_now(parent)
        failure = (
            AcpProcessDied("kiro-cli exited (code 137)")
            if death == "typed"
            else AcpError("ACP process exited before the turn completed")
        )

        async def events():
            yield self._text("partial")
            raise failure

        died = await _run_turn(state, parent, "continue", events=events)

        assert died[0].count("death summary") == 1
        assert [entry["noteId"] for entry in parent._pending_context] == [note_id]
        assert note_id not in state._consumed_merge_cards
        assert parent._inflight_merge_contexts == []
        [recovery] = parent._queue
        assert "death summary" not in recovery["content"]
        state.flush_slot_now(parent)
        held = [
            entry.get("id")
            for entry in state.conversation_log.get_metadata(key).get("deferred_notes") or []
        ]
        assert held == [note_id]

        parent._queue.clear()
        recovered = await _run_turn(state, parent, recovery["content"])
        await self._assert_card_reached_the_model_once(chats, "death summary", note_id, recovered)

    @pytest.mark.asyncio
    async def test_a_retained_image_discard_restores_the_full_merge_card(self, chats, model):
        """A discarded native conversation cannot retain its drained card prompt."""
        from unittest.mock import AsyncMock

        from kiro_crew.acp.client import AcpError

        state = chats.state
        parent = chats.parent
        key = slot_history_key(parent)
        summary = "retained-image merge " + "0123456789" * 220
        assert len(summary) > 2000
        note_id = await self._merged_card(chats, summary)
        state.flush_slot_now(parent)
        failure = AcpError("The model could not process an image", transient=False)
        failure.structural_terminal = True
        failure.image_format_unsupported = True
        state.sessions.discard_conversation = AsyncMock()

        async def events():
            yield self._text("partial")
            raise failure

        rejected = await _run_turn(state, parent, "continue", events=events)

        assert rejected[0].count(summary) == 1
        state.sessions.discard_conversation.assert_awaited_once()
        assert [entry["noteId"] for entry in parent._pending_context] == [note_id]
        assert note_id not in state._consumed_merge_cards
        assert parent._inflight_merge_contexts == []
        [recovery] = parent._queue
        assert summary not in recovery["content"]
        state.flush_slot_now(parent)
        held = [
            entry.get("id")
            for entry in state.conversation_log.get_metadata(key).get("deferred_notes") or []
        ]
        assert held == [note_id]

        parent._queue.clear()
        recovered = await _run_turn(state, parent, recovery["content"])
        await self._assert_card_reached_the_model_once(chats, summary, note_id, recovered)

    @pytest.mark.asyncio
    async def test_a_compaction_failure_after_output_returns_the_card_for_the_next_turn(
        self, chats, model
    ):
        """The completion is synthetic and the turn was never answered, so the
        reset session lacks the prompt that carried the frame: the card goes back
        on the queue, keeps its durable hold, and the next turn carries it once."""
        from kiro_crew.acp.types import STOP_REASON_COMPACTION_FAILED
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        state = chats.state
        parent = chats.parent
        key = slot_history_key(parent)
        note_id = await self._merged_card(chats, "compaction-failed summary")
        state.flush_slot_now(parent)

        async def events():
            yield self._text("partial")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_COMPACTION_FAILED)

        abandoned = await _run_turn(state, parent, "continue", events=events)

        assert abandoned[0].count("compaction-failed summary") == 1
        state.sessions.reset.assert_awaited_once()
        assert parent._queue == []
        assert [entry["noteId"] for entry in parent._pending_context] == [note_id]
        assert note_id not in state._consumed_merge_cards
        assert parent._inflight_merge_contexts == []
        state.flush_slot_now(parent)
        held = [
            entry.get("id")
            for entry in state.conversation_log.get_metadata(key).get("deferred_notes") or []
        ]
        assert held == [note_id]

        resent = await _run_turn(state, parent, "continue")
        await self._assert_card_reached_the_model_once(
            chats, "compaction-failed summary", note_id, resent
        )

    @pytest.mark.asyncio
    async def test_a_runtime_death_reported_as_a_stop_reason_returns_the_card(self, chats, model):
        """The ``error:`` stop class replaces the runtime on the next claim with no
        reset flag set, so the card's return must follow from the terminal itself."""
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        state = chats.state
        parent = chats.parent
        key = slot_history_key(parent)
        note_id = await self._merged_card(chats, "unacked summary")
        state.flush_slot_now(parent)

        async def events():
            yield self._text("partial")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="error: cancel unacked")

        died = await _run_turn(state, parent, "continue", events=events)

        assert died[0].count("unacked summary") == 1
        state.sessions.reset.assert_not_awaited()
        assert [entry["noteId"] for entry in parent._pending_context] == [note_id]
        assert note_id not in state._consumed_merge_cards
        assert parent._inflight_merge_contexts == []
        [recovery] = parent._queue
        assert "unacked summary" not in recovery["content"]
        state.flush_slot_now(parent)
        held = [
            entry.get("id")
            for entry in state.conversation_log.get_metadata(key).get("deferred_notes") or []
        ]
        assert held == [note_id]

        parent._queue.clear()
        recovered = await _run_turn(state, parent, recovery["content"])
        await self._assert_card_reached_the_model_once(chats, "unacked summary", note_id, recovered)

    @pytest.mark.asyncio
    async def test_a_card_held_through_a_restart_and_a_failed_turn_is_delivered_once(
        self, chats, model
    ):
        state = chats.state
        key = slot_history_key(chats.parent)
        async with _client(state) as client:
            _, draft = await _draft(client)
            status, body = await _merge(client, "restart retry summary", draft)
        assert (status, body["deferred"]) == (200, False)
        state.flush_slot_now(chats.parent)
        del state._slots["parent"]
        restored = _rehydrate_slot_from_history(state, "parent")
        assert restored is not None
        assert [entry["content"] for entry in restored._pending_context] == [
            "restart retry summary"
        ]
        note_id = restored._pending_context[0]["noteId"]

        failed = await _run_turn(state, restored, "continue", outcome="error")
        assert failed[0].count("restart retry summary") == 1
        assert [entry["content"] for entry in restored._pending_context] == [
            "restart retry summary"
        ]
        assert note_id not in state._consumed_merge_cards
        assert state.conversation_log.get_metadata(key).get("deferred_notes")

        succeeded = await _run_turn(state, restored, "continue again")
        assert succeeded[0].count("restart retry summary") == 1
        assert restored._pending_context == []
        state.flush_slot_now(restored)
        assert not state.conversation_log.get_metadata(key).get("deferred_notes")

        later = await _run_turn(state, restored, "one more")
        assert "restart retry summary" not in later[0]
        del state._slots["parent"]
        again = _rehydrate_slot_from_history(state, "parent")
        assert again is not None
        assert again._pending_context == []
        assert again._deferred_notes == []

    @pytest.mark.asyncio
    async def test_a_running_turns_drained_cards_still_fill_the_merge_cap(self, chats, model):
        for index in range(_MAX_CONTEXT_PER_SOURCE):
            chats.parent.append_pending_context(_queued_merge_context(index))
        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            started, release = asyncio.Event(), asyncio.Event()
            turn = asyncio.create_task(
                _run_turn(chats.state, chats.parent, "continue", started=started, release=release)
            )
            chats.parent.task = turn
            await await_until(started, what="the stub's start")
            status, body = await _merge(client, "one too many", draft)
            release.set()
            await await_until(turn, what="the running turn completion")

        assert (status, body["code"]) == (429, "merge_queue_full")
        assert _cards(chats.parent) == []

    @pytest.mark.asyncio
    async def test_a_completed_turns_cards_free_the_merge_cap_before_it_unwinds(
        self, chats, model, monkeypatch
    ):
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard.chat_handlers import _source_cap_reached

        for index in range(_MAX_CONTEXT_PER_SOURCE):
            chats.parent.append_pending_context(_queued_merge_context(index))
        cap_reached_once_landed: list[bool] = []
        monkeypatch.setattr(
            chat_runner,
            "record_interaction_event",
            lambda *_args: cap_reached_once_landed.append(
                _source_cap_reached(chats.parent, MERGE_NOTE_SOURCE)
            ),
        )

        await _run_turn(chats.state, chats.parent, "continue")

        assert cap_reached_once_landed == [False]

    @pytest.mark.asyncio
    async def test_a_failed_turn_restores_cards_accepted_up_to_the_cap(self, chats, model):
        drained = 6
        accepted = _MAX_CONTEXT_PER_SOURCE - drained
        for index in range(drained):
            chats.parent.append_pending_context(_queued_merge_context(index))
        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            started, release = asyncio.Event(), asyncio.Event()
            turn = asyncio.create_task(
                _run_turn(
                    chats.state,
                    chats.parent,
                    "continue",
                    outcome="error",
                    started=started,
                    release=release,
                )
            )
            chats.parent.task = turn
            await await_until(started, what="the stub's start")
            for index in range(accepted):
                status, body = await _merge(client, f"accepted merge {index}", draft)
                assert status == 200, body
                if index + 1 < accepted:
                    _seed(
                        chats.fork,
                        ("user", f"follow-up {index}"),
                        ("assistant", f"answer {index}"),
                    )
                    _, draft = await _draft(client)
            release.set()
            await await_until(turn, what="the failed turn completion")

        assert len(chats.parent._pending_context) == _MAX_CONTEXT_PER_SOURCE
        assert all(entry.get("noteId") for entry in chats.parent._pending_context)
        from kiro_crew.dashboard.chat_runner import drain_pending_context

        next_prefix = drain_pending_context(chats.parent)
        assert next_prefix.count(f'[Background context from "{MERGE_NOTE_SOURCE}"]') == (
            _MAX_CONTEXT_PER_SOURCE
        )

    @pytest.mark.asyncio
    async def test_a_failed_turn_restores_cards_without_overfilling_the_queue(self, chats, model):
        card_count = 4
        for index in range(card_count):
            assert chats.parent.append_pending_context(_queued_merge_context(index)) is True
        started, release = asyncio.Event(), asyncio.Event()
        turn = asyncio.create_task(
            _run_turn(
                chats.state,
                chats.parent,
                "continue",
                outcome="error",
                started=started,
                release=release,
            )
        )
        chats.parent.task = turn
        await await_until(started, what="the stub's start")
        ordinary_count = _MAX_PENDING_CONTEXT - card_count
        for index in range(_MAX_PENDING_CONTEXT):
            seated = chats.parent.append_pending_context(
                {
                    "content": f"ordinary {index}",
                    "source": f"other {index}",
                    "ephemeral": True,
                    "injectedAt": 1e12,
                }
            )
            assert seated is (index < ordinary_count)
        release.set()
        await await_until(turn, what="the queue-pressure turn completion")

        assert [entry["content"] for entry in chats.parent._pending_context] == [
            *(f"queued merge {index}" for index in range(card_count)),
            *(f"ordinary {index}" for index in range(ordinary_count)),
        ]

    @pytest.mark.asyncio
    async def test_a_completed_turn_retires_the_merge_context_in_its_own_save(
        self, chats, model, monkeypatch
    ):
        from kiro_crew.dashboard import chat_runner

        state = chats.state
        key = slot_history_key(chats.parent)
        async with _client(state) as client:
            _, draft = await _draft(client)
            status, body = await _merge(client, "durable completed summary", draft)
        assert status == 200, body
        [held] = state.conversation_log.get_metadata(key)["deferred_notes"]

        saved_holds: list[list[dict]] = []
        save_slot_off_loop = chat_runner.save_slot_off_loop

        async def capture_turn_save(saved_state, saved_slot, *args, **kwargs):
            saved = await save_slot_off_loop(saved_state, saved_slot, *args, **kwargs)
            saved_holds.append(
                saved_state.conversation_log.get_metadata(key).get("deferred_notes") or []
            )
            return saved

        monkeypatch.setattr(chat_runner, "save_slot_off_loop", capture_turn_save)
        await _run_turn(state, chats.parent, "continue")

        assert saved_holds and saved_holds[0] == []
        assert state._consumed_merge_cards[held["id"]] == (key, True)
        del state._slots["parent"]
        restored = _rehydrate_slot_from_history(state, "parent")
        assert restored is not None
        assert restored._pending_context == []

    @pytest.mark.asyncio
    async def test_a_completed_turn_retires_the_merge_context_hold(self, chats, model):
        state = chats.state
        key = slot_history_key(chats.parent)
        async with _client(state) as client:
            _, draft = await _draft(client)
            status, body = await _merge(client, "completed summary", draft)
        assert status == 200, body

        await _run_turn(state, chats.parent, "continue")
        state.flush_slot_now(chats.parent)

        assert chats.parent._pending_context == []
        assert not state.conversation_log.get_metadata(key).get("deferred_notes")

    @pytest.mark.asyncio
    async def test_a_delivered_card_rotated_out_of_the_live_file_is_not_replayed(
        self, chats, model
    ):
        state = chats.state
        key = slot_history_key(chats.parent)
        async with _client(state) as client:
            _, draft = await _draft(client)
            status, body = await _merge(client, "rotated summary", draft)
        assert status == 200, body
        state.flush_slot_now(chats.parent)
        [held] = state.conversation_log.get_metadata(key)["deferred_notes"]
        assert held["delivered"] is True
        _move_parent_rows_to_rotated_archive(chats)

        del state._slots["parent"]
        restored = _rehydrate_slot_from_history(state, "parent")
        assert restored is not None
        assert _cards(restored) == []
        assert restored._deferred_notes == []
        assert [entry["content"] for entry in restored._pending_context] == ["rotated summary"]
        restored.flush_deferred_notes()
        state.flush_slot_now(restored)
        rows = state.conversation_log.read_messages_chained_full(key)
        assert (
            sum(
                1
                for row in rows
                if isinstance(row.get("meta"), dict) and MERGED_FROM_META_KEY in row["meta"]
            )
            == 1
        )

    @pytest.mark.asyncio
    async def test_a_channel_linked_parents_card_retires_after_a_restart(self, chats, model):
        state = chats.state
        parent, fork = _channel_parent_and_fork(state, _CHANNEL_STEM, linked=True)
        key = slot_history_key(parent)
        delivery = await _place_merge_card(state, parent, fork, "channel summary")
        assert delivery.deferred is False
        state.flush_slot_now(parent)
        assert len(state.conversation_log.get_metadata(key)["deferred_notes"]) == 1

        del state._slots[_CHANNEL_STEM]
        restored = _rehydrate_slot_from_history(state, _CHANNEL_STEM)
        assert restored is not None
        assert restored.linked_session_key == _CHANNEL_KEY
        [context] = restored._pending_context
        assert (context["content"], context["noteTranscript"]) == ("channel summary", _CHANNEL_STEM)

        prompts = await _run_turn(state, restored, "continue")
        assert prompts[0].count("channel summary") == 1
        state.flush_slot_now(restored)
        assert not state.conversation_log.get_metadata(key).get("deferred_notes")
        del state._slots[_CHANNEL_STEM]
        again = _rehydrate_slot_from_history(state, _CHANNEL_STEM)
        assert again is not None
        assert again._pending_context == []

    @pytest.mark.asyncio
    async def test_a_channel_parent_restored_as_a_recent_session_retires_its_card(
        self, chats, model
    ):
        state = chats.state
        parent, fork = _channel_parent_and_fork(state, _CHANNEL_STEM, linked=True)
        key = slot_history_key(parent)
        delivery = await _place_merge_card(state, parent, fork, "recent channel summary")
        assert delivery.deferred is False
        state.flush_slot_now(parent)
        log = state.conversation_log
        meta = log.get_metadata(key)
        assert len(meta["deferred_notes"]) == 1
        messages = log.read_messages_chained(key)

        del state._slots[_CHANNEL_STEM]
        # The stem is the key the restore reads the file by; the dispatcher's own
        # skip of channel keys is bypassed to reach the apply half directly.
        _apply_recent_session(
            state,
            _CHANNEL_STEM,
            _CHANNEL_STEM,
            {},
            meta,
            messages,
            conv_log=log,
            kiro_model_map={},
            restore_cfg=None,
        )
        restored = state._slots[_CHANNEL_STEM]
        assert restored.linked_session_key == _CHANNEL_KEY
        [context] = restored._pending_context
        assert context["noteTranscript"] == _CHANNEL_STEM

        prompts = await _run_turn(state, restored, "continue")
        assert prompts[0].count("recent channel summary") == 1
        state.flush_slot_now(restored)
        assert not log.get_metadata(key).get("deferred_notes")

    @pytest.mark.asyncio
    async def test_an_unbound_channel_parent_bound_later_retires_its_card(self, chats, model):
        from kiro_crew.dashboard.channel_slots import _rebind_unbound_channel_slot

        state = chats.state
        parent, fork = _channel_parent_and_fork(state, _CHANNEL_STEM, linked=False)
        assert slot_history_key(parent) == _CHANNEL_STEM
        delivery = await _place_merge_card(state, parent, fork, "unbound summary")
        assert delivery.deferred is False
        [context] = parent._pending_context
        assert context["noteTranscript"] == _CHANNEL_STEM
        state.flush_slot_now(parent)
        assert len(state.conversation_log.get_metadata(_CHANNEL_STEM)["deferred_notes"]) == 1

        parent._channel_runtime_origin = True
        assert _rebind_unbound_channel_slot(state, parent, _CHANNEL_KEY)
        assert slot_history_key(parent) == _CHANNEL_KEY

        await _run_turn(state, parent, "continue")
        assert parent._pending_context == []
        state.flush_slot_now(parent)
        assert not state.conversation_log.get_metadata(_CHANNEL_KEY).get("deferred_notes")
        del state._slots[_CHANNEL_STEM]
        again = _rehydrate_slot_from_history(state, _CHANNEL_STEM)
        assert again is not None
        assert again._pending_context == []

    @pytest.mark.asyncio
    async def test_a_card_dropped_as_foreign_authorized_at_the_drain_retires(self, chats, model):
        state = chats.state
        parent, fork = _channel_parent_and_fork(state, _CHANNEL_STEM, linked=False)
        delivery = await _place_merge_card(state, parent, fork, "foreign summary")
        assert delivery.deferred is False
        [context] = parent._pending_context
        assert (
            context["noteSession"] == effective_session_key(parent) == f"dashboard:{_CHANNEL_STEM}"
        )
        state.flush_slot_now(parent)
        assert len(state.conversation_log.get_metadata(_CHANNEL_STEM)["deferred_notes"]) == 1

        parent.linked_session_key = _CHANNEL_KEY
        prompts = await _run_turn(state, parent, "continue")

        # The drain dropped the context as another session's, so the agent never
        # read it; the drop is this turn's consumption of the card all the same.
        assert "foreign summary" not in prompts[0]
        assert parent._pending_context == []
        state.flush_slot_now(parent)
        assert not state.conversation_log.get_metadata(_CHANNEL_KEY).get("deferred_notes")

    def test_a_returned_card_is_not_queued_twice_under_another_spelling_of_its_file(self, chats):
        from kiro_crew.dashboard.chat_runner import _restore_consumed_merge_contexts

        parent = chats.parent
        parent.channel_origin = True
        queued = {**_queued_merge_context(1), "noteTranscript": _CHANNEL_STEM}
        parent.append_pending_context(queued)
        consumed = {**_queued_merge_context(1), "noteTranscript": _CHANNEL_KEY}

        _restore_consumed_merge_contexts(parent, [consumed])

        assert parent._pending_context == [queued]

    @pytest.mark.asyncio
    async def test_the_routes_scan_the_full_corpora_off_the_event_loop(
        self, chats, model, monkeypatch
    ):
        archived_mids = {row["meta"]["mid"] for row in chats.parent.messages}
        _move_parent_rows_to_rotated_archive(chats)
        loop_thread = threading.get_ident()
        on_loop_scans: list[str] = []

        def watched(name: str):
            original = getattr(chat_merge_back, name)

            def wrapper(*args, **kwargs):
                if threading.get_ident() == loop_thread:
                    on_loop_scans.append(name)
                return original(*args, **kwargs)

            monkeypatch.setattr(chat_merge_back, name, wrapper)

        watched("_pre_id_pool")
        watched("_message_positions")
        merge_cards_ = chat_merge_back.merge_cards

        def cards_watched(rows, held_notes, fork_session):
            rows = list(rows)
            # Scanning the live window on the loop is bounded work; a row the
            # archive holds can only reach the loop through the full corpus.
            if threading.get_ident() == loop_thread and any(
                isinstance(row, dict)
                and isinstance(row.get("meta"), dict)
                and row["meta"].get("mid") in archived_mids
                for row in rows
            ):
                on_loop_scans.append("merge_cards")
            return merge_cards_(rows, held_notes, fork_session)

        monkeypatch.setattr(chat_merge_back, "merge_cards", cards_watched)

        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            status, body = await _merge(client, "summary", draft)

        assert status == 200, body
        assert len(_cards(chats.parent)) == 1
        assert on_loop_scans == []

    def test_an_inflight_merge_card_holds_a_pending_context_seat(self, chats):
        for index in range(_MAX_PENDING_CONTEXT - 1):
            assert (
                chats.parent.append_pending_context(
                    {
                        "content": f"plain-{index}",
                        "source": f"plain-{index}",
                        "injectedAt": 1e12,
                    }
                )
                is True
            )
        assert chats.parent.has_pending_context_seat() is True

        chats.parent._inflight_merge_contexts = [_queued_merge_context(0)]

        assert chats.parent.has_pending_context_seat() is False
        chats.parent._inflight_merge_contexts = []
        assert chats.parent.has_pending_context_seat() is True

    @pytest.mark.asyncio
    async def test_a_turn_ending_while_the_cards_write_fails_shows_nothing(
        self, chats, model, monkeypatch
    ):
        # The parent is mid-turn, so the card is held, and that turn ends while the
        # hold is written. The card waits for the write, which then fails.
        entered, release, failing = threading.Event(), threading.Event(), threading.Event()
        failing.set()
        write = chat_handlers.persist_deferred_notes_sync

        def fail_after_the_turn_ends(*args, **kwargs):
            if not failing.is_set():
                return write(*args, **kwargs)
            entered.set()
            assert release.wait(10), "the failed card write was not released"
            raise OSError("disk full")

        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            chats.parent.task = asyncio.get_running_loop().create_future()
            monkeypatch.setattr(
                chat_handlers, "persist_deferred_notes_sync", fail_after_the_turn_ends
            )
            merging = asyncio.create_task(_merge(client, "Redis works.", draft))
            await async_wait_until(
                entered.is_set, describe=lambda: "the failed card write never entered its hold"
            )
            chats.parent.task = None
            chats.parent.flush_deferred_notes()
            release.set()
            status, body = await await_until(merging, what="the failed card-write merge completion")
            shown, held = _cards(chats.parent), list(chats.parent._deferred_notes)
            # Once writes work again the same draft merges, once.
            failing.clear()
            retry_status, retry_body = await _merge(client, "Redis works.", draft)

        assert (status, body["code"]) == (503, "deferred_note_persist_failed")
        assert (shown, held) == ([], [])
        assert retry_status == 200, retry_body
        assert [card["content"] for card in _cards(chats.parent)] == ["Redis works."]

    @pytest.mark.asyncio
    async def test_a_card_its_parents_turn_end_kept_back_is_shown_once_written(
        self, chats, model, monkeypatch
    ):
        entered, release = threading.Event(), threading.Event()
        write = chat_handlers.persist_deferred_notes_sync

        def written_after_the_turn_ends(*args, **kwargs):
            entered.set()
            assert release.wait(10), "the successful card write was not released"
            return write(*args, **kwargs)

        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            chats.parent.task = asyncio.get_running_loop().create_future()
            monkeypatch.setattr(
                chat_handlers, "persist_deferred_notes_sync", written_after_the_turn_ends
            )
            merging = asyncio.create_task(_merge(client, "Redis works.", draft))
            await async_wait_until(
                entered.is_set, describe=lambda: "the successful card write never entered its hold"
            )
            chats.parent.task = None
            chats.parent.flush_deferred_notes()
            kept_back = _cards(chats.parent)
            release.set()
            status, body = await await_until(
                merging, what="the successful card-write merge completion"
            )

        assert kept_back == []
        # The turn is over, so the card is shown as soon as it is written.
        assert (status, body["deferred"]) == (200, False), body
        assert [card["content"] for card in _cards(chats.parent)] == ["Redis works."]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("deleted", ["before the hold is written", "after it is written"])
    async def test_a_parent_deleted_and_replaced_while_its_card_is_written_gets_nothing(
        self, chats, model, monkeypatch, deleted
    ):
        # The person permanently deletes the parent while the card is written, and
        # a new chat is made on its key before the merge answers.
        state = chats.state
        original_created_at = "2026-10-01T12:00:00+00:00"
        chats.parent.created_at = original_created_at
        state.conversation_log.update_metadata(
            slot_history_key(chats.parent), {"created_at": original_created_at}
        )
        chats.parent._disk_meta_created_at = original_created_at
        chats.fork.forked_from_created_at = original_created_at
        paused, resume = threading.Event(), threading.Event()
        write = chat_handlers.persist_deferred_notes_sync

        def pause_around_the_write(*args, **kwargs):
            if deleted.startswith("before"):
                paused.set()
                assert resume.wait(10), "the parent write was not released"
                return write(*args, **kwargs)
            outcome = write(*args, **kwargs)
            paused.set()
            assert resume.wait(10), "the parent write was not released"
            return outcome

        async with _client(state) as client:
            _, draft = await _draft(client)
            monkeypatch.setattr(
                chat_handlers, "persist_deferred_notes_sync", pause_around_the_write
            )
            merging = asyncio.create_task(_merge(client, "Redis works.", draft))
            await async_wait_until(
                paused.is_set, describe=lambda: "the parent write never entered its hold"
            )
            state._slots.pop("parent")
            state.conversation_log.delete_session(slot_history_key(chats.parent))
            stranger = state.get_or_create_slot("parent")
            stranger.created_at = "2026-10-01T12:00:01+00:00"
            _seed(stranger, ("user", "an unrelated chat"), ("assistant", "hello"))
            state.flush_slot_now(stranger)
            resume.set()
            status, body = await await_until(
                merging, what="the parent-replacement merge completion"
            )

        assert (status, body["code"]) == (409, "parent_deleted"), body
        # Nothing was shown under the key either: the deleted chat is not flushed.
        assert _cards(chats.parent) == []
        key = slot_history_key(stranger)
        assert not state.conversation_log.get_metadata(key).get("deferred_notes")
        assert _cards(stranger) == []
        on_disk = state.conversation_log.read_messages_chained_full(key)
        assert not any(MERGED_FROM_META_KEY in (row.get("meta") or {}) for row in on_disk)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("unverifiable", ["stat failure", "unreadable metadata"])
    async def test_a_parent_whose_transcript_cannot_be_read_after_the_write_is_merged(
        self, chats, model, monkeypatch, audit, unverifiable
    ):
        # The card is in the parent's durable hold when the parent's transcript
        # becomes unreadable. Nothing shows the parent is gone, and the card will
        # reach its agent, so the merge is acknowledged rather than refused as
        # ``parent_deleted``.
        state, log = chats.state, chats.state.conversation_log
        parent_key = slot_history_key(chats.parent)
        broken = threading.Event()
        write = chat_handlers.persist_deferred_notes_sync
        real_path, real_status = log._path, log.get_metadata_status

        class _UnstatablePath(type(real_path(parent_key))):
            def stat(self, *args, **kwargs):
                raise PermissionError("stat refused")

        def path_of(key):
            path = real_path(key)
            if broken.is_set() and key == parent_key and unverifiable == "stat failure":
                return _UnstatablePath(path)
            return path

        def status_of(key):
            if broken.is_set() and key == parent_key and unverifiable == "unreadable metadata":
                return {}, False
            return real_status(key)

        def break_the_read_after_the_write(*args, **kwargs):
            outcome = write(*args, **kwargs)
            broken.set()
            return outcome

        async with _client(state) as client:
            _, draft = await _draft(client)
            audit.clear()
            monkeypatch.setattr(log, "_path", path_of)
            monkeypatch.setattr(log, "get_metadata_status", status_of)
            monkeypatch.setattr(
                chat_handlers, "persist_deferred_notes_sync", break_the_read_after_the_write
            )
            status, body = await _merge(client, "Redis works.", draft)
            broken.clear()

        assert status == 200, body
        assert (body["ok"], body["parent"]) == (True, "parent")
        held = log.get_metadata(parent_key).get("deferred_notes") or []
        assert [note["content"] for note in held] == ["Redis works."]
        assert [card["content"] for card in _cards(chats.parent)] == ["Redis works."]
        assert [(event["operation"], event["outcome"]) for event in audit] == [
            (chat_merge_back._AUDIT_MERGE, "allowed")
        ]

    @pytest.mark.asyncio
    async def test_the_next_merge_covers_only_what_came_after(self, chats, model):
        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            await _merge(client, "first", draft)
            status, body = await _draft(client)
            assert (status, body["code"]) == (409, "nothing_to_merge")

            _seed(chats.fork, ("user", "now add eviction"), ("assistant", "LRU, 512MB"))
            status, second = await _draft(client)

        assert status == 200, second
        assert second["messages"] == 2
        branch = model.calls[-1]["prompt"].split("FORK MESSAGES")[1]
        assert "now add eviction" in branch
        assert "try redis" not in branch

    @pytest.mark.asyncio
    async def test_the_cursor_survives_a_restart_through_the_parents_transcript(self, chats, model):
        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            await _merge(client, "first", draft)
        chats.state.flush_slot_now(chats.parent)
        # A restart rebuilds the parent's window from disk: drop the in-memory rows.
        chats.parent.messages.clear()

        async with _client(chats.state) as client:
            status, body = await _draft(client)

        assert (status, body["code"]) == (409, "nothing_to_merge")

    @pytest.mark.asyncio
    async def test_a_parent_mid_turn_holds_the_card_until_the_turn_ends(self, chats, model):
        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            chats.parent.task = asyncio.get_running_loop().create_future()
            status, body = await _merge(client, "held summary", draft)
            assert status == 200, body
            assert body["deferred"] is True
            assert _cards(chats.parent) == []
            # A second merge of the same messages is refused while the first is held.
            status, again = await _merge(client, "again", draft)

        assert (status, again["code"]) == (409, "already_merged")
        chats.parent.task = None
        chats.parent.flush_deferred_notes()
        (card,) = _cards(chats.parent)
        assert card["meta"][MERGED_FROM_META_KEY]["through"] == draft["through"]

    @pytest.mark.asyncio
    async def test_the_same_merge_twice_is_refused(self, chats, model):
        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            await _merge(client, "first", draft)
            status, body = await _merge(client, "first again", draft)

        assert (status, body["code"]) == (409, "already_merged")
        assert len(_cards(chats.parent)) == 1

    @pytest.mark.asyncio
    async def test_a_card_replayed_by_a_downgrade_counts_as_one_card(self, chats, model):
        # An older build ignores the hold entry's ``delivered`` flag and writes
        # the card's row a second time. The two rows share one ``noteId`` and one
        # ``mergedFrom``, so the covered range is counted once: the same messages
        # stay merged, a new draft covers only what came after them, and the
        # next merge is written beside both rows.
        async with _client(chats.state) as client:
            _, first = await _draft(client)
            status, body = await _merge(client, "Redis works.", first)
            assert status == 200, body
            (card,) = _cards(chats.parent)
            chats.parent.messages.append(
                {**card, "meta": {**card["meta"], "mid": f"{card['meta']['mid']}-replay"}}
            )
            chats.state.flush_slot_now(chats.parent)
            assert len(_cards(chats.parent)) == 2

            status, again = await _merge(client, "Redis works, again.", first)
            assert (status, again["code"]) == (409, "already_merged")
            _seed(chats.fork, ("user", "now add eviction"), ("assistant", "LRU, 512MB"))
            status, draft = await _draft(client)
            assert status == 200, draft
            assert (draft["messages"], draft["remaining"]) == (2, 0)
            status, body = await _merge(client, "LRU eviction added.", draft)

        assert status == 200, body
        assert body["messages"] == 2
        cards = _cards(chats.parent)
        assert [c["meta"]["noteId"] for c in cards[:2]] == [card["meta"]["noteId"]] * 2
        assert cards[2]["meta"][MERGED_FROM_META_KEY]["after"] == first["through"]

    @pytest.mark.asyncio
    async def test_a_reply_switched_to_another_variant_makes_the_draft_stale(self, chats, model):
        reply = chats.fork.messages[-1]
        reply["variants"] = [
            {"content": "redis failed: no TTL support", "ts": "t1"},
            {"content": reply["content"], "ts": reply.get("ts", "")},
        ]
        reply["variant_idx"] = 1

        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            # The switch keeps the reply's message id and changes its text.
            resp = await client.post("/api/chat/slots/fork/switch-variant", json={"index": 0})
            assert resp.status == 200, await resp.text()
            status, body = await _merge(client, "Redis works. TTL 300s.", draft)

        assert (status, body["code"]) == (409, "merge_draft_stale")
        assert _cards(chats.parent) == []

    @pytest.mark.asyncio
    async def test_a_merged_reply_switched_to_another_variant_can_be_merged_again(
        self, chats, model
    ):
        reply = chats.fork.messages[-1]
        reply["variants"] = [
            {"content": "redis failed: no TTL support", "ts": "t1"},
            {"content": reply["content"], "ts": reply.get("ts", "")},
        ]
        reply["variant_idx"] = 1

        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            status, _ = await _merge(client, "Redis works.", draft)
            assert status == 200
            resp = await client.post("/api/chat/slots/fork/switch-variant", json={"index": 0})
            assert resp.status == 200, await resp.text()
            status, again = await _draft(client)
            assert status == 200, again
            status, body = await _merge(client, "Redis failed after all.", again)
            assert status == 200, body
            status, after = await _draft(client)

        assert again["messages"] == 2
        assert "redis failed: no TTL support" in model.calls[-1]["prompt"].split("FORK MESSAGES")[1]
        assert [c["content"] for c in _cards(chats.parent)] == [
            "Redis works.",
            "Redis failed after all.",
        ]
        # The second card covers the range as it reads now.
        assert (status, after["code"]) == (409, "nothing_to_merge")

    @pytest.mark.asyncio
    async def test_a_draft_whose_start_another_merge_covered_is_stale(self, chats, model):
        async with _client(chats.state) as client:
            _, first = await _draft(client)
            _seed(chats.fork, ("user", "now add eviction"), ("assistant", "LRU, 512MB"))
            _, both = await _draft(client)
            status, _ = await _merge(client, "Redis works.", first)
            assert status == 200
            status, body = await _merge(client, "Redis works, with LRU eviction.", both)

        assert (status, body["code"]) == (409, "merge_draft_stale")
        assert len(_cards(chats.parent)) == 1

    @pytest.mark.asyncio
    async def test_the_last_check_reads_merges_the_transcript_read_missed(self, chats, model):
        async with _client(chats.state) as client:
            _, first = await _draft(client)
            _seed(chats.fork, ("user", "now add eviction"), ("assistant", "LRU, 512MB"))
            _, both = await _draft(client)
            # A card only the parent's window holds, with no message id, so the
            # transcript read cannot match it to a disk row.
            block = {
                "session": effective_session_key(chats.fork),
                "slot": "fork",
                "title": "",
                "createdAt": chats.fork._disk_meta_created_at,
                "after": chats.fork.messages[1]["meta"]["mid"],
                "through": first["through"],
                "digest": first["digest"],
                "messages": 2,
            }
            card = {
                "role": "inject",
                "content": "Redis works.",
                "meta": {"noteId": "note-other-tab", MERGED_FROM_META_KEY: block},
            }
            chats.parent.messages.append(card)
            status, body = await _merge(client, "Redis works, with LRU eviction.", both)

        assert (status, body["code"]) == (409, "merge_draft_stale")
        assert _cards(chats.parent) == [card]

    @pytest.mark.asyncio
    async def test_a_through_the_fork_no_longer_has_is_refused(self, chats, model):
        async with _client(chats.state) as client:
            status, body = await _merge(client, "summary", _ends_at("no-such-message"))

        assert (status, body["code"]) == (409, "merge_point_missing")

    @pytest.mark.asyncio
    async def test_a_through_inside_the_copied_prefix_is_already_merged(self, chats, model):
        copied = chats.fork.messages[0]["meta"]["mid"]

        async with _client(chats.state) as client:
            status, body = await _merge(client, "summary", _ends_at(copied))

        assert (status, body["code"]) == (409, "already_merged")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("payload", "code", "status"),
        [
            ({"summary": "  ", "through": "m"}, "invalid_summary", 400),
            ({"summary": 7, "through": "m"}, "invalid_summary", 400),
            (
                {"summary": "x" * (MAX_DEFERRED_NOTE_CHARS + 1), "through": "m"},
                "summary_too_long",
                413,
            ),
            ({"summary": "ok", "through": ""}, "invalid_through", 400),
            (
                {"summary": "ok", "through": "m" * (MERGED_FROM_MAX_KEY_CHARS + 1)},
                "invalid_through",
                400,
            ),
            ({"summary": "ok", "through": "m"}, "invalid_digest", 400),
            ({"summary": "ok", "through": "m", "digest": "abc"}, "invalid_digest", 400),
            ({"summary": "ok", "through": "m", "digest": "A" * 64}, "invalid_digest", 400),
        ],
    )
    async def test_a_malformed_body_is_refused(self, chats, model, payload, code, status):
        async with _client(chats.state) as client:
            resp = await client.post("/api/chat/slots/fork/merge-back", json=payload)
            body = await resp.json()

        assert (resp.status, body["code"]) == (status, code)

    @pytest.mark.asyncio
    async def test_a_parent_that_moved_on_since_the_draft_still_takes_the_merge(self, chats, model):
        # The summary never states how far the parent has moved, so nothing in
        # it can go stale when the parent carries on while the person reads it.
        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            _seed(chats.parent, ("user", "went with memcached"), ("assistant", "ok"))

            status, body = await _merge(client, "Redis works.", draft)

        assert status == 200, body
        assert [card["content"] for card in _cards(chats.parent)] == ["Redis works."]

    @pytest.mark.asyncio
    async def test_a_full_merge_queue_is_refused_rather_than_written_unread(self, chats, model):
        for index in range(10):
            chats.parent.append_pending_context(
                {
                    "content": f"m{index}",
                    "source": MERGE_NOTE_SOURCE,
                    "ephemeral": True,
                    "injectedAt": 1e12,
                }
            )

        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            status, body = await _merge(client, "summary", draft)

        assert (status, body["code"]) == (429, "merge_queue_full")
        assert _cards(chats.parent) == []

    @pytest.mark.asyncio
    async def test_a_credential_shaped_summary_is_refused_without_a_card(self, chats, model):
        # Pending context reaches the parent's agent as prompt text, so it never
        # carries a credential, and a merge is nothing without its context half:
        # the route refuses instead of writing a card the agent would never read.
        token = "ghp_" + "abcdefghijklmnopqrstuvwxyz0123456789"  # no literal credential in source
        held_before = list(chats.parent._deferred_notes)
        queued_before = list(chats.parent._pending_context)

        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            status, body = await _merge(client, f"Deploy with token {token}.", draft)

        assert (status, body["code"]) == (400, "context_secret_shaped")
        assert token not in body["error"]
        assert _cards(chats.parent) == []
        assert chats.parent._deferred_notes == held_before
        assert chats.parent._pending_context == queued_before

    @pytest.mark.asyncio
    async def test_a_full_context_queue_refuses_the_merge_without_a_card(self, chats, model):
        # Fifty entries from fifty sources: no per-source cap is reached, so the
        # refusal comes from the whole queue having no seat.
        for index in range(_MAX_PENDING_CONTEXT):
            assert (
                chats.parent.append_pending_context(
                    {
                        "content": f"ordinary {index}",
                        "source": f"other {index}",
                        "ephemeral": True,
                        "injectedAt": 1e12,
                    }
                )
                is True
            )

        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            status, body = await _merge(client, "summary", draft)

        assert (status, body["code"]) == (429, "merge_queue_full")
        assert _cards(chats.parent) == []

    @pytest.mark.asyncio
    async def test_context_filling_during_cursor_work_refuses_without_a_card(
        self, chats, model, monkeypatch
    ):
        entered, release = threading.Event(), threading.Event()
        merge_cursor = chat_merge_back._merge_cursor

        def pause_cursor(*args):
            entered.set()
            assert release.wait(10), "the queue-fill cursor was not released"
            return merge_cursor(*args)

        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            monkeypatch.setattr(chat_merge_back, "_merge_cursor", pause_cursor)
            merging = asyncio.create_task(_merge(client, "summary", draft))
            await async_wait_until(
                entered.is_set, describe=lambda: "the write never entered its hold"
            )
            for index in range(10):
                chats.parent.append_pending_context(
                    {
                        "content": f"m{index}",
                        "source": MERGE_NOTE_SOURCE,
                        "ephemeral": True,
                        "injectedAt": 1e12,
                    }
                )
            release.set()
            status, body = await await_until(merging, what="the queue-fill merge completion")

        assert (status, body["code"]) == (429, "merge_queue_full")
        assert _cards(chats.parent) == []

    @pytest.mark.asyncio
    async def test_cards_changing_during_cursor_work_make_the_draft_stale(
        self, chats, model, monkeypatch
    ):
        entered, release = threading.Event(), threading.Event()
        merge_cursor = chat_merge_back._merge_cursor

        def pause_cursor(*args):
            entered.set()
            assert release.wait(10), "the concurrent-card cursor was not released"
            return merge_cursor(*args)

        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            monkeypatch.setattr(chat_merge_back, "_merge_cursor", pause_cursor)
            merging = asyncio.create_task(_merge(client, "summary", draft))
            await async_wait_until(
                entered.is_set, describe=lambda: "the write never entered its hold"
            )
            chats.parent.messages.append(
                _card(
                    chats.fork.messages[1]["meta"]["mid"],
                    draft["through"],
                    chats.fork.messages[2:],
                    "concurrent-card",
                )
            )
            release.set()
            status, body = await await_until(merging, what="the stale-card merge completion")

        assert (status, body["code"]) == (409, "merge_draft_stale")
        assert len(_cards(chats.parent)) == 1

    @pytest.mark.asyncio
    async def test_a_twin_starting_during_cursor_work_receives_the_card(
        self, chats, model, monkeypatch
    ):
        twin = chats.state.get_or_create_slot("twin")
        twin.linked_session_key = effective_session_key(chats.parent)
        twin._disk_meta_created_at = chats.parent._disk_meta_created_at
        entered, release = threading.Event(), threading.Event()
        merge_cursor = chat_merge_back._merge_cursor

        def pause_cursor(*args):
            entered.set()
            assert release.wait(10), "the twin-start cursor was not released"
            return merge_cursor(*args)

        async with _client(chats.state) as client:
            _, draft = await _draft(client)
            monkeypatch.setattr(chat_merge_back, "_merge_cursor", pause_cursor)
            merging = asyncio.create_task(_merge(client, "summary", draft))
            await async_wait_until(
                entered.is_set, describe=lambda: "the write never entered its hold"
            )
            twin.task = asyncio.get_running_loop().create_future()
            release.set()
            status, body = await await_until(merging, what="the twin-start merge completion")

        assert (status, body["parent"], body["deferred"]) == (200, "twin", True)
        assert len(twin._deferred_notes) == 1
        twin.task.cancel()

    @pytest.mark.asyncio
    async def test_an_app_token_cannot_merge(self, chats, model):
        through = chats.fork.messages[-1]["meta"]["mid"]

        async with _client(chats.state, request_app="some-app") as client:
            status, body = await _merge(client, "summary", _ends_at(through))

        assert (status, body["code"]) == (404, "slot_not_found")
        assert _cards(chats.parent) == []
