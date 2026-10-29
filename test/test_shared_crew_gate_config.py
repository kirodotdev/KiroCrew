"""Shared Crew repair: A raising containment probe fails closed in hard-limit mode; a turn gate call picks up admission enabled by config."""

from __future__ import annotations

import asyncio
import subprocess

import pytest

from kiro_crew.config.sections_shared_crew import effective_limits, shared_crew_config_from
from kiro_crew.shared_crew import admission_gate as ag
from kiro_crew.shared_crew import containment as _containment
from kiro_crew.shared_crew import turn_gate
from kiro_crew.shared_crew.admission import (
    Liveness,
    OperationKind,
    OperationState,
    ReasonCode,
)
from kiro_crew.shared_crew.containment import ContainmentReport, SliceManager
from kiro_crew.shared_crew.contracts import (
    DisplaySnapshot,
    EnforcementLevel,
    Principal,
    PrincipalKind,
    Provenance,
    principal_id_for,
)
from kiro_crew.shared_crew.work_store import WorkStore
from kiro_crew.taskq.store import TaskStore
from kiro_crew.testing.clock import ManualClock
from kiro_crew.testing.wait import await_until


def _person(login: str) -> Principal:
    return Principal(
        id=principal_id_for("tailscale", "tailnet", login),
        kind=PrincipalKind.HUMAN,
        provider="tailscale",
        issuer="tailnet",
        subject=login,
        provenance=Provenance.TAILSCALE_SERVE,
        display=DisplaySnapshot(name=login.split("@")[0], login=login),
    )


ALICE = _person("alice@example.test")


def _report(**kw) -> ContainmentReport:
    base = dict(
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
    base.update(kw)
    return ContainmentReport(**base)


def _limits(**cfg):
    raw = {"admission_enabled": True, "principal_concurrency": 1, "crew_concurrency": 2}
    raw.update(cfg)
    return effective_limits(shared_crew_config_from(raw), auto_crew_concurrency=8)


def _service(tmp_path, *, probe=None, **cfg) -> ag.AdmissionService:
    clock = ManualClock(start=50_000.0)
    store = WorkStore(TaskStore(tmp_path / "t.db", clock=clock.time).open(), clock=clock.time)
    return ag.AdmissionService(
        store,
        limits=_limits(**cfg),
        clock=clock.time,
        containment_probe=probe or (lambda: _report()),
        slices=SliceManager(
            runner=lambda cmd: subprocess.CompletedProcess(cmd, 0, "", ""),
            systemctl="/usr/bin/systemctl",
        ),
        liveness=lambda ref: Liveness.STOPPED,
    )


# -- 7. a raising probe fails closed ---------------------------------------------------------


@pytest.mark.asyncio
async def test_raising_probe_fails_closed_in_hard_limit_mode(tmp_path, monkeypatch):
    def boom():
        raise OSError("bus gone")

    svc = _service(tmp_path, probe=boom, hard_limits=True)
    monkeypatch.setattr(ag, "_SERVICE", svc)
    monkeypatch.setattr(_containment, "_HARD_MODE_REPORT", None)
    report = await svc.containment_report(refresh=True)
    assert report.refusal_reason == _containment.REASON_UNKNOWN
    assert _containment.hard_mode_refusal() == _containment.REASON_UNKNOWN
    refused = await svc.admit(
        budget_key=ALICE.id,
        session_key="s",
        operation=OperationKind.CHAT_TURN,
        idempotency_key="key-refused-1",
    )
    assert refused.rejected and refused.reason is ReasonCode.CONTAINMENT_UNAVAILABLE

    soft = _service(tmp_path / "soft", probe=boom)
    assert (await soft.containment_report(refresh=True)).refusal_reason is None


@pytest.mark.asyncio
async def test_startup_check_that_raises_publishes_a_refusal_in_hard_mode(monkeypatch):
    async def broken():
        raise RuntimeError("store exploded")

    monkeypatch.setattr(ag, "current_service", broken)
    monkeypatch.setattr(ag, "_configured_hard_limits", lambda: True)
    monkeypatch.setattr(_containment, "_HARD_MODE_REPORT", None)
    report = await ag.startup_check()
    assert report is not None and report.refusal_reason == _containment.REASON_UNKNOWN
    assert _containment.hard_mode_refusal() == _containment.REASON_UNKNOWN

    monkeypatch.setattr(ag, "_configured_hard_limits", lambda: False)
    monkeypatch.setattr(_containment, "_HARD_MODE_REPORT", None)
    soft = await ag.startup_check()
    assert soft is not None and soft.refusal_reason is None
    assert _containment.hard_mode_refusal() is None


# -- 8. enabling admission by config reaches the turn gate -------------------------------


@pytest.mark.asyncio
async def test_turn_gate_picks_up_admission_enabled_by_config(tmp_path, monkeypatch):
    off = effective_limits(shared_crew_config_from({}), auto_crew_concurrency=2)
    disabled = ag.AdmissionService(None, limits=off)
    store = WorkStore(TaskStore(tmp_path / "t.db").open())
    monkeypatch.setattr(ag, "_load_limits", lambda: _limits())
    monkeypatch.setattr(ag, "_open_default_store", lambda: store)
    monkeypatch.setattr(ag, "_SERVICE_LOOP", asyncio.get_running_loop())
    ag.install_service(disabled)
    monkeypatch.setattr(ag, "_last_config_at", -1e9)
    try:
        assert not turn_gate.admission_active()
        admission = await turn_gate.admit_turn(
            budget_key=ALICE.id, session_key="dashboard:cfg", text="hi"
        )
        assert admission is not None and admission.admitted
        assert turn_gate.admission_active()
    finally:
        ticker = ag._ticker
        if ticker is not None:
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)
        ag.install_service(None)


@pytest.mark.asyncio
async def test_turn_gate_without_a_gateway_service_stays_off(monkeypatch):
    monkeypatch.setattr(ag, "_SERVICE", None)
    assert await turn_gate.admission_on() is False
    assert await turn_gate.admit_turn(budget_key=ALICE.id, session_key="s", text="x") is None


@pytest.mark.parametrize("installed_disabled", [False, True])
def test_concurrent_service_callers_share_initialization_across_loops(
    tmp_path, monkeypatch, installed_disabled
):
    import threading
    from unittest.mock import Mock

    from kiro_crew.testing.wait import await_until

    limits = _limits()
    store = WorkStore(TaskStore(tmp_path / "singleton.db").open())
    opens = Mock(return_value=store)
    ticks = Mock()
    monkeypatch.setattr(ag, "_open_default_store", opens)
    monkeypatch.setattr(ag, "_start_ticker", ticks)
    monkeypatch.setattr(ag, "_last_config_at", float("-inf"))
    monkeypatch.setattr(ag, "_SERVICE", None)
    monkeypatch.setattr(ag, "_SERVICE_LOOP", None)

    async def contenders():
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        second_entered = asyncio.Event()
        release = threading.Event()

        def load():
            assert threading.get_ident() != loop_thread
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(30), "configuration read was not released"
            return limits

        async def second():
            second_entered.set()
            return await ag.current_service()

        loop_thread = threading.get_ident()
        monkeypatch.setattr(ag, "_load_limits", load)
        if installed_disabled:
            off = _limits(admission_enabled=False)
            ag.install_service(ag.AdmissionService(None, limits=off))
            monkeypatch.setattr(ag, "_SERVICE_LOOP", loop)
            monkeypatch.setattr(ag, "_last_config_at", float("-inf"))
        tasks = [asyncio.create_task(ag.current_service())]
        try:
            await await_until(entered)
            tasks.append(asyncio.create_task(second()))
            await await_until(second_entered)
        finally:
            release.set()
            results = await await_until(asyncio.gather(*tasks))
        assert len(results) == 2
        assert results[0] is results[1] is ag.installed_service()
        assert results[0].enabled
        return results[0]

    first = asyncio.run(contenders())
    assert opens.call_count == ticks.call_count == 1
    second = asyncio.run(contenders())
    assert second is not first
    assert opens.call_count == ticks.call_count == 2


@pytest.mark.asyncio
async def test_gateway_start_builds_the_service_before_any_restored_turn(monkeypatch):
    """The startup restore builds admission's service before it pauses or restores.

    A channel turn then finds a service from the first message on, so an
    enabled config gates it even before any HTTP admission call.
    """
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.dashboard import server
    from kiro_crew.shared_crew import wiring

    order: list[str] = []
    state = MagicMock()

    class _Stop(Exception):
        pass

    async def build():
        order.append("service")

    async def pause(_what):
        order.append("pause")
        raise _Stop

    def install(gateway):
        assert gateway is state
        order.append("wiring")

    monkeypatch.setattr(wiring, "install", install)
    monkeypatch.setattr(ag, "current_service", AsyncMock(side_effect=build))
    monkeypatch.setattr(server.cautious_boot, "pause_before", pause)
    with pytest.raises(_Stop):
        await server._restore_dashboard_sessions(state, MagicMock())
    assert order == ["wiring", "service", "pause"]


def test_admission_on_builds_nothing_without_an_installed_service(monkeypatch):
    """A process that never started a gateway (a CLI, a unit test) reads no config."""
    from unittest.mock import Mock

    loads = Mock()
    monkeypatch.setattr(ag, "_SERVICE", None)
    monkeypatch.setattr(ag, "_load_limits", loads)
    assert asyncio.run(turn_gate.admission_on()) is False
    loads.assert_not_called()
    assert ag.installed_service() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("setting", ["admission_enabled", "containment_enabled"])
async def test_disabling_containment_resets_only_owned_instance_quota(
    tmp_path, monkeypatch, setting
):
    import threading

    svc = _service(tmp_path)
    slices = svc._slices
    calls = []
    loop_thread = threading.get_ident()

    def run(cmd):
        assert threading.get_ident() != loop_thread
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(slices, "_runner", run)
    monkeypatch.setattr(slices, "populated", lambda _name: True)
    monkeypatch.setattr(svc, "_slice_busy", lambda _name: ["kirocrew-rt-busy.scope"])
    monkeypatch.setattr(ag, "_SERVICE", svc)
    monkeypatch.setattr(ag, "_SERVICE_LOOP", asyncio.get_running_loop())
    monkeypatch.setattr(ag, "_start_ticker", lambda _service: None)
    monkeypatch.setattr(ag, "_last_config_at", float("-inf"))
    monkeypatch.setattr(ag, "_load_limits", lambda: _limits(**{setting: False}))
    principal = await svc.prepare_principal_slice(ALICE.id)
    instance = _containment.instance_slice_name()
    assert slices.is_ready(instance) and slices.is_ready(principal)
    calls.clear()
    assert await ag.current_service() is svc
    assert calls == [
        ["/usr/bin/systemctl", "--user", "set-property", "--runtime", instance, "CPUQuota="]
    ]
    assert slices.is_ready(instance) and slices.is_ready(principal)
    assert _containment._ACTIVE_SLICES is None
    assert await ag.shutdown_cleanup(rounds=1) == 0
    assert len(calls) == 1
    calls.clear()
    await svc.set_limits(_limits())
    await svc.prepare_principal_slice(ALICE.id)
    assert calls[0][-1] == f"CPUQuota={svc.limits.crew_cpu_quota_percent}%"


@pytest.mark.asyncio
async def test_disabled_service_shutdown_releases_owned_idle_slices(tmp_path, monkeypatch):
    svc = _service(tmp_path, admission_enabled=False)
    slices = svc._slices
    principal = await _containment.prepare_slices(slices, ALICE.id, _limits(), _report())
    instance = _containment.instance_slice_name()
    calls = []
    monkeypatch.setattr(
        slices,
        "_runner",
        lambda cmd: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""),
    )
    monkeypatch.setattr(slices, "populated", lambda _name: False)
    monkeypatch.setattr(svc, "_slice_busy", lambda _name: [])
    monkeypatch.setattr(ag, "_SERVICE", svc)
    assert await ag.shutdown_cleanup(rounds=1) == 2
    assert slices.applied() == ()
    assert [(cmd[2], cmd[3]) for cmd in calls] == [
        ("stop", principal),
        ("revert", principal),
        ("stop", instance),
        ("revert", instance),
    ]


@pytest.mark.asyncio
async def test_quota_reset_failure_keeps_limits_for_retry(tmp_path, monkeypatch):
    svc = _service(tmp_path)
    await svc.prepare_principal_slice(ALICE.id)
    original = svc.limits
    monkeypatch.setattr(
        svc._slices, "_runner", lambda cmd: subprocess.CompletedProcess(cmd, 1, "", "failed")
    )
    with pytest.raises(RuntimeError, match="could not reset"):
        await svc.set_limits(_limits(admission_enabled=False))
    assert svc.limits == original
    calls = []
    monkeypatch.setattr(
        svc._slices,
        "_runner",
        lambda cmd: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""),
    )
    await svc.set_limits(_limits(admission_enabled=False))
    assert not svc.enabled and calls[0][-1] == "CPUQuota="


@pytest.mark.asyncio
async def test_disable_waits_for_inflight_slice_preparation(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from kiro_crew.testing.wait import await_until

    svc = _service(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    prepare = _containment.prepare_slices
    reset = AsyncMock(wraps=_containment.reset_disabled_instance_quota)

    async def held_prepare(*args):
        entered.set()
        await await_until(release)
        return await prepare(*args)

    monkeypatch.setattr(_containment, "prepare_slices", held_prepare)
    monkeypatch.setattr(_containment, "reset_disabled_instance_quota", reset)
    tasks = [asyncio.create_task(svc.prepare_principal_slice(ALICE.id))]
    disabling = asyncio.Event()

    async def disable():
        disabling.set()
        await svc.set_limits(_limits(admission_enabled=False))

    try:
        await await_until(entered)
        tasks.append(asyncio.create_task(disable()))
        await await_until(disabling)
        reset.assert_not_awaited()
    finally:
        release.set()
        await await_until(asyncio.gather(*tasks))
    reset.assert_awaited_once()
    assert not svc.enabled
    assert svc._slices._applied[_containment.instance_slice_name()] == ()
    assert await svc.prepare_principal_slice(ALICE.id) == ""


@pytest.mark.asyncio
async def test_disabled_tick_wakes_queued_turn_and_settles_attached_task(tmp_path, monkeypatch):
    svc = _service(tmp_path, crew_concurrency=1)
    task = await svc.admit(
        budget_key=ALICE.id,
        session_key="task",
        operation=OperationKind.TASK,
        idempotency_key="attached-task",
    )
    await task.attach_child("execution")
    queued = await svc.admit(
        budget_key=ALICE.id,
        session_key="chat",
        operation=OperationKind.CHAT_TURN,
        idempotency_key="queued-turn",
    )
    parked = await svc.admit(
        budget_key=ALICE.id,
        session_key="busy-chat",
        operation=OperationKind.CHAT_TURN,
        idempotency_key="parked-turn",
        park=True,
    )
    assert queued.queued and parked.queued
    waiting = asyncio.Event()
    wait_admitted = svc.wait_admitted

    async def wait(work_id):
        waiting.set()
        return await wait_admitted(work_id)

    monkeypatch.setattr(svc, "wait_admitted", wait)
    ran = []

    async def turn():
        ran.append(True)

    waiter = asyncio.create_task(queued.run(turn()))
    try:
        await await_until(waiting)
        await svc.set_limits(_limits(admission_enabled=False, crew_concurrency=1))
        svc.register_probe(OperationKind.TASK, lambda _id: OperationState.RUNNING)
        await svc.tick()
        for admission in (queued, parked):
            ended = await svc.current_work(admission.work.work_id)
            assert ended.state is OperationState.CANCELLED
            assert ended.reason is ReasonCode.POLICY_CHANGED
        with pytest.raises(ag.AdmissionEnded):
            await await_until(waiter)
        assert not ran
        assert svc.scheduler.usage(ALICE.id)[1].active == 1
        svc.register_probe(OperationKind.TASK, lambda _id: OperationState.COMPLETED)
        assert (await svc.tick())["children"] == 1
        assert (await svc.current_work(task.work.work_id)).state is OperationState.COMPLETED
        assert not svc._live
        assert (
            svc.scheduler.usage(ALICE.id)[1].active == svc.scheduler.usage(ALICE.id)[1].queued == 0
        )
        fresh = await svc.admit(
            budget_key=ALICE.id,
            session_key="fresh",
            operation=OperationKind.CHAT_TURN,
            idempotency_key="fresh-turn",
        )
        assert fresh.passthrough
        await fresh.run(turn())
        assert ran == [True]
    finally:
        waiter.cancel()
        await await_until(asyncio.gather(waiter, return_exceptions=True))


@pytest.mark.asyncio
@pytest.mark.parametrize("ticker_running", [False, True])
async def test_config_disable_keeps_ticker_until_attached_work_settles(
    tmp_path, monkeypatch, ticker_running
):
    svc = _service(tmp_path, crew_concurrency=1)
    task = await svc.admit(
        budget_key=ALICE.id,
        session_key="task",
        operation=OperationKind.TASK,
        idempotency_key="ticker-task",
    )
    await task.attach_child("execution")
    svc.register_probe(OperationKind.TASK, lambda _id: OperationState.RUNNING)
    monkeypatch.setattr(ag, "_SERVICE", svc)
    monkeypatch.setattr(ag, "_SERVICE_LOOP", asyncio.get_running_loop())
    monkeypatch.setattr(ag, "_ticker", None)
    monkeypatch.setattr(ag, "_last_config_at", float("-inf"))
    monkeypatch.setattr(
        ag, "_load_limits", lambda: _limits(admission_enabled=False, crew_concurrency=1)
    )
    start_ticker = ag._start_ticker
    monkeypatch.setattr(ag, "_start_ticker", lambda service: start_ticker(service, period=0))
    first_tick, release = asyncio.Event(), asyncio.Event()
    tick = svc.tick

    async def held_tick():
        result = await tick()
        first_tick.set()
        await await_until(release)
        return result

    monkeypatch.setattr(svc, "tick", held_tick)
    try:
        if ticker_running:
            ag._start_ticker(svc)
            await await_until(first_tick)
        assert await ag.current_service() is svc
        assert not svc.enabled
        svc.register_probe(OperationKind.TASK, lambda _id: OperationState.COMPLETED)
        release.set()
        assert ag._ticker is not None
        await await_until(ag._ticker)
        assert (await svc.current_work(task.work.work_id)).state is OperationState.COMPLETED
        assert not svc._live and svc.scheduler.usage(ALICE.id)[1].active == 0
        ag._start_ticker(svc)
        assert ag._ticker is None
    finally:
        release.set()
        if ag._ticker is not None:
            ag._ticker.cancel()
            await await_until(asyncio.gather(ag._ticker, return_exceptions=True))


@pytest.mark.asyncio
async def test_disabling_admission_preserves_running_merged_turn_outcome(tmp_path):
    svc = _service(tmp_path)
    work_ids = []
    for key in ("primary-turn", "follower-turn"):
        admission = await svc.admit(
            budget_key=ALICE.id,
            session_key="merged-chat",
            operation=OperationKind.CHAT_TURN,
            idempotency_key=key,
            park=True,
        )
        work_ids.append(admission.work.work_id)
    merged = await svc.start_parked(work_ids)
    assert merged is not None
    await svc.drain_promotions()

    async def turn():
        await svc.set_limits(_limits(admission_enabled=False))
        await svc.tick()
        assert svc.live_work(work_ids[0]).state is OperationState.RUNNING
        assert svc.live_work(work_ids[1]).state is OperationState.QUEUED

    await asyncio.wait_for(merged.run(turn()), timeout=30)
    for work_id in work_ids:
        assert (await svc.current_work(work_id)).state is OperationState.COMPLETED
    assert not svc._live
