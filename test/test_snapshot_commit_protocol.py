"""Drive the cancellation protocol at its own suspension points.

The handler-level suites reach this module through a fake whose commit runs to
COMPLETION before it raises, so the drain never executes there and deleting it leaves
them green. These tests cancel a task that is genuinely suspended inside the protocol,
which is the only way the absorb-and-outlive branches run at all.
"""

import asyncio

import pytest

from kiro_crew.dashboard.snapshot_commit import (
    VocabularyDeleteCancellations,
    close_in_flight,
    commit_snapshot_while_holding_the_lock,
    drain_shielded,
    slots_with_a_close_in_flight,
    sweep_to_completion_despite_cancellation,
)


async def _cancel_once_suspended(task):
    """Let *task* reach its first await, then cancel it there."""
    for _ in range(10):
        await asyncio.sleep(0)
        if not task.done():
            break
    task.cancel()


class TestTheDrainOutlivesAnAlreadyCancelledCaller:
    @pytest.mark.asyncio
    async def test_a_finished_task_needs_no_await_at_all(self):
        loop = asyncio.get_running_loop()
        already = loop.create_future()
        already.set_result("wrote")

        assert await drain_shielded(already) is None, (
            "a task that is already done must be reported as having absorbed nothing, "
            "so a caller cannot be handed a cancellation that was never delivered"
        )

    @pytest.mark.asyncio
    async def test_a_task_that_fails_stops_the_drain_instead_of_spinning(self):
        async def _blows_up():
            await asyncio.sleep(0)
            raise RuntimeError("the worker died")

        task = asyncio.ensure_future(_blows_up())

        assert await drain_shielded(task) is None, (
            "a failed worker must end the drain, not be retried: the caller derives the "
            "outcome from task.exception() once"
        )
        assert isinstance(task.exception(), RuntimeError)

    @pytest.mark.asyncio
    async def test_a_cancellation_is_absorbed_and_the_worker_still_finishes(self):
        landed = []

        async def _worker():
            await asyncio.sleep(0.02)
            landed.append("wrote")
            return "wrote"

        worker = asyncio.ensure_future(_worker())
        absorbed = {}

        async def _drain():
            absorbed["value"] = await drain_shielded(worker)

        drainer = asyncio.ensure_future(_drain())
        await _cancel_once_suspended(drainer)
        # Absorbed, not re-raised: deriving the ending is the caller's, so the drain
        # returns the cancellation rather than propagating it.
        await drainer

        assert landed == ["wrote"], (
            "the drain returned before the shielded worker finished, so a cancelled "
            "handler can release its lock while the write is still in flight"
        )
        assert isinstance(absorbed["value"], asyncio.CancelledError), (
            "the cancellation delivered during the drain was discarded, so the caller "
            "cannot tell it was cancelled and never re-raises"
        )


class TestACancelledCommitStillPublishes:
    @pytest.mark.asyncio
    async def test_a_write_that_lands_after_the_cancellation_is_still_published(self):
        loop = asyncio.get_running_loop()
        write = loop.create_future()
        published = []

        async def _commit():
            await commit_snapshot_while_holding_the_lock(write, lambda: published.append("pub"))

        task = asyncio.ensure_future(_commit())
        await _cancel_once_suspended(task)
        loop.call_later(0.01, lambda: None if write.done() else write.set_result("wrote"))

        with pytest.raises(asyncio.CancelledError):
            await task

        assert published == ["pub"], (
            "the publication was dropped on the cancelled path, so memory and disk "
            "disagree after a write that actually landed"
        )

    @pytest.mark.asyncio
    async def test_a_failed_write_is_raised_instead_of_the_cancellation(self):
        loop = asyncio.get_running_loop()
        write = loop.create_future()
        published = []

        async def _commit():
            await commit_snapshot_while_holding_the_lock(write, lambda: published.append("pub"))

        task = asyncio.ensure_future(_commit())
        await _cancel_once_suspended(task)
        loop.call_later(
            0.01,
            lambda: None if write.done() else write.set_exception(RuntimeError("disk full")),
        )

        with pytest.raises(RuntimeError, match="disk full"):
            await task

        assert published == [], (
            "a write that never reached disk was published anyway, so memory now holds "
            "a value no load can reproduce"
        )


class TestTheSweepOutlivesTheHandler:
    @pytest.mark.asyncio
    async def test_a_cancelled_handler_still_finishes_its_sweep(self):
        swept = []

        async def _sweep():
            await asyncio.sleep(0.02)
            swept.append("swept")

        async def _run():
            await sweep_to_completion_despite_cancellation(_sweep())

        task = asyncio.ensure_future(_run())
        await _cancel_once_suspended(task)
        with pytest.raises(asyncio.CancelledError):
            await task

        assert swept == ["swept"], (
            "the sweep was abandoned when the handler was cancelled, leaving slot "
            "metadata naming a vocabulary row that is already gone"
        )

    @pytest.mark.asyncio
    async def test_a_sweep_failure_supersedes_the_cancellation(self):
        async def _sweep():
            await asyncio.sleep(0.01)
            raise RuntimeError("the sweep blew up")

        async def _run():
            await sweep_to_completion_despite_cancellation(_sweep())

        task = asyncio.ensure_future(_run())
        await _cancel_once_suspended(task)

        with pytest.raises(RuntimeError, match="the sweep blew up"):
            await task


class TestTheLedgerReRaisesInOrder:
    def test_the_first_sweep_cancellation_is_the_one_kept(self):
        ledger = VocabularyDeleteCancellations()
        first = asyncio.CancelledError()
        second = asyncio.CancelledError()

        with ledger.capturing_sweep():
            raise first
        with ledger.capturing_sweep():
            raise second

        assert ledger.sweep is first, (
            "a later sweep cancellation overwrote the earliest one, so the reported "
            "stopping point is not where the handler actually stopped"
        )

    def test_a_sweep_only_ledger_re_raises_the_sweep(self):
        ledger = VocabularyDeleteCancellations()
        ledger.sweep = asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            ledger.reraise_in_order()

    def test_the_commit_cancellation_outranks_the_sweep(self):
        ledger = VocabularyDeleteCancellations()

        class _CommitCancel(asyncio.CancelledError):
            pass

        ledger.commit = _CommitCancel()
        ledger.sweep = asyncio.CancelledError()

        with pytest.raises(_CommitCancel):
            ledger.reraise_in_order()

    def test_a_non_cancellation_from_a_sweep_reaches_the_caller(self):
        ledger = VocabularyDeleteCancellations()

        with pytest.raises(RuntimeError, match="not a cancellation"):
            with ledger.capturing_sweep():
                raise RuntimeError("not a cancellation")

        assert ledger.sweep is None


class TestTheCloseRegistrySpansThePop:
    class _State:
        """A weakref-able stand-in: the registry partitions on state identity only."""

    def test_a_slot_is_visible_for_the_span_and_gone_after(self):
        state = self._State()
        slot = object()

        assert slots_with_a_close_in_flight(state) == []
        with close_in_flight(state, "s-closing", slot):
            assert ("s-closing", slot) in slots_with_a_close_in_flight(state)
        assert slots_with_a_close_in_flight(state) == [], (
            "the registry kept the slot after its close returned, so a later delete "
            "sweeps an object no longer being closed"
        )

    def test_two_slots_can_hold_the_same_name_at_once(self):
        state = self._State()
        original = object()
        recreated = object()

        with close_in_flight(state, "s1", original):
            with close_in_flight(state, "s1", recreated):
                in_flight = slots_with_a_close_in_flight(state)

        assert len(in_flight) == 2, (
            "keying the registry by name dropped the original closing slot when a "
            "same-key recreate arrived, so its deleted id is never swept"
        )

    def test_the_slot_is_released_even_when_the_close_raises(self):
        state = self._State()
        slot = object()

        with pytest.raises(RuntimeError):
            with close_in_flight(state, "s-boom", slot):
                raise RuntimeError("close failed")

        assert slots_with_a_close_in_flight(state) == [], (
            "a close that raised left its slot published forever, so every later "
            "delete sweeps a stale object"
        )

    def test_one_state_never_sees_another_states_closing_slot(self):
        mine = self._State()
        theirs = self._State()
        their_slot = object()

        with close_in_flight(theirs, "s1", their_slot):
            visible_to_me = slots_with_a_close_in_flight(mine)

        assert visible_to_me == [], (
            "a process-global registry exposed another state's closing slot, so this "
            "state's delete sweeps and force-saves a slot it does not own"
        )
