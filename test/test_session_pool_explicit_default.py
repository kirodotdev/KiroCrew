"""Tests for how a pending explicit effort Default reaches a session's cold start.

A Default picked for a chat stays durable in the session map until a cold start applies
its ``cli.json`` projection: the allocation skips the warm pool for it, reserves the key
from the basis read through registration, waits for a pick's in-flight save, and clears
the flag only once the projection ran. The warm-pool fence those starts rewrite inside
lives in ``test_session_pool.py``; the shared fixture and stand-ins in
``session_pool_helpers.py``.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import nullcontext
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from session_pool_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _isolate_config_dir,
    _make_manager,
    _make_provider,
)

from kiro_crew.testing.ids import UNALLOCATABLE_PID
from kiro_crew.testing.wait import async_wait_until, default_timeout


async def _await_test(awaitable, what: str):
    try:
        async with asyncio.timeout(default_timeout()) as deadline:
            return await awaitable
    except TimeoutError as exc:
        if not deadline.expired():
            raise
        raise AssertionError(f"timed out waiting for {what}") from exc


class TestExplicitEffortDefaultAllocation:
    """A pending explicit Default stays durable until a start applies its projection."""

    KEY = "test-key"

    @staticmethod
    def _cold_manager(applied: bool, start: AsyncMock | None = None):
        mgr, factory = _make_manager()
        mgr._drain_and_claim = AsyncMock(return_value=_make_provider())
        mgr._schedule_replenish = MagicMock()
        mgr._record_pool_decision = MagicMock()
        provider = _make_provider()
        provider.explicit_effort_default_applied = applied
        if start is not None:
            provider.start = start
        factory.side_effect = None
        factory.return_value = provider
        return mgr, factory, provider

    @pytest.mark.asyncio
    async def test_a_reservation_loser_waits_for_the_default_start_to_register(self):
        reserver_started = asyncio.Event()
        release_reserver = asyncio.Event()
        contender_checked_reservation = asyncio.Event()

        async def reserver_start() -> None:
            reserver_started.set()
            await _await_test(release_reserver.wait(), "reserver release")

        mgr, factory, reserver = self._cold_manager(
            applied=True, start=AsyncMock(side_effect=reserver_start)
        )
        contender = _make_provider()
        contender.explicit_effort_default_applied = False
        factory.side_effect = [reserver, contender]
        service = mgr._allocation_boundary()
        real_reserve = service._reserve_explicit_effort_default
        reserve_calls = 0

        def reserve(key: str, *args):
            nonlocal reserve_calls
            reserve_calls += 1
            result = real_reserve(key, *args)
            if reserve_calls == 2:
                contender_checked_reservation.set()
            return result

        service._reserve_explicit_effort_default = reserve
        mgr.set_explicit_effort_default(self.KEY, True)
        reserving = asyncio.create_task(mgr.get_or_create(self.KEY, agent="kirocrew"))
        contender_task: asyncio.Task | None = None
        try:
            await _await_test(reserver_started.wait(), "reserving start")
            contender_task = asyncio.create_task(mgr.get_or_create(self.KEY, agent="kirocrew"))
            await _await_test(contender_checked_reservation.wait(), "contender reservation check")

            assert contender.start.await_count == 0
            release_reserver.set()
            registered, _is_new, _resumed = await _await_test(reserving, "reserving allocation")
            assert registered is reserver
            mgr.release(self.KEY)
            reused, _is_new, _resumed = await _await_test(contender_task, "waiting allocation")

            assert reused is reserver
            assert factory.call_count == 1
            contender.arm_explicit_effort_default.assert_not_called()
            assert mgr.explicit_effort_default_pending(self.KEY) is False
        finally:
            release_reserver.set()
            participants = [reserving]
            if contender_task is not None:
                participants.append(contender_task)
            for participant in participants:
                if not participant.done():
                    participant.cancel()
            await _await_test(
                asyncio.gather(*participants, return_exceptions=True),
                "default allocation race cleanup",
            )

    @pytest.mark.asyncio
    async def test_a_waiter_reserves_and_arms_after_the_reserving_start_fails(self):
        reserver_started = asyncio.Event()
        release_reserver = asyncio.Event()
        contender_checked_reservation = asyncio.Event()

        async def failing_start() -> None:
            reserver_started.set()
            await _await_test(release_reserver.wait(), "reserver release")
            raise RuntimeError("spawn failed")

        mgr, factory, reserver = self._cold_manager(
            applied=False, start=AsyncMock(side_effect=failing_start)
        )
        contender = _make_provider()
        contender.explicit_effort_default_applied = True
        factory.side_effect = [reserver, contender]
        service = mgr._allocation_boundary()
        real_reserve = service._reserve_explicit_effort_default
        reserve_calls = 0

        def reserve(key: str, *args):
            nonlocal reserve_calls
            reserve_calls += 1
            result = real_reserve(key, *args)
            if reserve_calls == 2:
                contender_checked_reservation.set()
            return result

        service._reserve_explicit_effort_default = reserve
        mgr.set_explicit_effort_default(self.KEY, True)
        reserving = asyncio.create_task(mgr.get_or_create(self.KEY, agent="kirocrew"))
        contender_task: asyncio.Task | None = None
        try:
            await _await_test(reserver_started.wait(), "reserving start")
            contender_task = asyncio.create_task(mgr.get_or_create(self.KEY, agent="kirocrew"))
            await _await_test(contender_checked_reservation.wait(), "contender reservation check")

            assert contender.start.await_count == 0
            release_reserver.set()
            with pytest.raises(RuntimeError, match="spawn failed"):
                await _await_test(reserving, "failing allocation")
            created, _is_new, _resumed = await _await_test(contender_task, "replacement allocation")

            assert created is contender
            contender.arm_explicit_effort_default.assert_called_once_with(
                mgr.fence_effort_overlay_rewrite
            )
            assert factory.call_count == 2
            assert mgr.explicit_effort_default_pending(self.KEY) is False
        finally:
            release_reserver.set()
            participants = [reserving]
            if contender_task is not None:
                participants.append(contender_task)
            for participant in participants:
                if not participant.done():
                    participant.cancel()
            await _await_test(
                asyncio.gather(*participants, return_exceptions=True),
                "failed default allocation race cleanup",
            )

    @pytest.mark.asyncio
    async def test_cancelling_a_default_reservation_waiter_leaks_no_state(self):
        reserver_started = asyncio.Event()
        release_reserver = asyncio.Event()
        contender_checked_reservation = asyncio.Event()

        async def reserver_start() -> None:
            reserver_started.set()
            await _await_test(release_reserver.wait(), "reserver release")

        mgr, factory, reserver = self._cold_manager(
            applied=True, start=AsyncMock(side_effect=reserver_start)
        )
        contender = _make_provider()
        factory.side_effect = [reserver, contender]
        service = mgr._allocation_boundary()
        real_reserve = service._reserve_explicit_effort_default
        reserve_calls = 0

        def reserve(key: str, *args):
            nonlocal reserve_calls
            reserve_calls += 1
            result = real_reserve(key, *args)
            if reserve_calls == 2:
                contender_checked_reservation.set()
            return result

        async def await_waiter_released_allocation() -> None:
            while len(service._allocation_reservations.get(self.KEY, ())) != 1:
                await asyncio.sleep(0)

        service._reserve_explicit_effort_default = reserve
        mgr.set_explicit_effort_default(self.KEY, True)
        reserving = asyncio.create_task(mgr.get_or_create(self.KEY, agent="kirocrew"))
        contender_task: asyncio.Task | None = None
        try:
            await _await_test(reserver_started.wait(), "reserving start")
            contender_task = asyncio.create_task(mgr.get_or_create(self.KEY, agent="kirocrew"))
            await _await_test(contender_checked_reservation.wait(), "contender reservation check")
            await _await_test(await_waiter_released_allocation(), "waiter allocation release")

            contender_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await _await_test(contender_task, "cancelled waiter")
            assert contender.start.await_count == 0
            assert len(service.state.explicit_effort_default_reservations) == 1
            assert len(service._allocation_reservations[self.KEY]) == 1

            release_reserver.set()
            created, _is_new, _resumed = await _await_test(reserving, "reserving allocation")
            assert created is reserver
            assert service.state.explicit_effort_default_reservations == {}
            assert service._allocation_reservations == {}
        finally:
            release_reserver.set()
            participants = [reserving]
            if contender_task is not None:
                participants.append(contender_task)
            for participant in participants:
                if not participant.done():
                    participant.cancel()
            await _await_test(
                asyncio.gather(*participants, return_exceptions=True),
                "cancelled default allocation race cleanup",
            )

    @pytest.mark.asyncio
    async def test_a_pending_default_skips_the_pool_and_arms_the_provider(self):
        mgr, _factory, provider = self._cold_manager(applied=True)
        mgr.set_explicit_effort_default(self.KEY, True)

        created, _is_new, _ = await mgr.get_or_create(self.KEY, agent="kirocrew")

        # A warm process read cli.json at its own spawn and Default cannot be
        # pushed live, so only a cold start can apply the intent.
        assert created is provider
        mgr._drain_and_claim.assert_not_awaited()
        mgr._record_pool_decision.assert_called_once_with("bypass_effort", self.KEY)
        provider.arm_explicit_effort_default.assert_called_once_with(
            mgr.fence_effort_overlay_rewrite
        )
        assert mgr.explicit_effort_default_pending(self.KEY) is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("applied", [True, False])
    async def test_the_key_is_reserved_from_the_basis_read_through_registration(self, applied):
        # The span the effort handler's refusal rests on: the allocation
        # reservation is held before the start reads the one-shot Default and
        # is still held when the session is registered, so no pick can change
        # the basis a start publishes on. ``effort_basis_locked`` is that
        # reservation, read as the handler reads it.
        mgr, _factory, provider = self._cold_manager(applied=applied)
        mgr.set_explicit_effort_default(self.KEY, True)
        seen: list[tuple[str, bool]] = []
        real_pending = mgr.explicit_effort_default_pending

        def pending(key: str) -> bool:
            seen.append(("read", mgr.effort_basis_locked(key)))
            return real_pending(key)

        async def start() -> None:
            seen.append(("start", mgr.effort_basis_locked(self.KEY)))

        class Registry(dict[str, object]):
            def __setitem__(self, key: str, session: object) -> None:
                seen.append(("register", mgr.effort_basis_locked(key)))
                super().__setitem__(key, session)

        mgr.explicit_effort_default_pending = pending
        provider.start = AsyncMock(side_effect=start)
        mgr._allocation_boundary()._sessions = Registry()
        assert mgr.effort_basis_locked(self.KEY) is False

        created, _is_new, _ = await mgr.get_or_create(self.KEY, agent="kirocrew")

        assert created is provider
        assert seen[0] == ("read", True)
        assert ("start", True) in seen
        assert seen[-1] == ("register", True)
        assert all(locked for _step, locked in seen)
        # Released once the allocation returned: the next pick lands.
        assert mgr.effort_basis_locked(self.KEY) is False

    @pytest.mark.asyncio
    async def test_a_claim_waiting_on_the_live_session_leaves_the_basis_unlocked(self):
        # A second caller queued for the live session's lease holds the key's
        # reservation for as long as the running turn lasts, but it starts
        # nothing and reads no effort basis, so a Default pick made meanwhile
        # is not refused as if a session were starting.
        mgr, _factory, _provider = self._cold_manager(applied=False)
        created, _is_new, _ = await mgr.get_or_create(self.KEY, agent="kirocrew")

        waiter = asyncio.create_task(mgr.get_or_create(self.KEY, agent="kirocrew"))
        try:
            await async_wait_until(
                lambda: mgr._has_allocation_reservation(self.KEY),
                describe=lambda: "the second claimant never reserved the key",
            )
            assert mgr.effort_basis_locked(self.KEY) is False
        finally:
            mgr.release(self.KEY)
        reused, _is_new, _ = await _await_test(waiter, "the queued claim")
        assert reused is created
        mgr.release(self.KEY)
        assert mgr.effort_basis_locked(self.KEY) is False

    # -- a pick saving its flag when the start begins settles before the read --
    #
    # The handler's check passes before the start takes the key's reservation,
    # so the pick's in-memory write is already there when the start begins and
    # its save is still in flight. The start waits for that write to settle
    # before its single basis read, so it reads the saved value or the one a
    # failed save put back, never an in-memory value no save keeps.

    async def _a_default_pick_saving_as_the_start_begins(
        self,
        mgr,
        outcome: str,
        pick_key: str | None = None,
        start_key: str | None = None,
        alias_folds: list[tuple[str, str]] | None = None,
    ):
        """Race a Default pick's save against a key's cold start.

        The pick is the effort handler's write as ``record_default_intent``
        makes it: the ``effort_basis_locked`` check passes (no reservation yet),
        the flag is written in memory under the key's intent-write count, and
        the save runs until the start has reached its basis read. ``outcome``
        is what the save then does: "saved", "raised" or "cancelled". Returns
        the pick's task and the start's reads as ``(value, save_settled)``.
        """
        pick_key = self.KEY if pick_key is None else pick_key
        start_key = self.KEY if start_key is None else start_key
        save_started = asyncio.Event()
        save_release = asyncio.Event()
        start_at_read = asyncio.Event()
        save_settled = False
        flushes = 0

        async def aflush() -> None:
            nonlocal flushes, save_settled
            flushes += 1
            if flushes > 1:
                # The pick's put-back save and the start's post-application clear save.
                return
            save_started.set()
            await _await_test(save_release.wait(), "save_release")
            save_settled = True
            if outcome == "raised":
                raise OSError("disk full")
            if outcome == "cancelled":
                raise asyncio.CancelledError()

        real_wait = mgr.wait_for_effort_intent_writes
        real_pending = mgr.explicit_effort_default_pending
        reads: list[tuple[bool, bool]] = []

        async def wait_for_writes(key: str) -> None:
            # The start announces it is at its read; the wait right before it
            # is where it stops while the save is in flight.
            if alias_folds is not None:
                alias_folds.append((mgr._fold_key(pick_key), mgr._fold_key(start_key)))
            start_at_read.set()
            await real_wait(key)

        def pending(key: str) -> bool:
            # Announced here too, for a start that reaches the read without
            # waiting: the save is then released only after the read landed.
            start_at_read.set()
            value = real_pending(key)
            reads.append((value, save_settled))
            return value

        async def pick() -> bool:
            assert mgr.effort_basis_locked(pick_key) is False
            with mgr.effort_intent_write(pick_key):
                prior = real_pending(pick_key)
                assert mgr.set_explicit_effort_default(pick_key, True) is True
                try:
                    await mgr.aflush()
                except asyncio.CancelledError:
                    mgr.set_explicit_effort_default(pick_key, prior)
                    await mgr.aflush()
                    raise
                except Exception:
                    mgr.set_explicit_effort_default(pick_key, prior)
                    return False
            return True

        mgr.aflush = aflush
        mgr.wait_for_effort_intent_writes = wait_for_writes
        mgr.explicit_effort_default_pending = pending
        alias_seed = None
        if alias_folds is not None:
            alias_seed = object()
            mgr._allocation_boundary()._sessions[start_key] = alias_seed
        pick_task = asyncio.create_task(pick())
        start_task: asyncio.Task | None = None
        try:
            await _await_test(save_started.wait(), "default save starting")
            if alias_seed is not None:
                assert mgr._allocation_boundary()._sessions.pop(start_key) is alias_seed
            start_task = asyncio.create_task(mgr.get_or_create(start_key, agent="kirocrew"))
            await _await_test(start_at_read.wait(), "cold start reaching its basis read")
            save_release.set()
            created, _is_new, _ = await _await_test(start_task, "cold start completion")
        except BaseException:
            save_release.set()
            participants = [pick_task]
            if start_task is not None:
                participants.append(start_task)
            for participant in participants:
                if not participant.done():
                    participant.cancel()
            await _await_test(
                asyncio.gather(*participants, return_exceptions=True),
                "allocation-race cleanup",
            )
            raise
        return pick_task, created, reads

    @pytest.mark.asyncio
    async def test_a_default_whose_save_raises_under_a_cold_start_is_not_read_by_it(self):
        mgr, _factory, provider = self._cold_manager(applied=True)

        pick_task, created, reads = await self._a_default_pick_saving_as_the_start_begins(
            mgr, "raised"
        )

        # The pick is refused (the handler answers 503) and put the flag back;
        # the start read that restored value, after the save had settled, so it
        # took the warm pool as a start with no Default pending does and armed
        # nothing.
        assert await _await_test(pick_task, "pick_task") is False
        assert reads[0] == (False, True)
        assert created is not provider
        mgr._drain_and_claim.assert_awaited_once()
        provider.arm_explicit_effort_default.assert_not_called()
        assert mgr.explicit_effort_default_pending(self.KEY) is False
        assert self.KEY not in mgr._effort_intent_writes
        assert mgr.effort_basis_locked(self.KEY) is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("pick_key", "start_key"),
        [
            ("slack:1700000000.000100", "1700000000.000100"),
            ("1700000000.000100", "slack:1700000000.000100"),
        ],
    )
    async def test_a_start_under_an_alias_waits_for_a_failed_default_save(
        self, pick_key, start_key
    ):
        mgr, _factory, provider = self._cold_manager(applied=True)
        alias_folds: list[tuple[str, str]] = []

        pick_task, created, reads = await self._a_default_pick_saving_as_the_start_begins(
            mgr,
            "raised",
            pick_key=pick_key,
            start_key=start_key,
            alias_folds=alias_folds,
        )

        assert alias_folds[0][0] == alias_folds[0][1]
        assert await _await_test(pick_task, "pick_task") is False
        assert reads[0] == (False, True)
        assert created is not provider
        mgr._drain_and_claim.assert_awaited_once()
        provider.arm_explicit_effort_default.assert_not_called()
        assert mgr.explicit_effort_default_pending(start_key) is False
        assert "slack:1700000000.000100" not in mgr._effort_intent_writes

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("pick_key", "start_key"),
        [
            ("slack:1700000000.000100", "1700000000.000100"),
            ("1700000000.000100", "slack:1700000000.000100"),
        ],
    )
    async def test_a_cold_start_under_an_alias_waits_for_a_failed_default_save(
        self, pick_key, start_key
    ):
        mgr, _factory, provider = self._cold_manager(applied=True)

        pick_task, created, reads = await self._a_default_pick_saving_as_the_start_begins(
            mgr,
            "raised",
            pick_key=pick_key,
            start_key=start_key,
        )

        assert await _await_test(pick_task, "pick_task") is False
        assert reads[0] == (False, True)
        assert created is not provider
        mgr._drain_and_claim.assert_awaited_once()
        provider.arm_explicit_effort_default.assert_not_called()
        assert mgr.explicit_effort_default_pending(start_key) is False

    @pytest.mark.asyncio
    async def test_a_default_whose_save_lands_under_a_cold_start_is_applied_and_cleared(self):
        mgr, _factory, provider = self._cold_manager(applied=True)

        pick_task, created, reads = await self._a_default_pick_saving_as_the_start_begins(
            mgr, "saved"
        )

        # The start read the saved value once the save had settled, skipped the
        # pool for it, applied it, and cleared the durable flag afterward.
        assert await _await_test(pick_task, "pick_task") is True
        assert reads[0] == (True, True)
        assert created is provider
        mgr._drain_and_claim.assert_not_awaited()
        provider.arm_explicit_effort_default.assert_called_once_with(
            mgr.fence_effort_overlay_rewrite
        )
        assert mgr.explicit_effort_default_pending(self.KEY) is False
        assert self.KEY not in mgr._effort_intent_writes

    @pytest.mark.asyncio
    async def test_a_default_whose_save_is_cancelled_under_a_cold_start_is_not_read_by_it(self):
        mgr, _factory, provider = self._cold_manager(applied=True)

        pick_task, created, reads = await self._a_default_pick_saving_as_the_start_begins(
            mgr, "cancelled"
        )

        with pytest.raises(asyncio.CancelledError):
            await _await_test(pick_task, "pick_task")
        assert reads[0] == (False, True)
        assert created is not provider
        mgr._drain_and_claim.assert_awaited_once()
        provider.arm_explicit_effort_default.assert_not_called()
        assert mgr.explicit_effort_default_pending(self.KEY) is False
        assert self.KEY not in mgr._effort_intent_writes

    @pytest.mark.asyncio
    async def test_the_keys_write_count_is_gone_once_its_last_write_has_exited(self):
        # Nothing outlives a write: the entry is removed when the count reaches
        # zero, however the write ended, and a waiter wakes at that moment.
        mgr, _factory, _provider = self._cold_manager(applied=True)

        with mgr.effort_intent_write(self.KEY):
            assert mgr._effort_intent_writes[self.KEY].count == 1
        assert self.KEY not in mgr._effort_intent_writes

        with pytest.raises(OSError):
            with mgr.effort_intent_write(self.KEY):
                raise OSError("disk full")
        assert self.KEY not in mgr._effort_intent_writes

        with pytest.raises(asyncio.CancelledError):
            with mgr.effort_intent_write(self.KEY):
                raise asyncio.CancelledError()
        assert self.KEY not in mgr._effort_intent_writes

        # Two picks under one key: the entry stays until the last one exits,
        # and a waiter is released only then.
        waiting = asyncio.Event()

        async def waiter() -> None:
            waiting.set()
            await mgr.wait_for_effort_intent_writes(self.KEY)

        with mgr.effort_intent_write(self.KEY):
            with mgr.effort_intent_write(self.KEY):
                entry = mgr._effort_intent_writes[self.KEY]
                assert entry.count == 2
                waiter_task = asyncio.create_task(waiter())
                await _await_test(waiting.wait(), "waiting")
                assert not waiter_task.done()
            assert entry.count == 1
            assert not entry.settled.is_set()
            assert not waiter_task.done()
        assert entry.settled.is_set()
        await _await_test(waiter_task, "waiter_task")
        assert mgr._effort_intent_writes == {}

    @pytest.mark.asyncio
    async def test_with_no_write_in_flight_the_basis_read_is_not_delayed(self):
        mgr, _factory, provider = self._cold_manager(applied=True)
        mgr.set_explicit_effort_default(self.KEY, True)
        assert getattr(mgr, "_effort_intent_writes", {}) == {}

        # The wait completes on its first step: nothing to wait for, no
        # suspension, and no write was ever entered.
        wait = mgr.wait_for_effort_intent_writes(self.KEY)
        with pytest.raises(StopIteration):
            wait.send(None)

        created, _is_new, _ = await mgr.get_or_create(self.KEY, agent="kirocrew")

        assert created is provider
        provider.arm_explicit_effort_default.assert_called_once_with(
            mgr.fence_effort_overlay_rewrite
        )
        assert mgr.explicit_effort_default_pending(self.KEY) is False
        assert getattr(mgr, "_effort_intent_writes", {}) == {}

    @pytest.mark.asyncio
    async def test_a_start_that_did_not_apply_it_keeps_the_intent(self):
        mgr, _factory, _provider = self._cold_manager(applied=False)
        mgr.set_explicit_effort_default(self.KEY, True)

        await mgr.get_or_create(self.KEY, agent="kirocrew")

        # The projection could not rewrite the file (locked, a link, past its
        # ceiling), so the next cold start tries again.
        assert mgr.explicit_effort_default_pending(self.KEY) is True

    @pytest.mark.asyncio
    async def test_the_start_rewrites_inside_the_warm_pools_fence(self):
        inside: list[int] = []

        async def start() -> None:
            (fence,) = provider.arm_explicit_effort_default.call_args.args
            with fence():
                inside.append(mgr._pool.state.effort_overlay_rewrites)

        mgr, _factory, provider = self._cold_manager(
            applied=True, start=AsyncMock(side_effect=start)
        )
        mgr.set_explicit_effort_default(self.KEY, True)
        before = time.monotonic()

        await mgr.get_or_create(self.KEY, agent="kirocrew")

        # The fence the start rewrites inside is the pool's: claims are refused
        # while it is up, and runtimes queued before it came down are discarded.
        assert inside == [1]
        assert mgr._pool.state.effort_overlay_rewrites == 0
        assert mgr._pool.state.effort_overlay_epoch >= before

    @pytest.mark.asyncio
    @pytest.mark.parametrize("error", [RuntimeError("spawn failed"), asyncio.CancelledError()])
    @pytest.mark.parametrize("applied", [True, False])
    async def test_a_start_that_fails_arms_it_again_only_when_it_did_not_apply(
        self, applied, error
    ):
        mgr, _factory, _provider = self._cold_manager(
            applied=applied, start=AsyncMock(side_effect=error)
        )
        mgr.set_explicit_effort_default(self.KEY, True)

        with pytest.raises(type(error)):
            await mgr.get_or_create(self.KEY, agent="kirocrew")

        # A projection that ran is cleared in memory for the deferred save. A
        # failure before projection leaves the durable intent untouched.
        assert mgr.explicit_effort_default_pending(self.KEY) is (not applied)
        assert mgr._allocation_state.explicit_effort_default_reservations == {}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("start_fails", [False, True])
    async def test_a_default_revoked_before_provider_arming_stays_revoked(
        self, monkeypatch, start_fails
    ):
        from kiro_crew import session_allocation

        async def pre_spawn_identity(*_args, **_kwargs):
            # The reservation serializes competing starts but does not authorize
            # an overlay rewrite after the durable intent has been revoked.
            assert mgr.set_explicit_effort_default(self.KEY, False) is True
            return ""

        monkeypatch.setattr(session_allocation, "pre_spawn_identity", pre_spawn_identity)
        start = AsyncMock(side_effect=RuntimeError("spawn failed")) if start_fails else None
        mgr, _factory, provider = self._cold_manager(applied=False, start=start)
        assert mgr.set_explicit_effort_default(self.KEY, True) is True

        if start_fails:
            with pytest.raises(RuntimeError, match="spawn failed"):
                await mgr.get_or_create(self.KEY, agent="kirocrew")
        else:
            await mgr.get_or_create(self.KEY, agent="kirocrew")

        # The pending Default was the authorization to remove an entry Kiro
        # Crew did not write. Once another allocation clears it, this start arms
        # no replacement and a later failure does not restore that old intent.
        provider.arm_explicit_effort_default.assert_not_called()
        assert mgr.explicit_effort_default_pending(self.KEY) is False

    @pytest.mark.asyncio
    async def test_the_flag_stays_on_disk_until_the_projection_applies(self, tmp_path):
        from kiro_crew.session_map import SessionMap

        work_dir = tmp_path / "work"
        settings = work_dir / ".kiro" / "settings"
        settings.mkdir(parents=True)
        cli_json = settings / "cli.json"
        cli_json.write_text(
            '{"chat":{"modelDefaults":{"gpt-5.6":{"reasoning":{"effort":"max"}}}}}',
            encoding="utf-8",
        )
        start_entered = asyncio.Event()
        apply_projection = asyncio.Event()

        async def start() -> None:
            start_entered.set()
            await _await_test(apply_projection.wait(), "projection release")
            cli_json.write_text('{"chat":{"modelDefaults":{}}}', encoding="utf-8")
            provider.explicit_effort_default_applied = True

        mgr, factory = _make_manager()
        provider = _make_provider()
        provider.explicit_effort_default_applied = False
        provider.start = AsyncMock(side_effect=start)
        factory.side_effect = None
        factory.return_value = provider
        mgr._drain_and_claim = AsyncMock(return_value=None)
        mgr._schedule_replenish = MagicMock()
        mgr._record_pool_decision = MagicMock()
        mgr.set_explicit_effort_default(self.KEY, True)
        await mgr.aflush()

        allocation = asyncio.create_task(
            mgr.get_or_create(self.KEY, agent="kirocrew", cwd=str(work_dir))
        )
        await _await_test(start_entered.wait(), "provider start")

        # This fresh reader sees the file, not the manager's in-memory map. A
        # kill here must leave the next process enough intent to replay Default.
        assert SessionMap().get_flag(self.KEY, "explicit_effort_default") is True
        assert '"effort":"max"' in cli_json.read_text(encoding="utf-8")

        apply_projection.set()
        created, _is_new, _ = await _await_test(allocation, "allocation")
        assert created is provider
        assert SessionMap().get_flag(self.KEY, "explicit_effort_default") is False
        assert '"effort":"max"' not in cli_json.read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_a_kill_before_projection_leaves_the_next_manager_armed(self):
        from kiro_crew.session_map import SessionMap

        mgr, _factory, _provider = self._cold_manager(applied=False)
        mgr.set_explicit_effort_default(self.KEY, True)
        await mgr.aflush()
        allocation = mgr._allocation_boundary()
        with mgr.hold_explicit_effort_default(self.KEY):
            assert allocation._reserve_explicit_effort_default(self.KEY) is True

        # Simulate process death: no provider exception arm and no manager cleanup.
        del allocation
        del mgr
        assert SessionMap().get_flag(self.KEY, "explicit_effort_default") is True

        restarted, _factory, provider = self._cold_manager(applied=True)
        created, _is_new, _ = await restarted.get_or_create(self.KEY, agent="kirocrew")

        assert created is provider
        provider.arm_explicit_effort_default.assert_called_once_with(
            restarted.fence_effort_overlay_rewrite
        )
        assert restarted.explicit_effort_default_pending(self.KEY) is False

    @pytest.mark.asyncio
    async def test_a_cancelled_clear_keeps_the_applied_default_cleared_in_memory(self):
        mgr, _factory, provider = self._cold_manager(applied=True)
        mgr.set_explicit_effort_default(self.KEY, True)
        mgr.aflush = AsyncMock(side_effect=asyncio.CancelledError())

        with pytest.raises(asyncio.CancelledError):
            await mgr.get_or_create(self.KEY, agent="kirocrew")

        provider.start.assert_awaited_once()
        assert mgr.explicit_effort_default_pending(self.KEY) is False
        assert mgr._allocation_state.explicit_effort_default_reservations == {}

    @pytest.mark.asyncio
    async def test_the_clear_is_saved_only_after_the_projection_applies(self):
        mgr, _factory, provider = self._cold_manager(applied=True)
        mgr.set_explicit_effort_default(self.KEY, True)
        seen: list[tuple[bool, int, int]] = []

        async def aflush():
            seen.append(
                (
                    mgr.explicit_effort_default_pending(self.KEY),
                    provider.arm_explicit_effort_default.call_count,
                    provider.start.await_count,
                )
            )

        mgr.aflush = aflush

        await mgr.get_or_create(self.KEY, agent="kirocrew")

        assert seen == [(False, 1, 1)]
        provider.arm_explicit_effort_default.assert_called_once_with(
            mgr.fence_effort_overlay_rewrite
        )
        assert mgr.explicit_effort_default_pending(self.KEY) is False

    @pytest.mark.asyncio
    async def test_the_started_pid_is_shielded_while_the_clear_is_saved(self):
        mgr, _factory, provider = self._cold_manager(applied=True)
        provider.pid = UNALLOCATABLE_PID
        mgr.set_explicit_effort_default(self.KEY, True)
        shielded_during_save: list[bool] = []

        async def aflush():
            shielded_during_save.append(UNALLOCATABLE_PID in mgr._starting_pids)

        mgr.aflush = aflush

        await mgr.get_or_create(self.KEY, agent="kirocrew")

        assert shielded_during_save == [True]
        assert UNALLOCATABLE_PID not in mgr._starting_pids

    @pytest.mark.asyncio
    async def test_a_start_cancelled_during_the_clear_save_drops_its_pid_shield(self):
        mgr, _factory, provider = self._cold_manager(applied=True)
        provider.pid = UNALLOCATABLE_PID
        mgr.set_explicit_effort_default(self.KEY, True)
        mgr.aflush = AsyncMock(side_effect=asyncio.CancelledError())

        with pytest.raises(asyncio.CancelledError):
            await mgr.get_or_create(self.KEY, agent="kirocrew")

        assert UNALLOCATABLE_PID not in mgr._starting_pids

    @pytest.mark.asyncio
    async def test_a_clear_save_failure_does_not_fail_the_start(self, caplog):
        mgr, _factory, provider = self._cold_manager(applied=True)
        mgr.set_explicit_effort_default(self.KEY, True)
        real_aflush = mgr.aflush
        attempts = 0

        async def fail_once() -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("disk full")
            await real_aflush()

        mgr.aflush = fail_once

        created, _is_new, _ = await mgr.get_or_create(self.KEY, agent="kirocrew")

        assert created is provider
        assert self.KEY in mgr._sessions
        assert mgr.explicit_effort_default_pending(self.KEY) is False
        assert "the Default already ran" in caplog.text

        await mgr.aflush()
        from kiro_crew.session_map import SessionMap

        assert SessionMap().get_flag(self.KEY, "explicit_effort_default") is False

    @pytest.mark.asyncio
    async def test_two_allocations_of_one_canonical_key_only_one_arms(self):
        mgr, _factory, _provider = self._cold_manager(applied=False)
        service = mgr._allocation_boundary()
        slack_key = "slack:1700000000.000100"
        bare_key = "1700000000.000100"
        assert mgr.set_explicit_effort_default(slack_key, True) is True
        providers = [_make_provider(), _make_provider()]
        first_reserved = asyncio.Event()
        release_first = asyncio.Event()

        async def reserve(key: str, provider, wait: bool) -> bool:
            with mgr.hold_explicit_effort_default(key):
                reserved = service._reserve_explicit_effort_default(key)
                if reserved:
                    provider.arm_explicit_effort_default(mgr.fence_effort_overlay_rewrite)
                if wait:
                    first_reserved.set()
                    await _await_test(release_first.wait(), "first reservation release")
                if reserved:
                    service._release_explicit_effort_default(key)
                return reserved

        first = asyncio.create_task(reserve(slack_key, providers[0], True))
        try:
            await _await_test(first_reserved.wait(), "first reservation")
            second = asyncio.create_task(reserve(bare_key, providers[1], False))
            assert await _await_test(second, "second reservation") is False
        finally:
            release_first.set()
        assert await _await_test(first, "first reservation completion") is True

        assert providers[0].arm_explicit_effort_default.call_count == 1
        providers[1].arm_explicit_effort_default.assert_not_called()
        assert service.state.explicit_effort_default_reservations == {}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["unapplied", "raised"])
    async def test_another_chat_cannot_take_the_row_while_the_flag_is_armed(
        self, monkeypatch, path
    ):
        from kiro_crew import session_map as session_map_module

        monkeypatch.setattr(session_map_module, "EXPLICIT_EFFORT_DEFAULT_ROW_CAP", 1)
        other = "dashboard:another-chat"
        admitted = []

        def another_chat_picks_default():
            # Another chat's Default pick while this key's flag keeps the row counted.
            if not admitted:
                admitted.append(mgr.set_explicit_effort_default(other, True))

        async def start():
            another_chat_picks_default()
            if path == "raised":
                raise RuntimeError("spawn failed")

        mgr, _factory, _provider = self._cold_manager(
            applied=False, start=AsyncMock(side_effect=start)
        )
        assert mgr.set_explicit_effort_default(self.KEY, True) is True

        if path == "raised":
            with pytest.raises(RuntimeError, match="spawn failed"):
                await mgr.get_or_create(self.KEY, agent="kirocrew")
        else:
            await mgr.get_or_create(self.KEY, agent="kirocrew")

        # The armed flag keeps this key's row counted, so the other pick is
        # refused and the row bound remains intact.
        assert admitted == [False]
        assert mgr.explicit_effort_default_pending(self.KEY) is True
        assert mgr.explicit_effort_default_pending(other) is False

    @pytest.mark.asyncio
    async def test_the_row_is_free_again_once_an_applied_start_clears_the_flag(self, monkeypatch):
        from kiro_crew import session_map as session_map_module

        monkeypatch.setattr(session_map_module, "EXPLICIT_EFFORT_DEFAULT_ROW_CAP", 1)
        other = "dashboard:another-chat"
        during_start = []

        async def start():
            during_start.append(mgr.set_explicit_effort_default(other, True))

        mgr, _factory, _provider = self._cold_manager(
            applied=True, start=AsyncMock(side_effect=start)
        )
        assert mgr.set_explicit_effort_default(self.KEY, True) is True

        await mgr.get_or_create(self.KEY, agent="kirocrew")

        # Refused while the flag keeps this key's row counted; free once the
        # replacement runs and clears it.
        assert during_start == [False]
        assert mgr.explicit_effort_default_pending(self.KEY) is False
        assert mgr.set_explicit_effort_default(other, True) is True

    @pytest.mark.asyncio
    async def test_the_applied_clear_frees_the_row_before_the_session_is_registered(
        self, monkeypatch
    ):
        from kiro_crew import session_allocation
        from kiro_crew import session_map as session_map_module

        monkeypatch.setattr(session_map_module, "EXPLICIT_EFFORT_DEFAULT_ROW_CAP", 1)
        other = "dashboard:another-chat"
        after_start = []

        async def stamp_spawn_identity(*_args, **_kwargs):
            after_start.append(mgr.set_explicit_effort_default(other, True))

        monkeypatch.setattr(session_allocation, "stamp_spawn_identity", stamp_spawn_identity)
        mgr, _factory, _provider = self._cold_manager(applied=True)
        assert mgr.set_explicit_effort_default(self.KEY, True) is True

        await mgr.get_or_create(self.KEY, agent="kirocrew")

        # The applied clear frees the row before the allocation registers the session.
        assert after_start == [True]

    @pytest.mark.asyncio
    async def test_a_level_start_leaves_the_row_to_the_default_it_does_not_apply(self, monkeypatch):
        # A level start does not clear a Default it does not apply, so it never
        # writes the flag: the pending flag itself keeps the row counted, and
        # another chat's pick is refused during the start and after it.
        from kiro_crew import session_map as session_map_module

        monkeypatch.setattr(session_map_module, "EXPLICIT_EFFORT_DEFAULT_ROW_CAP", 1)
        other = "dashboard:another-chat"
        during_start = []
        writes_of_this_key = []

        async def start():
            during_start.append(mgr.set_explicit_effort_default(other, True))

        mgr, _factory, _provider = self._cold_manager(
            applied=True, start=AsyncMock(side_effect=start)
        )
        mgr._drain_and_claim = AsyncMock(return_value=None)
        assert mgr.set_explicit_effort_default(self.KEY, True) is True
        real_set_explicit_effort_default = mgr.set_explicit_effort_default

        def record_writes(key: str, pending: bool) -> bool:
            if mgr._fold_key(key) == mgr._fold_key(self.KEY):
                writes_of_this_key.append(pending)
            return real_set_explicit_effort_default(key, pending)

        mgr.set_explicit_effort_default = record_writes

        await mgr.get_or_create(self.KEY, agent="kirocrew", reasoning_effort_override="high")

        assert writes_of_this_key == []
        assert during_start == [False]
        assert mgr.explicit_effort_default_pending(self.KEY) is True
        assert mgr.explicit_effort_default_pending(other) is False
        assert mgr.set_explicit_effort_default(other, True) is False

    @pytest.mark.asyncio
    async def test_a_level_the_caller_starts_on_leaves_a_pending_default_in_place(self):
        # A start that does not apply the Default must not clear it: the level
        # the caller chose is not a newer effort action on the key (a level pick
        # through the effort handler, or a level session_set_model commits at
        # the turn start, clears the flag itself), so the Default is left for
        # the next start that takes no override.
        mgr, _factory, provider = self._cold_manager(applied=True)
        mgr._drain_and_claim = AsyncMock(return_value=None)
        mgr.set_explicit_effort_default(self.KEY, True)

        await mgr.get_or_create(self.KEY, agent="kirocrew", reasoning_effort_override="high")

        # A stale Default never authorizes replacing an entry for a level start,
        # and the level start leaves it pending.
        provider.arm_explicit_effort_default.assert_not_called()
        assert mgr.explicit_effort_default_pending(self.KEY) is True

    @pytest.mark.asyncio
    async def test_a_committed_session_set_model_level_spends_the_pending_default(
        self, tmp_path, monkeypatch
    ):
        from chat_test_helpers import _make_state

        from kiro_crew.dashboard import session_control as session_control_module
        from kiro_crew.dashboard.chat_utils import effective_session_key, slot_history_key

        mgr, _factory, provider = self._cold_manager(applied=True)
        mgr._drain_and_claim = AsyncMock(return_value=None)
        state = _make_state(tmp_path)
        caller = state.get_or_create_slot("chat-1")
        target = state.get_or_create_slot("chat-2")
        state.sessions = mgr
        monkeypatch.setattr(session_control_module, "session_control_enabled", lambda: True)
        key = effective_session_key(target)
        mgr.set_explicit_effort_default(key, True)
        settings = tmp_path / ".kiro" / "settings" / "cli.json"
        settings.parent.mkdir(parents=True)
        original = b'{"chat":{"modelDefaults":{"sonnet":{"output_config":{"effort":"max"}}}}}'
        settings.write_bytes(original)

        await session_control_module.set_model_target(
            state,
            caller_session_key=slot_history_key(caller),
            target="chat-2",
            reasoning_effort="high",
        )
        assert session_control_module.apply_pending_model_pick(state, target) is True
        assert mgr.explicit_effort_default_pending(key) is False

        created, _is_new, _ = await mgr.get_or_create(key, agent="kirocrew", cwd=str(tmp_path))

        assert created is provider
        provider.arm_explicit_effort_default.assert_not_called()
        assert settings.read_bytes() == original

    @pytest.mark.asyncio
    async def test_a_pending_default_survives_a_level_start_and_the_next_start_applies_it(
        self,
    ):
        """A start carrying an override never clears a Default it does not apply.

        The override is a level the caller chose: a slot's level read before the
        Default pick landed, an alias slot's own level, or a caller's pin. That
        start runs the level, arms nothing, and leaves the flag pending; the
        key's next start without an override skips the pool for it, arms the
        provider and clears it after the projection. Before the fix the level
        start cleared the flag at its basis read, so the next start found nothing
        to apply.
        """
        mgr, factory, level_provider = self._cold_manager(applied=True)
        mgr._drain_and_claim = AsyncMock(return_value=None)
        assert mgr.set_explicit_effort_default(self.KEY, True) is True

        created, _is_new, _ = await mgr.get_or_create(
            self.KEY, agent="kirocrew", reasoning_effort_override="high"
        )

        # The level start ran the caller's level and took the pool path (a miss
        # here), arming no Default and leaving the flag as it found it.
        assert created is level_provider
        assert factory.call_args.kwargs["reasoning_effort_override"] == "high"
        level_provider.arm_explicit_effort_default.assert_not_called()
        assert mgr.explicit_effort_default_pending(self.KEY) is True
        assert mgr._record_pool_decision.call_args_list == [call("miss_empty", self.KEY)]

        mgr.release(self.KEY)
        await mgr.reset(self.KEY)
        default_provider = _make_provider()
        default_provider.explicit_effort_default_applied = True
        factory.return_value = default_provider

        created, _is_new, _ = await mgr.get_or_create(self.KEY, agent="kirocrew")

        # The next start without an override applies the Default and clears it.
        assert created is default_provider
        assert "reasoning_effort_override" not in factory.call_args.kwargs
        default_provider.arm_explicit_effort_default.assert_called_once_with(
            mgr.fence_effort_overlay_rewrite
        )
        assert mgr.explicit_effort_default_pending(self.KEY) is False
        assert mgr._record_pool_decision.call_args_list[-1] == call("bypass_effort", self.KEY)
        mgr._drain_and_claim.assert_awaited_once()

    def test_a_provider_that_reads_no_overlay_has_nothing_to_arm(self):
        from kiro_crew.providers.base import LLMProvider

        # Declared on the base so the session layer calls and reads them without
        # a probe; a provider with no workspace overlay clears an applied Default.
        assert LLMProvider.arm_explicit_effort_default(object(), nullcontext) is None
        assert LLMProvider.explicit_effort_default_applied.fget(object()) is True

    @pytest.mark.asyncio
    async def test_without_a_pending_default_nothing_is_armed(self):
        mgr, _factory, provider = self._cold_manager(applied=False)
        mgr._drain_and_claim = AsyncMock(return_value=None)

        await mgr.get_or_create(self.KEY, agent="kirocrew")

        mgr._drain_and_claim.assert_awaited_once()
        provider.arm_explicit_effort_default.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_reset_keeps_the_intent_for_the_next_cold_start(self):
        # The handler records the intent and then resets the session; the reset
        # recycles the process and keeps the map entry the intent lives on.
        mgr, _factory, _provider = self._cold_manager(applied=True)
        await mgr.get_or_create(self.KEY, agent="kirocrew")
        mgr.set_explicit_effort_default(self.KEY, True)

        await mgr.reset(self.KEY)

        assert mgr.explicit_effort_default_pending(self.KEY) is True
