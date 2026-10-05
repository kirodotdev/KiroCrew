"""A spawn queued for memory waits at most ``agent.subagent_queue_max_wait_secs``.

Owner decision A1: a memory wait is finite. A row the memory floor keeps deferring
past the bound ends in ONE delivered terminal,
``never started: waiting for memory``, and leaves its parent's queued count at 0.
The bound is read live (no restart) and ``0`` turns it off.

Every test drives the real ``SubagentManager`` pump against a real task store on
a virtual store clock (``overload_fakes.Clock``), so "thirty minutes later" is one
``advance`` and nothing sleeps for it. The admit wait is the production 30 s on
that clock, so each deferral parks a row for 30 virtual seconds and the time a
row spends parked is what the bound measures; the pump's own wall-clock wake-ups
(``call_later(30)``) never fire inside a test, so only the passes a test drives
run.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from _hot_reload_helpers import change
from overload_fakes import Clock, mock_ctx, mock_sessions

import kiro_crew.subagent as subagent_mod
from kiro_crew import taskq
from kiro_crew.config import live
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.paths import data_home
from kiro_crew.config.schema import requires_restart
from kiro_crew.subagent import SubagentManager
from kiro_crew.subagent_manager.admission import SpawnAdmissionCoordinator, taskq_bridge
from kiro_crew.subagent_wait_reasons import QUEUED_WAIT_EXPIRED_TEXT

pytestmark = pytest.mark.usefixtures("healthy_host_memory")

_KEY = "agent.subagent_queue_max_wait_secs"
_PARENT = "dash:maxwait"
_ADMIT = 30.0


def _cfg(bound: int) -> KiroCrewConfig:
    cfg = KiroCrewConfig()
    cfg.agent.subagent_cost_gb = 0.5
    cfg.agent.spawn_min_memory_gb = 4.0
    cfg.agent.subagent_queue_max_wait_secs = bound
    return cfg


class _Harness:
    """A real manager with a real store, its memory short until a test says otherwise."""

    def __init__(
        self,
        mgr: SubagentManager,
        clock: Clock,
        free: dict[str, float],
        cfgs: dict[str, KiroCrewConfig],
    ) -> None:
        self.mgr = mgr
        self.clock = clock
        self.free = free
        self.cfgs = cfgs
        self.started = asyncio.Event()
        self.queued: list[dict[str, Any]] = []
        self.done: list[tuple[str, dict[str, Any]]] = []
        self.delivered: list[Any] = []

    async def on_event(self, etype: str, info: Any, extra: dict[str, Any]) -> None:
        if etype == "subagent_queued" and info.parent_session_key == _PARENT:
            self.queued.append(dict(extra))
        elif etype == "subagent_done":
            self.done.append((info.id, dict(extra)))

    async def on_done(self, info: Any) -> None:
        self.delivered.append(info)

    def state(self, agent_id: str) -> str:
        rec = self.mgr._taskq.get(agent_id)
        assert rec is not None
        return rec.state

    async def settle(self) -> None:
        """Every posted store write and every scheduled emit has landed."""
        for _ in range(3):
            await self.mgr._taskq.run(lambda: None)
            await asyncio.sleep(0.02)

    async def pump(self, passes: int = 3) -> None:
        """Run whole pump passes, each one awaited to its end."""
        for _ in range(passes):
            self.mgr._drain_queue()
            task = getattr(self.mgr, "_drain_task", None)
            if task is not None:
                await asyncio.wait_for(asyncio.shield(task), 5)
            await self.settle()

    async def spawn(self, task: str = "work", parent: str = _PARENT, **kwargs: Any) -> Any:
        info = await self.mgr.spawn_async(task, parent_session_key=parent, **kwargs)
        assert info is not None and info.queued is True and not info.done, info
        for _ in range(100):
            await self.settle()
            if await self.mgr._taskq.run(self.mgr._taskq.latest_events, [info.id], ["deferred"]):
                break
        else:
            raise AssertionError(f"{info.id} was never deferred")
        return info


def _patch_host(monkeypatch) -> tuple[dict[str, KiroCrewConfig], dict[str, float]]:
    """The config, the short host and the off-loop store a test manager is built on."""
    cfgs = {"cfg": _cfg(1800)}
    monkeypatch.setattr(KiroCrewConfig, "load", lambda: cfgs["cfg"])
    monkeypatch.setattr(subagent_mod, "Stats", MagicMock())
    monkeypatch.setattr(subagent_mod, "sel", MagicMock())
    monkeypatch.setattr(SpawnAdmissionCoordinator, "open_store_off_loop", True)
    monkeypatch.setattr(SpawnAdmissionCoordinator, "pump_off_loop", True)
    free = {"gb": 1.0}

    def memory_check(*, min_gb, **_kw):
        return free["gb"] >= min_gb, free["gb"]

    monkeypatch.setattr(subagent_mod, "check_memory_available", memory_check)
    return cfgs, free


@contextlib.asynccontextmanager
async def _harness(monkeypatch) -> AsyncIterator[_Harness]:
    cfgs, free = _patch_host(monkeypatch)
    mgr = SubagentManager(sessions=mock_sessions(), ctx_builder=mock_ctx(), max_concurrent=3)
    await asyncio.wait_for(mgr.wait_taskq_ready(), 5)
    clock = Clock(1000.0)
    mgr._taskq._clock = clock
    mgr._spawn_stagger_secs = 0.0
    mgr._taskq_admit_wait_secs = _ADMIT
    h = _Harness(mgr, clock, free, cfgs)
    mgr._on_event = h.on_event
    mgr._on_done = h.on_done

    async def worker(info) -> None:
        h.started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(mgr, "_run", AsyncMock(side_effect=worker))
    try:
        yield h
    finally:
        mgr._shutting_down = True
        tasks = [task for task in mgr._tasks.values() if not task.done()]
        tasks += [task for task in mgr._report_tasks if not task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
        drain = getattr(mgr, "_drain_task", None)
        if drain is not None and not drain.done():
            drain.cancel()
            await asyncio.gather(drain, return_exceptions=True)
        mgr._taskq.close()


async def _reload_bound(h: _Harness, bound: int) -> None:
    """One reload through the live watcher's own prefix filter: no restart."""
    new = _cfg(bound)
    h.cfgs["cfg"] = new
    live.watch().prime(new)
    await live.watch()._dispatch(change(new, _KEY, old=_cfg(1800)))


def _expired_reports(h: _Harness, agent_id: str) -> list[dict[str, Any]]:
    return [extra for aid, extra in h.done if aid == agent_id]


async def _recheck(h: _Harness, times: int = 1) -> None:
    """Let each parked deferral lapse and run the passes that re-check it.

    A pass picks one row, so a pass per waiting row (the pump's own follow-up
    pass, one stagger later, is what does this in production)."""
    for _ in range(times):
        h.clock.advance(_ADMIT)
        await h.pump()


class TestTheBoundEndsTheWait:
    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_expiry_delivers_one_terminal_and_the_depth_goes_to_zero(
        self, monkeypatch, caplog
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="kiro_crew.subagent_manager.admission")
        async with _harness(monkeypatch) as h:
            await _reload_bound(h, 90)
            info = await h.spawn()
            # Re-checks that still find the host short: the row is counted, and
            # inside the bound it keeps waiting however often it is re-checked.
            await _recheck(h, 2)
            assert h.queued[-1]["queued"] == 1, h.queued
            h.clock.advance(_ADMIT - 1)
            await h.pump()
            assert h.state(info.id) == taskq.QUEUED
            assert h.delivered == []
            # 90 s parked: the pass that re-parks it ends it.
            h.clock.advance(1)
            await h.pump(1)
            assert h.state(info.id) == taskq.FAILED
            assert [d.id for d in h.delivered] == [info.id]
            assert h.delivered[0].error == QUEUED_WAIT_EXPIRED_TEXT
            assert h.delivered[0].outcome == "failed"
            reports = _expired_reports(h, info.id)
            assert len(reports) == 1 and reports[0]["error"] == QUEUED_WAIT_EXPIRED_TEXT
            assert h.queued[-1] == {"queued": 0}
            assert await h.mgr.queued_count_for_async(_PARENT) == 0
            # The sweep wrote the terminal before the report's own settle, which
            # then finds the row already failed: nothing was lost, nothing warns.
            assert not [r for r in caplog.records if "did not commit" in r.getMessage()]
            # The terminal is the row's last word: more passes, and memory coming
            # back, deliver nothing more and start nothing.
            h.free["gb"] = 32.0
            for _ in range(3):
                h.clock.advance(120)
                await h.pump()
            assert len(h.delivered) == 1 and len(_expired_reports(h, info.id)) == 1
            assert not h.started.is_set()

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_a_row_that_waits_for_a_slot_is_not_a_memory_wait(self, monkeypatch) -> None:
        async with _harness(monkeypatch) as h:
            await _reload_bound(h, 60)
            info = await h.spawn()
            # Memory recovers but every slot is taken: the row's wait is now the
            # capacity queue, which this bound does not cover.
            h.free["gb"] = 32.0
            h.mgr._running_count = h.mgr._max_concurrent
            try:
                h.clock.advance(600)
                await h.pump()
                assert h.state(info.id) == taskq.QUEUED
                assert h.delivered == []
            finally:
                h.mgr._running_count = 0
            # A slot frees while memory is short again: the row is parked once
            # more, but the 600 s it queued for a slot are not memory wait, so
            # this re-check does not end it.
            h.free["gb"] = 1.0
            h.clock.advance(1)
            await h.pump(1)
            assert h.state(info.id) == taskq.QUEUED
            assert h.delivered == []
            # Its memory wait is still bounded: 30 s parked before, 30 s now.
            await _recheck(h)
            assert h.state(info.id) == taskq.FAILED
            assert [d.error for d in h.delivered] == [QUEUED_WAIT_EXPIRED_TEXT]

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_the_expiry_publishes_the_depth_it_changed(self, monkeypatch) -> None:
        """The expiry's queued-stop report re-publishes the depth, with no pump pass around it."""
        async with _harness(monkeypatch) as h:
            info = await h.spawn()
            await _recheck(h, 2)
            assert h.queued[-1]["queued"] == 1, h.queued
            await _reload_bound(h, 60)
            seen = len(h.queued)
            h.clock.advance(1)
            assert await h.mgr._admission.taskq_expire_memory_waits_async() == 1
            await h.settle()
            assert h.state(info.id) == taskq.FAILED
            assert h.queued[seen:] and h.queued[-1] == {"queued": 0}, h.queued[seen:]

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_the_inline_pump_ends_the_wait_too(self, monkeypatch) -> None:
        """The inline twin (``pump_off_loop`` off): same verdict, same one report."""
        async with _harness(monkeypatch) as h:
            monkeypatch.setattr(SpawnAdmissionCoordinator, "pump_off_loop", False)
            await _reload_bound(h, 60)
            info = await h.spawn()
            await _recheck(h, 2)
            assert h.state(info.id) == taskq.FAILED
            assert [d.error for d in h.delivered] == [QUEUED_WAIT_EXPIRED_TEXT]
            await _recheck(h, 2)
            assert len(_expired_reports(h, info.id)) == 1


class TestTheBoundIsLive:
    def test_the_key_is_watched_and_carries_no_restart_mark(self) -> None:
        assert _KEY in SubagentManager.LIVE_CONFIG_PATHS
        assert requires_restart(_KEY) is False
        assert KiroCrewConfig().agent.subagent_queue_max_wait_secs == 1800

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_a_reload_changes_the_bound_without_a_restart(self, monkeypatch) -> None:
        async with _harness(monkeypatch) as h:
            info = await h.spawn()
            await _recheck(h, 4)
            assert h.state(info.id) == taskq.QUEUED  # 1800 s default: still waiting
            await _reload_bound(h, 60)
            assert h.mgr._subagent_queue_max_wait_secs == 60
            h.clock.advance(1)
            await h.pump(1)
            assert h.state(info.id) == taskq.FAILED
            assert [d.error for d in h.delivered] == [QUEUED_WAIT_EXPIRED_TEXT]

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_zero_turns_the_bound_off(self, monkeypatch) -> None:
        async with _harness(monkeypatch) as h:
            await _reload_bound(h, 0)
            info = await h.spawn()
            h.clock.advance(86400)
            await h.pump()
            assert h.state(info.id) == taskq.QUEUED
            assert h.delivered == []


class TestEveryWaitingRowExpires:
    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_a_row_restored_from_the_store_keeps_its_clock(self, monkeypatch) -> None:
        """A durable row whose wait an EARLIER process recorded is bounded by that
        recorded wait, not from the first time this process sees it."""
        async with _harness(monkeypatch) as h:
            await _reload_bound(h, 60)
            store = h.mgr._taskq
            old = taskq.TaskRecord(
                id="feedfacefeedface",
                kind=taskq.KIND_SUBAGENT,
                session_key=_PARENT,
                params={"task": "from before the restart", "parent_session_key": _PARENT},
            )
            # Its first 30 s parked were written straight to tasks.db, through
            # no gate of this manager, the way an earlier process left them.
            await store.run(store.accept_one, old)
            await store.run(store.defer, old.id, h.clock.t + _ADMIT, reason="low memory")
            h.clock.advance(_ADMIT)
            await store.run(store.defer, old.id, h.clock.t + _ADMIT, reason="low memory")
            fresh = await h.spawn("accepted here")
            # One more re-check: the old row has 60 s parked, the fresh one 30 s.
            await _recheck(h)
            assert h.state(old.id) == taskq.FAILED
            assert h.state(fresh.id) == taskq.QUEUED
            assert [d.id for d in h.delivered] == [old.id]
            await _recheck(h)
            assert h.state(fresh.id) == taskq.FAILED
            assert [d.id for d in h.delivered] == [old.id, fresh.id]
            assert {d.error for d in h.delivered} == {QUEUED_WAIT_EXPIRED_TEXT}
            assert h.queued[-1] == {"queued": 0}

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_a_row_without_a_durable_queue_is_bounded_too(self, monkeypatch) -> None:
        """No store row for the sweep to read: a start in temporary memory waits
        in the in-memory window, and the gate bounds that floor wait from its
        own parked clock (``_floor_waits``), ending it with the same terminal."""
        from kiro_crew.subagent_manager.admission import MEMORY_WAIT_UNTIL_KEY

        async with _harness(monkeypatch) as h:
            h.mgr._memory_mode_for_session = lambda _key: "temporary"
            await _reload_bound(h, 90)
            info = await h.mgr.spawn_async("scratch", parent_session_key=_PARENT)
            assert info is not None and info.queued and not info.done, info
            assert info.queued_reason == "low_memory"
            assert h.mgr._taskq.get(info.id) is None

            def waiting() -> bool:
                return any(p.get("_preassigned_id") == info.id for p in h.mgr._queue)

            async def recheck() -> None:
                # The current park has run its whole admit wait: move its clock
                # back by that much, make the row eligible, and run the passes.
                closed, since, end = h.mgr._floor_waits[info.id]
                span = end - since
                h.mgr._floor_waits[info.id] = (closed, since - span, end - span)
                for params in h.mgr._queue:
                    params[MEMORY_WAIT_UNTIL_KEY] = 0.0
                await h.pump()

            # Two re-checks that still find the host short: 60 s parked, waiting.
            for _ in range(2):
                await recheck()
                assert waiting() and h.delivered == []
            # 90 s parked: the re-check that re-parks it ends it.
            await recheck()
            assert not waiting()
            assert [d.id for d in h.delivered] == [info.id]
            assert h.delivered[0].error == QUEUED_WAIT_EXPIRED_TEXT
            assert info.id not in h.mgr._floor_waits
            assert await h.mgr.queued_count_for_async(_PARENT) == 0
            # Memory coming back starts nothing and delivers nothing more.
            h.free["gb"] = 32.0
            await h.pump()
            assert len(h.delivered) == 1 and not h.started.is_set()

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_zero_leaves_an_in_memory_floor_wait_unbounded(self, monkeypatch) -> None:
        from kiro_crew.subagent_manager.admission import MEMORY_WAIT_UNTIL_KEY

        async with _harness(monkeypatch) as h:
            h.mgr._memory_mode_for_session = lambda _key: "temporary"
            await _reload_bound(h, 0)
            info = await h.mgr.spawn_async("scratch", parent_session_key=_PARENT)
            assert info is not None and info.queued and not info.done, info
            closed, since, end = h.mgr._floor_waits[info.id]
            h.mgr._floor_waits[info.id] = (closed + 10**6, since - 60, end - 60)
            for params in h.mgr._queue:
                params[MEMORY_WAIT_UNTIL_KEY] = 0.0
            await h.pump()
            assert h.delivered == []
            assert any(p.get("_preassigned_id") == info.id for p in h.mgr._queue)


class TestAnEndedParent:
    """An expiry never injects into a conversation that ended: the injector would
    create it again. A successor under the same key is reported as usual."""

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_an_ended_parents_row_is_stopped_not_expired(self, monkeypatch) -> None:
        async with _harness(monkeypatch) as h:
            await _reload_bound(h, 60)
            info = await h.spawn()
            ids = h.mgr.snapshot_teardown_children(_PARENT)
            await h.mgr.cancel_for_teardown(ids, parent_session_key=_PARENT, verb="close")
            # The teardown's store sweep stopped the row, so it has nothing to expire.
            assert h.state(info.id) == taskq.CANCELLED
            h.clock.advance(1)
            successor = await h.spawn("successor work")
            await _recheck(h, 2)
            assert h.state(successor.id) == taskq.FAILED
            assert [d.id for d in h.delivered] == [successor.id]
            assert h.queued[-1] == {"queued": 0}

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_an_expiry_inside_an_open_teardown_does_not_inject(self, monkeypatch) -> None:
        # Between the snapshot and its cancel's store sweep, the retired row is
        # still queued, so the sweep can end it. Its card ends; nothing injects.
        async with _harness(monkeypatch) as h:
            await _reload_bound(h, 60)
            info = await h.spawn()
            ids = h.mgr.snapshot_teardown_children(_PARENT)
            # A successor conversation under the SAME key spawns its own row.
            h.clock.advance(1)
            successor = await h.spawn("successor work")
            await _recheck(h, 2)
            assert h.state(info.id) == taskq.FAILED
            assert h.state(successor.id) == taskq.FAILED
            assert len(_expired_reports(h, info.id)) == 1
            assert [d.id for d in h.delivered] == [successor.id]
            assert h.queued[-1] == {"queued": 0}
            await h.mgr.cancel_for_teardown(ids, parent_session_key=_PARENT, verb="close")
            h.clock.advance(120)
            await h.pump()
            assert [d.id for d in h.delivered] == [successor.id]
            assert len(_expired_reports(h, info.id)) == 1


class TestARefusedWriteKeepsWhatLanded:
    """Each expiry commits on its own, so a store refusal later in the same sweep
    must not discard the reports the rows that already landed are owed."""

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_a_refused_finish_still_reports_the_rows_that_landed(self, monkeypatch) -> None:
        async with _harness(monkeypatch) as h:
            await _reload_bound(h, 0)
            first = await h.spawn("first")
            second = await h.spawn("second")
            await _recheck(h, 2)
            await _reload_bound(h, 60)
            store = h.mgr._taskq
            real = store.finish
            landed: list[str] = []
            refused: list[str] = []
            busy = {"on": True}

            def busy_after_the_first(task_id: str, *args: Any, **kwargs: Any) -> bool:
                # Only the sweep's own write (the report's settle finishes the row
                # too): the first lands, every later one is refused while busy.
                if kwargs.get("report_owed") and busy["on"]:
                    if landed and task_id not in landed:
                        refused.append(task_id)
                        raise taskq.TaskStoreUnavailable("database is locked")
                    landed.append(task_id)
                return real(task_id, *args, **kwargs)

            monkeypatch.setattr(store, "finish", busy_after_the_first)
            await h.mgr._admission.taskq_expire_memory_waits_async()
            await _until_delivered(h, 1)
            assert landed and refused, (landed, refused)
            assert {landed[0], refused[0]} == {first.id, second.id}
            assert h.state(landed[0]) == taskq.FAILED
            assert [d.id for d in h.delivered] == [landed[0]]
            # The refused row is still only queued: the next sweep ends it.
            assert h.state(refused[0]) == taskq.QUEUED
            busy["on"] = False
            await h.mgr._admission.taskq_expire_memory_waits_async()
            await _until_delivered(h, 2)
            assert h.state(refused[0]) == taskq.FAILED
            assert sorted(d.id for d in h.delivered) == sorted([first.id, second.id])
            assert len(_expired_reports(h, landed[0])) == 1
            assert len(_expired_reports(h, refused[0])) == 1


def _later_store(h: _Harness) -> taskq.TaskStore:
    """The same ``tasks.db`` as a LATER process opens it: another incarnation."""
    return taskq.TaskStore(h.mgr._taskq.path, clock=h.clock, network_fs=False).open()


def _write_lost_expiries(path: Path, agent_ids: list[str]) -> None:
    """An earlier process ended each wait and was lost before reporting it."""
    earlier = taskq.TaskStore(path, network_fs=False).open()
    try:
        for agent_id in agent_ids:
            earlier.accept_one(
                taskq.TaskRecord(
                    id=agent_id,
                    kind=taskq.KIND_SUBAGENT,
                    session_key=_PARENT,
                    params={"task": "lost report", "parent_session_key": _PARENT},
                )
            )
            assert earlier.finish(
                agent_id, taskq.FAILED, error=QUEUED_WAIT_EXPIRED_TEXT, report_owed=True
            )
    finally:
        earlier.close()


async def _until_delivered(h: _Harness, count: int) -> None:
    for _ in range(200):
        if len(h.delivered) >= count:
            break
        await h.settle()
    await h.settle()


class TestAnExpiryOutlivesItsProcess:
    """The expiry's terminal commits before its report runs, so the store says the
    report is owed until it has run; a process lost in between leaves it to the next."""

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_a_reported_expiry_owes_nothing(self, monkeypatch) -> None:
        async with _harness(monkeypatch) as h:
            await _reload_bound(h, 60)
            info = await h.spawn()
            await _recheck(h, 2)
            assert [d.id for d in h.delivered] == [info.id]
            later = _later_store(h)
            try:
                assert later.owed_reports(taskq.KIND_SUBAGENT) == []
            finally:
                later.close()

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_the_next_start_reports_an_expiry_its_writer_did_not(self, monkeypatch) -> None:
        async with _harness(monkeypatch) as h:
            _write_lost_expiries(h.mgr._taskq.path, ["deadbeefdeadbeef"])
            h.mgr._admission.taskq_boot_dispatch()
            await h.settle()
            assert [d.id for d in h.delivered] == ["deadbeefdeadbeef"]
            assert h.delivered[0].error == QUEUED_WAIT_EXPIRED_TEXT
            assert len(_expired_reports(h, "deadbeefdeadbeef")) == 1
            # Reported once: neither this process nor a later one makes it again.
            h.mgr._admission.taskq_boot_dispatch()
            await h.settle()
            later = _later_store(h)
            try:
                assert later.owed_reports(taskq.KIND_SUBAGENT) == []
            finally:
                later.close()
            assert len(h.delivered) == 1

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_a_report_cancelled_with_its_process_stays_owed(self, monkeypatch) -> None:
        async with _harness(monkeypatch) as h:
            await _reload_bound(h, 60)
            info = await h.spawn()
            hang = asyncio.Event()

            async def wedged(_info: Any) -> None:
                await hang.wait()

            h.mgr._on_done = wedged
            await _recheck(h, 2)
            assert h.state(info.id) == taskq.FAILED
            # The shutdown drain cancels a report that did not finish in time.
            reports = [t for t in h.mgr._report_tasks if not t.done()]
            assert reports
            for task in reports:
                task.cancel()
            await asyncio.gather(*reports, return_exceptions=True)
            await h.settle()
            later = _later_store(h)
            try:
                assert [r.id for r in later.owed_reports(taskq.KIND_SUBAGENT)] == [info.id]
            finally:
                later.close()

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_a_report_that_did_not_reach_the_parent_stays_owed(self, monkeypatch) -> None:
        # ``_report_terminal`` returns False when the injection failed: the
        # report task ran, but the parent was never told.
        async with _harness(monkeypatch) as h:
            await _reload_bound(h, 60)
            info = await h.spawn()
            attempts: list[str] = []

            async def refused(failed: Any) -> None:
                attempts.append(failed.id)
                raise RuntimeError("the parent's provider is gone")

            h.mgr._on_done = refused
            await _recheck(h, 2)
            for _ in range(200):
                if attempts and not [t for t in h.mgr._report_tasks if not t.done()]:
                    break
                await asyncio.sleep(0.02)
            await h.settle()
            assert attempts == [info.id]
            later = _later_store(h)
            try:
                assert [r.id for r in later.owed_reports(taskq.KIND_SUBAGENT)] == [info.id]
            finally:
                later.close()

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_the_gateway_boot_order_replays_when_the_store_attaches(
        self, monkeypatch
    ) -> None:
        # The gateway builds the manager on the loop and starts the reaper before
        # the off-loop open finishes, so the boot dispatch finds no store and the
        # attach in ``_initialize_taskq`` is the one place the replay is reached.
        _patch_host(monkeypatch)
        path = taskq.TaskStore.default_path(data_home())
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_lost_expiries(path, ["deadbeefdeadbeef"])
        delivered: list[Any] = []

        async def on_done(info: Any) -> None:
            delivered.append(info)

        mgr = SubagentManager(sessions=mock_sessions(), ctx_builder=mock_ctx(), max_concurrent=3)
        mgr._on_done = on_done
        try:
            assert mgr._taskq is None, "the open must still be in flight"
            mgr.start_reaper()
            await asyncio.wait_for(mgr.wait_taskq_ready(), 5)
            assert mgr._taskq is not None
            for _ in range(200):
                if delivered:
                    break
                await asyncio.sleep(0.02)
            assert [d.id for d in delivered] == ["deadbeefdeadbeef"]
            assert delivered[0].error == QUEUED_WAIT_EXPIRED_TEXT
        finally:
            await mgr.cancel_all()
            if mgr._taskq is not None:
                await asyncio.to_thread(mgr._taskq.close)

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_the_replay_waits_for_the_memory_barrier(self, monkeypatch) -> None:
        # The gateway holds dispatch until memory is prepared. A report injected
        # before then fails on MemoryStartupUnavailable, which the injector
        # swallows, so a replay run under the hold would clear an owed report
        # that was never delivered.
        _patch_host(monkeypatch)
        path = taskq.TaskStore.default_path(data_home())
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_lost_expiries(path, ["deadbeefdeadbeef"])
        delivered: list[Any] = []

        async def on_done(info: Any) -> None:
            delivered.append(info)

        mgr = SubagentManager(
            sessions=mock_sessions(),
            ctx_builder=mock_ctx(),
            max_concurrent=3,
            defer_queue_dispatch=True,
        )
        mgr._on_done = on_done
        try:
            mgr.start_reaper()
            await asyncio.wait_for(mgr.wait_taskq_ready(), 5)
            assert mgr._taskq is not None
            mgr._admission.taskq_boot_dispatch()
            mgr._admission.taskq_schedule_owed_replay()
            for _ in range(10):
                await mgr._taskq.run(lambda: None)
                await asyncio.sleep(0.02)
            assert delivered == []
            later = taskq.TaskStore(path, network_fs=False).open()
            try:
                assert [r.id for r in later.owed_reports(taskq.KIND_SUBAGENT)] == [
                    "deadbeefdeadbeef"
                ]
            finally:
                later.close()
            mgr.release_queue_dispatch()
            for _ in range(200):
                if delivered:
                    break
                await asyncio.sleep(0.02)
            assert [d.id for d in delivered] == ["deadbeefdeadbeef"]
            assert delivered[0].error == QUEUED_WAIT_EXPIRED_TEXT
        finally:
            await mgr.cancel_all()
            if mgr._taskq is not None:
                await asyncio.to_thread(mgr._taskq.close)

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_a_refused_read_leaves_the_replay_to_the_next_sweep(self, monkeypatch) -> None:
        async with _harness(monkeypatch) as h:
            _write_lost_expiries(h.mgr._taskq.path, ["deadbeefdeadbeef"])
            store = h.mgr._taskq
            real = store.owed_reports
            reads: list[int] = []

            def busy_once(*args: Any, **kwargs: Any) -> list[taskq.TaskRecord]:
                reads.append(1)
                if len(reads) == 1:
                    raise taskq.TaskStoreUnavailable("database is locked")
                return real(*args, **kwargs)

            monkeypatch.setattr(store, "owed_reports", busy_once)
            h.mgr._admission.taskq_boot_dispatch()
            await h.settle()
            assert reads == [1] and h.delivered == []
            # The real reaper loop, back to back, with every other sweep step
            # stubbed: only its own replay hook can retry the refused read.
            sweeps: list[int] = []
            mgr = h.mgr
            monkeypatch.setattr(subagent_mod, "_REAPER_INTERVAL", 0)
            monkeypatch.setattr(subagent_mod, "compact_cost_log", lambda: sweeps.append(1))
            monkeypatch.setattr(mgr, "_rebuild_conversation_registry", AsyncMock())
            monkeypatch.setattr(mgr, "_sample_live_costs", MagicMock())
            monkeypatch.setattr(mgr, "_refresh_learned_settled", MagicMock())
            monkeypatch.setattr(mgr, "_sweep_stuck_waves_async", AsyncMock())
            monkeypatch.setattr(mgr, "_sweep_digest_holds_async", AsyncMock())
            monkeypatch.setattr(mgr, "_sweep_conversations", MagicMock())
            monkeypatch.setattr(mgr, "_taskq_pump", MagicMock())
            reaper = asyncio.ensure_future(mgr._reaper_loop())
            try:
                await _until_delivered(h, 1)
                assert [d.id for d in h.delivered] == ["deadbeefdeadbeef"]
                # Later sweeps: the replay is done, so no further read.
                seen = len(sweeps)
                for _ in range(200):
                    if len(sweeps) >= seen + 3:
                        break
                    await asyncio.sleep(0.01)
                await h.settle()
            finally:
                reaper.cancel()
                await asyncio.gather(reaper, return_exceptions=True)
            assert len(sweeps) >= seen + 3, "the reaper stopped sweeping"
            assert len(reads) == 2 and len(h.delivered) == 1

    @pytest.mark.asyncio
    @pytest.mark.timeout(120)
    async def test_every_owed_report_is_replayed_past_one_page(self, monkeypatch) -> None:
        async with _harness(monkeypatch) as h:
            ids = [f"{i:016x}" for i in range(taskq_bridge._OWED_REPLAY_PAGE + 1)]
            _write_lost_expiries(h.mgr._taskq.path, ids)
            h.mgr._admission.taskq_boot_dispatch()
            await _until_delivered(h, len(ids))
            assert sorted(d.id for d in h.delivered) == ids
            later = _later_store(h)
            try:
                assert later.owed_reports(taskq.KIND_SUBAGENT) == []
            finally:
                later.close()


_WAVE_PARENT = "dashboard:main"


@contextlib.contextmanager
def _through_the_gateway(h: _Harness, stream: Any = None) -> Any:
    """The real gateway ``_subagent_done`` as *h*'s ``on_done``, over an idle slot.

    The slot keeps a real content-keyed delivery ledger and the dashboard settles
    it through the real manager, as production wires ``DashboardState(subagents=
    <the manager>)``. Yields ``(injected, marked)``: the turns injected into the
    parent, and the run folders ``mark_delivered`` tombstoned. A channel or cron
    parent is injected through the parent's session instead, by *stream* in
    place of ``stream_and_collect``.
    """
    from unittest.mock import patch

    from test_subagent_scale import _make_orchestrator, _mock_dashboard_state, _mock_sessions

    orch = _make_orchestrator()
    orch.sessions = _mock_sessions()
    orch.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), False, False))
    orch.sessions.cancel_current = AsyncMock()
    orch.ctx_builder = MagicMock()
    orch.ctx_builder.hooks = MagicMock()
    orch.ctx_builder.build_message = MagicMock(return_value=("the announce", None))
    orch.dashboard_state = _mock_dashboard_state()
    slot = MagicMock()
    slot.mode = "chat"
    slot.running = False
    slot.task = None
    slot._subagent_deliveries_inflight = 0
    ledger: dict[str, list[Any]] = {}
    slot.note_pending_subagent_delivery = MagicMock(
        side_effect=lambda content, debts: ledger.setdefault(content, []).extend(debts)
    )
    slot.take_pending_subagent_deliveries = MagicMock(
        side_effect=lambda contents: [d for c in contents for d in ledger.pop(c, [])]
    )
    orch.dashboard_state.get_slot = MagicMock(return_value=slot)
    orch.dashboard_state.subagents = h.mgr
    with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False):
        with patch("kiro_crew.slack.gateway.SubagentManager") as factory:
            factory.return_value = MagicMock()
            orch._init_subagents()
            on_done = factory.call_args.kwargs["on_done"]
    orch.subagent_mgr = h.mgr
    h.mgr._on_done = on_done
    injected: list[str] = []
    marked: list[str] = []

    async def consuming_run_chat(_state, _slot, text, *, _on_consumed=None, **_kw):
        injected.append(text)
        if _on_consumed is not None:
            _on_consumed()

    with (
        patch("kiro_crew.slack.gateway._run_chat", consuming_run_chat),
        patch("kiro_crew.slack.gateway.session_store_for_turn", AsyncMock(return_value="")),
        patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            stream or AsyncMock(return_value="the parent's reply"),
        ),
        patch.object(
            subagent_mod,
            "mark_delivered",
            side_effect=lambda agent_id, **_kw: marked.append(agent_id),
        ),
    ):
        yield injected, marked


async def _expire(h: _Harness, agent_id: str) -> None:
    """Re-check until *agent_id*'s wait has been parked past the bound and ended."""
    for _ in range(6):
        if h.state(agent_id) != taskq.QUEUED:
            break
        await _recheck(h)
    await h.settle()
    assert h.state(agent_id) == taskq.FAILED


async def _a_wave_with_one_live_member(h: _Harness, parent: str = _WAVE_PARENT) -> tuple[Any, Any]:
    """Member A starts and stays running; member B is deferred for memory."""
    h.free["gb"] = 16.0
    live = await h.mgr.spawn_async(
        "live sibling", parent_session_key=parent, batch_id="wv", batch_total=2
    )
    assert live is not None
    await asyncio.wait_for(h.started.wait(), 5)
    h.free["gb"] = 1.0
    waiting = await h.spawn("waits for memory", parent=parent, batch_id="wv", batch_total=2)
    return live, waiting


class TestADigestHeldExpiryStaysOwed:
    """A wave member's expiry is held for the wave's digest while a sibling runs.

    The report task returns as soon as the gateway HOLDS the line, but the line is
    then only in this process's memory: the owed mark must outlive it until the
    digest that carries it reaches the parent, or a restart in between loses the
    parent's only word that the spawn will never run (it has no run folder for
    orphan recovery to find)."""

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_a_restart_before_the_digest_replays_the_held_expiry(self, monkeypatch) -> None:
        async with _harness(monkeypatch) as h:
            await _reload_bound(h, 60)
            with _through_the_gateway(h) as (injected, _marked):
                live, waiting = await _a_wave_with_one_live_member(h)
                await _expire(h, waiting.id)
                assert len(_expired_reports(h, waiting.id)) == 1
                # Held for the digest: nothing reached the parent yet.
                assert injected == []
                assert h.mgr._agents[live.id].done is False
            # The process is lost here. The next incarnation still owes it.
            later = _later_store(h)
            try:
                assert [r.id for r in later.owed_reports(taskq.KIND_SUBAGENT)] == [waiting.id]
            finally:
                later.close()
            restarted = SubagentManager(
                sessions=mock_sessions(), ctx_builder=mock_ctx(), max_concurrent=3
            )
            delivered: list[Any] = []

            async def on_done(info: Any) -> None:
                delivered.append(info)

            restarted._on_done = on_done
            try:
                await asyncio.wait_for(restarted.wait_taskq_ready(), 5)
                restarted._admission.taskq_boot_dispatch()
                for _ in range(200):
                    if delivered:
                        break
                    await asyncio.sleep(0.02)
                assert [d.id for d in delivered] == [waiting.id]
                assert delivered[0].error == QUEUED_WAIT_EXPIRED_TEXT
            finally:
                await restarted.cancel_all()
                if restarted._taskq is not None:
                    await asyncio.to_thread(restarted._taskq.close)

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_the_digest_that_delivers_the_expiry_clears_it(self, monkeypatch) -> None:
        async with _harness(monkeypatch) as h:
            await _reload_bound(h, 60)
            with _through_the_gateway(h) as (injected, marked):
                live, waiting = await _a_wave_with_one_live_member(h)
                await _expire(h, waiting.id)
                assert injected == []
                # The sibling finishes and closes the wave: its digest carries the
                # expiry's line, and the turn that consumes it settles the debt.
                done = subagent_mod.SubagentInfo(
                    id=live.id,
                    task="live sibling",
                    parent_session_key=_WAVE_PARENT,
                    batch_id="wv",
                    batch_total=2,
                )
                done.done = True
                done.result = "sibling result"
                monkeypatch.setattr(
                    h.mgr, "batch_members_pending_async", AsyncMock(return_value=False)
                )
                await h.mgr._on_done(done)
                for _ in range(200):
                    if injected:
                        break
                    await asyncio.sleep(0.02)
                await h.settle()
                assert len(injected) == 1 and QUEUED_WAIT_EXPIRED_TEXT in injected[0]
                # The flusher's own folder is tombstoned; the expiry has none.
                assert marked == [live.id]
            later = _later_store(h)
            try:
                assert later.owed_reports(taskq.KIND_SUBAGENT) == []
            finally:
                later.close()

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_a_solo_expiry_into_an_idle_slot_is_cleared_once_consumed(
        self, monkeypatch
    ) -> None:
        # No wave, nothing held: the idle-slot branch owes the expiry's debt to
        # the turn's consumption, which must then settle it, or every restart
        # replays an expiry the parent already received.
        async with _harness(monkeypatch) as h:
            await _reload_bound(h, 60)
            with _through_the_gateway(h) as (injected, marked):
                waiting = await h.spawn("waits for memory", parent=_WAVE_PARENT)
                await _expire(h, waiting.id)
                for _ in range(200):
                    if injected:
                        break
                    await asyncio.sleep(0.02)
                await h.settle()
                assert len(injected) == 1 and QUEUED_WAIT_EXPIRED_TEXT in injected[0]
                assert marked == []
            later = _later_store(h)
            try:
                assert later.owed_reports(taskq.KIND_SUBAGENT) == []
            finally:
                later.close()

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_a_queued_announce_carries_the_debt_to_its_drain(self, monkeypatch) -> None:
        from kiro_crew.slack.gateway import GatewayOrchestrator
        from kiro_crew.subagent import SubagentDelivery

        info = subagent_mod.SubagentInfo(id="e1", task="t", error=QUEUED_WAIT_EXPIRED_TEXT)
        info._report_owed = True
        slot = MagicMock()
        GatewayOrchestrator._defer_queued_delivery(slot, "announce", info, flush_only=False)
        slot.note_pending_subagent_delivery.assert_called_once_with(
            "announce", [SubagentDelivery("e1", 0.0, 0.0, report_owed=True)]
        )
        assert info._delivery_queued is True
        # An ordinary failed run still owes no mark: its own tombstone stands.
        plain = subagent_mod.SubagentInfo(id="f1", task="t", error="boom")
        slot = MagicMock()
        GatewayOrchestrator._defer_queued_delivery(slot, "announce", plain, flush_only=False)
        slot.note_pending_subagent_delivery.assert_called_once_with("announce", [])

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_a_held_expiry_of_an_ended_parent_is_cleared_not_replayed(
        self, monkeypatch
    ) -> None:
        # The wave's flusher reports to a parent a teardown retired: nothing is
        # injected, and the held expiry it carried is owed to no one any more.
        from kiro_crew.subagent import SubagentDelivery

        async with _harness(monkeypatch) as h:
            cleared: list[list[str]] = []
            monkeypatch.setattr(
                SpawnAdmissionCoordinator,
                "taskq_clear_owed_reports",
                lambda _self, ids: cleared.append(list(ids)),
            )
            info = subagent_mod.SubagentInfo(id="flusher", task="t", parent_session_key=_PARENT)
            info._digest_settle_deliveries = [
                SubagentDelivery("expired", 0.0, 0.0, report_owed=True),
                SubagentDelivery("sibling", 1.0, 0.1),
            ]
            h.mgr._teardown_cancelled_ids.add(info.id)
            await h.mgr._report_terminal(
                info,
                source="test",
                injection_timeout_reason="test",
                mark_delivered_on_success=False,
                settle_digest=True,
            )
            assert h.delivered == []
            assert cleared == [["expired"]]
            # The run's sibling keeps no delivered mark: orphan recovery still finds it.
            assert "sibling" in h.mgr._teardown_cancelled_ids


class TestARefusedTeardownSweepIsRetried:
    """A parent-end teardown stops its parent's store-only rows by reading the
    store. A read the store refuses leaves them queued, so the fence stays open
    (an expiry of one injects nothing into the conversation that ended) and the
    reaper retries the sweep until it lands, still sparing a successor's rows."""

    @staticmethod
    def _refuse_once(monkeypatch) -> list[int]:
        # The STORE refuses, not the bridge: the bridge's own handling of a
        # refused read is what decides whether the teardown sees it at all.
        real = taskq.TaskStore.list_pending
        reads: list[int] = []

        def refused_once(self: Any, *args: Any, **kwargs: Any) -> list[Any]:
            if kwargs.get("session_key") == _PARENT:
                reads.append(1)
                if len(reads) == 1:
                    raise taskq.TaskStoreUnavailable("database is locked")
            return real(self, *args, **kwargs)

        monkeypatch.setattr(taskq.TaskStore, "list_pending", refused_once)
        return reads

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_the_reaper_retries_the_sweep_and_spares_a_successor(self, monkeypatch) -> None:
        async with _harness(monkeypatch) as h:
            await _reload_bound(h, 0)
            retired = await h.spawn("retired work")
            reads = self._refuse_once(monkeypatch)
            ids = h.mgr.snapshot_teardown_children(_PARENT)
            await h.mgr.cancel_for_teardown(ids, parent_session_key=_PARENT, verb="close")
            assert reads == [1]
            assert h.state(retired.id) == taskq.QUEUED
            h.clock.advance(1)
            successor = await h.spawn("successor work")
            # The reaper's hook, as each sweep calls it.
            assert await h.mgr.retry_owed_teardown_sweeps() == 1
            await h.settle()
            assert h.state(retired.id) == taskq.CANCELLED
            assert h.state(successor.id) == taskq.QUEUED
            assert h.delivered == []
            # Done: a later sweep reads nothing again.
            assert await h.mgr.retry_owed_teardown_sweeps() == 0
            assert reads == [1, 1]
            assert h.mgr._teardown_store_sweeps == []

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_an_expiry_before_the_retry_does_not_inject(self, monkeypatch) -> None:
        async with _harness(monkeypatch) as h:
            await _reload_bound(h, 60)
            retired = await h.spawn("retired work")
            self._refuse_once(monkeypatch)
            ids = h.mgr.snapshot_teardown_children(_PARENT)
            await h.mgr.cancel_for_teardown(ids, parent_session_key=_PARENT, verb="close")
            assert h.state(retired.id) == taskq.QUEUED
            await _expire(h, retired.id)
            # Its card ends; nothing injects into the conversation that ended.
            assert len(_expired_reports(h, retired.id)) == 1
            assert h.delivered == []

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_the_reaper_sweep_calls_the_retry(self, monkeypatch) -> None:
        async with _harness(monkeypatch) as h:
            mgr = h.mgr
            retries: list[int] = []

            async def retry() -> int:
                retries.append(1)
                return 0

            monkeypatch.setattr(mgr, "retry_owed_teardown_sweeps", retry)
            monkeypatch.setattr(subagent_mod, "_REAPER_INTERVAL", 0)
            monkeypatch.setattr(subagent_mod, "compact_cost_log", lambda: None)
            monkeypatch.setattr(mgr, "_rebuild_conversation_registry", AsyncMock())
            monkeypatch.setattr(mgr, "_sample_live_costs", MagicMock())
            monkeypatch.setattr(mgr, "_refresh_learned_settled", MagicMock())
            monkeypatch.setattr(mgr, "_sweep_stuck_waves_async", AsyncMock())
            monkeypatch.setattr(mgr, "_sweep_digest_holds_async", AsyncMock())
            monkeypatch.setattr(mgr, "_sweep_conversations", MagicMock())
            monkeypatch.setattr(mgr, "_taskq_pump", MagicMock())
            reaper = asyncio.ensure_future(mgr._reaper_loop())
            try:
                for _ in range(200):
                    if len(retries) >= 2:
                        break
                    await asyncio.sleep(0.01)
            finally:
                reaper.cancel()
                await asyncio.gather(reaper, return_exceptions=True)
            assert len(retries) >= 2


def _owed_ids(h: _Harness) -> list[str]:
    later = _later_store(h)
    try:
        return [r.id for r in later.owed_reports(taskq.KIND_SUBAGENT)]
    finally:
        later.close()


async def _expire_through_the_gateway(h: _Harness, parent: str, stream: Any) -> tuple[str, Any]:
    """One expiry of a *parent* spawn, reported by the gateway over *stream*."""
    await _reload_bound(h, 60)
    with _through_the_gateway(h, stream) as _:
        waiting = await h.spawn("waits for memory", parent=parent)
        await _expire(h, waiting.id)
        for _ in range(200):
            if not [t for t in h.mgr._report_tasks if not t.done()]:
                break
            await asyncio.sleep(0.02)
        await h.settle()
    assert len(_expired_reports(h, waiting.id)) == 1
    return waiting.id, stream


class TestAGivenUpInjectionStaysOwed:
    """A channel or cron parent's injection fails inside the gateway, which logs it
    and returns normally, so the report task returns True although the parent was
    never told. The owed mark must survive that, or no restart ever reports it:
    a cron parent with no tab gets no failure notice either."""

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    @pytest.mark.parametrize("parent", ["cron:j1", "slack:U000"])
    async def test_a_dead_parent_runtime_leaves_the_expiry_owed(self, monkeypatch, parent) -> None:
        from kiro_crew.acp.client import AcpProcessDied

        async with _harness(monkeypatch) as h:
            stream = AsyncMock(side_effect=AcpProcessDied("ACP process pipe broken"))
            agent_id, _ = await _expire_through_the_gateway(h, parent, stream)
            assert stream.await_count >= 1, "the gateway never tried to inject"
            assert _owed_ids(h) == [agent_id]

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_a_cron_failure_swallowed_without_a_notice_leaves_it_owed(
        self, monkeypatch
    ) -> None:
        # The cron arm's catch-all logs and returns with no failure notice at all.
        async with _harness(monkeypatch) as h:
            stream = AsyncMock(side_effect=ValueError("the cron session broke"))
            agent_id, _ = await _expire_through_the_gateway(h, "cron:j1", stream)
            assert stream.await_count == 1
            assert _owed_ids(h) == [agent_id]

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_a_channel_failure_on_every_attempt_leaves_it_owed(self, monkeypatch) -> None:
        async with _harness(monkeypatch) as h:
            stream = AsyncMock(side_effect=ValueError("the channel session broke"))
            agent_id, _ = await _expire_through_the_gateway(h, "slack:U000", stream)
            assert stream.await_count == 1
            assert _owed_ids(h) == [agent_id]

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    @pytest.mark.parametrize("parent", ["cron:j1", "slack:U000"])
    async def test_an_injection_that_lands_clears_it(self, monkeypatch, parent) -> None:
        # The control: the same route, injected, owes nothing afterwards.
        async with _harness(monkeypatch) as h:
            stream = AsyncMock(return_value="noted")
            await _expire_through_the_gateway(h, parent, stream)
            assert stream.await_count == 1
            assert _owed_ids(h) == []


class TestAGivenUpDigestKeepsItsHeldExpiriesOwed:
    """A cron or channel wave's digest carries the expiry it held. When the gateway
    gives up on that digest's injection it still returns normally, so the settle
    after it runs; the held expiry must stay owed, or no start ever reports it."""

    @staticmethod
    async def _flush(h: _Harness, live: Any, flush: str, monkeypatch: Any) -> Any:
        """Release the wave's digest: its last member's report, or the reaper's
        forced flush. Returns the record that carried the digest, or None for
        the forced flush, whose record is internal."""
        monkeypatch.setattr(h.mgr, "batch_members_pending_async", AsyncMock(return_value=False))
        if flush == "forced":
            before = set(h.mgr._tasks)
            h.mgr.force_digest_flush("wv", live.parent_session_key, 2, 120.0)
            (key,) = [k for k in h.mgr._tasks if k not in before]
            await asyncio.wait_for(h.mgr._tasks[key], 10)
            return None
        done = subagent_mod.SubagentInfo(
            id=live.id,
            task="live sibling",
            parent_session_key=live.parent_session_key,
            batch_id="wv",
            batch_total=2,
        )
        done.done = True
        done.result = "sibling result"
        await h.mgr._report_terminal(
            done,
            source="test",
            injection_timeout_reason="test",
            mark_delivered_on_success=True,
            settle_digest=True,
        )
        return done

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    @pytest.mark.parametrize("flush", ["last member", "forced"])
    @pytest.mark.parametrize(
        ("parent", "error"),
        [("cron:j1", "dead runtime"), ("slack:U000", "dead runtime"), ("cron:j1", "swallowed")],
    )
    async def test_a_failed_digest_leaves_the_held_expiry_owed(
        self, monkeypatch, parent, error, flush
    ) -> None:
        from kiro_crew.acp.client import AcpProcessDied

        raised = (
            AcpProcessDied("ACP process pipe broken")
            if error == "dead runtime"
            else ValueError("the cron session broke")
        )
        stream = AsyncMock(side_effect=raised)
        async with _harness(monkeypatch) as h:
            await _reload_bound(h, 60)
            with _through_the_gateway(h, stream) as _:
                live, waiting = await _a_wave_with_one_live_member(h, parent)
                await _expire(h, waiting.id)
                # Held for the digest: nothing was injected yet.
                assert stream.await_count == 0
                assert h.mgr._agents[live.id].done is False
                carrier = await self._flush(h, live, flush, monkeypatch)
                await h.settle()
                assert stream.await_count >= 1, "the digest was never injected"
                if carrier is not None:
                    assert carrier._report_undelivered is True
            assert _owed_ids(h) == [waiting.id]

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    @pytest.mark.parametrize("flush", ["last member", "forced"])
    @pytest.mark.parametrize("parent", ["cron:j1", "slack:U000"])
    async def test_a_digest_that_lands_clears_it(self, monkeypatch, parent, flush) -> None:
        # The control: the same wave, its digest injected, owes nothing.
        stream = AsyncMock(return_value="noted")
        async with _harness(monkeypatch) as h:
            await _reload_bound(h, 60)
            with _through_the_gateway(h, stream) as _:
                live, waiting = await _a_wave_with_one_live_member(h, parent)
                await _expire(h, waiting.id)
                await self._flush(h, live, flush, monkeypatch)
                await h.settle()
                assert stream.await_count == 1
            assert _owed_ids(h) == []
