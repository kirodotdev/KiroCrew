"""Shared Crew repair: The chat work list reads by tab key while chat work is stored by session key."""

from __future__ import annotations

import asyncio
import subprocess
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config.sections_shared_crew import effective_limits, shared_crew_config_from
from kiro_crew.shared_crew import admission_entry as entry
from kiro_crew.shared_crew import admission_gate as ag
from kiro_crew.shared_crew import routes_admission, turn_gate, wiring, work_store
from kiro_crew.shared_crew.admission import (
    Liveness,
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
from kiro_crew.testing.wait import async_wait_until, await_until


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


# -- 1. the work list reads a tab key as its session ----------------------------------


def _work_app(state: object) -> web.Application:
    app = web.Application()
    app["state"] = state
    routes_admission.register(app)
    return app


@pytest.mark.asyncio
async def test_queued_chat_turn_is_listed_under_the_tab_key(service):
    """The chat asks with its slot key; the turn's work is stored as ``dashboard:<slot>``."""
    release = asyncio.Event()

    async def hold():
        await release.wait()

    slot_a, slot_b = _Slot("tab-a"), _Slot("tab-b")
    with turn_gate.initiated_by(ALICE.id):
        first = asyncio.create_task(turn_gate.gate_turn(_STATE, slot_a, "one", {}, hold))
    await async_wait_until(lambda: service.scheduler.usage(ALICE.id)[0].active == 1)
    with turn_gate.initiated_by(ALICE.id):
        second = asyncio.create_task(turn_gate.gate_turn(_STATE, slot_b, "two", {}, hold))
    await async_wait_until(lambda: service.scheduler.usage(ALICE.id)[0].queued == 1)
    state = SimpleNamespace(_slots={"tab-a": slot_a, "tab-b": slot_b})
    try:
        async with TestClient(TestServer(_work_app(state))) as client:
            by_tab = await (
                await client.get(routes_admission.WORK_PATH, params={"session_key": "tab-b"})
            ).json()
            by_session = await (
                await client.get(
                    routes_admission.WORK_PATH, params={"session_key": "dashboard:tab-b"}
                )
            ).json()
            running = await (
                await client.get(routes_admission.WORK_PATH, params={"session_key": "tab-a"})
            ).json()
    finally:
        release.set()
        await await_until(asyncio.gather(first, second))
    assert [w["state"] for w in by_tab["items"]] == ["queued"]
    assert by_tab["items"][0]["session_key"] == "dashboard:tab-b"
    assert by_tab == by_session
    assert [w["state"] for w in running["items"]] == ["running"]


def test_work_session_keys_cover_a_live_slot_and_a_closed_tab():
    channel_slot = SimpleNamespace(key="tab-c", linked_session_key="slack:1.2")
    state = SimpleNamespace(_slots={"tab-c": channel_slot})
    assert routes_admission.work_session_keys(state, "tab-c") == ["tab-c", "slack:1.2"]
    assert routes_admission.work_session_keys(state, "gone") == ["gone", "dashboard:gone"]
    assert routes_admission.work_session_keys(None, "dashboard:x") == ["dashboard:x"]


@pytest.mark.asyncio
async def test_every_work_write_reaches_tabs_as_a_frame_naming_the_tab(service, monkeypatch):
    sent: list[dict] = []
    state = SimpleNamespace(broadcast_ws=lambda kind, frame: sent.append(frame))
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_utils.dashboard_slot_key",
        lambda key: key.removeprefix("dashboard:"),
    )
    wiring.install(state)
    try:

        async def body():
            return None

        with turn_gate.initiated_by(ALICE.id):
            await turn_gate.gate_turn(_STATE, _Slot("tab-f"), "hi", {}, body)
    finally:
        wiring.uninstall()
    states = [f["work"]["state"] for f in sent]
    assert states[0] == "admitted" and states[-1] == "completed"
    assert "running" in states
    assert {f["type"] for f in sent} == {"shared_crew.work"}
    assert {f["slot_key"] for f in sent} == {"tab-f"}
    assert work_store._CHANGE_LISTENER is None


@pytest.mark.asyncio
async def test_open_work_is_listed_before_a_long_finished_history(service):
    from kiro_crew.shared_crew.admission import OperationState as S

    pool = [SimpleNamespace(work_id=f"d{i}", state=S.COMPLETED) for i in range(3)]
    pool.append(SimpleNamespace(work_id="q", state=S.QUEUED))

    class _Store:
        async def list(self, *, states=None, limit, **_kw):
            return [w for w in pool if states is None or w.state in states][:limit]

    service._store = _Store()
    rows = await service.list_work(limit=3)
    assert [w.work_id for w in rows] == ["q", "d0", "d1"]
