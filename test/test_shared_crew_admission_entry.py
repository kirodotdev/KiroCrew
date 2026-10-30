"""Entry-point adapters: chat turns never steer while queued, cron and background runs
are charged and bounded, subagent probes map the manager's truth, and the lane
resolver makes the subagent round-robin per person."""

from __future__ import annotations

import asyncio
import subprocess
from types import SimpleNamespace

import pytest

from kiro_crew.config.sections_shared_crew import effective_limits, shared_crew_config_from
from kiro_crew.shared_crew import admission_entry as entry
from kiro_crew.shared_crew import admission_gate as ag
from kiro_crew.shared_crew.admission import Liveness, OperationKind, OperationState, ReasonCode
from kiro_crew.shared_crew.containment import ContainmentReport, SliceManager
from kiro_crew.shared_crew.contracts import EnforcementLevel
from kiro_crew.shared_crew.work_store import WorkStore
from kiro_crew.taskq import lanes
from kiro_crew.taskq.store import TaskStore
from kiro_crew.testing.clock import ManualClock
from kiro_crew.testing.wait import async_wait_until

S = OperationState
BOUND = 30.0


class _Req(dict):
    headers: dict = {}


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(start=50_000.0)


@pytest.fixture
def service(tmp_path, clock, monkeypatch):
    store = WorkStore(TaskStore(tmp_path / "t.db", clock=clock.time).open(), clock=clock.time)
    limits = effective_limits(
        shared_crew_config_from(
            {"admission_enabled": True, "principal_concurrency": 1, "crew_concurrency": 2}
        ),
        auto_crew_concurrency=8,
    )
    report = ContainmentReport(
        platform="linux",
        available=True,
        reason="",
        cpu=True,
        memory=True,
        pids=True,
        level=EnforcementLevel.BEST_EFFORT,
        hard_ready=True,
        slices_enabled=True,
    )
    svc = ag.AdmissionService(
        store,
        limits=limits,
        clock=clock.time,
        containment_probe=lambda: report,
        slices=SliceManager(
            runner=lambda cmd: subprocess.CompletedProcess(cmd, 0, "", ""),
            systemctl="/usr/bin/systemctl",
        ),
        liveness=lambda ref: Liveness.STOPPED,
    )

    async def _current():
        return svc

    monkeypatch.setattr(ag, "current_service", _current)
    monkeypatch.setattr(ag, "_SERVICE", svc)
    monkeypatch.setattr(lanes, "_LANE_RESOLVER", svc.budget_for_session)
    yield svc
    entry._PENDING_SLOTS.clear()


@pytest.mark.asyncio
async def test_queued_chat_turn_is_pending_so_a_later_send_cannot_steer(service):
    first = await entry.admit_chat_turn(_Req(), session_key="s1", slot_key="a", message="one")
    second = await entry.admit_chat_turn(_Req(), session_key="s2", slot_key="b", message="two")
    assert first.admitted and second.queued
    started = asyncio.Event()
    release = asyncio.Event()

    async def turn(name):
        started.set()
        await release.wait()
        return name

    t1 = asyncio.ensure_future(
        entry.admitted_chat_turn(
            "a", first, turn("a"), still_live=lambda: True, on_ended=lambda: None
        )
    )
    await asyncio.wait_for(started.wait(), BOUND)
    started.clear()
    t2 = asyncio.ensure_future(
        entry.admitted_chat_turn(
            "b", second, turn("b"), still_live=lambda: True, on_ended=lambda: None
        )
    )
    await async_wait_until(lambda: entry.admission_pending("b"), timeout=BOUND)
    assert not entry.admission_pending("a")
    release.set()
    assert await asyncio.wait_for(t1, BOUND) == "a"
    assert await asyncio.wait_for(t2, BOUND) == "b"
    assert not entry.admission_pending("b")


@pytest.mark.asyncio
async def test_closed_slot_ends_the_queued_turn_unstarted(service):
    await entry.admit_chat_turn(_Req(), session_key="s1", slot_key="a", message="one")
    queued = await entry.admit_chat_turn(_Req(), session_key="s2", slot_key="b", message="two")
    ran: list[str] = []
    ended: list[bool] = []

    async def turn():
        ran.append("b")

    holder = service.scheduler.active_tickets()[0].work_id
    task = asyncio.ensure_future(
        entry.admitted_chat_turn(
            "b", queued, turn(), still_live=lambda: False, on_ended=lambda: ended.append(True)
        )
    )
    await service.cancel(holder, expected_revision=service.live_work(holder).revision)
    assert await asyncio.wait_for(task, BOUND) is None
    assert ran == [] and ended == [True]
    rec = service.live_work(queued.work.work_id) or await service.current_work(queued.work.work_id)
    assert rec.reason is ReasonCode.CANCELLED_BY_USER


@pytest.mark.asyncio
async def test_cron_dispatch_is_charged_to_automation_and_skipped_when_refused(service):
    job = SimpleNamespace(id="j1", last_status="", last_error="", created_by_principal="")
    done = asyncio.Event()

    async def dispatch():
        await done.wait()
        return "ok"

    running = asyncio.ensure_future(entry.admitted_cron_dispatch(job, dispatch()))
    await async_wait_until(
        lambda: service.scheduler.usage(ag.AUTOMATION_PRINCIPAL.id)[0].active == 1,
        timeout=BOUND,
    )
    blocked = await service.admit(
        budget_key=ag.AUTOMATION_PRINCIPAL.id,
        session_key="cron:j2",
        operation=OperationKind.CRON,
        idempotency_key="cron-key-0002",
        allow_queue=False,
    )
    assert blocked.rejected and blocked.reason is ReasonCode.PRINCIPAL_ACTIVE_LIMIT
    done.set()
    assert await asyncio.wait_for(running, BOUND) == "ok"


@pytest.mark.asyncio
async def test_background_run_starts_now_or_is_refused_and_settles_by_probe(service):
    runs = {"r1": SimpleNamespace(status="running")}
    runner = SimpleNamespace(_runs=runs)
    adm = await entry.admit_background_run(
        _Req(),
        operation=OperationKind.TASK,
        session_key="s1",
        probe=entry.taskrunner_probe(runner),
    )
    assert adm.admitted
    await adm.attach_child("r1", session_key="s1")
    refused = await entry.admit_background_run(
        _Req(), operation=OperationKind.TASK, session_key="s2", probe=None
    )
    assert refused.rejected and refused.refusal_status() == 429
    assert (await service.tick())["children"] == 0
    runs["r1"].status = "completed"
    assert (await service.tick())["children"] == 1
    assert (await service.current_work(adm.work.work_id)).state is S.COMPLETED
    assert not service._scheduler.is_active(adm.work.work_id)


def test_subagent_probe_reads_manager_then_queue():
    live = SimpleNamespace(done=False, error="")
    mgr = SimpleNamespace(_agents={"a": live}, taskq_store=lambda: None)
    probe = entry.subagent_probe(mgr)
    assert probe("a") is None
    live.done, live.error = True, "boom"
    assert probe("a") is S.FAILED
    assert probe("gone") is S.UNKNOWN_OUTCOME
    row = SimpleNamespace(state="done")
    mgr2 = SimpleNamespace(_agents={}, taskq_store=lambda: SimpleNamespace(get=lambda _i: row))
    assert entry.subagent_probe(mgr2)("x") is S.COMPLETED
    row.state = "running"
    assert entry.subagent_probe(mgr2)("x") is None


def test_workflow_probe_maps_registry_status():
    snaps = {"w": {"status": "running"}}
    svc = SimpleNamespace(status=lambda rid: snaps.get(rid))
    probe = entry.workflow_probe(svc)
    assert probe("w") is None
    snaps["w"]["status"] = "finished"
    assert probe("w") is S.COMPLETED
    assert probe("missing") is S.UNKNOWN_OUTCOME


@pytest.mark.asyncio
async def test_lane_resolver_makes_one_lane_per_person(service):
    adm = await entry.admit_chat_turn(_Req(), session_key="tab-1", slot_key="t1", message="x")
    gate = asyncio.Event()
    started = asyncio.Event()

    async def turn():
        started.set()
        await gate.wait()

    task = asyncio.ensure_future(adm.run(turn()))
    await asyncio.wait_for(started.wait(), BOUND)
    assert lanes.lane_key_for("tab-1") == lanes.PRINCIPAL_LANE_PREFIX + adm.work.budget_key
    assert lanes.lane_key_for("cron:x") == lanes.SYSTEM_LANE  # unknown to the resolver
    gate.set()
    await asyncio.wait_for(task, BOUND)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
async def test_cron_retry_reuses_running_reservation(service, outcome):
    from dataclasses import replace

    from kiro_crew.shared_crew.turn_gate import admitted_work

    await service.set_limits(replace(service.limits, crew_active=1))
    job = SimpleNamespace(id="retry", created_by_principal="", last_status="", last_error="")
    work_ids = []

    async def retry():
        work_ids.append(admitted_work())
        if outcome == "error":
            raise RuntimeError("dispatch failed")
        if outcome == "cancel":
            raise asyncio.CancelledError
        return "retried"

    async def dispatch():
        work_ids.append(admitted_work())
        return await entry.admitted_cron_dispatch(job, retry())

    run = entry.admitted_cron_dispatch(job, dispatch())
    if outcome == "success":
        assert await asyncio.wait_for(run, BOUND) == "retried"
    else:
        error = RuntimeError if outcome == "error" else asyncio.CancelledError
        with pytest.raises(error):
            await asyncio.wait_for(run, BOUND)
    assert len(work_ids) == 2 and work_ids[0] and work_ids[0] == work_ids[1]
    rows = await service.list_work(session_key="cron:retry")
    assert len(rows) == 1
    assert (
        rows[0].state
        is {
            "success": S.COMPLETED,
            "error": S.FAILED,
            "cancel": S.CANCELLED,
        }[outcome]
    )
    assert service.scheduler.active_tickets() == ()
    assert admitted_work() == ""
