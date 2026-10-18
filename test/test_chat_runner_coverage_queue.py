"""Coverage tests for ``kiro_crew.dashboard.chat_runner``: the queue drain.

Covers ``_start_next_queued_turn`` (the hold while subagents run, and run-now), the
pending synthesis run, and ``_finish_queue_cycle`` with its synthesis fire gate.
``test_chat_runner_coverage.py`` describes the two harnesses the family uses; the
shared builders are in ``chat_runner_coverage_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_runner_coverage_helpers import _errors, _slot, _state

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_utils import chunk_generation
from kiro_crew.testing.wait import async_wait_until

# ── queue drain / synthesis ───────────────────────────────────────────────


class TestStartNextQueuedTurn:
    @pytest.mark.asyncio
    async def test_empty_queue_starts_nothing(self, tmp_path):
        state, slot = _state(tmp_path), _slot()

        assert await chat_runner._start_next_queued_turn(state, slot) is False

    @pytest.mark.asyncio
    async def test_config_failure_falls_back_to_sequential_dequeue(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        slot.queue_append("one")
        slot.queue_append("two")
        state.subagents = None

        with (
            patch.object(chat_runner.KiroCrewConfig, "load", side_effect=RuntimeError("bad toml")),
            patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()) as spawn,
            patch.object(chat_runner, "_run_chat", return_value=MagicMock()),
        ):
            assert await chat_runner._start_next_queued_turn(state, slot) is True

        assert spawn.call_count == 1
        assert len(slot._queue) == 1

    @pytest.mark.asyncio
    async def test_running_subagents_hold_user_messages_back(self, tmp_path):
        """With a fan-out in flight only a system injection may drain."""
        state, slot = _state(tmp_path), _slot()
        slot.queue_append("a user message")
        state.subagents = MagicMock(running_agents_for=MagicMock(return_value=["agent-1"]))

        assert await chat_runner._start_next_queued_turn(state, slot) is False
        assert len(slot._queue) == 1

    @pytest.mark.asyncio
    async def test_run_now_bypasses_the_child_hold_for_the_selected_message(self, tmp_path):
        """The explicit card action runs one selected user message beside child work."""
        state, slot = _state(tmp_path), _slot()
        q1 = slot.queue_append("first")
        q2 = slot.queue_append("second")
        state.subagents = MagicMock(running_agents_for=MagicMock(return_value=["agent-1"]))

        with (
            patch.object(chat_runner.KiroCrewConfig, "load") as load,
            patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()) as spawn,
            patch.object(
                chat_runner, "_run_chat", new=MagicMock(return_value=MagicMock())
            ) as run_chat,
        ):
            load.return_value.dashboard.merge_queued_messages = False
            started = await chat_runner._start_next_queued_turn(
                state,
                slot,
                allow_user_during_subagents=True,
                required_queue_id=q2,
            )

        assert started is True
        assert spawn.call_count == 1
        assert run_chat.call_args.args[2] == "second"
        assert [item["id"] for item in slot._queue] == [q1]

    @pytest.mark.asyncio
    async def test_run_now_never_merges_unselected_cards(self, tmp_path):
        """The selected card runs alone even when ordinary queue merging is on."""
        state, slot = _state(tmp_path), _slot()
        q1 = slot.queue_append("first")
        q2 = slot.queue_append("selected")
        q3 = slot.queue_append("third")
        state.subagents = MagicMock(running_agents_for=MagicMock(return_value=["agent-1"]))

        with (
            patch.object(chat_runner.KiroCrewConfig, "load") as load,
            patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()) as spawn,
            patch.object(
                chat_runner, "_run_chat", new=MagicMock(return_value=MagicMock())
            ) as run_chat,
        ):
            load.return_value.dashboard.merge_queued_messages = True
            started = await chat_runner._start_next_queued_turn(
                state,
                slot,
                allow_user_during_subagents=True,
                required_queue_id=q2,
            )

        assert started is True
        assert spawn.call_count == 1
        assert run_chat.call_args.args[2] == "selected"
        assert [item["id"] for item in slot._queue] == [q1, q3]

    @pytest.mark.asyncio
    async def test_run_now_starts_nothing_when_the_selected_card_is_gone(self, tmp_path):
        """Selection identity prevents a stale click from starting another card."""
        state, slot = _state(tmp_path), _slot()
        qid = slot.queue_append("another card")
        state.subagents = MagicMock(running_agents_for=MagicMock(return_value=["agent-1"]))

        started = await chat_runner._start_next_queued_turn(
            state,
            slot,
            allow_user_during_subagents=True,
            required_queue_id="missing",
        )

        assert started is False
        assert [item["id"] for item in slot._queue] == [qid]

    @pytest.mark.asyncio
    async def test_reset_notice_is_emitted_for_a_stopping_slot(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        slot.queue_append("next please")
        slot._stopping = True
        state.subagents = None

        with (
            patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()),
            patch.object(chat_runner, "_run_chat", return_value=MagicMock()),
        ):
            assert await chat_runner._start_next_queued_turn(state, slot) is True

        assert any("Session reset" in err for err in _errors(slot))
        assert slot._stopping is False

    @pytest.mark.asyncio
    async def test_a_held_note_lands_above_the_successors_own_row(self, tmp_path):
        """A note held mid-turn must be written before the next turn's user row.

        The note's context half drains inside that turn's ``_run_chat``, so
        flushing after it started would put the visible line below the response
        the note shaped. Only ``_finish_queue_cycle`` flushes, and the main
        dispatch path calls it AFTER this function.
        """
        state, slot = _state(tmp_path), _slot()
        slot._deferred_notes.append({"content": "held", "cls": "reconcile-note"})
        slot.queue_append("next please")
        state.subagents = None

        with (
            patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()),
            patch.object(chat_runner, "_run_chat", return_value=MagicMock()),
        ):
            assert await chat_runner._start_next_queued_turn(state, slot) is True

        roles = [m["role"] for m in slot.messages]
        contents = [m["content"] for m in slot.messages]
        assert "held" in contents
        assert "user" in roles
        assert contents.index("held") < roles.index("user")
        assert slot._deferred_notes == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "children, ends_batch",
        [
            ({}, True),
            ({"running": [{"id": "b2", "stalled": True}]}, False),
            ({"in_memory": True}, False),
            ({"delivering": 1}, False),
            ({"unreadable": True}, False),
            ({"probe_raises": True}, False),
            ({"queued_in_store": 1}, False),
            ({"store_unreadable": True}, False),
        ],
        ids=[
            "settled",
            "running",
            "pending-in-memory",
            "delivering",
            "unreadable",
            "probe-raises",
            "queued-in-store",
            "store-unreadable",
        ],
    )
    async def test_a_user_message_ends_the_batch_only_when_nothing_is_out(
        self, tmp_path, children, ends_batch
    ):
        """A user message drained at a turn's end (`_hand_off_queue`, the
        ending turn still holding the floor) disarms the synthesis either way.
        It clears the batch's turn count only when the fire gate answers clear,
        so no child of the batch is still out: clearing a live batch's count
        loses its synthesis, while keeping a stale one costs at most one extra
        synthesis. A store nobody could read keeps it too. The running child is
        flagged stalled so the drain's own child hold lets the message through;
        the fire gate still counts it as running."""
        state, slot = _state(tmp_path), _slot()
        slot._pending_synthesis = True
        slot._synthesis_completion_turns = 2
        slot._subagent_deliveries_inflight = children.get("delivering", 0)
        subs = _children(
            running=children.get("running", ()),
            in_memory=children.get("in_memory", False),
            # A gate-deferred child the store alone holds. The manager has
            # published no depth for it (its table stays empty), so only the
            # fire gate's store read sees it. None is a store nobody could read.
            queued=(
                None if children.get("store_unreadable") else children.get("queued_in_store", 0)
            ),
        )
        if children.get("unreadable"):
            subs.running_agents_for = MagicMock(return_value=None)
        if children.get("probe_raises"):
            subs.has_in_memory_pending_work_for = MagicMock(side_effect=RuntimeError("gone"))
        state.subagents = subs
        slot.queue_append("a user message")
        slot.task = ending = asyncio.get_running_loop().create_future()

        with (
            patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()) as spawn,
            patch.object(chat_runner, "_run_chat", new=MagicMock(return_value=MagicMock())),
        ):
            try:
                await chat_runner._hand_off_queue(
                    state, slot, drain=True, allow_automatic_successor=True
                )
            finally:
                ending.cancel()

        assert spawn.call_count == 1, "the message drained as the next turn"
        assert slot._pending_synthesis is False, "the user takes over, as before"
        assert slot._synthesis_completion_turns == (0 if ends_batch else 2)

    @pytest.mark.asyncio
    async def test_an_armed_drain_reads_no_store_and_keeps_the_count(self, tmp_path):
        """The drain itself (Run now, Continue, rewind, regenerate, the parked
        drain) is origin/main's await-free dequeue: it reads no store, so it
        cannot judge the batch, and it keeps the count. Only its callers that
        read the fire gate with the floor held say `end_batch`."""
        state, slot = _state(tmp_path), _slot()
        slot._pending_synthesis = True
        slot._synthesis_completion_turns = 2
        state.subagents = _children()
        slot.queue_append("a user message")
        seen: list = []

        async def _verdict(_state, _slot):
            seen.append(True)
            return chat_runner.SYNTHESIS_CLEAR

        with (
            patch.object(chat_runner, "synthesis_fire_verdict", new=_verdict),
            patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()) as spawn,
            patch.object(chat_runner, "_run_chat", new=MagicMock(return_value=MagicMock())),
        ):
            started = await chat_runner._start_next_queued_turn(state, slot)

        assert started is True
        assert spawn.call_count == 1
        assert seen == [], "no fire-gate read in the drain"
        state.subagents.queued_count_or_none_async.assert_not_awaited()
        state.subagents.has_in_memory_pending_work_for.assert_not_called()
        assert slot._pending_synthesis is False, "the user takes over, as before"
        assert slot._synthesis_completion_turns == 2, "a drain that read nothing keeps the count"

    @pytest.mark.asyncio
    async def test_the_hand_off_reads_while_the_ending_turn_holds_the_floor(self, tmp_path):
        """The turn-end hand-off takes the one fire-gate read, and it takes it
        while `slot.task` is still the ending turn and the message is still
        queued: a completion that lands then queues behind the turn instead of
        taking the floor, so the drain that follows cannot dispatch beside one.
        The verdict reaches the drain as `end_batch`."""
        state, slot = _state(tmp_path), _slot()
        slot._pending_synthesis = True
        slot._synthesis_completion_turns = 2
        state.subagents = _children()
        qid = slot.queue_append("a user message")
        slot.task = ending = asyncio.get_running_loop().create_future()
        seen: list = []

        async def _verdict(_state, _slot):
            seen.append(([item["id"] for item in _slot._queue], _slot.task))
            # flake-ok: pre-existing zero-length yield standing in for the store read's suspension, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0)  # a real suspension, as the store read is
            return chat_runner.SYNTHESIS_CLEAR

        with (
            patch.object(chat_runner, "synthesis_fire_verdict", new=_verdict),
            patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()) as spawn,
            patch.object(chat_runner, "_run_chat", new=MagicMock(return_value=MagicMock())),
        ):
            try:
                await chat_runner._hand_off_queue(
                    state, slot, drain=True, allow_automatic_successor=True
                )
            finally:
                ending.cancel()

        assert seen == [([qid], ending)], "one read, message still queued, floor still held"
        assert spawn.call_count == 1
        assert slot._pending_synthesis is False
        assert slot._synthesis_completion_turns == 0

    @pytest.mark.asyncio
    async def test_an_unarmed_hand_off_reads_no_store(self, tmp_path):
        """Only an armed synthesis's disarm needs the verdict, so an ordinary
        turn end costs no store read."""
        state, slot = _state(tmp_path), _slot()
        state.subagents = _children()
        slot.queue_append("a user message")
        slot.task = ending = asyncio.get_running_loop().create_future()

        with (
            patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()) as spawn,
            patch.object(chat_runner, "_run_chat", new=MagicMock(return_value=MagicMock())),
        ):
            try:
                await chat_runner._hand_off_queue(
                    state, slot, drain=True, allow_automatic_successor=True
                )
            finally:
                ending.cancel()

        assert spawn.call_count == 1
        state.subagents.queued_count_or_none_async.assert_not_awaited()
        state.subagents.has_in_memory_pending_work_for.assert_not_called()


def _no_children() -> MagicMock:
    """A registry with no child attached: none running and none queued.

    The synthesis checks ask the teardown guards' predicate, which also counts
    children the spawn gate still holds; a bare MagicMock would answer that
    count with a truthy mock and read as a queued child.
    """
    return _children()


def _children(*, running=(), queued: int | None = 0, in_memory: bool = False) -> MagicMock:
    """A registry double for the synthesis fire gate with every probe set
    explicitly: a bare MagicMock answers an unset probe with a truthy mock
    (``int(MagicMock()) == 1``), which reads as a queued child. *queued*
    ``None`` is a store nobody could read. The manager's published depth
    table, which the slots push reads, stays empty: no synthesis check reads
    it, so a queued child is *queued* alone."""
    return MagicMock(
        running_agents_for=MagicMock(return_value=list(running)),
        queued_count_for_async=AsyncMock(return_value=queued or 0),
        queued_count_or_none_async=AsyncMock(return_value=queued),
        has_in_memory_pending_work_for=MagicMock(return_value=in_memory),
        published_queued_depths=MagicMock(return_value={}),
    )


class TestRunPendingSynthesis:
    @pytest.mark.asyncio
    async def test_unarmed_synthesis_just_finishes_the_cycle(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        slot._pending_synthesis = False
        slot._synthesis_inflight = True

        with patch.object(chat_runner, "_finish_queue_cycle") as finish:
            await chat_runner._run_pending_synthesis(state, slot)

        finish.assert_called_once()
        assert slot._synthesis_inflight is False

    @pytest.mark.asyncio
    async def test_a_queued_message_takes_priority_over_synthesis(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        slot._pending_synthesis = True
        slot.queue_append("queued work")

        with patch.object(
            chat_runner, "_start_next_queued_turn", new=AsyncMock(return_value=True)
        ) as start:
            await chat_runner._run_pending_synthesis(state, slot)

        start.assert_awaited_once()
        assert slot._pending_synthesis is True

    @pytest.mark.asyncio
    async def test_the_dispatch_does_not_read_the_store_again(self, tmp_path):
        """The fire gate's verdict is taken once, by the caller: the synthesis
        dispatch itself asks no registry probe."""
        state, slot = _state(tmp_path), _slot()
        slot._pending_synthesis = True
        state.subagents = _children()

        async def _ok():
            return None

        def _capture(_state, _slot, coro, *a, **kw):
            coro.close()
            return asyncio.ensure_future(_ok())

        with (
            patch.object(chat_runner, "spawn_guarded_turn", side_effect=_capture),
            patch.object(chat_runner, "_run_chat", return_value=MagicMock()),
        ):
            await chat_runner._run_pending_synthesis(state, slot)

        state.subagents.queued_count_or_none_async.assert_not_awaited()
        state.subagents.running_agents_for.assert_not_called()
        assert slot._pending_synthesis is False

    @pytest.mark.asyncio
    async def test_running_the_synthesis_ends_its_batch(self, tmp_path):
        """The consumed arm takes the batch's turn count with it, so the next
        lone completion is a batch of its own."""
        state, slot = _state(tmp_path), _slot()
        slot._pending_synthesis = True
        slot._synthesis_completion_turns = 2
        state.subagents = _children()

        async def _ok():
            return None

        def _capture(_state, _slot, coro, *a, **kw):
            coro.close()
            return asyncio.ensure_future(_ok())

        with (
            patch.object(chat_runner, "spawn_guarded_turn", side_effect=_capture),
            patch.object(chat_runner, "_run_chat", new=MagicMock(return_value=MagicMock())),
        ):
            await chat_runner._run_pending_synthesis(state, slot)

        assert slot._synthesis_completion_turns == 0

    @pytest.mark.asyncio
    async def test_a_queued_user_message_ends_the_batch_without_a_read(self, tmp_path):
        """The caller launched this task on a clear verdict, so a user message
        it drains instead of the synthesis ends the batch (count zeroed) with
        no second fire-gate read: the drain runs with `end_batch`."""
        state, slot = _state(tmp_path), _slot()
        slot._pending_synthesis = True
        slot._synthesis_inflight = True
        slot._synthesis_completion_turns = 2
        state.subagents = _children()
        slot.queue_append("a user message")

        with (
            patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()) as spawn,
            patch.object(chat_runner, "_run_chat", new=MagicMock(return_value=MagicMock())),
        ):
            await chat_runner._run_pending_synthesis(state, slot)

        assert spawn.call_count == 1, "the user message ran, not the synthesis"
        assert [m["role"] for m in slot.messages if m["role"] == "inject"] == []
        state.subagents.queued_count_or_none_async.assert_not_awaited()
        state.subagents.has_in_memory_pending_work_for.assert_not_called()
        assert slot._pending_synthesis is False
        assert slot._synthesis_completion_turns == 0
        assert slot._synthesis_inflight is False

    @pytest.mark.asyncio
    async def test_synthesis_timeout_is_swallowed(self, tmp_path):
        """The ceiling already rendered a card; re-raising would go unretrieved."""
        state, slot = _state(tmp_path), _slot()
        slot._pending_synthesis = True
        state.subagents = _no_children()

        async def _boom():
            raise asyncio.TimeoutError

        with (
            patch.object(
                chat_runner, "spawn_guarded_turn", return_value=asyncio.ensure_future(_boom())
            ),
            patch.object(chat_runner, "_run_chat", return_value=MagicMock()),
        ):
            await chat_runner._run_pending_synthesis(state, slot)

        assert slot._pending_synthesis is False
        assert slot._synthesis_inflight is False

    @pytest.mark.asyncio
    async def test_synthesis_prompt_is_appended_as_an_inject_row(self, tmp_path):
        """The prompt must reach the transcript as `inject`, never as user speech.

        This site bypasses `_start_next_queued_turn` (it runs no queue entry),
        which is the only other place a turn-dispatching path appends a row.
        Without a row of its own the prompt reaches the conversation log with
        nothing on the dashboard, and resurfaces attributed to the USER on
        replay.
        """
        state, slot = _state(tmp_path), _slot()
        slot._pending_synthesis = True
        state.subagents = _no_children()

        async def _ok():
            return None

        # Observed INSIDE the dispatch, so the test can prove the row was already
        # appended when the turn started. Asserting only on the final state cannot
        # tell append-before-dispatch from append-after, and the ordering is the
        # whole point: a turn that dies immediately must still leave the row.
        seen: dict = {}

        def _capture(_state, _slot, coro, *a, **kw):
            seen["rows"] = len(_slot.messages)
            coro.close()  # never awaited; avoids an un-awaited-coroutine warning
            return asyncio.ensure_future(_ok())

        with (
            patch.object(chat_runner, "spawn_guarded_turn", side_effect=_capture),
            patch.object(chat_runner, "_run_chat", return_value=MagicMock()) as run_chat,
        ):
            await chat_runner._run_pending_synthesis(state, slot)

        assert seen.get("rows") == 1, "the row must exist BEFORE the turn is dispatched"
        rows = [m for m in slot.messages if m.get("content", "").startswith("[SYSTEM] Sub-agent")]
        assert len(rows) == 1, "the synthesis prompt must appear exactly once"
        row = rows[0]
        assert row["role"] == "inject"
        # Never the user-bubble class: that is what produced the reported defect.
        assert row.get("cls") == "msg msg-inject"
        # Durable provenance. `cls` is NOT persisted for role `inject`, so a render
        # side keyed on cls-derived data mis-renders every restored row; `meta` is.
        assert (row.get("meta") or {}).get("injectKind") == "synthesis"
        # And the turn itself must declare it is runner-authored, rather than
        # leaving a downstream marker-match to recover the same fact.
        assert run_chat.call_args.kwargs.get("_synthetic_payload") is True

    def test_inject_provenance_survives_the_persistence_boundary(self):
        """`injectKind` must round-trip; the older `cls` channel does not.

        This is the property the whole render decision now rests on. An inject
        row's ``cls`` is persisted only for ``role == "system"``, so the
        ``cronLabel`` the frontend reads (synthesized from ``cls`` at emit time)
        silently disappears on the next rehydrate — which is exactly how a
        carve-out keyed on it swallowed every restored cron notification. Assert
        the durable channel directly rather than trusting the live shape.
        """
        from kiro_crew.dashboard.chat_persistence import _build_message_entry_uncached

        cron_cls = json.dumps({"cronLabel": "nightly-audit"})
        entry = _build_message_entry_uncached(
            {
                "role": "inject",
                "content": '[Cron notification from "nightly-audit"]\nreport\n',
                "cls": cron_cls,
                "meta": {"injectKind": "cron", "cronLabel": "nightly-audit"},
            }
        )
        # The negative half — proves the bug this replaces was real, and fails if
        # someone "fixes" it by widening the cls gate instead.
        assert "cls" not in entry, "cls is not persisted for an inject row"
        assert entry["meta"]["injectKind"] == "cron"
        assert entry["meta"]["cronLabel"] == "nightly-audit"

        synth = _build_message_entry_uncached(
            {
                "role": "inject",
                "content": "[SYSTEM] Sub-agent synthesis: go",
                "cls": "msg msg-inject",
                "meta": {"injectKind": "synthesis"},
            }
        )
        assert synth["meta"]["injectKind"] == "synthesis"


class TestFinishQueueCycle:
    @pytest.mark.asyncio
    async def test_eligible_synthesis_is_started_instead_of_going_idle(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        state._slots[slot.key] = slot  # a live slot is registered
        slot._pending_synthesis = True
        state.subagents = _no_children()

        with patch.object(chat_runner, "_run_pending_synthesis", new=AsyncMock()):
            await chat_runner._finish_queue_cycle(state, slot)
            # flake-ok: pre-existing zero-length yield that lets a task the call scheduled run, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0)

        assert slot._synthesis_inflight is True
        assert not any(m.get("role") == "done" for m in slot.messages)
        if slot.task is not None:
            slot.task.cancel()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "children",
        [
            # The running half is what is pinned: the queued probe answers 0.
            {"running": ["a"], "queued": 0},
            # A child only the store holds (gate-deferred, or claimed and not
            # registered).
            {"queued": 1},
            # A sibling whose report still waits on its teardown, a window
            # entry, a live follow-up watcher.
            {"in_memory": True},
        ],
        ids=["running", "queued-in-store", "pending-in-memory"],
    )
    async def test_an_attached_child_keeps_the_cycle_idle(self, tmp_path, children):
        """The fire gate holds for any of its four terms: the cycle goes idle
        with the arm kept, and no synthesis task starts."""
        state, slot = _state(tmp_path), _slot()
        state._slots[slot.key] = slot  # a live slot is registered
        slot._pending_synthesis = True
        state.subagents = _children(**children)

        with patch.object(chat_runner, "_run_pending_synthesis", new=AsyncMock()) as synth:
            await chat_runner._finish_queue_cycle(state, slot)
            # flake-ok: pre-existing zero-length yield that lets a task the call scheduled run, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0)

        synth.assert_not_called()
        assert slot._synthesis_inflight is False
        assert slot._pending_synthesis is True
        assert slot._synthesis_recheck is None, "a real answer arms no re-check"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("slot_key", ["slack_1712.000100", "cron-nightly"])
    async def test_a_linked_tab_is_checked_on_its_own_session_key(self, tmp_path, slot_key):
        """A channel- or cron-born tab's children register under its real
        session, not ``dashboard:<slot>``: a gate-deferred child there keeps the
        synthesis from firing."""
        state, slot = _state(tmp_path), _slot(slot_key)
        linked = "slack:1712.000100" if slot_key.startswith("slack_") else "cron:nightly"
        slot.linked_session_key = linked
        state._slots[slot.key] = slot
        slot._pending_synthesis = True
        subs = _children()
        subs.queued_count_or_none_async = AsyncMock(
            side_effect=lambda key: 1 if key == linked else 0
        )
        state.subagents = subs

        with patch.object(chat_runner, "_run_pending_synthesis", new=AsyncMock()) as synth:
            await chat_runner._finish_queue_cycle(state, slot)
            # flake-ok: pre-existing zero-length yield that lets a task the call scheduled run, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0)

        synth.assert_not_called()
        subs.running_agents_for.assert_called_once_with(linked)
        subs.queued_count_or_none_async.assert_awaited_once_with(linked)

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("close_subagent_managers")
    async def test_a_slow_teardown_sibling_holds_the_fire_gate(self, tmp_path):
        """A: done and reported. B: done, its report still waiting on a teardown
        longer than the reset timeout. Nothing is running or queued, but B's
        result has not landed: the real manager's in-memory term holds the
        fire gate until B's task ends."""
        from kiro_crew.subagent import SubagentInfo, SubagentManager

        state, slot = _state(tmp_path), _slot()
        state._slots[slot.key] = slot
        slot._pending_synthesis = True
        mgr = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
        parent = f"dashboard:{slot.key}"
        b = SubagentInfo(id="b1", task="b", parent_session_key=parent)
        b.done = True
        mgr._agents[b.id] = b
        teardown = asyncio.get_running_loop().create_future()  # never resolves here
        mgr._tasks[b.id] = teardown
        mgr.queued_count_or_none_async = AsyncMock(return_value=0)
        state.subagents = mgr
        try:
            with patch.object(chat_runner, "_run_pending_synthesis", new=AsyncMock()) as synth:
                await chat_runner._finish_queue_cycle(state, slot)
                # flake-ok: pre-existing zero-length yield that lets a task the call scheduled run, moved verbatim; tracked for a de-flake pass
                await asyncio.sleep(0)
            synth.assert_not_called()
            assert slot._pending_synthesis is True
        finally:
            teardown.cancel()

    @pytest.mark.asyncio
    async def test_a_message_sent_during_the_fire_check_runs_next(self, tmp_path):
        """The user sends while the fire gate reads the store: the turn still
        looks busy, so the message queues behind it. With a child still queued
        the synthesis does not fire, and the cycle drains that message the way a
        normal turn end does instead of leaving it waiting for another turn."""
        state, slot = _state(tmp_path), _slot()
        state._slots[slot.key] = slot
        slot._pending_synthesis = True
        subs = _children()

        async def _queued_during_probe(_key):
            # flake-ok: pre-existing zero-length yield standing in for the store probe's suspension, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0)
            slot.queue_append("user msg sent during the probe")
            return 1  # one gate-held child: attached

        subs.queued_count_or_none_async = AsyncMock(side_effect=_queued_during_probe)
        state.subagents = subs

        with patch.object(
            chat_runner, "_start_next_queued_turn", new=AsyncMock(return_value=True)
        ) as start:
            await chat_runner._finish_queue_cycle(state, slot)

        start.assert_awaited_once()
        assert not any(m.get("role") == "done" for m in slot.messages)

    @pytest.mark.asyncio
    async def test_an_unreadable_store_re_checks_and_fires_on_its_own(self, tmp_path):
        """No completion is left to re-trigger the check after a store outage at
        the last turn end, so the idle slot re-asks on a timer and fires once
        the store answers -- without waiting for a user turn."""
        state, slot = _state(tmp_path), _slot()
        state._slots[slot.key] = slot
        slot._pending_synthesis = True
        subs = _children()
        answers = iter([None, 0])
        subs.queued_count_or_none_async = AsyncMock(side_effect=lambda _k: next(answers))
        state.subagents = subs

        with (
            patch.object(chat_runner, "_SYNTHESIS_RECHECK_SECS", 0.01),
            patch.object(chat_runner, "_run_pending_synthesis", new=AsyncMock()) as synth,
        ):
            await chat_runner._finish_queue_cycle(state, slot)
            assert slot._synthesis_recheck is not None, "an outage arms the re-check"
            synth.assert_not_called()
            await async_wait_until(lambda: synth.await_count)

        synth.assert_awaited_once()
        assert slot._synthesis_inflight is True
        if slot.task is not None:
            slot.task.cancel()

    @pytest.mark.asyncio
    async def test_a_batch_reported_in_one_turn_drops_the_synthesis(self, tmp_path):
        """One completion turn already carried every result: the cycle ends as
        an ordinary one and the arm is consumed on the turn-end path."""
        state, slot = _state(tmp_path), _slot()
        state._slots[slot.key] = slot
        slot._pending_synthesis = True
        slot._synthesis_completion_turns = 1
        state.subagents = _children()

        with patch.object(chat_runner, "_run_pending_synthesis", new=AsyncMock()) as synth:
            await chat_runner._finish_queue_cycle(state, slot)

        synth.assert_not_called()
        assert slot._pending_synthesis is False
        assert slot._synthesis_completion_turns == 0
        assert slot._synthesis_recheck is None
        assert slot.messages[-1]["role"] == "done"

    @pytest.mark.asyncio
    async def test_the_outage_re_check_launches_the_synthesis_whatever_the_count(self, tmp_path):
        """The re-check runs only after the turn-end gate could not read the
        store, so it keeps main's shape: a clear answer launches the synthesis
        even for a batch one turn reported. The turn's chat_done already went
        out with the arm set, and the synthesis turn ends with its own."""
        state, slot = _state(tmp_path), _slot()
        state._slots[slot.key] = slot
        slot._pending_synthesis = True
        slot._synthesis_completion_turns = 1
        subs = _children()
        answers = iter([None, 0])
        subs.queued_count_or_none_async = AsyncMock(side_effect=lambda _k: next(answers))
        state.subagents = subs

        def _chat_dones() -> list:
            return [
                c.args[1]["continuing"]
                for c in state.broadcast_ws.call_args_list
                if c.args and c.args[0] == "chat_done"
            ]

        with (
            patch.object(chat_runner, "_SYNTHESIS_RECHECK_SECS", 0.01),
            patch.object(chat_runner, "_run_pending_synthesis", new=AsyncMock()) as synth,
        ):
            await chat_runner._finish_queue_cycle(state, slot)
            assert _chat_dones() == [True], "the arm was still set at the turn's end"
            assert slot._synthesis_recheck is not None, "an outage arms the re-check"
            synth.assert_not_called()
            await async_wait_until(lambda: synth.await_count)

        synth.assert_awaited_once()
        assert slot._pending_synthesis is True, "consumed by the synthesis task, not here"
        assert slot._synthesis_completion_turns == 1
        assert _chat_dones() == [True], "no second chat_done: the synthesis turn ends its own"
        if slot.task is not None:
            slot.task.cancel()

    @pytest.mark.asyncio
    async def test_closing_the_slot_cancels_a_pending_re_check(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        state._slots[slot.key] = slot
        slot._pending_synthesis = True
        state.subagents = _children(queued=None)

        with patch.object(chat_runner, "_SYNTHESIS_RECHECK_SECS", 60.0):
            await chat_runner._finish_queue_cycle(state, slot)
        handle = slot._synthesis_recheck
        assert handle is not None
        slot.begin_close()
        assert handle.cancelled() and slot._synthesis_recheck is None

    @pytest.mark.asyncio
    async def test_a_held_note_is_withheld_from_an_automatic_synthesis_turn(self, tmp_path):
        """A note is owed to the next USER turn, so synthesis must not drain it.

        Synthesis is dispatched from this same function, so flushing here would
        hand the held context to a turn the user never asked for. The user-turn
        seams flush on their own, so withholding cannot lose the note.
        """
        state, slot = _state(tmp_path), _slot()
        state._slots[slot.key] = slot  # a live slot is registered
        slot._pending_synthesis = True
        state.subagents = _no_children()

        with (
            patch.object(type(slot), "flush_deferred_notes", return_value=0) as flush,
            patch.object(chat_runner, "_run_pending_synthesis", new=AsyncMock()),
        ):
            await chat_runner._finish_queue_cycle(state, slot)
            # flake-ok: pre-existing zero-length yield that lets a task the call scheduled run, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0)

        assert slot._synthesis_inflight is True
        flush.assert_not_called()
        if slot.task is not None:
            slot.task.cancel()

    @pytest.mark.asyncio
    async def test_a_closing_slot_does_not_lose_its_held_note_to_synthesis(self, tmp_path):
        """A slot already removed from the registry has no next USER turn.

        The withhold above is scoped to WHICH successor drains the note, and it
        assumes a successor exists. A slot gone from ``state._slots`` is being
        torn down, so withholding there discards the note the POST acknowledged.
        """
        state, slot = _state(tmp_path), _slot()
        state.subagents = _no_children()
        slot._pending_synthesis = True
        slot._deferred_notes.append({"content": "held across the close", "cls": "reconcile-note"})
        # The teardown already dropped it from the registry.
        assert state._slots.get(slot.key) is None

        with patch.object(chat_runner, "_run_pending_synthesis", new=AsyncMock()):
            await chat_runner._finish_queue_cycle(state, slot)
            # flake-ok: pre-existing zero-length yield that lets a task the call scheduled run, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0)

        assert slot._deferred_notes == [], "the held note was discarded on close"
        assert any(
            m.get("role") == "inject" and "held across the close" in m.get("content", "")
            for m in slot.messages
        )
        if slot.task is not None:
            slot.task.cancel()

    @pytest.mark.asyncio
    async def test_a_cycle_with_no_synthesis_still_flushes(self, tmp_path):
        """The withhold is scoped to synthesis; every other cycle still flushes."""
        state, slot = _state(tmp_path), _slot()
        slot._pending_synthesis = False

        with (
            patch.object(type(slot), "flush_deferred_notes", return_value=0) as flush,
            patch.object(chat_runner, "title_then_refresh", new=AsyncMock()),
        ):
            await chat_runner._finish_queue_cycle(state, slot)
            # flake-ok: pre-existing zero-length yield that lets a task the call scheduled run, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0)

        flush.assert_called_once()

    @pytest.mark.asyncio
    async def test_idle_cycle_emits_done_and_refreshes_the_sidebar(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        slot._cycle_reached_provider = True  # a provider served this cycle

        with patch.object(chat_runner, "title_then_refresh", new=AsyncMock()):
            await chat_runner._finish_queue_cycle(state, slot)
            # flake-ok: pre-existing zero-length yield that lets a task the call scheduled run, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0)

        assert slot.messages[-1]["role"] == "done"
        assert slot.task is None
        state.refresh_slot_source_status.assert_called_once_with(slot.key)
        state.broadcast_ws.assert_any_call(
            "chat_done",
            {
                "slot": slot.key,
                "turn": slot._turn_generation,
                "turn_gen": chunk_generation(),
                "continuing": False,
                "needs_input": False,
            },
        )
        assert slot._cycle_reached_provider is False, "the next cycle starts unserved"
