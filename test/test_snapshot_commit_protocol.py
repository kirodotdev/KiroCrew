"""Drive the cancellation protocol at its own suspension points.

The handler-level suites reach this module through a fake whose commit runs to
COMPLETION before it raises, so the drain never executes there and deleting it leaves
them green. These tests cancel a task that is genuinely suspended inside the protocol,
which is the only way the absorb-and-outlive branches run at all.
"""

import asyncio
import logging
import threading
from pathlib import Path

import pytest

from kiro_crew.dashboard import chat_tags_store
from kiro_crew.dashboard.chat_utils import run_to_completion
from kiro_crew.dashboard.folder_repository import FolderRepository
from kiro_crew.dashboard.snapshot_commit import (
    VocabularyDeleteCancellations,
    close_in_flight,
    slots_with_a_close_in_flight,
)
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.testing.wait import await_until, until_parked


async def _cancel_once_suspended(task):
    """Let *task* reach its first await, then cancel it there."""
    for _ in range(10):
        await asyncio.sleep(0)
        if not task.done():
            break
    task.cancel()


async def _bounded(task, what):
    """Await an owned *task* under the lost-run bound, cancelling it if the bound fails."""
    try:
        return await await_until(task, what=what)
    finally:
        if not task.done():
            task.cancel()


def _gated_writer(gate, outcome):
    """A blocking writer for the worker thread: waits for *gate*, then lands or fails."""

    def _write(*_args):
        assert gate.wait(timeout=30), "the test never released the write"
        if isinstance(outcome, BaseException):
            raise outcome

    return _write


async def _cancel_then_release(task, gate):
    """Cancel *task* while its write is parked on the worker, then let the write finish."""
    await _cancel_once_suspended(task)
    gate.set()


class TestACancelledTagCommitStillPublishes:
    @pytest.mark.asyncio
    async def test_a_write_that_lands_after_the_cancellation_is_still_published(self, monkeypatch):
        gate = threading.Event()
        monkeypatch.setattr(chat_tags_store, "_write_tags_snapshot", _gated_writer(gate, None))
        published = []

        task = asyncio.ensure_future(
            chat_tags_store._commit_tags_snapshot(
                None, [], on_committed=lambda: published.append("pub")
            )
        )
        await _cancel_then_release(task, gate)

        with pytest.raises(asyncio.CancelledError):
            await _bounded(task, "the cancelled tag commit to unwind")

        assert published == ["pub"], (
            "the confirmation was dropped on the cancelled path, so a delete cannot tell "
            "a removal that landed from one that was only attempted"
        )

    @pytest.mark.asyncio
    async def test_a_failed_write_is_raised_instead_of_the_cancellation(self, monkeypatch):
        gate = threading.Event()
        monkeypatch.setattr(
            chat_tags_store,
            "_write_tags_snapshot",
            _gated_writer(gate, RuntimeError("disk full")),
        )
        published = []

        task = asyncio.ensure_future(
            chat_tags_store._commit_tags_snapshot(
                None, [], on_committed=lambda: published.append("pub")
            )
        )
        await _cancel_then_release(task, gate)

        with pytest.raises(RuntimeError, match="disk full"):
            await _bounded(task, "the failed tag commit to unwind")

        assert published == [], (
            "a write that never reached disk was confirmed anyway, so memory now holds "
            "a value no load can reproduce"
        )


class TestACancelledFolderCommitKeepsWhatLanded:
    async def _mutate(self, rows, gate, outcome, published):
        repo = FolderRepository(lambda: logging.getLogger(__name__))

        def _remove(live):
            live[:] = []
            return True, None

        return await repo.mutate(
            lambda: rows,
            LoopBoundLock(),
            _remove,
            lambda: Path("folders.json"),
            _gated_writer(gate, outcome),
            on_committed=lambda: published.append("pub"),
        )

    @pytest.mark.asyncio
    async def test_a_landed_write_is_confirmed_and_kept_after_a_cancellation(self):
        rows = [{"id": "f1"}]
        gate = threading.Event()
        published = []

        task = asyncio.ensure_future(self._mutate(rows, gate, None, published))
        await _cancel_then_release(task, gate)

        with pytest.raises(asyncio.CancelledError):
            await _bounded(task, "the cancelled folder commit to unwind")

        assert published == ["pub"], "a landed folder write was not confirmed"
        assert rows == [], "a landed folder write was rolled back in memory"

    @pytest.mark.asyncio
    async def test_a_failed_write_restores_the_rows_and_is_not_confirmed(self):
        rows = [{"id": "f1"}]
        gate = threading.Event()
        published = []

        task = asyncio.ensure_future(self._mutate(rows, gate, RuntimeError("disk full"), published))
        await _cancel_then_release(task, gate)

        with pytest.raises(RuntimeError, match="disk full"):
            await _bounded(task, "the failed folder commit to unwind")

        assert published == [], "a failed folder write was confirmed"
        assert rows == [{"id": "f1"}], "a failed folder write was kept in memory"


class TestTheSweepOutlivesTheHandler:
    @pytest.mark.asyncio
    async def test_a_cancelled_handler_still_finishes_its_sweep(self):
        swept = []
        release = asyncio.Event()

        async def _sweep():
            await release.wait()
            swept.append("swept")

        async def _run():
            await run_to_completion(_sweep())

        task = asyncio.ensure_future(_run())
        await until_parked(release)
        await _cancel_once_suspended(task)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await _bounded(task, "the cancelled sweep to unwind")

        assert swept == ["swept"], (
            "the sweep was abandoned when the handler was cancelled, leaving slot "
            "metadata naming a vocabulary row that is already gone"
        )

    @pytest.mark.asyncio
    async def test_a_sweep_failure_supersedes_the_cancellation(self):
        release = asyncio.Event()

        async def _sweep():
            await release.wait()
            raise RuntimeError("the sweep blew up")

        async def _run():
            await run_to_completion(_sweep())

        task = asyncio.ensure_future(_run())
        await until_parked(release)
        await _cancel_once_suspended(task)
        release.set()

        with pytest.raises(RuntimeError, match="the sweep blew up"):
            await _bounded(task, "the failed sweep to unwind")


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

    def test_an_overlapping_close_of_one_slot_outlives_the_first_to_exit(self):
        state = self._State()
        slot = object()

        with close_in_flight(state, "s1", slot):
            with close_in_flight(state, "s1", slot):
                pass
            still_in_flight = slots_with_a_close_in_flight(state)

        assert ("s1", slot) in still_in_flight, (
            "keying the registry per OBJECT let the inner close's exit unregister the "
            "outer one still running, so the slot is in no view a vocabulary delete "
            "can sweep and a failed restore puts back the deleted id"
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
