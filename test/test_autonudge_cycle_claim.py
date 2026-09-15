"""The per-cycle claim protocol: a claim is owed, not spent, until it settles.

Every test here exercises one production invariant:

* a claim reaches disk BEFORE the turn it claims, so an unresolved one cannot say
  whether the reader already received that turn -- it is owed, and the record is held
  inactive with ``stopped_reason == "interrupted_cycle"`` until re-activated;
* the claim rides every store payload through ``_serialized_loops``, so a concurrent
  write cannot erase it;
* settlement commits inside the lock via ``_persist_locked(before=...)``, and a write
  that never lands undoes the delivery rather than charging the reader for it.
"""

from __future__ import annotations

import json
from contextlib import suppress

import pytest

from kiro_crew.autonudge import AutoNudgeService
from kiro_crew.monitoring.models import MonitorState


class TestTheInterruptedCycleClaimIsOwedNotSpent:
    """A claim that loaded unresolved is held, and re-activation is what settles it.

    ``_load`` cannot know whether the claimed turn went out, so it refuses to treat the
    claim as spent: the record is deactivated with a reason the UI can render, and the
    marker is carried on disk so a second restart does not lose it either.
    """

    @pytest.mark.asyncio
    async def test_an_uncapped_loop_also_owes_the_interrupted_cycle(self, tmp_path) -> None:
        """A cap applied later is refused against ``cycle_count``, so it is never inert."""
        store = tmp_path / "autonudge.json"
        row = {
            "id": "uncapped1",
            "slot_key": "chat-1-1",
            "message": "go",
            "idle_secs": 300,
            "max_cycles": 0,
            "cycle_count": 4,
            "inflight_cycle": 5,
        }
        store.write_text(json.dumps({"version": 1, "loops": [row]}), encoding="utf-8")
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            loaded = svc._loops["uncapped1"]
            assert (
                loaded.max_cycles == 0
            ), f"precondition: fixture is not uncapped, max_cycles={loaded.max_cycles!r}"
            assert loaded.cycle_count == 4, (
                "an uncapped loop COMMITTED a cycle whose delivery was never confirmed, so a "
                "cap applied later is measured against a nudge that never went out"
            )
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_capped_loop_still_owes_the_interrupted_cycle(self, tmp_path) -> None:
        """The other arm: a cap DOES bill the cycle, so there the recovery must survive."""
        store = tmp_path / "autonudge.json"
        row = {
            "id": "capped1",
            "slot_key": "chat-1-1",
            "message": "go",
            "idle_secs": 300,
            "max_cycles": 9,
            "cycle_count": 4,
            "inflight_cycle": 5,
        }
        store.write_text(json.dumps({"version": 1, "loops": [row]}), encoding="utf-8")
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            loaded = svc._loops["capped1"]
            assert loaded.max_cycles == 9, "precondition: fixture is not capped"
            assert loaded.cycle_count == 4, (
                "a capped loop counted an undelivered cycle as spent, so the cap bills a nudge "
                "that never went out"
            )
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_held_claim_survives_the_rewrite_that_follows_the_restart(
        self, tmp_path
    ) -> None:
        """GPT 5.6 (BLOCKING): the held claim was in memory only, so a rewrite erased it.

        ``_serialized_loops`` re-emitted the marker from the delivering map alone, and the
        load-time hold does not populate that map -- so the next write dropped it and a
        second restart found nothing to hold.
        """
        store = tmp_path / "autonudge.json"
        row = {
            "id": "held1",
            "slot_key": "chat-1-1",
            "message": "go",
            "idle_secs": 300,
            "max_cycles": 9,
            "cycle_count": 4,
            "inflight_cycle": 5,
            "active": True,
        }
        store.write_text(json.dumps({"version": 1, "loops": [row]}), encoding="utf-8")

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            assert svc._unreconciled_claim.get("held1") == 5, "precondition: nothing was held"

            await svc._persist_locked()

            disk = json.loads(store.read_text(encoding="utf-8"))["loops"][0]
            assert (
                disk.get("inflight_cycle") == 5
            ), "the rewrite dropped the held claim, so the next restart replays the turn: " + repr(
                disk.get("inflight_cycle")
            )
            assert disk.get("cycle_count") == 4, "the held cycle was spent by the rewrite"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_boolean_claim_value_is_not_read_as_cycle_one(self, tmp_path) -> None:
        """GPT 5.6 (BLOCKING): ``isinstance(True, int)`` let a boolean read as cycle 1."""
        store = tmp_path / "autonudge.json"
        row = {
            "id": "boolclaim1",
            "slot_key": "chat-1-1",
            "message": "go",
            "idle_secs": 300,
            "max_cycles": 1,
            "cycle_count": 0,
            "inflight_cycle": True,
            "active": True,
        }
        store.write_text(json.dumps({"version": 1, "loops": [row]}), encoding="utf-8")

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            loaded = svc._loops["boolclaim1"]
            assert svc._unreconciled_claim == {}, (
                "a boolean was accepted as a cycle claim, so re-activation spends a phantom "
                "cycle: " + repr(svc._unreconciled_claim)
            )
            assert loaded.active is True, "a boolean claim stopped a loop that had nothing owed"
            assert loaded.cycle_count == 0, "a phantom cycle was billed against the cap"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_claim_that_does_not_follow_the_count_is_discarded(self, tmp_path) -> None:
        """GPT 5.6 (BLOCKING): an oversized claim jumped the count and spent the budget."""
        store = tmp_path / "autonudge.json"
        row = {
            "id": "oversize1",
            "slot_key": "chat-1-1",
            "message": "go",
            "idle_secs": 300,
            "max_cycles": 24,
            "cycle_count": 4,
            "inflight_cycle": 99,
            "active": True,
        }
        store.write_text(json.dumps({"version": 1, "loops": [row]}), encoding="utf-8")

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            loaded = svc._loops["oversize1"]
            assert svc._unreconciled_claim == {}, (
                "an out-of-range claim was accepted, so re-activation spends the budget "
                "without delivering: " + repr(svc._unreconciled_claim)
            )
            assert (
                loaded.cycle_count == 4
            ), f"the stored count was jumped by a corrupt claim: {loaded.cycle_count}"
            assert loaded.active is True, "a discarded claim still stopped the loop"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_failed_reactivation_write_keeps_the_unresolved_claim(self, tmp_path) -> None:
        """GPT 5.6 (BLOCKING): the rollback restored fields but not the popped claims.

        Reactivation pops both claim maps before persisting. The write-failure handler
        restored the dataclass fields and the two pending-wake sets, so a refused write
        left the claim gone from memory while the store still carried it -- and the next
        successful write then serialized without ``inflight_cycle``, erasing the durable
        marker and redelivering the interrupted turn.
        """
        store = tmp_path / "autonudge.json"
        row = {
            "id": "rollback1",
            "slot_key": "chat-1-1",
            "message": "go",
            "idle_secs": 300,
            "max_cycles": 9,
            "cycle_count": 4,
            "inflight_cycle": 5,
            "active": True,
        }
        store.write_text(json.dumps({"version": 1, "loops": [row]}), encoding="utf-8")

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            assert svc._unreconciled_claim.get("rollback1") == 5, "precondition: nothing was held"
            # The load-time hold populates only the unreconciled map, so the delivering
            # entry is seeded here to reach the second claim this same block drops.
            delivering = (5, 1.0)
            svc._delivering_claim["rollback1"] = delivering

            def _refuse(payload):
                raise OSError("disk refused the reactivation write")

            svc._write_state = _refuse  # type: ignore[method-assign]

            with pytest.raises(OSError):
                await svc.update("rollback1", active=True)

            assert svc._unreconciled_claim.get("rollback1") == 5, (
                "the refused write erased the held claim, so the next write omits "
                "inflight_cycle and the interrupted turn is redelivered: "
                + repr(svc._unreconciled_claim)
            )
            assert svc._delivering_claim.get("rollback1") == delivering, (
                "the refused write erased the delivering claim, so the marker is re-emitted "
                "from neither map: " + repr(svc._delivering_claim)
            )
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_settings_only_update_keeps_a_held_cycle_claim(self, tmp_path) -> None:
        """GPT 5.6 (BLOCKING): a settings-only edit on a held record dropped the claim.

        The pop ran before the active check, so editing an interval while reconciliation
        was still owed discarded the claim, and the write that followed serialized the
        row without ``inflight_cycle`` -- erasing the durable marker with no recovery.
        """
        store = tmp_path / "autonudge.json"
        row = {
            "id": "settings1",
            "slot_key": "chat-1-1",
            "message": "go",
            "idle_secs": 300,
            "max_cycles": 9,
            "cycle_count": 4,
            "inflight_cycle": 5,
            "active": True,
        }
        store.write_text(json.dumps({"version": 1, "loops": [row]}), encoding="utf-8")

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            assert svc._loops["settings1"].active is False, "precondition: the claim was not held"
            assert svc._unreconciled_claim.get("settings1") == 5, "precondition: nothing was held"

            await svc.update("settings1", idle_secs=600)

            assert svc._unreconciled_claim.get("settings1") == 5, (
                "a settings-only edit spent the held claim, so the interrupted cycle is no "
                "longer owed: " + repr(svc._unreconciled_claim)
            )
            disk = json.loads(store.read_text(encoding="utf-8"))["loops"][0]
            assert disk.get("inflight_cycle") == 5, (
                "the settings write erased the durable marker, so a later restart replays the "
                "turn: " + repr(disk.get("inflight_cycle"))
            )
            assert disk.get("cycle_count") == 4, "a settings edit billed the held cycle"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_reconciling_a_spent_cycle_discharges_the_terminal_debt(self, tmp_path) -> None:
        """GPT 5.6 (BLOCKING): the reconcile settled the cycle but left the terminal debt.

        Accepting the interrupted cycle as spent says the final turn reached the reader,
        yet a surviving ``terminal_pending`` still records that turn as owed -- so the
        next fire delivers a terminal turn that was already delivered once.
        """
        store = tmp_path / "autonudge.json"
        row = {
            "id": "debt1",
            "slot_key": "chat-1-1",
            "message": "go",
            "idle_secs": 300,
            "max_cycles": 9,
            "cycle_count": 4,
            "inflight_cycle": 5,
            "active": True,
        }
        store.write_text(json.dumps({"version": 1, "loops": [row]}), encoding="utf-8")

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            svc._load()
            held = svc._loops["debt1"]
            assert svc._unreconciled_claim.get("debt1") == 5, "precondition: nothing was held"
            held.monitor = MonitorState(
                kind="github_pull_request",
                target="owner/repo#123",
                objective="review_ready",
                created_ts=1_000.0,
            )
            # A GATED loop carries probe state while still delivering down the legacy
            # path, so it reaches the reconcile that a controller record is refused from.
            held.gate = True
            # The settlement write failed after the turn landed, so the debt outlived it
            # while ``terminal_delivered`` did not -- the load recovery needs both.
            held.monitor.terminal_pending = "success"

            await svc.update("debt1", active=True)

            assert held.monitor is not None, "the monitor was dropped by the update"
            assert held.monitor.terminal_pending == "", (
                "the reconcile spent the cycle but left the final turn owed, so it is "
                "delivered a second time: " + repr(held.monitor.terminal_pending)
            )
            assert svc._unreconciled_claim.get("debt1") is None, "the claim was not settled"
            assert held.cycle_count == 5, "the interrupted cycle was not accounted as spent"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_a_cancelled_persist_keeps_the_lock_until_the_write_lands(self, tmp_path) -> None:
        """GPT 5.6 (BLOCKING): cancellation freed the lock while the write was in flight.

        The executor thread cannot be interrupted, so the snapshot lands regardless. A
        cancelled caller that frees ``_lock`` lets a later writer land behind it, and the
        claim it just committed then reads as spent on the next reactivation.
        """
        import asyncio
        import threading
        import time as _time

        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            started = threading.Event()
            landed = threading.Event()

            def _slow_write(payload):
                started.set()
                _time.sleep(0.4)
                landed.set()

            svc._write_state = _slow_write  # type: ignore[method-assign]
            runner = asyncio.get_running_loop()

            task = asyncio.create_task(svc._persist_locked())
            assert await runner.run_in_executor(None, started.wait, 3.0), "the write never began"
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            assert svc._lock.locked(), (
                "the cancelled caller released the lock while the write was still in "
                "flight, so a later writer can land behind the committed claim"
            )
            assert await runner.run_in_executor(None, landed.wait, 3.0), "the write never landed"

            for _ in range(100):
                if not svc._lock.locked():
                    break
                await asyncio.sleep(0.02)
            assert not svc._lock.locked(), "the supervised persist never released the lock"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_an_unresolved_claim_is_held_inactive_instead_of_replaying_the_turn(
        self, tmp_path
    ) -> None:
        """GPT 5.6 (BLOCKING): a restart between the claim and its turn re-delivered it.

        The claim is durable BEFORE the turn, so an unresolved one cannot say whether the
        reader already received that turn. Arming it anyway sent a second copy.
        """
        import asyncio

        store = tmp_path / "autonudge.json"
        row = {
            "id": "interrupted1",
            "slot_key": "chat-1-1",
            "message": "go",
            "idle_secs": 300,
            "max_cycles": 9,
            "cycle_count": 4,
            "inflight_cycle": 5,
            "active": True,
            "next_due_ts": 1.0,
            "last_fire_ts": 1.0,
        }
        store.write_text(json.dumps({"version": 1, "loops": [row]}), encoding="utf-8")

        fired: list[str] = []

        async def on_fire(loop):
            fired.append(loop.id)
            return True

        svc = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
        try:
            await svc.start()
            await asyncio.sleep(0.4)

            assert (
                fired == []
            ), "the interrupted cycle was delivered a second time on restart: " + repr(fired)
            loaded = svc._loops["interrupted1"]
            assert (
                loaded.active is False
            ), "an unresolved claim was left armed, so the turn it claims runs again"
            assert loaded.cycle_count == 4, "the unresolved cycle was spent without a decision"

            await svc.update("interrupted1", active=True)

            assert (
                svc._loops["interrupted1"].cycle_count == 5
            ), "re-activation left the claim owed, so the next tick still repeats the turn"
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_the_interrupted_hold_is_announced_not_only_logged(self, tmp_path) -> None:
        """Design Review: the hold stopped an unattended watch with only a log line.

        ``_load`` decides the hold off the event loop and before the service is published,
        so nothing downstream learns the record went inactive until some later unrelated
        broadcast. The gateway installs its observer BEFORE ``start()`` for exactly this
        reason, so the announcement belongs on that channel.
        """
        store = tmp_path / "autonudge.json"
        row = {
            "id": "announce1",
            "slot_key": "chat-1-1",
            "message": "go",
            "idle_secs": 300,
            "max_cycles": 9,
            "cycle_count": 4,
            "inflight_cycle": 5,
            "active": True,
            "next_due_ts": 1.0,
            "last_fire_ts": 1.0,
        }
        store.write_text(json.dumps({"version": 1, "loops": [row]}), encoding="utf-8")

        seen: list[tuple[str, str, bool, str]] = []

        svc = AutoNudgeService(base_dir=tmp_path)
        svc.subscribe(
            lambda event, loop: seen.append(
                (event, loop.id, loop.active, loop.stopped_reason)
                if loop
                else (event, "", True, "")
            )
        )
        try:
            await svc.start()

            held = [entry for entry in seen if entry[1] == "announce1"]
            assert held, (
                "the interrupted-cycle hold reached no observer, so the loop stopped "
                "silently until a human noticed: " + repr(seen)
            )
            assert held[0][2] is False, f"the announcement did not carry the stop: {held[0]!r}"
            assert (
                held[0][3] == "interrupted_cycle"
            ), "the announcement did not carry the reason the UI renders: " + repr(held[0])
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_the_fire_now_refusal_names_the_remedy_for_an_owed_settlement(
        self, tmp_path
    ) -> None:
        """Design Review: the 409 named the debt but not the way out of it.

        A settlement that never landed is settled from the record on disk at the next
        load, so restarting is the remedy -- and the refusal is the one surface an
        operator reaches for when a watch will not fire.
        """
        svc = AutoNudgeService(base_dir=tmp_path)
        try:
            await svc.start()
            loop = await svc.add(slot_key="chat-1-1", message="go", idle_secs=300)
            svc._settlement_owed.add(loop.id)

            _armed, detail, status = await svc.fire_now(loop.id)

            assert status == 409, f"an owed settlement was armed by hand: {status!r}"
            assert "restart" in (detail or ""), (
                "the refusal states the debt without the remedy, so the operator is told "
                "only that it will not fire: " + repr(detail)
            )
        finally:
            svc.stop()

    """The claim is durable before the turn, and its rollback is durable too.

    Ordering is the whole design: the write that records the claim lands first, so a
    crash in the delivery window is recoverable in either direction -- settle a turn
    that did go out, release a claim for one that did not.
    """

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
            real_persist = svc._persist_locked

            async def _fail_the_release_only():
                calls["n"] += 1
                if calls["n"] == 1:  # the CLAIM itself must still land durably
                    await real_persist()
                    return
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
                svc._delivering_claim.get(loop.id, (None, None))[0] == before_count + 1
            ), f"the claim was not retained: {svc._delivering_claim.get(loop.id)!r}"
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
            real_persist = svc._persist_locked

            async def _fail_after_the_claim():
                calls["n"] += 1
                if calls["n"] == 1:  # the claim itself must still land
                    await real_persist()
                    return
                raise OSError("store is unwritable")

            svc._persist_locked = _fail_after_the_claim
            svc._arm_timer = lambda lp, delay=None: armed.append(delay or 0.0)

            await svc._run_fire_cycle(loop)

            assert armed == [], f"re-armed {armed!r} against a store that refuses the rollback"
            assert svc._delivering_claim.get(loop.id, (None, None))[0] == before + 1, (
                f"the claim was dropped ({svc._delivering_claim.get(loop.id)!r}) while the disk "
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

            async def _fail_only_the_release():
                calls["n"] += 1
                if calls["n"] == 2:
                    raise OSError("store is unwritable")
                await real_persist()

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
