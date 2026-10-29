"""Hard containment, queued cancellation, and unreadable admission configuration."""

from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from kiro_crew.config import loader
from kiro_crew.config import sections_shared_crew as config
from kiro_crew.shared_crew import admission_entry as entry
from kiro_crew.shared_crew import admission_gate as ag
from kiro_crew.shared_crew import containment as c
from kiro_crew.shared_crew.admission import Liveness, OperationKind, OperationState, ReasonCode
from kiro_crew.shared_crew.contracts import Principal
from kiro_crew.shared_crew.work_store import WorkStore
from kiro_crew.taskq.store import TaskStore

BUDGET = Principal.system("limits-test").id


@pytest.fixture
def service(tmp_path, manual_clock):
    tasks = TaskStore(tmp_path / "work.db", clock=manual_clock.time).open()
    report = c.ContainmentReport(
        platform="linux",
        available=True,
        reason="",
        cpu=True,
        memory=True,
        pids=True,
        hard_limits=True,
        hard_ready=True,
        slices_enabled=True,
    )
    svc = ag.AdmissionService(
        WorkStore(tasks, clock=manual_clock.time),
        limits=config.effective_limits(
            config.SharedCrewConfig(admission_enabled=True, hard_limits=True),
            auto_crew_concurrency=2,
        ),
        clock=manual_clock.time,
        containment_probe=lambda: report,
        slices=c.SliceManager(
            runner=lambda cmd: subprocess.CompletedProcess(cmd, 0, "", ""),
            systemctl="/usr/bin/systemctl",
        ),
        liveness=lambda ref: Liveness.STOPPED,
    )
    yield svc
    tasks.close()


async def admit(service, key="message-key-0001", *, park=False, operation=OperationKind.CHAT_TURN):
    return await service.admit(
        budget_key=BUDGET,
        session_key="dashboard:s1",
        operation=operation,
        idempotency_key=key,
        park=park,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["crew", "principal", "disabled", "manager"])
@pytest.mark.parametrize(
    "operation", [OperationKind.CHAT_TURN, OperationKind.SUBAGENT, OperationKind.WORKFLOW]
)
async def test_hard_admission_refuses_unapplied_slices(service, monkeypatch, failure, operation):
    def apply(name, limits, **kwargs):
        return name != (
            c.instance_slice_name() if failure == "crew" else c.principal_slice_name(BUDGET)
        )

    if failure == "disabled":
        await service.set_limits(replace(service.limits, containment_enabled=False))
    elif failure == "manager":
        service._slices = None
    else:
        monkeypatch.setattr(service._slices, "apply", apply)
    result = await admit(service, operation=operation)
    assert result.rejected and result.reason is ReasonCode.CONTAINMENT_UNAVAILABLE
    assert service.scheduler.active_tickets() == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["crew", "principal"])
async def test_accepted_turn_settles_if_slice_apply_fails(service, monkeypatch, failure):
    accepted = await admit(service)
    assert accepted.admitted
    monkeypatch.setattr(
        service._slices,
        "apply",
        lambda name, *args, **kwargs: name
        != (c.instance_slice_name() if failure == "crew" else c.principal_slice_name(BUDGET)),
    )
    ran = []

    async def body():
        ran.append(True)

    with pytest.raises(ag.AdmissionEnded):
        await asyncio.wait_for(accepted.run(body()), 30)
    work = await service.current_work(accepted.work.work_id)
    assert work.state is OperationState.CANCELLED
    assert work.reason is ReasonCode.CONTAINMENT_UNAVAILABLE
    assert ran == []
    assert service.scheduler.active_tickets() == ()


def test_hard_probe_requires_enabled_slices(monkeypatch):
    from kiro_crew import platform_compat, sandbox

    monkeypatch.setattr(c, "sys", type("Platform", (), {"platform": "linux"}))
    monkeypatch.setattr(sandbox, "_probe_cgroup_scope", lambda: (True, ""))
    monkeypatch.setattr(c, "_delegated_controllers", lambda: {"cpu", "memory", "pids"})
    monkeypatch.setattr(platform_compat, "trusted_system_bin_quiet", lambda name: "/bin/" + name)
    report = c.probe_containment(hard_limits=True, slices_enabled=False)
    assert not report.hard_ready
    assert report.refusal_reason is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled_index", [0, 1])
async def test_queue_drain_excludes_cancelled_text(service, tmp_path, monkeypatch, cancelled_index):
    from chat_test_helpers import _make_state

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.dashboard import chat_runner as runner

    state = _make_state(tmp_path)
    state.subagents = None
    state.broadcast_ws = MagicMock()
    slot = state.get_or_create_slot("s1")
    accepted = [await admit(service, key=f"parked-message-{i}", park=True) for i in range(2)]
    for i, work in enumerate(accepted):
        slot.queue_append(f"instructions-{i}")
        entry.stamp_queued_work(slot, slot._queue[-1]["id"], work)
    victim = accepted[cancelled_index].work
    await service.cancel(victim.work_id, expected_revision=victim.revision)
    monkeypatch.setattr(ag, "_SERVICE", service)
    cfg = KiroCrewConfig()
    cfg.dashboard.merge_queued_messages = True
    monkeypatch.setattr(runner.KiroCrewConfig, "load", lambda: cfg)
    captured = []

    def run(_state, _slot, prompt, **kwargs):
        captured.append(prompt)
        return asyncio.sleep(0)

    def spawn(_state, _slot, coro):
        coro.close()
        return MagicMock()

    monkeypatch.setattr(runner, "_run_chat", run)
    monkeypatch.setattr(runner, "spawn_guarded_turn", spawn)
    assert await runner._start_next_queued_turn(state, slot)
    assert captured == [f"instructions-{1 - cancelled_index}"]


@pytest.mark.asyncio
@pytest.mark.parametrize("document", ["{", "[]", "null", '"bad"'])
async def test_bad_config_keeps_current_limits(service, tmp_path, monkeypatch, document):
    path = tmp_path / "config.json"
    path.write_text(document, encoding="utf-8")
    monkeypatch.setattr(loader, "config_path", lambda: path)
    monkeypatch.setattr(ag, "_SERVICE", service)
    monkeypatch.setattr(ag, "_SERVICE_LOOP", asyncio.get_running_loop())
    monkeypatch.setattr(ag, "_last_config_at", float("-inf"))
    expected = service.limits
    assert await ag.current_service() is service
    assert service.limits == expected
    assert ag._configured_hard_limits() is True


def test_absent_config_uses_defaults_and_valid_config_loads(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr(loader, "config_path", lambda: path)
    assert config.load_shared_crew_config() == config.SharedCrewConfig()
    path.write_text(
        json.dumps({"dashboard": {"shared_crew": {"admission_enabled": True}}}), encoding="utf-8"
    )
    assert config.load_shared_crew_config().admission_enabled


@pytest.mark.asyncio
async def test_cancel_after_merge_stops_the_whole_unstarted_turn(service):
    first = await admit(service, key="merged-message-01", park=True)
    second = await admit(service, key="merged-message-02", park=True)
    merged = await service.start_parked([first.work.work_id, second.work.work_id])
    await service.drain_promotions()
    await service.cancel(second.work.work_id, expected_revision=second.work.revision)
    ran = []

    async def body():
        ran.append(True)

    with pytest.raises(ag.AdmissionEnded):
        await asyncio.wait_for(merged.run(body()), 30)
    assert ran == []
    assert service.scheduler.active_tickets() == ()
    assert (await service.current_work(second.work.work_id)).state is OperationState.CANCELLED


@pytest.mark.asyncio
async def test_mixed_cancelled_work_cannot_bypass_the_drain_guard(service):
    first = await admit(service, key="mixed-message-01", park=True)
    second = await admit(service, key="mixed-message-02", park=True)
    await service.cancel(first.work.work_id, expected_revision=first.work.revision)
    merged = await service.start_parked([first.work.work_id, second.work.work_id])
    assert merged.rejected and merged.reason is ReasonCode.CANCELLED_BY_USER
    assert service.scheduler.parked_tickets() == ()
    assert service.scheduler.active_tickets() == ()


@pytest.mark.asyncio
async def test_unreadable_config_preserves_limits(service, monkeypatch):
    def unreadable(path):
        raise PermissionError("unreadable test config")

    monkeypatch.setattr(loader, "read_config_text", unreadable)
    monkeypatch.setattr(ag, "_SERVICE", service)
    monkeypatch.setattr(ag, "_SERVICE_LOOP", asyncio.get_running_loop())
    monkeypatch.setattr(ag, "_last_config_at", float("-inf"))
    assert await ag.current_service() is service
    assert service.enabled and service.limits.hard_limits
    assert ag._configured_hard_limits()


@pytest.mark.asyncio
async def test_cold_bad_config_refuses_instead_of_defaulting_off(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text("{", encoding="utf-8")
    monkeypatch.setattr(loader, "config_path", lambda: path)
    monkeypatch.setattr(ag, "_SERVICE", None)
    monkeypatch.setattr(ag, "_last_config_at", float("-inf"))
    with pytest.raises(ValueError):
        await ag.current_service()


@pytest.mark.asyncio
async def test_soft_mode_keeps_best_effort_admission(service, monkeypatch):
    await service.set_limits(replace(service.limits, hard_limits=False))
    monkeypatch.setattr(service._slices, "apply", lambda *args, **kwargs: False)
    accepted = await admit(service)
    assert accepted.admitted

    async def body():
        return "ran"

    assert await asyncio.wait_for(accepted.run(body()), 30) == "ran"
