"""Shared Crew repair: A turn that returned after a runtime error settles failed."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.config.sections_shared_crew import effective_limits, shared_crew_config_from
from kiro_crew.shared_crew import admission_entry as entry
from kiro_crew.shared_crew import admission_gate as ag
from kiro_crew.shared_crew import turn_gate, work_store
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

REPO = Path(__file__).resolve().parent.parent
S = OperationState


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


@pytest.fixture
def service(tmp_path, monkeypatch):
    svc = _service(tmp_path)

    async def _current():
        return svc

    monkeypatch.setattr(ag, "current_service", _current)
    monkeypatch.setattr(ag, "_SERVICE", svc)
    monkeypatch.setattr("kiro_crew.dashboard.chat_slack.slot_is_live", lambda _s, _slot: True)
    yield svc
    entry._PENDING_SLOTS.clear()
    work_store.set_change_listener(None)


class _Slot:
    def __init__(self, key: str) -> None:
        self.key = key
        self.linked_session_key = ""
        self.messages: list[dict] = []
        self.total_messages = 0

    def append(self, role: str, text: str, cls: str = "", **_kw: object) -> dict:
        row = {"role": role, "content": text, "cls": cls}
        self.messages.append(row)
        self.total_messages += 1
        return row


_STATE = SimpleNamespace(push_slots_update=lambda: None)


# -- 4. a failed turn settles failed ------------------------------------------------------


@pytest.mark.parametrize(
    "stop_reason, rows, failed",
    [
        ("end_turn", [], False),
        ("", [{"role": "error"}], False),
        ("cancelled", [], False),
        ("error: runtime exited", [], True),
        ("timeout", [], True),
        (None, [{"role": "assistant"}], False),
        (None, [{"role": "error", "content": "runtime refused"}], True),
    ],
)
def test_turn_failure_rule(stop_reason, rows, failed):
    assert bool(turn_gate.turn_failure(stop_reason, rows)) is failed


@pytest.mark.asyncio
async def test_turn_that_errored_settles_failed_with_a_reason(service):
    slot = _Slot("tab-err")

    async def errored():
        slot.append("error", "the runtime could not start")

    with turn_gate.initiated_by(ALICE.id):
        await turn_gate.gate_turn(_STATE, slot, "hi", {}, errored, stop_reason=lambda: None)
    [work] = await service.list_work(session_key="dashboard:tab-err")
    assert work.state is S.FAILED
    assert work.reason is ReasonCode.RUNTIME_ERROR
    assert work.extra.get(ag.FAILURE_DETAIL_KEY)

    async def fine():
        slot.append("assistant", "ok")

    with turn_gate.initiated_by(ALICE.id):
        await turn_gate.gate_turn(
            _STATE, _Slot("tab-ok"), "hi", {}, fine, stop_reason=lambda: "end_turn"
        )
    [ok] = await service.list_work(session_key="dashboard:tab-ok")
    assert ok.state is S.COMPLETED and ok.reason is None


@pytest.mark.asyncio
async def test_turn_inside_handler_admitted_work_is_judged_too(service):
    """``POST /api/chat`` admits at the handler; the turn's own failure still settles it."""
    admission = await service.admit(
        budget_key=ALICE.id,
        session_key="dashboard:tab-h",
        operation=OperationKind.CHAT_TURN,
        idempotency_key="key-handler-1",
    )

    async def turn():
        await turn_gate.gate_turn(
            _STATE, _Slot("tab-h"), "hi", {}, _noop, stop_reason=lambda: "error: boom"
        )

    await admission.run(turn())
    [work] = await service.list_work(session_key="dashboard:tab-h")
    assert work.state is S.FAILED and work.reason is ReasonCode.RUNTIME_ERROR


async def _noop():
    return None


@pytest.mark.asyncio
async def test_raising_work_settles_failed_with_runtime_error(service):
    admission = await service.admit(
        budget_key=ALICE.id,
        session_key="s-raise",
        operation=OperationKind.CHAT_TURN,
        idempotency_key="key-raise-01",
    )

    async def boom():
        raise RuntimeError("runtime died")

    with pytest.raises(RuntimeError):
        await admission.run(boom())
    [work] = await service.list_work(session_key="s-raise")
    assert work.state is S.FAILED and work.reason is ReasonCode.RUNTIME_ERROR
