"""Retention of a sub-agent's ``result.txt`` is anchored on the parent CONSUMING
the completion, not on the run finishing (issue #4839).

``agent.subagent_result_ttl_secs`` exists so the parent can read the full
transcript after the completion event arrives. The clock is the ``died`` stamp on
the ``delivered`` tombstone, and that used to be written as soon as the gateway
had ROUTED the completion — including the route that only parks the announce in a
busy slot's queue. A queue wait is bounded by the turn ceiling, not by the TTL, so
a wave whose events were delivered two hours later handed the parent result paths
the reaper had already pruned, under the line "Full outputs are on disk".

The fix splits routing from consumption: the gateway records the owed ids on the
slot and leaves the folder un-tombstoned (so nothing prunes it, and a restart can
still recover it), and the queue drain writes the tombstone once the row becomes a
turn.
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.dashboard.chat_runner import _start_next_queued_turn
from kiro_crew.dashboard.chat_utils import SUBAGENT_COMPLETION_KIND, SYNTHETIC_RECOVERY_KIND
from kiro_crew.dashboard.state import (
    _MAX_PENDING_SUBAGENT_DELIVERIES,
    SUBAGENT_COMPLETION_PREFIX,
    _ChatSlot,
)
from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.subagent_persistence import (
    create_agent_folder,
    prune_stale_tombstones,
    write_result_chunk,
)

COMPLETION = f"{SUBAGENT_COMPLETION_PREFIX}\nAgent `a1` completed ✅\nResult saved at: /x"
SECOND_COMPLETION = f"{SUBAGENT_COMPLETION_PREFIX}\nAgent `a2` completed ✅\nResult saved at: /y"


def _ann(tag: str) -> str:
    """A distinct announce standing in for one queued completion."""
    return f"{SUBAGENT_COMPLETION_PREFIX}\nAgent `{tag}` completed ✅"


def _key(content: str) -> str:
    """The ledger's key for an announce (a digest, not the text)."""
    from kiro_crew.dashboard.state import _delivery_key

    return _delivery_key(content)


@pytest.fixture()
def agent_root(tmp_path, monkeypatch):
    """Point the sub-agent registry at a temp directory."""
    monkeypatch.setattr("kiro_crew.subagent_persistence._SUBAGENTS_DIR", tmp_path)
    return tmp_path


def _finished_run(agent_id: str, root) -> None:
    """A completed run's folder: state + a non-empty result.txt, no tombstone."""
    create_agent_folder(agent_id, task="t", parent_session="dashboard:main")
    write_result_chunk(agent_id, "the full transcript\n")


async def _settled(predicate, timeout: float = 3.0) -> None:
    """Poll until *predicate* holds — settlement lands on a worker thread."""
    deadline = asyncio.get_event_loop().time() + timeout
    while not predicate() and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.02)
    assert predicate()


# ── The slot-side ledger of owed delivery marks ───────────────────────


class TestPendingDeliveryLedger:
    def test_note_then_take_returns_ids_in_drain_order(self):
        slot = _ChatSlot("s1")
        slot.note_pending_subagent_delivery(_ann("one"), ["a1", "a2"])
        slot.note_pending_subagent_delivery(_ann("two"), ["a3"])

        assert slot.take_pending_subagent_deliveries([_ann("one"), _ann("two")]) == [
            "a1",
            "a2",
            "a3",
        ]
        # Claimed once: a second drain of the same row owes nothing.
        assert slot.take_pending_subagent_deliveries([_ann("one"), _ann("two")]) == []

    def test_empty_ids_record_nothing(self):
        """A failed member keeps the tombstone its own error path wrote, and a
        flush-only record has no folder — neither owes a mark."""
        slot = _ChatSlot("s1")
        slot.note_pending_subagent_delivery(_ann("one"), [])
        assert slot._subagent_delivery_pending == {}

    def test_row_that_left_the_queue_keeps_its_entry(self):
        """The ledger must NOT sweep entries whose row is no longer queued: the
        tail-drain at the end of a turn pops the NEXT completion row before this
        turn's settlement callback runs, so a sweep would delete the successor's
        debt and the next start would re-announce its consumed result."""
        slot = _ChatSlot("s1")
        slot.note_pending_subagent_delivery(_ann("first"), ["a1"])
        slot.note_pending_subagent_delivery(_ann("second"), ["a2"])
        slot._queue = []  # both rows drained; only the first turn has finished

        assert slot.take_pending_subagent_deliveries([_ann("first")]) == ["a1"]
        assert slot._subagent_delivery_pending == {_key(_ann("second")): ["a2"]}

    def test_ledger_is_bounded(self):
        """Without a sweep, a row that vanishes unconsumed leaves its entry, so
        the ledger evicts oldest-first instead of growing forever."""
        slot = _ChatSlot("s1")
        for i in range(_MAX_PENDING_SUBAGENT_DELIVERIES + 5):
            slot.note_pending_subagent_delivery(_ann(f"q{i}"), [f"a{i}"])

        assert len(slot._subagent_delivery_pending) == _MAX_PENDING_SUBAGENT_DELIVERIES
        assert _key(_ann("q0")) not in slot._subagent_delivery_pending  # oldest evicted
        assert (
            _key(_ann(f"q{_MAX_PENDING_SUBAGENT_DELIVERIES + 4}"))
            in slot._subagent_delivery_pending
        )


# ── The gateway side: routing to a queue owes, it does not deliver ────


def _member(agent_id: str = "a1", *, error: str = "") -> SubagentInfo:
    info = SubagentInfo(id=agent_id, task="t", parent_session_key="dashboard:main")
    info.done = True
    info.error = error
    info.result = "r"
    info.result_path = f"/tmp/{agent_id}/result.txt"
    return info


def _manager() -> SubagentManager:
    """A manager with no live runs: every settle finds no teardown gate to wait on.

    Production always has one -- the delivery debt only exists because this
    manager's own completion callback created it -- so a drain test without one
    would exercise a path the gateway never takes.
    """
    return SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())


def _defer(slot, info, *, flush_only: bool = False, announce: str = COMPLETION):
    from kiro_crew.slack.gateway import GatewayOrchestrator

    GatewayOrchestrator._defer_queued_delivery(slot, announce, info, flush_only=flush_only)


class TestDeferQueuedDelivery:
    def test_owes_the_member_and_flags_the_run_loop(self):
        slot = _ChatSlot("s1")
        info = _member()

        _defer(slot, info)

        assert slot._subagent_delivery_pending == {_key(COMPLETION): ["a1"]}
        # The run loop reads this AFTER _on_done returns and skips its own
        # mark_delivered, which is what keeps result.txt alive while queued.
        assert info._delivery_queued is True

    def test_wave_digest_transfers_its_held_ids(self):
        """A queued digest chunk carries its held members' results too, so their
        settle list moves to the slot instead of firing at enqueue."""
        slot = _ChatSlot("s1")
        info = _member("flusher")
        info._digest_settle_ids = ["h1", "h2"]

        _defer(slot, info)

        assert slot._subagent_delivery_pending == {_key(COMPLETION): ["flusher", "h1", "h2"]}
        # Transferred, not copied: _settle_digest_holds must not double-write.
        assert info._digest_settle_ids == []

    def test_failed_member_owes_nothing(self):
        slot = _ChatSlot("s1")
        info = _member(error="boom")

        _defer(slot, info)

        assert slot._subagent_delivery_pending == {}
        assert info._delivery_queued is True

    def test_stopped_member_owes_nothing(self):
        """A user stop leaves ``error`` EMPTY (it is a neutral outcome), so the
        error-nullability idiom reads it as completed. It already carries a reap
        tombstone with the 7-day post-mortem window, and a "delivered" write would
        cut its partial result down to the result TTL."""
        slot = _ChatSlot("s1")
        info = _member()
        info.user_stopped = True
        assert info.outcome == "stopped" and not info.error

        _defer(slot, info)

        assert slot._subagent_delivery_pending == {}

    def test_flush_only_record_owes_only_the_members_it_releases(self):
        """The synthetic flush record has no run of its own; only the held ids
        it is releasing are real folders."""
        slot = _ChatSlot("s1")
        info = _member("synthetic")
        info._digest_settle_ids = ["h1"]

        _defer(slot, info, flush_only=True)

        assert slot._subagent_delivery_pending == {_key(COMPLETION): ["h1"]}

    def test_slot_that_cannot_take_ids_keeps_the_old_behaviour(self):
        """Fail in the safe direction: if nothing can hold the debt, leave the
        run loop to tombstone now (a short window) rather than never."""
        slot = MagicMock()
        slot.note_pending_subagent_delivery.side_effect = RuntimeError("no ledger")
        info = _member()
        info._digest_settle_ids = ["h1"]

        _defer(slot, info)

        assert info._delivery_queued is False
        assert info._digest_settle_ids == ["h1"]


class TestRunLoopSkipsQueuedDelivery:
    @pytest.mark.asyncio
    async def test_no_tombstone_when_on_done_queued_the_completion(self, agent_root):
        """``_report_terminal`` marks delivered only when the result is actually
        in the parent's context. ``_on_done`` setting ``_delivery_queued`` says it
        is not."""
        mgr = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
        mgr._fire_event = AsyncMock()
        info = _member()
        _finished_run(info.id, agent_root)

        async def _on_done_queues(i):
            i._delivery_queued = True

        mgr._on_done = _on_done_queues
        await mgr._report_terminal(
            info,
            source="test",
            injection_timeout_reason="x",
            mark_delivered_on_success=True,
        )

        assert not (agent_root / info.id / "tombstone.json").exists()

    @pytest.mark.asyncio
    async def test_tombstone_when_the_completion_was_injected(self, agent_root):
        """The unqueued route is unchanged: a turn starts immediately, so the
        clock starts immediately."""
        mgr = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
        mgr._fire_event = AsyncMock()
        mgr._on_done = AsyncMock()
        info = _member("a2")
        _finished_run(info.id, agent_root)

        await mgr._report_terminal(
            info,
            source="test",
            injection_timeout_reason="x",
            mark_delivered_on_success=True,
        )

        ts = json.loads((agent_root / info.id / "tombstone.json").read_text(encoding="utf-8"))
        assert ts["cause"] == "delivered"


# ── The drain side: consumption starts the clock ──────────────────────


class TestConsumptionSignalIsPerTurn:
    """The settlement predicate reads a cell owned by the armed turn, never
    slot-wide state: a turn's tail-drain starts its successor before the
    predecessor's callback runs, so a shared field would be reset by the successor
    and leave the earlier (already consumed) completion unsettled."""

    def test_run_chat_reports_consumption_through_the_callers_hook(self):
        import inspect

        from kiro_crew.dashboard import chat_runner as mod

        src = inspect.getsource(mod._run_chat)
        lines = src.splitlines()
        # Reported on the two transitions that flip _turn_emitted ...
        reported_after_flip = [
            i
            for i, ln in enumerate(lines)
            if ln.strip() == "await _report_consumed(irreversible=True)"
            and lines[i - 1].strip().startswith("_turn_emitted = True")
        ]
        assert len(reported_after_flip) == 2
        # ... and on the provider's turn-complete event, which is what covers a
        # prompt that was consumed and produced NOTHING -- but only for a real
        # end-of-turn, since the same event carries the cut-short reasons whose
        # recovery re-queues the prompt.
        complete_at = src.index("elif event.kind == EVENT_COMPLETE:")
        window = src[complete_at : complete_at + 1400]
        assert "if event.stop_reason == STOP_REASON_END_TURN:\n" in window
        gate_at = window.index("if event.stop_reason == STOP_REASON_END_TURN:")
        assert window.index("await _report_consumed()") > gate_at
        # An equality against that one reason -- not a set that could quietly
        # readmit a cut-short turn (stale-recover, tool-stall, cancelled).
        gate_line = window[gate_at : window.index("\n", gate_at)]
        assert " in (" not in gate_line and " or " not in gate_line
        assert src.count("await _report_consumed(irreversible=True)") == 2
        assert src.count("await _report_consumed()") == 1
        # The retraction lives in the FIRST empty-response branch and happens
        # BEFORE the verbatim re-queue copies the callback. Reversing that order
        # drops the callback and strands the delivery after a successful replay.
        # Anchored on the rung marker rather than the branch condition: that
        # condition carries the productive-turn guard and is reformatted whenever
        # it grows a term, while the marker names the rung this invariant is about.
        first_empty_at = src.index("_empty_rung = EMPTY_RUNG_REPLAY")
        first_empty_end = src.index("            elif (", first_empty_at)
        first_empty = src[first_empty_at:first_empty_end]
        assert first_empty.count("await _report_consumed(False)") == 1
        assert first_empty.index("await _report_consumed(False)") < first_empty.index(
            "_queue_recovery("
        )
        assert "_last_turn_emitted" not in src
        drain = inspect.getsource(mod._start_next_queued_turn)
        assert '_run_kwargs["_on_consumed"] = _note_consumed' in drain

    def test_slot_carries_no_shared_consumption_flag(self):
        assert "_last_turn_emitted" not in _ChatSlot.__slots__

    @pytest.mark.asyncio
    async def test_a_successor_turn_cannot_unsettle_its_predecessor(self, agent_root, tmp_path):
        """Two queued completions: the first turn's own cell must still read
        consumed after a second turn has started and reported nothing."""
        from chat_test_helpers import _make_state

        state = _make_state(tmp_path / "state")
        state.subagents = _manager()
        slot = state.get_or_create_slot("successor")
        _finished_run("a1", agent_root)
        _finished_run("a2", agent_root)
        first_done: asyncio.Future = asyncio.get_event_loop().create_future()
        second_done: asyncio.Future = asyncio.get_event_loop().create_future()
        spawned: list[dict] = []

        def _spawn(_state, _slot, coro):
            hook = coro.cr_frame.f_locals.get("_on_consumed")
            coro.close()
            fut = second_done if spawned else first_done

            async def _turn():
                await fut

            spawned.append({"hook": hook})
            return asyncio.get_event_loop().create_task(_turn())

        slot.queue_append(COMPLETION, kind=SUBAGENT_COMPLETION_KIND)
        slot.note_pending_subagent_delivery(COMPLETION, ["a1"])
        with patch("kiro_crew.dashboard.chat_runner.spawn_guarded_turn", _spawn):
            assert await _start_next_queued_turn(state, slot) is True
            # First turn is consumed: its own cell records it.
            spawned[0]["hook"]()
            # Its tail-drain starts the SECOND completion before it finishes.
            slot.queue_append(SECOND_COMPLETION, kind=SUBAGENT_COMPLETION_KIND)
            slot.note_pending_subagent_delivery(SECOND_COMPLETION, ["a2"])
            assert await _start_next_queued_turn(state, slot) is True

        first_done.set_result(None)
        await _settled(lambda: (agent_root / "a1" / "tombstone.json").exists())
        # The successor reported nothing, so only the first is settled.
        assert not (agent_root / "a2" / "tombstone.json").exists()


class TestTeardownGateOnQueuedSettlement:
    """A ``delivered`` tombstone excludes the folder from restart orphan
    reconciliation, so it must not be written while the run's teardown is still
    killing its child -- a crash in that window would strand a live process that
    nothing reaps. ``_report_terminal`` holds its own write for that gate; the
    queued path settles elsewhere and must honour the same one."""

    @pytest.mark.asyncio
    async def test_settle_waits_for_the_runs_teardown(self, agent_root):
        mgr = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
        info = _member()
        _finished_run(info.id, agent_root)
        gate = asyncio.Event()
        mgr._teardown_gates[info.id] = gate
        mgr._agents[info.id] = info

        task = asyncio.create_task(mgr.settle_queued_delivery([info.id]))
        await asyncio.sleep(0.05)
        assert not (agent_root / info.id / "tombstone.json").exists()

        gate.set()  # teardown finished: the child is provably gone
        await task
        assert (agent_root / info.id / "tombstone.json").exists()

    @pytest.mark.asyncio
    async def test_settle_writes_immediately_once_teardown_is_done(self, agent_root):
        """A set gate -- or a run whose record is already gone -- means teardown
        has finished, so there is nothing to wait for."""
        mgr = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
        info = _member()
        _finished_run(info.id, agent_root)
        gate = asyncio.Event()
        gate.set()
        mgr._teardown_gates[info.id] = gate
        mgr._agents[info.id] = info
        _finished_run("evicted", agent_root)

        await mgr.settle_queued_delivery([info.id, "evicted"])

        assert (agent_root / info.id / "tombstone.json").exists()
        assert (agent_root / "evicted" / "tombstone.json").exists()

    @pytest.mark.asyncio
    async def test_an_evicted_run_still_waits_for_its_teardown(self, agent_root):
        """A dashboard "clear completed" / "cancel" pops a done-but-still-tearing-down
        run from BOTH ``_agents`` and ``_tasks``. The gate lives outside both, so the
        tombstone still waits -- inferring "record gone means child gone" would hide
        a live child from restart reconciliation."""
        mgr = _manager()
        info = _member()
        _finished_run(info.id, agent_root)
        gate = asyncio.Event()
        mgr._teardown_gates[info.id] = gate
        # Evicted exactly as api_spawn_clear does it: both records, together.
        mgr._agents.pop(info.id, None)
        mgr._tasks.pop(info.id, None)

        task = asyncio.create_task(mgr.settle_queued_delivery([info.id]))
        await asyncio.sleep(0.05)
        assert not (agent_root / info.id / "tombstone.json").exists()

        gate.set()
        await task
        assert (agent_root / info.id / "tombstone.json").exists()

    def test_the_drain_settles_only_through_the_manager(self):
        import inspect

        from kiro_crew.dashboard import chat_runner as mod

        src = inspect.getsource(mod._arm_queued_delivery_settlement)
        assert 'getattr(mgr, "settle_queued_delivery", None)' in src
        # No second write path: the debt only exists because the manager's own
        # completion callback created it, so a manager-less state cannot owe one,
        # and a direct write would bypass the teardown gate.
        assert not hasattr(mod, "_mark_queued_deliveries")
        assert "mark_delivered" not in src


class TestDrainSettlesDelivery:
    """Settlement is armed at dispatch and fires when the turn FINISHES.

    Two properties are pinned deliberately: nothing is written while the turn is
    still running (a durable tombstone before the turn is durable would lose the
    completion on a crash), and the write never happens inline on the event loop
    (``mark_delivered`` fsyncs).
    """

    def _turn_spawner(self, done: "asyncio.Future", *, consumed: bool = True):
        """Stand in for ``spawn_guarded_turn`` with a task the test controls.

        *consumed* mimics ``_run_chat`` reporting through the caller's hook: True
        when the model processed the prompt (turn-complete event, or an earlier
        token / tool call).
        """

        def _spawn(_state, _slot, coro):
            hook = coro.cr_frame.f_locals.get("_on_consumed")
            coro.close()  # the real runner would await it; we are not running a turn
            if consumed and hook is not None:
                hook()

            async def _turn():
                await done

            return asyncio.get_event_loop().create_task(_turn())

        return _spawn

    @pytest.mark.asyncio
    async def test_nothing_is_written_until_the_turn_completes(self, agent_root, tmp_path):
        """The dispatch path must not write (nor fsync) inline: the tombstone
        appears only after the turn it was armed on finishes."""
        from chat_test_helpers import _make_state

        state = _make_state(tmp_path / "state")
        state.subagents = _manager()
        slot = state.get_or_create_slot("drain-settles")
        _finished_run("a1", agent_root)
        slot.queue_append(COMPLETION, kind=SUBAGENT_COMPLETION_KIND)
        slot.note_pending_subagent_delivery(COMPLETION, ["a1"])
        done: asyncio.Future = asyncio.get_event_loop().create_future()

        with patch(
            "kiro_crew.dashboard.chat_runner.spawn_guarded_turn",
            self._turn_spawner(done),
        ):
            assert await _start_next_queued_turn(state, slot) is True

        # Turn still running: the promise is intact and the clock has not started.
        assert not (agent_root / "a1" / "tombstone.json").exists()
        assert slot._subagent_delivery_pending == {_key(COMPLETION): ["a1"]}

        done.set_result(None)
        await _settled(lambda: (agent_root / "a1" / "tombstone.json").exists())
        assert slot._subagent_delivery_pending == {}

    @pytest.mark.asyncio
    async def test_a_cancelled_turn_before_consumption_settles_nothing(self, agent_root, tmp_path):
        """Cancelled before the model got the prompt: it may never have read the
        result, so leave the folder un-tombstoned -- recoverable beats pruned."""
        from chat_test_helpers import _make_state

        state = _make_state(tmp_path / "state")
        state.subagents = _manager()
        slot = state.get_or_create_slot("drain-cancel")
        _finished_run("a1", agent_root)
        slot.queue_append(COMPLETION, kind=SUBAGENT_COMPLETION_KIND)
        slot.note_pending_subagent_delivery(COMPLETION, ["a1"])
        done: asyncio.Future = asyncio.get_event_loop().create_future()

        with patch(
            "kiro_crew.dashboard.chat_runner.spawn_guarded_turn",
            self._turn_spawner(done, consumed=False),
        ):
            assert await _start_next_queued_turn(state, slot) is True

        slot.task.cancel()
        await asyncio.sleep(0.05)

        assert not (agent_root / "a1" / "tombstone.json").exists()
        assert slot._subagent_delivery_pending == {_key(COMPLETION): ["a1"]}

    @pytest.mark.asyncio
    async def test_a_cancelled_turn_after_consumption_still_settles(self, agent_root, tmp_path):
        """Consumption is one-way: a session close that cancels the turn after the
        model already has the completion must still settle, or the next start
        re-announces a result the parent read."""
        from chat_test_helpers import _make_state

        state = _make_state(tmp_path / "state")
        state.subagents = _manager()
        slot = state.get_or_create_slot("drain-cancel-late")
        _finished_run("a1", agent_root)
        slot.queue_append(COMPLETION, kind=SUBAGENT_COMPLETION_KIND)
        slot.note_pending_subagent_delivery(COMPLETION, ["a1"])
        done: asyncio.Future = asyncio.get_event_loop().create_future()

        with patch(
            "kiro_crew.dashboard.chat_runner.spawn_guarded_turn",
            self._turn_spawner(done),  # consumed: the turn streamed output
        ):
            assert await _start_next_queued_turn(state, slot) is True

        slot.task.cancel()
        await _settled(lambda: (agent_root / "a1" / "tombstone.json").exists())

    @pytest.mark.asyncio
    async def test_a_failed_turn_before_consumption_settles_nothing(self, agent_root, tmp_path):
        from chat_test_helpers import _make_state

        state = _make_state(tmp_path / "state")
        state.subagents = _manager()
        slot = state.get_or_create_slot("drain-fail")
        _finished_run("a1", agent_root)
        slot.queue_append(COMPLETION, kind=SUBAGENT_COMPLETION_KIND)
        slot.note_pending_subagent_delivery(COMPLETION, ["a1"])
        done: asyncio.Future = asyncio.get_event_loop().create_future()

        with patch(
            "kiro_crew.dashboard.chat_runner.spawn_guarded_turn",
            self._turn_spawner(done, consumed=False),
        ):
            assert await _start_next_queued_turn(state, slot) is True

        done.set_exception(RuntimeError("turn blew up"))
        await asyncio.sleep(0.05)

        assert not (agent_root / "a1" / "tombstone.json").exists()

    @pytest.mark.asyncio
    async def test_an_auth_required_turn_settles_nothing(self, agent_root, tmp_path):
        """A signed-out CLI makes _run_chat render an error card and return
        NORMALLY, holding the queue for a post-login resume. The model never
        consumed the prompt, so nothing is reported and the result must survive
        until the user signs in."""
        from chat_test_helpers import _make_state

        state = _make_state(tmp_path / "state")
        state.subagents = _manager()
        slot = state.get_or_create_slot("drain-auth")
        _finished_run("a1", agent_root)
        slot.queue_append(COMPLETION, kind=SUBAGENT_COMPLETION_KIND)
        slot.note_pending_subagent_delivery(COMPLETION, ["a1"])
        done: asyncio.Future = asyncio.get_event_loop().create_future()

        with patch(
            "kiro_crew.dashboard.chat_runner.spawn_guarded_turn",
            self._turn_spawner(done, consumed=False),
        ):
            assert await _start_next_queued_turn(state, slot) is True

        slot._queue_held = True
        done.set_result(None)
        await asyncio.sleep(0.05)

        assert not (agent_root / "a1" / "tombstone.json").exists()
        assert slot._subagent_delivery_pending == {_key(COMPLETION): ["a1"]}

    @pytest.mark.asyncio
    async def test_a_requeued_turn_settles_nothing(self, agent_root, tmp_path):
        """A pre-stream provider death is HANDLED: _run_chat re-queues the prompt
        for a later retry and returns normally. The task looks successful, so only
        the emission signal keeps the retry's result alive."""
        from chat_test_helpers import _make_state

        state = _make_state(tmp_path / "state")
        state.subagents = _manager()
        slot = state.get_or_create_slot("drain-requeue")
        _finished_run("a1", agent_root)
        slot.queue_append(COMPLETION, kind=SUBAGENT_COMPLETION_KIND)
        slot.note_pending_subagent_delivery(COMPLETION, ["a1"])
        done: asyncio.Future = asyncio.get_event_loop().create_future()

        with patch(
            "kiro_crew.dashboard.chat_runner.spawn_guarded_turn",
            self._turn_spawner(done, consumed=False),
        ):
            assert await _start_next_queued_turn(state, slot) is True

        # What the AcpProcessDied branch does: replay the prompt, end the turn.
        slot.queue_insert(0, COMPLETION, kind=SYNTHETIC_RECOVERY_KIND)
        done.set_result(None)
        await asyncio.sleep(0.05)

        assert not (agent_root / "a1" / "tombstone.json").exists()

    @pytest.mark.asyncio
    async def test_a_second_empty_response_still_settles(self, agent_root, tmp_path):
        """The SECOND empty response re-queues a continuation, not the announce, so
        the completion has reached the model and the clock starts -- an emission-only
        predicate would strand this debt (no tokens were produced)."""
        from chat_test_helpers import _make_state

        state = _make_state(tmp_path / "state")
        state.subagents = _manager()
        slot = state.get_or_create_slot("drain-empty")
        _finished_run("a1", agent_root)
        slot.queue_append(COMPLETION, kind=SUBAGENT_COMPLETION_KIND)
        slot.note_pending_subagent_delivery(COMPLETION, ["a1"])
        done: asyncio.Future = asyncio.get_event_loop().create_future()
        hooks: list = []

        def _spawn(_state, _slot, coro):
            hooks.append(coro.cr_frame.f_locals.get("_on_consumed"))
            coro.close()

            async def _turn():
                await done

            return asyncio.get_event_loop().create_task(_turn())

        with patch("kiro_crew.dashboard.chat_runner.spawn_guarded_turn", _spawn):
            assert await _start_next_queued_turn(state, slot) is True

        # No tokens, no tool call -- only the provider's turn-complete event.
        hooks[0]()
        done.set_result(None)
        await _settled(lambda: (agent_root / "a1" / "tombstone.json").exists())

    @pytest.mark.asyncio
    async def test_a_first_empty_response_retracts_and_settles_nothing(self, agent_root, tmp_path):
        """The FIRST empty response re-queues this exact announce VERBATIM, so the
        delivery that counts has not happened yet. The turn reports consumption
        while streaming (it ends on a real end-of-turn) and then retracts it once
        the empty text is known -- settling here would put the result on a 1h clock
        that the replay has to beat."""
        from chat_test_helpers import _make_state

        state = _make_state(tmp_path / "state")
        state.subagents = _manager()
        slot = state.get_or_create_slot("drain-empty-first")
        _finished_run("a1", agent_root)
        slot.queue_append(COMPLETION, kind=SUBAGENT_COMPLETION_KIND)
        slot.note_pending_subagent_delivery(COMPLETION, ["a1"])
        done: asyncio.Future = asyncio.get_event_loop().create_future()
        hooks: list = []

        def _spawn(_state, _slot, coro):
            hooks.append(coro.cr_frame.f_locals.get("_on_consumed"))
            coro.close()

            async def _turn():
                await done

            return asyncio.get_event_loop().create_task(_turn())

        with patch("kiro_crew.dashboard.chat_runner.spawn_guarded_turn", _spawn):
            assert await _start_next_queued_turn(state, slot) is True

        hooks[0]()  # turn-complete on a real end-of-turn
        hooks[0](False)  # ... then the verbatim re-queue retracts it
        done.set_result(None)
        await asyncio.sleep(0.05)

        assert not (agent_root / "a1" / "tombstone.json").exists()
        assert slot._subagent_delivery_pending == {_key(COMPLETION): ["a1"]}

    @pytest.mark.asyncio
    async def test_a_replayed_completion_can_still_claim_its_debt(self, agent_root, tmp_path):
        """A pre-consumption failure re-queues the SAME announce under a freshly
        minted queue id. The debt is keyed on the announce, so the retry that
        finally delivers it can claim what the first attempt could not."""
        from chat_test_helpers import _make_state

        state = _make_state(tmp_path / "state")
        state.subagents = _manager()
        slot = state.get_or_create_slot("drain-replay")
        _finished_run("a1", agent_root)
        first_id = slot.queue_append(COMPLETION, kind=SUBAGENT_COMPLETION_KIND)
        slot.note_pending_subagent_delivery(COMPLETION, ["a1"])
        first_done: asyncio.Future = asyncio.get_event_loop().create_future()

        # Attempt 1: dies before the model consumed the prompt.
        with patch(
            "kiro_crew.dashboard.chat_runner.spawn_guarded_turn",
            self._turn_spawner(first_done, consumed=False),
        ):
            assert await _start_next_queued_turn(state, slot) is True
        first_done.set_result(None)
        await asyncio.sleep(0.05)
        assert not (agent_root / "a1" / "tombstone.json").exists()

        # The recovery replays the announce verbatim -- new id, same content.
        replay_id = slot.queue_insert(0, COMPLETION, kind=SYNTHETIC_RECOVERY_KIND)
        assert replay_id != first_id
        second_done: asyncio.Future = asyncio.get_event_loop().create_future()

        with patch(
            "kiro_crew.dashboard.chat_runner.spawn_guarded_turn",
            self._turn_spawner(second_done),
        ):
            assert await _start_next_queued_turn(state, slot) is True
        second_done.set_result(None)
        await _settled(lambda: (agent_root / "a1" / "tombstone.json").exists())
        assert slot._subagent_delivery_pending == {}

    @pytest.mark.asyncio
    async def test_a_pasted_announce_cannot_claim_the_debt(self, agent_root, tmp_path):
        """The ledger is content-keyed, so a user row whose TEXT is a copy of the
        announce would claim the genuine row's debt and start its clock early.
        Settlement is gated on the structural kind, which a typed row cannot carry."""
        from chat_test_helpers import _make_state

        state = _make_state(tmp_path / "state")
        state.subagents = _manager()
        slot = state.get_or_create_slot("spoof")
        _finished_run("a1", agent_root)
        # The genuine completion is queued BEHIND the user's copy of its text.
        slot._queue = [
            {"id": "u1", "content": COMPLETION, "kind": ""},
            {"id": "q1", "content": COMPLETION, "kind": SUBAGENT_COMPLETION_KIND},
        ]
        slot.note_pending_subagent_delivery(COMPLETION, ["a1"])
        done: asyncio.Future = asyncio.get_event_loop().create_future()

        with patch(
            "kiro_crew.dashboard.chat_runner.spawn_guarded_turn",
            self._turn_spawner(done),
        ):
            assert await _start_next_queued_turn(state, slot) is True
        done.set_result(None)
        await asyncio.sleep(0.05)

        # The spoof drained first and settled nothing; the debt is still owed to
        # the genuine row, which has not run yet.
        assert not (agent_root / "a1" / "tombstone.json").exists()
        assert slot._subagent_delivery_pending == {_key(COMPLETION): ["a1"]}

    @pytest.mark.asyncio
    async def test_drained_user_message_settles_nothing(self, agent_root, tmp_path):
        """Only a completion row owes a mark; a user turn draining past one must
        not start someone else's retention clock."""
        from chat_test_helpers import _make_state

        state = _make_state(tmp_path / "state")
        state.subagents = _manager()
        slot = state.get_or_create_slot("drain-user")
        _finished_run("a1", agent_root)
        slot.note_pending_subagent_delivery(_ann("other"), ["a1"])
        slot._queue = [{"id": "u1", "content": "carry on", "kind": ""}]
        done: asyncio.Future = asyncio.get_event_loop().create_future()

        with patch(
            "kiro_crew.dashboard.chat_runner.spawn_guarded_turn",
            self._turn_spawner(done),
        ):
            assert await _start_next_queued_turn(state, slot) is True

        done.set_result(None)
        await asyncio.sleep(0.05)

        assert not (agent_root / "a1" / "tombstone.json").exists()


# ── End to end: the reaper must not outrun the queue ──────────────────


class TestReaperDoesNotPruneAQueuedPromise:
    @pytest.mark.asyncio
    async def test_result_survives_a_queue_wait_longer_than_the_ttl(self, agent_root):
        """The reported failure, reduced: the completion is routed while the slot
        is busy, the parent's turn outlasts the TTL, and the reaper sweeps. The
        result the queued announce points at must still be there when the row
        finally drains — and only THEN start its retention window."""
        ttl = 3600
        slot = _ChatSlot("s1")
        info = _member()
        _finished_run(info.id, agent_root)

        # 1. Routed into a busy slot: queued, not delivered.
        slot.queue_append(COMPLETION, kind=SUBAGENT_COMPLETION_KIND)
        _defer(slot, info)

        # 2. The run loop honours the flag, so no clock is started.
        mgr = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
        mgr._fire_event = AsyncMock()
        mgr._on_done = AsyncMock()
        await mgr._report_terminal(
            info,
            source="test",
            injection_timeout_reason="x",
            mark_delivered_on_success=True,
        )

        # 3. The parent's turn runs long; the reaper sweeps well past the TTL.
        with patch("kiro_crew.subagent_persistence.time.time", return_value=time.time() + 4 * ttl):
            assert prune_stale_tombstones(7, ttl) == 0
        assert (agent_root / info.id / "result.txt").exists()

        # 4. The row finally drains and its turn runs: the promise is still
        #    honourable, and the retention window opens from there.
        await _manager().settle_queued_delivery(slot.take_pending_subagent_deliveries([COMPLETION]))
        assert (agent_root / info.id / "result.txt").exists()
        ts = json.loads((agent_root / info.id / "tombstone.json").read_text(encoding="utf-8"))
        assert ts["cause"] == "delivered" and ts["died"] >= time.time() - 60

        # 5. And it still bounds disk growth: one TTL after consumption, gone.
        with patch("kiro_crew.subagent_persistence.time.time", return_value=time.time() + 2 * ttl):
            assert prune_stale_tombstones(7, ttl) == 1
        assert not (agent_root / info.id).exists()

    @pytest.mark.asyncio
    async def test_gateway_queue_route_leaves_the_folder_readable(self, agent_root):
        """Same claim one layer up, through the real ``_subagent_done`` callback:
        a busy slot's completion is queued and its folder left un-tombstoned."""
        from test_subagent_scale import _mock_dashboard_state, _mock_sessions

        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = MagicMock()
        orch.ctx_builder.hooks = MagicMock()
        orch.dashboard_state = _mock_dashboard_state()
        slot = _ChatSlot("main")
        blocker = asyncio.create_task(asyncio.sleep(30))
        slot.task = blocker  # busy: an injection must wait behind this turn
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)

        with patch("kiro_crew.slack.gateway.SubagentManager") as mock_sm:
            mock_sm_inst = MagicMock()
            mock_sm_inst.start_reaper = MagicMock()
            mock_sm_inst.running_agents_for = MagicMock(return_value=[])
            mock_sm.return_value = mock_sm_inst
            with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False):
                orch._init_subagents()
            orch.subagent_mgr = mock_sm_inst
            on_done = mock_sm.call_args.kwargs["on_done"]

        info = _member()
        _finished_run(info.id, agent_root)
        try:
            with patch("kiro_crew.slack.gateway.INJECTION_TIMEOUT", 0.01):
                await on_done(info)
        finally:
            blocker.cancel()

        assert [q["kind"] for q in slot._queue] == [SUBAGENT_COMPLETION_KIND]
        assert slot._subagent_delivery_pending == {_key(slot._queue[0]["content"]): [info.id]}
        assert info._delivery_queued is True
        assert not (agent_root / info.id / "tombstone.json").exists()


def _make_orchestrator():
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.slack.gateway import GatewayOrchestrator

    cfg = KiroCrewConfig()
    with patch.object(cfg, "load_credentials", return_value={"KIROCREW_OWNER_ID": "U_OWNER"}):
        return GatewayOrchestrator(cfg, no_dashboard=False, no_crons=True, no_open=True)
