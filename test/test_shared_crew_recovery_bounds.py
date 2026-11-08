"""Restart, stop, initiator and containment truth for fair admission.

* A runtime that outlived a gateway crash is found under its principal slice,
  stopped, and holds its slot until proven stopped; capacity reads the
  reconciled truth right after a restart.
* The gateway's own stop records an interrupted turn and a never-started one
  as cancelled, never completed.
* Work an agent starts from inside a person's turn is charged to that person.
* ``hard_limits`` with admission off refuses rather than running unlimited; a
  raising probe reaches ``runtime_start``'s backstop.
* A new session is on disk before its work is accepted, and a turn a restart
  ended says so in its session.
* Idle per-principal slices are stopped and their drop-ins removed.

Real ``TaskStore`` in ``tmp_path``, ``ManualClock``, fake systemctl runners and
fake cgroup trees; nothing sleeps and nothing touches the host's systemd.
"""

from __future__ import annotations

import asyncio
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config.sections_shared_crew import effective_limits, shared_crew_config_from
from kiro_crew.shared_crew import admission_gate as ag
from kiro_crew.shared_crew import containment as _containment
from kiro_crew.shared_crew import host_hooks, routes_admission
from kiro_crew.shared_crew.admission import (
    ContainmentKind,
    Liveness,
    OperationKind,
    OperationState,
    ReasonCode,
    RuntimeOwnershipRef,
)
from kiro_crew.shared_crew.containment import ContainmentReport, SliceManager
from kiro_crew.shared_crew.contracts import (
    ACTOR_REQUEST_KEY,
    ActorContext,
    EnforcementLevel,
    Principal,
    Transport,
)
from kiro_crew.shared_crew.work_store import WorkStore
from kiro_crew.taskq.store import TaskStore
from kiro_crew.testing.clock import ManualClock

S = OperationState
BOUND = 30.0
A = Principal.system("alice-r2b").id
B = Principal.system("bob-r2b").id


def _report() -> ContainmentReport:
    return ContainmentReport(
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


class _Runner:
    """A fake ``systemctl`` that records every command it is asked to run."""

    def __init__(self, stdout: dict[str, str] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.stdout = stdout or {}

    def __call__(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        out = next((v for k, v in self.stdout.items() if k in cmd), "")
        return subprocess.CompletedProcess(cmd, 0, out, "")


def _open(path, clock) -> WorkStore:
    return WorkStore(TaskStore(path, clock=clock.time).open(), clock=clock.time)


def _limits(**cfg):
    raw = {"admission_enabled": True, "principal_concurrency": 2, "crew_concurrency": 2}
    raw.update(cfg)
    return effective_limits(shared_crew_config_from(raw), auto_crew_concurrency=8)


def _service(store, clock, *, runner=None, **kw) -> ag.AdmissionService:
    cfg = kw.pop("cfg", {})
    return ag.AdmissionService(
        store,
        limits=_limits(**cfg),
        clock=clock.time,
        containment_probe=_report,
        slices=SliceManager(runner=runner or _Runner(), systemctl="/usr/bin/systemctl"),
        **kw,
    )


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(start=80_000.0)


async def _admit(svc, who=A, session="dashboard:s1", key="r2b-key-000001"):
    return await svc.admit(
        budget_key=who,
        session_key=session,
        operation=OperationKind.CHAT_TURN,
        idempotency_key=key,
        digest=key,
    )


# -- a runtime that outlived the crash ----------------------------------


@pytest.mark.asyncio
async def test_restart_holds_and_reaps_a_runtime_alive_under_its_slice(
    tmp_path, clock, monkeypatch
):
    path = tmp_path / "t.db"
    store = _open(path, clock)
    svc = _service(store, clock, liveness=lambda ref: Liveness.STOPPED)
    adm = await _admit(svc)
    slice_name = _containment.principal_slice_name(A)
    ref = RuntimeOwnershipRef(
        execution_id=adm.work.work_id,
        containment=ContainmentKind.SYSTEMD_SCOPE,
        containment_unit=slice_name,
        liveness=Liveness.ALIVE,
    )
    store.advance_sync(adm.work, S.RUNNING, runtime=ref)
    store.tasks.close()

    # -- the gateway restarts; the pre-crash runtime scope still holds a process --
    states = {"kirocrew-rt-old.scope": "active"}
    stopped: list[str] = []

    def populated(name, **_kw):
        if name != slice_name:
            return []
        return sorted(u for u, st in states.items() if st == "active")

    def stop(unit, **_kw):
        stopped.append(unit)  # systemd answers the job; the unit goes inactive later
        return True

    monkeypatch.setattr(_containment, "populated_scopes", populated)
    monkeypatch.setattr(_containment, "stop_unit", stop)
    monkeypatch.setattr(_containment, "unit_active_state", lambda unit, **_kw: states.get(unit))
    store2 = _open(path, clock)
    svc2 = _service(store2, clock, slice_busy=populated)
    await svc2.ensure_ready()
    # Alive: the record keeps its slot, and the orphan was asked to stop.
    assert store2.get_sync(adm.work.work_id).state is S.RECONCILING
    assert svc2.capacity(A).crew.active == 1
    assert stopped == ["kirocrew-rt-old.scope"]
    # A later runtime of the same person in the same slice does not hold it open.
    states["kirocrew-rt-new.scope"] = "active"
    states["kirocrew-rt-old.scope"] = "inactive"
    assert (await svc2.tick())["proven"] == 1
    ended = store2.get_sync(adm.work.work_id)
    assert ended.state is S.UNKNOWN_OUTCOME
    assert ended.extra[ag.ORPHANS_REAPED_KEY] == 1
    assert svc2.capacity(A).crew.active == 0


@pytest.mark.asyncio
async def test_slice_liveness_reads_populated_runtime_scopes(tmp_path, monkeypatch):
    group = "user.slice/user-1.slice/user@1.service/k.slice/k-a.slice/k-a-p1.slice"
    base = tmp_path / group
    (base / "kirocrew-rt-live.scope").mkdir(parents=True)
    (base / "kirocrew-rt-live.scope" / "cgroup.events").write_text("populated 1\nfrozen 0\n")
    (base / "kirocrew-rt-done.scope").mkdir()
    (base / "kirocrew-rt-done.scope" / "cgroup.events").write_text("populated 0\nfrozen 0\n")
    (base / "other.scope").mkdir()
    (base / "other.scope" / "cgroup.events").write_text("populated 1\n")
    monkeypatch.setattr(
        "kiro_crew.platform_compat.trusted_system_bin", lambda name: "/usr/bin/systemctl"
    )
    runner = _Runner({"k-a-p1.slice": "/" + group + "\n"})
    found = _containment.populated_scopes("k-a-p1.slice", runner=runner, cgroup_root=str(tmp_path))
    assert found == ["kirocrew-rt-live.scope"]
    assert _containment.populated_scopes("not-a-unit", runner=runner) is None
    empty = _Runner({"k-a-p2.slice": "\n"})
    assert _containment.populated_scopes("k-a-p2.slice", runner=empty) == []


@pytest.mark.asyncio
async def test_capacity_after_restart_counts_reconciling_work(tmp_path, clock, monkeypatch):
    path = tmp_path / "t.db"
    store = _open(path, clock)
    svc = _service(store, clock, liveness=lambda ref: Liveness.STOPPED)
    adm = await _admit(svc)
    ref = RuntimeOwnershipRef(adm.work.work_id, containment_unit="kirocrew-rt-x.scope")
    store.advance_sync(adm.work, S.RUNNING, runtime=ref)
    store.tasks.close()
    store2 = _open(path, clock)
    svc2 = _service(store2, clock, liveness=lambda ref: Liveness.ALIVE)

    async def _current():
        return svc2

    monkeypatch.setattr(ag, "current_service", _current)
    app = web.Application()
    app.router.add_get("/cap", routes_admission.api_capacity)
    async with TestClient(TestServer(app)) as client:
        resp = await asyncio.wait_for(client.get("/cap"), BOUND)
        body = await resp.json()
    assert body["crew"]["active"] == 1


# -- the gateway's own stop ------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_records_interrupted_and_unstarted_turns_cancelled(tmp_path, clock):
    svc = _service(_open(tmp_path / "t.db", clock), clock, cfg={"principal_concurrency": 1})
    stopping = {"now": False}
    svc.set_shutdown_probe(lambda: stopping["now"])
    running = await _admit(svc, key="r2b-key-run001")
    queued = await _admit(svc, key="r2b-key-que001", session="dashboard:s2")
    assert running.admitted and queued.queued
    release = asyncio.Event()
    started = asyncio.Event()
    queued_ran: list[int] = []

    async def held():
        started.set()
        await release.wait()

    async def never():
        queued_ran.append(1)

    run_task = asyncio.ensure_future(running.run(held()))
    queued_task = asyncio.ensure_future(queued.run(never()))
    await asyncio.wait_for(started.wait(), BOUND)
    # SIGTERM: the process is stopping, and the running turn returns as it is cut.
    stopping["now"] = True
    release.set()
    await asyncio.wait_for(run_task, BOUND)
    with pytest.raises(ag.AdmissionEnded):
        await asyncio.wait_for(queued_task, BOUND)
    store = svc._store
    cut = store.get_sync(running.work.work_id)
    assert cut.state is S.CANCELLED
    assert cut.extra[ag.RECONCILE_DETAIL_KEY] == ag.STOPPED_WHILE_RUNNING
    never_ran = store.get_sync(queued.work.work_id)
    assert never_ran.state is S.CANCELLED
    assert never_ran.extra[ag.RECONCILE_DETAIL_KEY] == ag.STOPPED_BEFORE_START
    assert queued_ran == []


@pytest.mark.asyncio
async def test_a_turn_that_ends_normally_without_a_stop_is_completed(tmp_path, clock):
    svc = _service(_open(tmp_path / "t.db", clock), clock)
    svc.set_shutdown_probe(lambda: False)
    adm = await _admit(svc)
    await asyncio.wait_for(adm.run(asyncio.sleep(0)), BOUND)
    assert svc._store.get_sync(adm.work.work_id).state is S.COMPLETED


# -- the initiator survives internal auth ----------------------------------------


class _Req(dict):
    def __init__(self, headers: dict[str, str], actor: ActorContext) -> None:
        super().__init__()
        self.headers = headers
        self[ACTOR_REQUEST_KEY] = actor


@pytest.mark.asyncio
async def test_work_started_inside_a_persons_turn_is_charged_to_that_person(
    tmp_path, clock, monkeypatch
):
    svc = _service(_open(tmp_path / "t.db", clock), clock)
    monkeypatch.setattr(ag, "_SERVICE", svc)
    internal = ActorContext(
        principal=Principal.system("internal"), request_id="r", transport=Transport.INTERNAL
    )
    svc.bind_session("dashboard:alice-tab", A, "w-root")
    signed = {"tok-alice": "dashboard:alice-tab", "tok-nobody": "dashboard:nobody"}
    from kiro_crew.shared_crew import internal_caller

    monkeypatch.setattr(internal_caller, "_verify_token", lambda token: signed.get(token, ""))
    # A declared key is proven by the transport's shared check, which verifies
    # the same signed token.
    monkeypatch.setattr(
        "kiro_crew.member_memory_auth.verify_session_token", lambda token: signed.get(token, "")
    )

    async def budget(headers: dict[str, str], **marks: object) -> str:
        req = _Req(headers, internal)
        req.update(marks)
        await ag.attest_internal_session(req)
        return ag.actor_budget(req, automation=True)

    unknown = Principal.unknown().id
    # Kernel-attested or token-proven calling session: charged to its person.
    assert await budget({"X-Session-Key": "dashboard:alice-tab"}, peer_verified=True) == A
    assert (
        await budget({"X-Session-Key": "dashboard:alice-tab", "X-Session-Token": "tok-alice"}) == A
    )
    assert await budget({"X-Session-Token": "tok-alice"}) == A
    # The header alone is the caller's word: it names no one's budget.
    assert await budget({"X-Session-Key": "dashboard:alice-tab"}) == unknown
    # A token for another session, a forged one, an unbound session, or none: Unknown.
    assert (
        await budget({"X-Session-Key": "dashboard:alice-tab", "X-Session-Token": "tok-nobody"})
        == unknown
    )
    assert (
        await budget({"X-Session-Key": "dashboard:alice-tab", "X-Session-Token": "forged"})
        == unknown
    )
    assert await budget({"X-Session-Token": "tok-nobody"}) == unknown
    assert await budget({}) == unknown
    # None of them reaches the automation budget, a second one for anybody.
    assert ag.AUTOMATION_PRINCIPAL.id != unknown
    # A's agent spending on background work spends A's own budget.
    from kiro_crew.shared_crew import admission_entry

    async def _current():
        return svc

    monkeypatch.setattr(ag, "current_service", _current)
    inside = _Req({"X-Session-Key": "dashboard:alice-tab"}, internal)
    inside["internal_auth"] = True
    inside["peer_verified"] = True
    run = await admission_entry.admit_background_run(
        inside, operation=OperationKind.TASK, session_key="taskrunner:x", probe=None
    )
    assert run.admitted and run.work.budget_key == A
    claimed = _Req({"X-Session-Key": "dashboard:alice-tab"}, internal)
    claimed["internal_auth"] = True
    run2 = await admission_entry.admit_background_run(
        claimed, operation=OperationKind.TASK, session_key="taskrunner:y", probe=None
    )
    assert run2.admitted and run2.work.budget_key == unknown


# -- hard limits never run unlimited -------------------------------------


@pytest.mark.asyncio
async def test_hard_limits_with_admission_off_refuses_every_start(tmp_path, clock, monkeypatch):
    from kiro_crew.acp.runtime_start import _refuse_without_containment

    limits = effective_limits(
        shared_crew_config_from({"admission_enabled": False, "hard_limits": True}),
        auto_crew_concurrency=2,
    )
    svc = ag.AdmissionService(None, limits=limits, clock=clock.time)
    monkeypatch.setattr(_containment, "_HARD_MODE_REPORT", None)
    refused = await _admit(svc)
    assert refused.rejected and refused.reason is ReasonCode.CONTAINMENT_UNAVAILABLE

    async def _current():
        return svc

    monkeypatch.setattr(ag, "current_service", _current)
    report = await ag.startup_check()
    assert report is not None and report.refusal_reason == _containment.REASON_ADMISSION_OFF
    assert _containment.hard_mode_refusal() == _containment.REASON_ADMISSION_OFF
    with pytest.raises(_containment.ContainmentUnavailable):
        _refuse_without_containment()
    # With hard limits off, admission off stays the single-operator pass-through.
    plain = ag.AdmissionService(
        None,
        limits=effective_limits(shared_crew_config_from({}), auto_crew_concurrency=2),
    )
    monkeypatch.setattr(_containment, "_HARD_MODE_REPORT", None)
    assert (await _admit(plain)).passthrough
    assert plain.published_report() is None


def test_an_unprobed_hard_limit_service_refuses_runtime_starts(tmp_path, clock, monkeypatch):
    """Installed before its first probe, hard mode refuses instead of failing open."""
    from kiro_crew.acp.runtime_start import _refuse_without_containment

    svc = ag.AdmissionService(
        _open(tmp_path / "t.db", clock), limits=_limits(hard_limits=True), clock=clock.time
    )
    monkeypatch.setattr(_containment, "_HARD_MODE_REPORT", None)
    monkeypatch.setattr(ag, "_SERVICE", None)
    ag.install_service(svc)
    try:
        assert _containment.hard_mode_refusal() == _containment.REASON_UNKNOWN
        with pytest.raises(_containment.ContainmentUnavailable):
            _refuse_without_containment()
    finally:
        ag.install_service(None)


@pytest.mark.asyncio
async def test_a_raising_probe_reaches_the_runtime_start_backstop(tmp_path, clock, monkeypatch):
    from kiro_crew.acp.runtime_start import _refuse_without_containment

    def boom():
        raise OSError("bus gone")

    svc = ag.AdmissionService(
        _open(tmp_path / "t.db", clock),
        limits=_limits(hard_limits=True),
        clock=clock.time,
        containment_probe=boom,
    )
    monkeypatch.setattr(ag, "_SERVICE", svc)
    monkeypatch.setattr(_containment, "_HARD_MODE_REPORT", None)
    await svc.containment_report(refresh=True)
    with pytest.raises(_containment.ContainmentUnavailable) as caught:
        _refuse_without_containment()
    assert caught.value.reason == _containment.REASON_UNKNOWN


# -- a session is on disk before its work, and a lost turn says so ----------------


@pytest.fixture
def dash_state(tmp_path, monkeypatch):
    from chat_test_helpers import _make_state

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    st.push_slots_update = MagicMock()
    st.owner_id = ""
    return st


@pytest.mark.asyncio
async def test_a_new_session_is_recorded_before_its_work_is_accepted(dash_state):
    from kiro_crew.dashboard.chat_utils import slot_history_key

    slot = dash_state.get_or_create_slot("fresh-tab")
    key = slot_history_key(slot)
    assert not dash_state.conversation_log._read_metadata(key)
    assert await asyncio.wait_for(host_hooks.ensure_slot_recorded(dash_state, slot), BOUND)
    assert dash_state.conversation_log._read_metadata(key)


@pytest.mark.asyncio
async def test_a_turn_the_restart_ended_says_so_in_its_session():
    rows: list[tuple[str, str]] = []
    slot = SimpleNamespace(
        key="tab",
        linked_session_key="",
        messages=[],
        append=lambda r, t, c, **_kw: rows.append((r, t)),
    )
    state = SimpleNamespace(_slots={"tab": slot})
    note = host_hooks.reconcile_note(state)

    def work(state_, *, extra=None, reason=None, op=OperationKind.CHAT_TURN):
        return SimpleNamespace(
            operation=op, state=state_, extra=extra or {}, reason=reason, session_key="tab"
        )

    note(work(S.UNKNOWN_OUTCOME))
    note(work(S.CANCELLED))
    note(work(S.CANCELLED, extra={ag.PARKED_KEY: True}))
    note(work(S.CANCELLED, reason=ReasonCode.CANCELLED_BY_USER))
    note(work(S.UNKNOWN_OUTCOME, op=OperationKind.TASK))
    host_hooks.mark_sessions_restored(state)
    await asyncio.wait_for(state._shared_crew_note_delivery, BOUND)
    assert rows == [
        ("error", host_hooks.NOTE_UNKNOWN),
        ("error", host_hooks.NOTE_NOT_STARTED),
    ]


@pytest.mark.asyncio
async def test_reconciliation_tells_the_session_of_a_turn_it_ended(tmp_path, clock):
    path = tmp_path / "t.db"
    store = _open(path, clock)
    svc = _service(store, clock, liveness=lambda ref: Liveness.STOPPED)
    adm = await _admit(svc)
    store.advance_sync(adm.work, S.RUNNING, runtime=RuntimeOwnershipRef(adm.work.work_id))
    store.tasks.close()
    store2 = _open(path, clock)
    told: list[str] = []
    svc2 = _service(store2, clock, liveness=lambda ref: Liveness.STOPPED)
    svc2.set_reconcile_listener(lambda work, _payload: told.append(work.state.value))
    await svc2.ensure_ready()
    assert told == [S.UNKNOWN_OUTCOME.value]


# -- idle per-principal slices are released ---------------------------------------


@pytest.mark.asyncio
async def test_idle_principal_slices_are_stopped_and_their_drop_ins_removed(tmp_path, clock):
    runner = _Runner()
    busy: dict[str, list[str] | None] = {}
    svc = _service(
        _open(tmp_path / "t.db", clock),
        clock,
        runner=runner,
        liveness=lambda ref: Liveness.STOPPED,
        slice_busy=lambda name: busy.get(name, []),
        cfg={"containment_enabled": True},
    )
    adm = await _admit(svc)
    name = await svc.prepare_principal_slice(A)
    assert name and svc._slices.is_ready(name)
    # While A's work is live, the slice is kept.
    assert await svc.sweep_slices(clock.time() + 10 * ag.SLICE_IDLE_SECONDS) == 0
    await asyncio.wait_for(adm.run(asyncio.sleep(0)), BOUND)
    now = clock.time()
    assert await svc.sweep_slices(now) == 0  # idle clock starts
    busy[name] = ["kirocrew-rt-late.scope"]
    assert await svc.sweep_slices(now + ag.SLICE_IDLE_SECONDS) == 0  # a scope still runs
    assert svc._slices.is_ready(name)
    busy[name] = []
    runner.calls.clear()
    assert await svc.sweep_slices(now + ag.SLICE_IDLE_SECONDS) == 1
    assert not svc._slices.is_ready(name)
    verbs = [c[2] for c in runner.calls]
    assert verbs == ["stop", "revert"]
    assert all(c[-1] == name for c in runner.calls)


@pytest.mark.asyncio
async def test_slices_an_earlier_process_left_are_released_at_start(tmp_path, clock):
    stem = _containment.instance_slice_name()[: -len(".slice")]
    left = f"{stem}-pabcdef012345.slice"
    busy_left = f"{stem}-pbeefbeefbeef.slice"
    runner = _Runner({"list-units": f"{left} loaded active active\n{busy_left} loaded\n"})
    svc = _service(
        _open(tmp_path / "t.db", clock),
        clock,
        runner=runner,
        liveness=lambda ref: Liveness.STOPPED,
        slice_busy=lambda name: ["kirocrew-rt-x.scope"] if name == busy_left else [],
        cfg={"containment_enabled": True},
    )
    assert await svc.release_stale_slices() == 1
    stopped = [c[-1] for c in runner.calls if c[2] == "stop"]
    assert stopped == [left]


@pytest.mark.asyncio
async def test_a_stop_that_interrupted_a_running_turn_releases_its_slice(
    tmp_path, clock, monkeypatch
):
    runner = _Runner()
    busy: dict[str, list[str]] = {}
    svc = _service(
        _open(tmp_path / "t.db", clock),
        clock,
        runner=runner,
        liveness=lambda ref: Liveness.STOPPED,
        slice_busy=lambda name: busy.get(name, []),
        cfg={"containment_enabled": True},
    )
    monkeypatch.setattr(ag, "_SERVICE", svc)
    adm = await _admit(svc)
    release = asyncio.Event()
    started = asyncio.Event()

    async def held():
        started.set()
        await release.wait()

    turn = asyncio.ensure_future(adm.run(held()))
    await asyncio.wait_for(started.wait(), BOUND)
    name = await svc.prepare_principal_slice(A)
    assert name and svc._slices.is_ready(name)
    # The stop finds A's turn still live and its runtime scope still emptying.
    busy[name] = ["kirocrew-rt-a.scope"]
    pauses: list[float] = []

    async def pause(seconds: float) -> None:
        pauses.append(seconds)
        busy[name] = []  # the runtime the stop ended has exited

    runner.calls.clear()
    # A's slice, then the emptied instance slice.
    assert await ag.shutdown_cleanup(rounds=3, interval=0.25, pause=pause) == 2
    assert pauses == [0.25]
    assert not svc._slices.is_ready(name)
    instance = _containment.instance_slice_name()
    released = [(c[2], c[-1]) for c in runner.calls if c[2] in ("stop", "revert")]
    assert released == [
        ("stop", name),
        ("revert", name),
        ("stop", instance),
        ("revert", instance),
    ]
    release.set()
    await asyncio.wait_for(turn, BOUND)


@pytest.mark.asyncio
async def test_a_stop_keeps_a_slice_a_runtime_still_holds(tmp_path, clock, monkeypatch):
    runner = _Runner()
    svc = _service(
        _open(tmp_path / "t.db", clock),
        clock,
        runner=runner,
        liveness=lambda ref: Liveness.STOPPED,
        slice_busy=lambda name: ["kirocrew-rt-a.scope"],
        cfg={"containment_enabled": True},
    )
    monkeypatch.setattr(ag, "_SERVICE", svc)
    name = await svc.prepare_principal_slice(A)
    pauses: list[float] = []

    async def pause(seconds: float) -> None:
        pauses.append(seconds)

    runner.calls.clear()
    assert await ag.shutdown_cleanup(rounds=3, interval=0.25, pause=pause) == 0
    assert pauses == [0.25, 0.25]
    assert svc._slices.is_ready(name)
    assert not [c for c in runner.calls if c[2] in ("stop", "revert")]


def test_the_instance_slice_is_kept_while_anything_runs_under_it():
    from kiro_crew.shared_crew import admission_runtime

    runner = _Runner()
    slices = SliceManager(runner=runner, systemctl="/usr/bin/systemctl")
    instance = _containment.instance_slice_name()
    slices._applied[instance] = ()
    assert admission_runtime.release_instance_slice(slices, populated=lambda _n: True) == 0
    assert admission_runtime.release_instance_slice(slices, populated=lambda _n: None) == 0
    assert instance in slices.applied()
    assert admission_runtime.release_instance_slice(slices, populated=lambda _n: False) == 1
    assert instance not in slices.applied()
