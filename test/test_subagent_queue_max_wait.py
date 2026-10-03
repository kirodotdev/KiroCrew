"""A spawn queued for memory waits at most ``agent.subagent_queue_max_wait_secs``.

Owner decision A1: a memory wait is finite. A row the memory floor or the posture
gate keeps deferring past the bound ends in ONE delivered terminal,
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
from kiro_crew.resource_status import POSTURE_AMPLE, POSTURE_CRITICAL, AdmissionDecision
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

    async def spawn(self, task: str = "work", parent: str = _PARENT) -> Any:
        info = await self.mgr.spawn_async(task, parent_session_key=parent)
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
    monkeypatch.setattr(
        subagent_mod,
        "cached_admission_check",
        lambda: AdmissionDecision(admitted=True, posture=POSTURE_AMPLE, available_gb=32.0),
    )
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
    async def test_a_posture_deferral_is_bounded_too(self, monkeypatch) -> None:
        async with _harness(monkeypatch) as h:
            h.free["gb"] = 32.0
            monkeypatch.setattr(
                subagent_mod,
                "cached_admission_check",
                lambda: AdmissionDecision(
                    admitted=False,
                    posture=POSTURE_CRITICAL,
                    available_gb=1.0,
                    reason="host memory is critically low",
                ),
            )
            await _reload_bound(h, 60)
            info = await h.spawn()
            await _recheck(h, 2)
            assert h.state(info.id) == taskq.FAILED
            assert [d.error for d in h.delivered] == [QUEUED_WAIT_EXPIRED_TEXT]
            assert h.queued[-1] == {"queued": 0}

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
    async def test_a_row_without_a_durable_queue_never_waits(self, monkeypatch) -> None:
        """No store row, no wait to bound: a spawn the durable queue cannot hold
        is refused at the memory gate, so nothing is left waiting for memory."""
        async with _harness(monkeypatch) as h:
            h.mgr._memory_mode_for_session = lambda _key: "temporary"
            info = await h.mgr.spawn_async("scratch", parent_session_key=_PARENT)
            assert info is not None and info.done and info.error
            assert "memory" in info.error
            assert h.mgr._queue == []


class TestAnEndedParent:
    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_an_ended_parent_is_not_rebuilt_by_the_expiry(self, monkeypatch) -> None:
        async with _harness(monkeypatch) as h:
            await _reload_bound(h, 60)
            info = await h.spawn()
            ids = h.mgr.snapshot_teardown_children(_PARENT)
            await h.mgr.cancel_for_teardown(ids, parent_session_key=_PARENT, verb="close")
            # A successor conversation under the SAME key spawns its own row.
            h.clock.advance(1)
            successor = await h.spawn("successor work")
            await _recheck(h, 2)
            assert h.state(info.id) == taskq.FAILED
            assert h.state(successor.id) == taskq.FAILED
            # The retired row's card ends, but nothing injects into the conversation
            # that ended -- the injector would create it again. The successor's row
            # is reported as usual.
            assert len(_expired_reports(h, info.id)) == 1
            assert [d.id for d in h.delivered] == [successor.id]
            assert h.queued[-1] == {"queued": 0}
            h.clock.advance(120)
            await h.pump()
            assert [d.id for d in h.delivered] == [successor.id]
            assert len(_expired_reports(h, info.id)) == 1

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_the_stamp_is_kept_for_as_long_as_its_rows_wait(self, monkeypatch) -> None:
        """No age limit: a retired parent's row that waits longer than a day, with
        other teardowns in between, still finds the stamp when it ends."""
        async with _harness(monkeypatch) as h:
            await _reload_bound(h, 0)
            info = await h.spawn()
            h.mgr.snapshot_teardown_children(_PARENT)
            h.clock.advance(2 * 86400)
            h.mgr.snapshot_teardown_children("dash:another")
            await h.pump()
            successor = await h.spawn("successor work")
            await _reload_bound(h, 60)
            await _recheck(h, 2)
            assert h.state(info.id) == taskq.FAILED
            assert h.state(successor.id) == taskq.FAILED
            assert [d.id for d in h.delivered] == [successor.id]
            assert len(_expired_reports(h, info.id)) == 1

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_a_stamp_that_gates_nothing_is_dropped(self, monkeypatch) -> None:
        async with _harness(monkeypatch) as h:
            await _reload_bound(h, 0)
            info = await h.spawn()
            h.mgr.snapshot_teardown_children(_PARENT)
            h.mgr.snapshot_teardown_children("dash:no-rows")
            await h.mgr._admission.taskq_expire_memory_waits_async()
            # The parent with no unstarted row is forgotten; the one whose row
            # still waits is kept, until that row is gone too.
            assert set(h.mgr._retired_parents) == {_PARENT}
            await h.mgr._taskq.run(h.mgr._taskq.cancel, info.id)
            await h.mgr._admission.taskq_expire_memory_waits_async()
            assert h.mgr._retired_parents == {}


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

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_a_refused_stamp_read_still_reports_and_keeps_the_stamps(
        self, monkeypatch
    ) -> None:
        async with _harness(monkeypatch) as h:
            await _reload_bound(h, 0)
            info = await h.spawn()
            await _recheck(h, 2)
            h.mgr.snapshot_teardown_children("dash:no-rows")
            await _reload_bound(h, 60)
            store = h.mgr._taskq
            real = store.oldest_unstarted_by_session

            def busy(*args: Any, **kwargs: Any) -> dict[str, float]:
                raise taskq.TaskStoreUnavailable("database is locked")

            monkeypatch.setattr(store, "oldest_unstarted_by_session", busy)
            assert await h.mgr._admission.taskq_expire_memory_waits_async() == 1
            await _until_delivered(h, 1)
            assert h.state(info.id) == taskq.FAILED
            assert [d.id for d in h.delivered] == [info.id]
            # Nothing was read, so nothing is dropped; the next sweep drops it.
            assert "dash:no-rows" in h.mgr._retired_parents
            monkeypatch.setattr(store, "oldest_unstarted_by_session", real)
            await h.mgr._admission.taskq_expire_memory_waits_async()
            assert "dash:no-rows" not in h.mgr._retired_parents
            assert len(h.delivered) == 1


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
