"""The per-cycle claim protocol: a claim is owed, not spent, until it settles.

The store already carries an unresolved claim across a restart and holds the record with
``stopped_reason == "interrupted_cycle"`` until a re-activation settles it. Nothing wrote
that claim, so the reader had no producer and a turn interrupted mid-delivery was simply
lost. These tests cover the writer:

* the claim reaches disk BEFORE the turn it claims, so a crash in the delivery window is
  recoverable in either direction -- charge a turn that did go out, release one that did not;
* a refused turn makes its release durable before re-arming, and a release that cannot be
  written keeps the claim and records it undelivered instead;
* the claim rides every store payload, so a concurrent write cannot erase it.
"""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import suppress

import pytest

from kiro_crew.autonudge import AutoNudgeService
from kiro_crew.testing.wait import async_wait_until, await_until, default_timeout, until_parked


class TestACycleIsClaimedBeforeTheTurnItClaims:
    """The claim is durable before the turn, and its release is durable too."""

    @pytest.mark.asyncio
    async def test_a_cycle_is_claimed_durably_before_delivery(self, tmp_path) -> None:
        """GPT 5.6 (BLOCKING): fire-then-record repeats a delivered action after restart.

        The load-time guard only covers a store already refused at startup. When the store
        goes unwritable AFTER arming, the timer still fired, ``_on_fire`` delivered, and the
        bookkeeping write only LOGGED its failure -- so the cycle was never recorded and the
        restart delivered the same action again. The claim is now durable and precedes
        delivery, so an unwritable store refuses to deliver instead.
        """
        (tmp_path / "autonudge.json").write_text(
            json.dumps({"version": 1, "loops": []}), encoding="utf-8"
        )
        fired: list[str] = []

        async def _on_fire(loop):
            fired.append(loop.id)
            return True

        svc = AutoNudgeService(base_dir=tmp_path, on_fire=_on_fire)
        try:
            svc._load()
            loop = await svc.add(slot_key="chat-1-1", message="go", idle_secs=300)
            before = loop.cycle_count

            # The store becomes unwritable AFTER arming -- the case the load guard misses.
            def _refuse(_payload):
                raise OSError("store went read-only after arming")

            svc._write_state = _refuse  # type: ignore[method-assign]

            # The current path raises out of the re-arm persist AFTER delivering; the harm
            # under test is what was delivered and counted, not how the write failed.
            with suppress(Exception):
                await svc._run_fire_cycle(loop)

            assert (
                fired == []
            ), f"delivered with the cycle unrecordable, so a restart repeats it: {fired!r}"
            assert (
                loop.cycle_count == before
            ), f"a cycle was consumed with nothing delivered: {before} -> {loop.cycle_count}"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_the_delivered_cycle_number_is_not_shifted_by_the_claim(self, tmp_path) -> None:
        """GPT 5.6 (BLOCKING): the pre-claim shifted every delivered cycle number.

        The fire callback renders the cycle as ``cycle_count + 1`` -- correct while
        ``cycle_count`` counts DELIVERED cycles. Claiming before delivery made it already
        include this turn, so every nudge announced a number one too high. The claim must be
        durable on DISK before the turn goes out while the in-memory count still reads the
        delivered total, which is the contract every consumer was written against.
        """
        store = tmp_path / "autonudge.json"
        store.write_text(json.dumps({"version": 1, "loops": []}), encoding="utf-8")
        seen: list[tuple[int, int]] = []

        async def _on_fire(loop):
            on_disk = json.loads(store.read_text(encoding="utf-8"))["loops"][0].get(
                "inflight_cycle"
            )
            seen.append((loop.cycle_count, on_disk))
            return True

        svc = AutoNudgeService(base_dir=tmp_path, on_fire=_on_fire)
        try:
            svc._load()
            loop = await svc.add(slot_key="chat-1-1", message="go", idle_secs=300)
            before = loop.cycle_count

            await svc._run_fire_cycle(loop)

            assert len(seen) == 1, "the turn was not delivered"
            in_memory, on_disk = seen[0]
            assert in_memory == before, (
                f"the callback saw {in_memory} instead of {before}, so it announced "
                f"cycle {in_memory + 1} for the {before + 1}th turn"
            )
            assert on_disk == before + 1, (
                f"the claim was not durable during delivery (disk={on_disk}), so a crash "
                "mid-turn would repeat it"
            )
            assert loop.cycle_count == before + 1, "the delivered cycle was not counted"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_crash_between_the_claim_and_delivery_does_not_spend_the_last_cycle(
        self, tmp_path
    ) -> None:
        """GPT 5.6 (BLOCKING, UPHOLD-FENCED): the claim consumed an undelivered cycle.

        Claiming by INCREMENTING the spent counter makes the durable record say "spent" for the
        whole delivery, which the code itself notes can take minutes. Die in that window and a
        capped loop reloads with its budget gone and its final action never run -- silently, with
        no recovery path. The claim has to be recorded SEPARATELY from the delivered total.
        """
        store = tmp_path / "autonudge.json"
        store.write_text(json.dumps({"version": 1, "loops": []}), encoding="utf-8")
        svc = AutoNudgeService(base_dir=tmp_path)

        async def _die_before_delivering(loop):
            # A process death, not a callback failure: BaseException is not caught by the
            # delivery guard, so nothing after the durable claim gets to run.
            raise KeyboardInterrupt("process exited mid-delivery")

        svc._on_fire = _die_before_delivering
        try:
            svc._load()
            loop = await svc.add(slot_key="chat-1-1", message="go", idle_secs=300)
            loop.max_cycles = 1  # one cycle of budget: the case Opus fenced
            await svc._persist_locked()

            with pytest.raises(KeyboardInterrupt):
                await svc._run_fire_cycle(loop)
        finally:
            svc.stop()

        # POSITIVE CONTROL: the claim really was written before the death, so this is not a
        # vacuous pass over a cycle that never started.
        row = json.loads(store.read_text(encoding="utf-8"))["loops"][0]
        assert (
            row.get("last_fire_ts") or 0.0
        ) > 0.0, "precondition: the claim persist never happened, so nothing was under test"

        # RESTART: a fresh service reads the same store, exactly as the next boot would.
        revived = AutoNudgeService(base_dir=tmp_path)
        try:
            revived._load()
            reloaded = revived._loops[loop.id]
            assert reloaded.cycle_count == 0, (
                f"the restart counts {reloaded.cycle_count} of {reloaded.max_cycles} cycles as "
                "spent although none was ever delivered, so the capped loop's only scheduled "
                "action never runs"
            )
            assert not (
                reloaded.max_cycles and reloaded.cycle_count >= reloaded.max_cycles
            ), "the cap reads as reached on a loop that delivered nothing"
        finally:
            revived.stop()

    @pytest.mark.asyncio
    async def test_a_retained_claim_keeps_the_claimed_timestamp_not_the_stale_one(
        self, tmp_path
    ) -> None:
        """GPT 5.6 (BLOCKING): retaining the claim restored its count but not its clock.

        The release first rolls memory back to the PRE-claim pair, then re-applies only the
        COUNT when the release write fails -- and it has already popped the overlay, so a
        later write persists that memory verbatim: the incremented count beside a timestamp
        from before the turn. Nothing between the retain arm and the end of the cycle puts
        the claimed clock back.
        """
        store = tmp_path / "autonudge.json"
        store.write_text(json.dumps({"version": 1, "loops": []}), encoding="utf-8")
        svc = AutoNudgeService(base_dir=tmp_path)

        async def _refuse(loop):
            return False

        svc._on_fire = _refuse
        try:
            svc._load()
            loop = await svc.add(slot_key="chat-1-1", message="go", idle_secs=300)
            before_count = loop.cycle_count
            before_ts = loop.last_fire_ts

            calls = {"n": 0}

            async def _fail_the_release_only(*, admission=None):
                calls["n"] += 1
                raise OSError("store is unwritable")

            svc._persist_locked = _fail_the_release_only
            svc._arm_timer = lambda lp, delay=None: None

            await svc._run_fire_cycle(loop)

            # POSITIVE CONTROL: the claim really was written, so this is not a vacuous pass.
            row = json.loads(store.read_text(encoding="utf-8"))["loops"][0]
            assert calls["n"] >= 2, f"the release write was never attempted ({calls['n']} calls)"
            assert (
                row.get("inflight_cycle") == before_count + 1
            ), f"precondition: disk should carry the claim, got {row.get('inflight_cycle')!r}"
            assert (
                row.get("last_fire_ts") or 0.0
            ) > before_ts, "precondition: disk should carry the CLAIMED clock"

            # The claim is RETAINED rather than released: the marker, not an inflated count,
            # is what a restart reads, and `cycle_count` stays at the delivered total.
            assert (
                svc._store.delivering_claim.get(loop.id, (None, None))[0] == before_count + 1
            ), f"the claim was not retained: {svc._store.delivering_claim.get(loop.id)!r}"
            assert loop.cycle_count == before_count, (
                f"the undelivered cycle was counted as spent ({loop.cycle_count}), which is the "
                "loss the two-phase claim exists to prevent"
            )
            assert loop.last_fire_ts > before_ts, (
                f"the retained claim kept the stale clock {loop.last_fire_ts} (pre-claim was "
                f"{before_ts}), so the next write persists a claim its own timestamp cannot "
                "account for"
            )
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_refused_turn_makes_its_rollback_durable_before_rearming(
        self, tmp_path
    ) -> None:
        """GPT 5.6 (BLOCKING): a refused cycle could stay durably counted.

        The rollback went out through the DETACHED best-effort persist, whose docstring
        promises only "a fresh full countdown" -- reasoning written for a deadline
        assignment, not for a cycle count. Shutdown cancels that task, so the claim
        survives on disk: the cycle is charged against ``max_cycles`` having delivered
        nothing. Read with no intervening await, which is what a cancelled task leaves.
        """
        store = tmp_path / "autonudge.json"
        store.write_text(json.dumps({"version": 1, "loops": []}), encoding="utf-8")
        svc = AutoNudgeService(base_dir=tmp_path)

        async def _refuse(loop):
            return False

        svc._on_fire = _refuse
        try:
            svc._load()
            loop = await svc.add(slot_key="chat-1-1", message="go", idle_secs=300)
            before = loop.cycle_count

            await svc._run_fire_cycle(loop)

            row = json.loads(store.read_text(encoding="utf-8"))["loops"][0]
            assert row.get("inflight_cycle") is None, (
                f"the refused turn left claim marker {row.get('inflight_cycle')!r} on disk, so a "
                "restart re-runs a cycle the release had already settled"
            )
            assert row["cycle_count"] == before, (
                f"the refused turn left {row['cycle_count']} durably counted instead of {before}, "
                "so a shutdown here spends a cycle that never delivered"
            )
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_an_unpersistable_rollback_keeps_the_claim_and_stops_retrying(
        self, tmp_path
    ) -> None:
        """GPT 5.6 (BLOCKING), second half: on failure, retain the claim and stop.

        If the store will not take the rollback, re-arming spends the remaining budget one
        refused turn at a time while every write fails the same way. Keeping the claim
        durable is the safe direction -- a spent cycle, never a repeated action.
        """
        store = tmp_path / "autonudge.json"
        store.write_text(json.dumps({"version": 1, "loops": []}), encoding="utf-8")
        svc = AutoNudgeService(base_dir=tmp_path)
        armed: list[float] = []

        async def _refuse(loop):
            return False

        svc._on_fire = _refuse
        try:
            svc._load()
            loop = await svc.add(slot_key="chat-1-1", message="go", idle_secs=300)
            before = loop.cycle_count

            calls = {"n": 0}

            async def _fail_after_the_claim():
                calls["n"] += 1
                raise OSError("store is unwritable")

            svc._persist_locked = _fail_after_the_claim
            svc._arm_timer = lambda lp, delay=None: armed.append(delay or 0.0)

            await svc._run_fire_cycle(loop)

            assert armed == [], f"re-armed {armed!r} against a store that refuses the rollback"
            assert svc._store.delivering_claim.get(loop.id, (None, None))[0] == before + 1, (
                f"the claim was dropped ({svc._store.delivering_claim.get(loop.id)!r}) while the disk "
                "marker could not be cleared, so the turn is set up to repeat"
            )
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_failed_release_persists_the_undelivered_qualifier_on_retry(
        self, tmp_path
    ) -> None:
        """GPT 5.6 (BLOCKING, upheld on adjudication): the qualifier was memory-only.

        A refused turn whose release write raised added ``_undelivered_claim`` and then
        returned without another write, so disk kept ``inflight_cycle`` with no
        ``inflight_undelivered`` beside it. ``_load`` therefore could not repopulate the
        qualifier, and re-activation charged ``max(cycle_count, owed)`` for a turn that
        never went out -- with ``max_cycles=1`` the only scheduled action never runs and
        the record reads as complete.
        """
        store = tmp_path / "autonudge.json"
        store.write_text(json.dumps({"version": 1, "loops": []}), encoding="utf-8")
        svc = AutoNudgeService(base_dir=tmp_path)

        async def _refuse(loop):
            return False

        svc._on_fire = _refuse
        try:
            svc._load()
            loop = await svc.add(slot_key="chat-1-1", message="go", idle_secs=300)
            before = loop.cycle_count

            calls = {"n": 0}
            real_persist = svc._persist_locked

            async def _fail_only_the_release(*, admission=None):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise OSError("store is unwritable")
                await real_persist(admission=admission)

            svc._persist_locked = _fail_only_the_release
            svc._arm_timer = lambda lp, delay=None: None

            await svc._run_fire_cycle(loop)

            row = json.loads(store.read_text(encoding="utf-8"))["loops"][0]
            assert row.get("inflight_undelivered") is True, (
                f"a failed release left no undelivered qualifier on disk ({row!r}), so "
                f"re-activation charges cycle {before + 1} for a turn that never went out"
            )
            assert row.get("inflight_cycle") == before + 1, (
                f"the qualifier landed without the claim it qualifies: "
                f"{row.get('inflight_cycle')!r}"
            )
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_write_during_delivery_keeps_claim_count_and_timestamp_together(
        self, tmp_path
    ) -> None:
        """GPT 5.6 (BLOCKING): the claim overlay carried the count but not its timestamp.

        The claimed ``last_fire_ts`` is taken at claim time and then discarded from memory for
        the delivery window, so a write landing mid-delivery persisted the claimed
        ``cycle_count`` beside the PRE-claim timestamp. A restart then reads a loop that has
        fired more cycles than its own clock accounts for.
        """
        store = tmp_path / "autonudge.json"
        store.write_text(json.dumps({"version": 1, "loops": []}), encoding="utf-8")
        svc = AutoNudgeService(base_dir=tmp_path)
        seen: list[tuple[int, float]] = []

        async def _on_fire(loop):
            await svc._persist_locked()
            row = json.loads(store.read_text(encoding="utf-8"))["loops"][0]
            seen.append((row.get("inflight_cycle"), row.get("last_fire_ts") or 0.0))
            return True

        svc._on_fire = _on_fire
        try:
            svc._load()
            loop = await svc.add(slot_key="chat-1-1", message="go", idle_secs=300)
            before_count = loop.cycle_count
            before_ts = loop.last_fire_ts

            await svc._run_fire_cycle(loop)

            count, ts = seen[0]
            assert count == before_count + 1, f"claimed count not persisted: {count}"
            assert ts > before_ts, (
                f"the claimed count {count} was persisted beside the pre-claim timestamp "
                f"{ts} (claim was after {before_ts}), so the two disagree on disk"
            )
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_concurrent_write_during_delivery_keeps_the_durable_claim(
        self, tmp_path
    ) -> None:
        """GPT 5.6 (BLOCKING): a mid-delivery write erased the durable cycle claim.

        The claim is persisted before the turn goes out, then rolled back IN MEMORY so the
        callback still renders the delivered number. Any other writer snapshotting during
        that window -- a ``monitor_update`` arriving on the nudge itself -- persisted the
        rolled-back count, so the restart repeated an action already delivered.
        """
        store = tmp_path / "autonudge.json"
        store.write_text(json.dumps({"version": 1, "loops": []}), encoding="utf-8")
        svc = AutoNudgeService(base_dir=tmp_path)
        during: list[int] = []

        async def _on_fire(loop):
            # Exactly what a concurrent update does: snapshot and write, mid-delivery. Read
            # back INSIDE the window -- a crash here is what strands the erased claim.
            await svc._persist_locked()
            during.append(
                json.loads(store.read_text(encoding="utf-8"))["loops"][0].get("inflight_cycle")
            )
            return True

        svc._on_fire = _on_fire
        try:
            svc._load()
            loop = await svc.add(slot_key="chat-1-1", message="go", idle_secs=300)
            before = loop.cycle_count

            await svc._run_fire_cycle(loop)

            assert during == [before + 1], (
                f"a write during delivery persisted {during!r} instead of {[before + 1]!r}, "
                "so a crash in that window re-delivers the turn that already went out"
            )
        finally:
            svc.stop()


class TestARestoredClaimIsSettledOnReactivation:
    """A restart-restored claim is settled once, so a healthy loop is never re-paused."""

    @staticmethod
    async def _interrupted_store(tmp_path, *, undelivered: bool) -> str:
        store = tmp_path / "autonudge.json"
        store.write_text(json.dumps({"version": 1, "loops": []}), encoding="utf-8")
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            loop = await svc.add(slot_key="chat-1-1", message="go", idle_secs=300)
        finally:
            svc.stop()
        data = json.loads(store.read_text(encoding="utf-8"))
        data["loops"][0]["inflight_cycle"] = data["loops"][0]["cycle_count"] + 1
        if undelivered:
            data["loops"][0]["inflight_undelivered"] = True
        store.write_text(json.dumps(data), encoding="utf-8")
        return loop.id

    @staticmethod
    def _row(tmp_path) -> dict:
        return json.loads((tmp_path / "autonudge.json").read_text(encoding="utf-8"))["loops"][0]

    @pytest.mark.asyncio
    async def test_resume_settles_the_claim_and_a_later_restart_keeps_the_loop_running(
        self, tmp_path
    ) -> None:
        loop_id = await self._interrupted_store(tmp_path, undelivered=False)

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            loaded = svc._loops[loop_id]
            assert (loaded.active, loaded.stopped_reason) == (False, "interrupted_cycle")

            resumed = await svc.update(loop_id, active=True)
            assert resumed is not None and resumed.cycle_count == 1
        finally:
            svc.stop()

        row = self._row(tmp_path)
        assert "inflight_cycle" not in row and "inflight_undelivered" not in row, row
        assert row["cycle_count"] == 1

        again = AutoNudgeService(base_dir=tmp_path)
        try:
            again._load()
            reloaded = again._loops[loop_id]
            assert (reloaded.active, reloaded.stopped_reason) == (True, "")
        finally:
            again.stop()

    @pytest.mark.asyncio
    async def test_an_undelivered_claim_is_released_at_load_and_the_loop_keeps_running(
        self, tmp_path
    ) -> None:
        loop_id = await self._interrupted_store(tmp_path, undelivered=True)

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            loaded = svc._loops[loop_id]
            assert (loaded.active, loaded.stopped_reason, loaded.cycle_count) == (True, "", 0)
            assert loop_id not in svc._store.unreconciled_claim
            await svc._persist_locked()
        finally:
            svc.stop()

        row = self._row(tmp_path)
        assert "inflight_cycle" not in row and "inflight_undelivered" not in row, row
        assert row["cycle_count"] == 0

    @pytest.mark.asyncio
    async def test_a_refused_fire_after_resume_does_not_restore_the_stale_claim(
        self, tmp_path
    ) -> None:
        loop_id = await self._interrupted_store(tmp_path, undelivered=True)

        async def _slot_busy(_loop):
            return False

        svc = AutoNudgeService(base_dir=tmp_path, on_fire=_slot_busy)
        try:
            svc._load()
            resumed = await svc.update(loop_id, active=True)
            assert resumed is not None
            await svc._run_fire_cycle(resumed)
        finally:
            svc.stop()

        assert "inflight_cycle" not in self._row(tmp_path)
        again = AutoNudgeService(base_dir=tmp_path)
        try:
            again._load()
            assert again._loops[loop_id].stopped_reason != "interrupted_cycle"
        finally:
            again.stop()

    @pytest.mark.asyncio
    async def test_a_fresh_claim_supersedes_a_restored_one_without_a_resume(self, tmp_path) -> None:
        loop_id = await self._interrupted_store(tmp_path, undelivered=False)

        async def _slot_busy(_loop):
            return False

        svc = AutoNudgeService(base_dir=tmp_path, on_fire=_slot_busy)
        try:
            svc._load()
            loop = svc._loops[loop_id]
            assert loop_id in svc._store.unreconciled_claim
            loop.active = True
            await svc._run_fire_cycle(loop)
            assert loop_id not in svc._store.unreconciled_claim
        finally:
            svc.stop()
        assert "inflight_cycle" not in self._row(tmp_path)


class TestACancelledClaimKeepsItsUndeliveredQualifier:
    """A shutdown during the claim write cannot let that write erase the qualifier."""

    @pytest.mark.asyncio
    async def test_the_claim_write_settles_before_the_compensation_write(
        self, tmp_path, monkeypatch
    ) -> None:
        store = tmp_path / "autonudge.json"
        store.write_text(json.dumps({"version": 1, "loops": []}), encoding="utf-8")
        fired: list[str] = []

        async def _on_fire(loop):
            fired.append(loop.id)
            return True

        svc = AutoNudgeService(base_dir=tmp_path, on_fire=_on_fire)
        try:
            svc._load()
            loop = await svc.add(slot_key="chat-1-1", message="go", idle_secs=300)
            real_write = svc._write_state
            started, release = threading.Event(), threading.Event()
            calls: list[int] = []

            def _slow_claim_write(payload):
                calls.append(len(calls))
                if len(calls) == 1:
                    started.set()
                    release.wait(default_timeout())
                real_write(payload)

            real_shield = asyncio.shield
            shields = {"n": 0}

            def _counting_shield(aw):
                shields["n"] += 1
                return real_shield(aw)

            monkeypatch.setattr(asyncio, "shield", _counting_shield)
            svc._write_state = _slow_claim_write  # type: ignore[method-assign]
            task = asyncio.create_task(svc._run_fire_cycle(loop))
            await async_wait_until(started.is_set, describe=lambda: "claim write not started")
            lock_taken: list[bool] = []

            async def _take_lock():
                async with svc._lock:
                    lock_taken.append(release.is_set())

            racer = asyncio.create_task(_take_lock())
            await until_parked(svc._lock)
            shields["n"] = 0
            task.cancel()
            await async_wait_until(
                lambda: lock_taken or shields["n"] >= 1,
                describe=lambda: f"shields={shields['n']} lock_taken={lock_taken}",
            )
            assert lock_taken == [], "the lock was released while the claim write was in flight"
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await await_until(task, what="the cancelled fire cycle")
            await await_until(racer, what="the lock racer")
        finally:
            svc.stop()

        row = json.loads(store.read_text(encoding="utf-8"))["loops"][0]
        assert fired == []
        assert lock_taken == [True]
        assert len(calls) == 2, calls
        assert row.get("inflight_cycle") == 1, row
        assert row.get("inflight_undelivered") is True, row

    @pytest.mark.asyncio
    async def test_a_cancelled_release_still_settles_the_refused_claim(self, tmp_path) -> None:
        store = tmp_path / "autonudge.json"
        store.write_text(json.dumps({"version": 1, "loops": []}), encoding="utf-8")
        holding, let_go = asyncio.Event(), asyncio.Event()
        svc = AutoNudgeService(base_dir=tmp_path)

        async def _hold_the_lock():
            async with svc._lock:
                holding.set()
                await let_go.wait()

        holders: list[asyncio.Task] = []

        async def _slot_busy(_loop):
            holders.append(asyncio.create_task(_hold_the_lock()))
            await await_until(holding, what="a concurrent writer holding the lock")
            return False

        svc._on_fire = _slot_busy
        try:
            svc._load()
            loop = await svc.add(slot_key="chat-1-1", message="go", idle_secs=300)
            task = asyncio.create_task(svc._run_fire_cycle(loop))
            await await_until(holding, what="the refused fire")
            await until_parked(svc._lock)
            task.cancel()
            let_go.set()
            with pytest.raises(asyncio.CancelledError):
                await await_until(task, what="the cancelled fire cycle")
            await await_until(holders[0], what="the lock holder")
        finally:
            svc.stop()

        row = json.loads(store.read_text(encoding="utf-8"))["loops"][0]
        assert row.get("inflight_cycle") is None, row
        assert row["cycle_count"] == 0, row

    @pytest.mark.asyncio
    async def test_a_refused_claim_settles_under_the_timer_lease_after_admission_closes(
        self, tmp_path
    ) -> None:
        store = tmp_path / "autonudge.json"
        store.write_text(json.dumps({"version": 1, "loops": []}), encoding="utf-8")
        holding, let_go = asyncio.Event(), asyncio.Event()
        svc = AutoNudgeService(base_dir=tmp_path)

        async def _hold_the_lock():
            async with svc._lock:
                holding.set()
                await let_go.wait()

        holders: list[asyncio.Task] = []

        async def _slot_busy(_loop):
            holders.append(asyncio.create_task(_hold_the_lock()))
            await await_until(holding, what="a concurrent writer holding the lock")
            return False

        async def _admitted_fire(loop):
            admission = svc._admit_mutation(allow_multiple_persistence=True)
            previous = svc._mutation_admission.get()
            svc._mutation_admission.set(admission)
            try:
                await svc._run_fire_cycle(loop)
            finally:
                svc._mutation_admission.set(previous)

        svc._on_fire = _slot_busy
        try:
            svc._load()
            loop = await svc.add(slot_key="chat-1-1", message="go", idle_secs=300)
            task = asyncio.create_task(_admitted_fire(loop))
            await await_until(holding, what="the refused fire")
            await until_parked(svc._lock)
            svc._accepting_mutations = False
            let_go.set()
            await await_until(task, what="the refused fire cycle")
            await await_until(holders[0], what="the lock holder")
        finally:
            svc.stop()

        row = json.loads(store.read_text(encoding="utf-8"))["loops"][0]
        assert row.get("inflight_cycle") is None, row
        assert row["cycle_count"] == 0, row

    @pytest.mark.asyncio
    async def test_a_fire_cancelled_while_waiting_for_the_lock_leaves_no_claim(
        self, tmp_path
    ) -> None:
        store = tmp_path / "autonudge.json"
        store.write_text(json.dumps({"version": 1, "loops": []}), encoding="utf-8")
        fired: list[str] = []

        async def _on_fire(loop):
            fired.append(loop.id)
            return True

        svc = AutoNudgeService(base_dir=tmp_path, on_fire=_on_fire)
        holding, let_go = asyncio.Event(), asyncio.Event()

        async def _hold_the_lock():
            async with svc._lock:
                holding.set()
                await let_go.wait()

        try:
            svc._load()
            loop = await svc.add(slot_key="chat-1-1", message="go", idle_secs=300)
            fire_ts_before = loop.last_fire_ts
            holder = asyncio.create_task(_hold_the_lock())
            await await_until(holding, what="a concurrent writer holding the lock")
            task = asyncio.create_task(svc._run_fire_cycle(loop))
            await until_parked(svc._lock)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await await_until(task, what="the fire cancelled at the lock")
            let_go.set()
            await await_until(holder, what="the lock holder")
            assert loop.id not in svc._store.delivering_claim
            assert loop.last_fire_ts == fire_ts_before
            await svc._persist_locked()
        finally:
            svc.stop()

        row = json.loads(store.read_text(encoding="utf-8"))["loops"][0]
        assert fired == []
        assert row.get("inflight_cycle") is None, row
        assert row["cycle_count"] == 0, row

    @pytest.mark.asyncio
    async def test_a_pause_queued_ahead_of_the_fire_at_the_lock_stops_delivery(
        self, tmp_path
    ) -> None:
        store = tmp_path / "autonudge.json"
        store.write_text(json.dumps({"version": 1, "loops": []}), encoding="utf-8")
        fired: list[str] = []

        async def _on_fire(loop):
            fired.append(loop.id)
            return True

        svc = AutoNudgeService(base_dir=tmp_path, on_fire=_on_fire)
        holding, let_go = asyncio.Event(), asyncio.Event()

        async def _hold_the_lock():
            async with svc._lock:
                holding.set()
                await let_go.wait()

        try:
            svc._load()
            loop = await svc.add(slot_key="chat-1-1", message="go", idle_secs=300)
            holder = asyncio.create_task(_hold_the_lock())
            await await_until(holding, what="a concurrent writer holding the lock")
            pause = asyncio.create_task(svc.update(loop.id, active=False))
            await until_parked(svc._lock)
            svc._firing.add(loop.id)
            fire = asyncio.create_task(svc._run_fire_cycle(loop))
            await until_parked(svc._lock, count=2)
            let_go.set()
            await await_until(holder, what="the lock holder")
            await await_until(pause, what="the pause")
            await await_until(fire, what="the fire behind the pause")
            svc._firing.discard(loop.id)
            assert loop.id not in svc._store.delivering_claim
            await svc._persist_locked()
        finally:
            svc.stop()

        row = json.loads(store.read_text(encoding="utf-8"))["loops"][0]
        assert fired == []
        assert row["active"] is False, row
        assert row.get("inflight_cycle") is None, row
        assert row["cycle_count"] == 0, row
