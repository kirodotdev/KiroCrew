"""The admission service: durable accepted work, revalidation, cancellation, deadlines,
crash reconciliation, children, hard-limit mode and capability truthfulness.

Every test drives a real ``TaskStore`` in ``tmp_path`` with a ``ManualClock`` and
explicit events; nothing sleeps and nothing touches the host's cgroups.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.config import sections_shared_crew
from kiro_crew.config.sections_shared_crew import (
    SharedCrewConfig,
    effective_limits,
    shared_crew_config_from,
)
from kiro_crew.shared_crew import admission_gate as ag
from kiro_crew.shared_crew.admission import (
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
A = Principal.system("alice-test").id
B = Principal.system("bob-test").id
BOUND = 30.0


def _report(**kw) -> ContainmentReport:
    base = dict(
        platform="linux",
        available=True,
        reason="",
        cpu=True,
        memory=True,
        pids=True,
        level=EnforcementLevel.BEST_EFFORT,
        hard_limits=False,
        hard_ready=True,
        slices_enabled=True,
    )
    base.update(kw)
    return ContainmentReport(**base)


class _Runner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(start=10_000.0)


def _open(path: Path, clock: ManualClock) -> WorkStore:
    return WorkStore(TaskStore(path, clock=clock.time).open(), clock=clock.time)


def _service(store, clock, *, report=None, liveness=None, child_probe=None, **cfg):
    raw = {"admission_enabled": True, "principal_concurrency": 2, "crew_concurrency": 3}
    fixed = {k.upper(): cfg.pop(k) for k in list(cfg) if hasattr(sections_shared_crew, k.upper())}
    raw.update(cfg)
    with patch.dict(sections_shared_crew.__dict__, fixed):
        limits = effective_limits(shared_crew_config_from(raw), auto_crew_concurrency=8)
    runner = _Runner()
    svc = ag.AdmissionService(
        store,
        limits=limits,
        clock=clock.time,
        containment_probe=lambda: report or _report(),
        slices=SliceManager(runner=runner, systemctl="/usr/bin/systemctl"),
        liveness=liveness or (lambda ref: Liveness.STOPPED),
        child_probe=child_probe,
    )
    svc.runner = runner  # type: ignore[attr-defined]
    return svc


async def _admit(svc, who=A, session="s1", key=None, digest="d", **kw):
    return await svc.admit(
        budget_key=who,
        session_key=session,
        operation=kw.pop("operation", OperationKind.CHAT_TURN),
        idempotency_key=key or f"key-{who[-6:]}-{session}-{id(kw)}-{digest}",
        digest=digest,
        **kw,
    )


class _Gate:
    """A coroutine body the test releases explicitly."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.runs = 0

    async def body(self, value="done"):
        self.runs += 1
        self.started.set()
        await self.release.wait()
        return value


async def _bounded(aw):
    return await asyncio.wait_for(aw, BOUND)


@pytest.mark.asyncio
async def test_disabled_is_passthrough_and_writes_nothing(tmp_path, clock):
    store = _open(tmp_path / "t.db", clock)
    limits = effective_limits(SharedCrewConfig(), auto_crew_concurrency=8)
    svc = ag.AdmissionService(store, limits=limits, clock=clock.time)
    adm = await _admit(svc)
    assert adm.passthrough
    assert await adm.run(asyncio.sleep(0, result=7)) == 7
    assert store.list_sync() == []
    cap = svc.capacity(A)
    assert cap.enforcement is EnforcementLevel.NONE


@pytest.mark.asyncio
async def test_admit_run_complete_release_and_durable_record(tmp_path, clock):
    store = _open(tmp_path / "t.db", clock)
    svc = _service(store, clock)
    adm = await _admit(svc)
    assert adm.admitted
    assert store.get_sync(adm.work.work_id).state is S.ADMITTED  # written before the ack
    gate = _Gate()
    runner = asyncio.ensure_future(adm.run(gate.body()))
    await _bounded(gate.started.wait())
    assert store.get_sync(adm.work.work_id).state is S.RUNNING
    assert svc._scheduler.is_active(adm.work.work_id)
    gate.release.set()
    assert await _bounded(runner) == "done"
    final = store.get_sync(adm.work.work_id)
    assert final.state is S.COMPLETED and final.runtime.proven_stopped
    assert not svc._scheduler.is_active(adm.work.work_id)
    # The principal slice was sized off-loop before the work ran.
    assert any("set-property" in c for c in svc.runner.calls)


@pytest.mark.asyncio
async def test_same_principal_tabs_and_sessions_cannot_multiply_budget(tmp_path, clock):
    store = _open(tmp_path / "t.db", clock)
    svc = _service(store, clock)
    first = [await _admit(svc, session=f"tab{i}", digest=str(i)) for i in range(3)]
    assert [a.decision for a in first] == ["admitted", "admitted", "queued"]
    assert first[2].reason is ReasonCode.PRINCIPAL_ACTIVE_LIMIT
    other = await _admit(svc, who=B, session="b1")
    assert other.admitted
    gates = [_Gate() for _ in range(3)]
    tasks = [asyncio.ensure_future(a.run(g.body())) for a, g in zip(first, gates)]
    await _bounded(gates[0].started.wait())
    await _bounded(gates[1].started.wait())
    assert not gates[2].started.is_set()
    gates[0].release.set()
    await _bounded(gates[2].started.wait())  # admitted once a slot freed
    for g in gates:
        g.release.set()
    await _bounded(asyncio.gather(*tasks))
    assert sum(g.runs for g in gates) == 3


@pytest.mark.asyncio
async def test_duplicate_submission_is_idempotent_and_conflict_is_409(tmp_path, clock):
    store = _open(tmp_path / "t.db", clock)
    svc = _service(store, clock)
    one = await _admit(svc, key="client-key-0001", digest="same")
    two = await _admit(svc, key="client-key-0001", digest="same")
    assert two.replayed and two.work.work_id == one.work.work_id
    assert len(store.list_sync()) == 1
    clash = await _admit(svc, key="client-key-0001", digest="different")
    assert clash.rejected and clash.reason is ReasonCode.IDEMPOTENCY_CONFLICT
    assert clash.refusal_status() == 409
    assert clash.refusal_body()["current"]["work_id"] == one.work.work_id
    # Running the replayed handle twice still runs the prompt once.
    gate = _Gate()
    gate.release.set()
    await _bounded(one.run(gate.body()))
    with pytest.raises(ag.AdmissionEnded):
        await _bounded(two.run(gate.body()))
    assert gate.runs == 1


@pytest.mark.asyncio
async def test_queue_expiry_ends_unstarted_work(tmp_path, clock):
    store = _open(tmp_path / "t.db", clock)
    svc = _service(store, clock, principal_concurrency=1)
    hold = await _admit(svc, session="a")
    queued = await _admit(svc, session="b")
    assert queued.queued
    gate = _Gate()
    waiter = asyncio.ensure_future(queued.run(gate.body()))
    clock.advance(299)
    assert (await svc.tick())["expired"] == 0
    clock.advance(1)
    assert (await svc.tick())["expired"] == 1
    with pytest.raises(ag.AdmissionEnded) as ended:
        await _bounded(waiter)
    assert ended.value.work.state is S.EXPIRED
    assert ended.value.work.reason is ReasonCode.QUEUE_EXPIRED
    assert gate.runs == 0
    assert hold.admitted


@pytest.mark.asyncio
async def test_promotion_expires_a_ticket_whose_expiry_passed_before_the_tick(tmp_path, clock):
    store = _open(tmp_path / "t.db", clock)
    svc = _service(store, clock, principal_concurrency=1)
    hold = await _admit(svc, session="a")
    queued = await _admit(svc, session="b")
    assert queued.queued
    gate = _Gate()
    waiter = asyncio.ensure_future(queued.run(gate.body()))
    clock.advance(300)
    first = _Gate()
    first.release.set()
    await _bounded(hold.run(first.body()))
    await _bounded(svc.drain_promotions())
    with pytest.raises(ag.AdmissionEnded) as ended:
        await _bounded(waiter)
    assert ended.value.work.state is S.EXPIRED
    assert ended.value.work.reason is ReasonCode.QUEUE_EXPIRED
    assert gate.runs == 0
    assert not svc._scheduler.is_active(queued.work.work_id)


@pytest.mark.asyncio
async def test_bounded_queue_rejection_carries_reason_and_retry_after(tmp_path, clock):
    store = _open(tmp_path / "t.db", clock)
    svc = _service(store, clock, principal_concurrency=1, principal_queue_depth=1)
    await _admit(svc, session="a")
    await _admit(svc, session="b")
    full = await _admit(svc, session="c")
    assert full.rejected and full.reason is ReasonCode.PRINCIPAL_QUEUE_FULL
    assert full.refusal_status() == 429
    assert full.refusal_headers() == {"Retry-After": "10"}
    body = full.refusal_body()
    assert body["code"] == "principal_queue_full"
    assert body["capacity"]["principal"]["queue_limit"] == 1
    assert len(store.list_sync()) == 2  # a refusal was never accepted


@pytest.mark.asyncio
async def test_request_size_and_storage_limits(tmp_path, clock):
    store = _open(tmp_path / "t.db", clock)
    svc = _service(store, clock, max_request_bytes=2048, principal_storage_mb=1)
    big = await _admit(svc, request_bytes=4096)
    assert big.rejected and big.reason is ReasonCode.REQUEST_TOO_LARGE
    assert big.refusal_status() == 413
    await svc.charge_storage(A, 1024 * 1024)
    over = await _admit(svc, request_bytes=10)
    assert over.rejected and over.reason is ReasonCode.STORAGE_LIMIT
    assert over.refusal_status() == 507
    assert (await _admit(svc, who=B)).admitted


@pytest.mark.asyncio
async def test_revalidation_refusal_cancels_before_start(tmp_path, clock):
    store = _open(tmp_path / "t.db", clock)
    svc = _service(store, clock)
    adm = await _admit(svc)
    gate = _Gate()

    async def revoked():
        return ReasonCode.GRANT_REVOKED

    with pytest.raises(ag.AdmissionEnded) as ended:
        await _bounded(adm.run(gate.body(), revalidate=revoked))
    assert ended.value.work.state is S.CANCELLED
    assert ended.value.work.reason is ReasonCode.GRANT_REVOKED
    assert gate.runs == 0 and not svc._scheduler.is_active(adm.work.work_id)


@pytest.mark.asyncio
async def test_policy_change_while_queued_prevents_start(tmp_path, clock):
    store = _open(tmp_path / "t.db", clock)
    svc = _service(store, clock, principal_concurrency=1)
    first = await _admit(svc, session="a")
    queued = await _admit(svc, session="b")
    gate1, gate2 = _Gate(), _Gate()
    started = asyncio.ensure_future(first.run(gate1.body()))
    await _bounded(gate1.started.wait())
    await svc.set_limits(
        effective_limits(
            shared_crew_config_from(
                {
                    "admission_enabled": True,
                    "principal_concurrency": 1,
                    "crew_concurrency": 3,
                    "containment_enabled": False,
                }
            ),
            auto_crew_concurrency=8,
        )
    )
    gate1.release.set()
    await _bounded(started)  # already running: the change does not stop it
    with pytest.raises(ag.AdmissionEnded) as ended:
        await _bounded(queued.run(gate2.body()))
    assert ended.value.work.reason is ReasonCode.POLICY_CHANGED and gate2.runs == 0


@pytest.mark.asyncio
async def test_cancel_queued_and_running_with_revision_checks(tmp_path, clock):
    store = _open(tmp_path / "t.db", clock)
    svc = _service(store, clock, principal_concurrency=1)
    running = await _admit(svc, session="a")
    queued = await _admit(svc, session="b")
    with pytest.raises(ag.StaleRevision):
        await svc.cancel(queued.work.work_id, expected_revision=queued.work.revision + 5)
    cancelled = await svc.cancel(queued.work.work_id, expected_revision=queued.work.revision)
    assert cancelled.state is S.CANCELLED and cancelled.reason is ReasonCode.CANCELLED_BY_USER
    gate = _Gate()
    task = asyncio.ensure_future(running.run(gate.body()))
    await _bounded(gate.started.wait())
    current = svc.live_work(running.work.work_id)
    nxt = await svc.cancel(current.work_id, expected_revision=current.revision)
    assert nxt.state is S.CANCELLING
    assert svc._scheduler.is_active(current.work_id)  # not freed until the run stopped
    with pytest.raises(asyncio.CancelledError):
        await _bounded(task)
    final = store.get_sync(current.work_id)
    assert final.state is S.CANCELLED and final.reason is ReasonCode.CANCELLED_BY_USER
    assert not svc._scheduler.is_active(current.work_id)
    with pytest.raises(ag.InvalidTransition):
        await svc.cancel(current.work_id, expected_revision=final.revision)


@pytest.mark.asyncio
async def test_run_deadline_cancels_and_releases(tmp_path, clock):
    store = _open(tmp_path / "t.db", clock)
    svc = _service(store, clock, root_deadline_seconds=60)
    adm = await _admit(svc)
    gate = _Gate()
    task = asyncio.ensure_future(adm.run(gate.body()))
    await _bounded(gate.started.wait())
    clock.advance(59)
    assert (await svc.tick())["deadline"] == 0
    clock.advance(1)
    assert (await svc.tick())["deadline"] == 1
    with pytest.raises(asyncio.CancelledError):
        await _bounded(task)
    final = store.get_sync(adm.work.work_id)
    assert (final.state, final.reason) == (S.CANCELLED, ReasonCode.RUN_DEADLINE)
    assert not svc._scheduler.is_active(adm.work.work_id)


@pytest.mark.asyncio
async def test_hard_mode_fails_closed_on_missing_delegation(tmp_path, clock):
    store = _open(tmp_path / "t.db", clock)
    report = _report(
        available=False,
        reason="controllers_not_delegated",
        memory=False,
        pids=False,
        cpu=False,
        level=EnforcementLevel.NONE,
        hard_limits=True,
        hard_ready=False,
    )
    svc = _service(store, clock, report=report, hard_limits=True)
    adm = await _admit(svc)
    assert adm.rejected and adm.reason is ReasonCode.CONTAINMENT_UNAVAILABLE
    assert adm.refusal_status() == 503
    assert "controllers_not_delegated" in adm.refusal_body()["error"]
    assert store.list_sync() == []
    # Hard mode off: the same host admits and reports the weaker guarantee.
    soft = _service(store, clock, report=_report(**{**report.__dict__, "hard_limits": False}))
    assert (await _admit(soft, key="soft-key-0001")).admitted
    assert soft.capacity(A).enforcement is EnforcementLevel.BEST_EFFORT


def test_capability_payload_is_truthful():
    rep = _report()
    body = rep.to_dict()
    assert body["level"] == "best_effort"  # never "hard": adversarial escape is open
    assert body["adversarial_escape_closed"] is False
    assert body["controls"]["io"] == "unavailable"
    assert body["controls"]["disk"] == "app_accounting"
    assert body["state"] == "available"
    none = ContainmentReport(
        platform="darwin", available=False, reason="not_linux", cpu=False, memory=False, pids=False
    ).to_dict()
    assert none["level"] == "none" and none["state"] == "unavailable"
    assert none["controls"]["cpu"] == "unavailable"


@pytest.mark.asyncio
async def test_crash_reconciliation_never_replays_and_holds_live_runtime(tmp_path, clock):
    path = tmp_path / "t.db"
    store = _open(path, clock)
    svc = _service(store, clock, principal_concurrency=3, crew_concurrency=3)
    # Crash before accept: nothing written, so a retry later is new work once.
    queued_hold = [await _admit(svc, session=f"h{i}", digest=f"h{i}") for i in range(3)]
    queued = await _admit(svc, session="q", digest="q")  # accepted, queued
    assert queued.queued
    admitted_not_launched = queued_hold[0]  # accepted + admitted, never launched
    ran = queued_hold[1]
    ran_alive = queued_hold[2]
    w_ran = store.advance_sync(ran.work, S.RUNNING, runtime=RuntimeOwnershipRef(ran.work.work_id))
    alive_ref = RuntimeOwnershipRef(ran_alive.work.work_id, containment_unit="kirocrew-rt-x.scope")
    store.advance_sync(ran_alive.work, S.RUNNING, runtime=alive_ref)
    store.tasks.close()
    # -- the gateway restarts --
    store2 = _open(path, clock)
    alive = {"x": Liveness.ALIVE}
    svc2 = _service(
        store2,
        clock,
        principal_concurrency=3,
        crew_concurrency=1,
        liveness=lambda ref: alive["x"] if ref.containment_unit else Liveness.STOPPED,
    )
    await svc2.ensure_ready()
    by_id = {w.work_id: w for w in store2.list_sync()}
    assert by_id[queued.work.work_id].state is S.CANCELLED
    assert (
        by_id[queued.work.work_id].extra[ag.RECONCILE_DETAIL_KEY]
        == ag.RECONCILE_RESTARTED_BEFORE_START
    )
    assert by_id[admitted_not_launched.work.work_id].state is S.CANCELLED
    assert by_id[w_ran.work_id].state is S.UNKNOWN_OUTCOME  # may have run: surfaced
    assert by_id[ran_alive.work.work_id].state is S.RECONCILING
    # The live runtime still holds the only Crew slot: new work queues behind it.
    fresh = await _admit(svc2, who=B, session="new", digest="n")
    assert fresh.queued
    alive["x"] = Liveness.STOPPED
    assert (await svc2.tick())["proven"] == 1
    await svc2.drain_promotions()
    assert store2.get_sync(ran_alive.work.work_id).state is S.UNKNOWN_OUTCOME
    assert store2.get_sync(fresh.work.work_id).state is S.ADMITTED


@pytest.mark.asyncio
async def test_relauncher_reruns_never_started_work_exactly_once(tmp_path, clock):
    path = tmp_path / "t.db"
    store = _open(path, clock)
    svc = _service(store, clock, principal_concurrency=1)
    await _admit(svc, session="a", digest="a")
    queued = await _admit(svc, session="b", digest="b", payload={"message": "hi"})
    store.tasks.close()
    store2 = _open(path, clock)
    svc2 = _service(store2, clock, principal_concurrency=1)
    seen: list[dict] = []
    done = asyncio.Event()

    async def relaunch(payload, admission):
        async def body():
            seen.append(dict(payload))

        await admission.run(body())
        done.set()

    svc2.register_relauncher(OperationKind.CHAT_TURN, relaunch)
    await svc2.ensure_ready()
    await _bounded(done.wait())
    assert seen == [{"message": "hi"}]
    assert store2.get_sync(queued.work.work_id).state is S.COMPLETED
    await svc2.ensure_ready()  # a second call reconciles nothing again
    assert seen == [{"message": "hi"}]


@pytest.mark.asyncio
async def test_children_inherit_initiator_and_hold_root_until_stopped(tmp_path, clock):
    store = _open(tmp_path / "t.db", clock)
    states: dict[str, OperationState] = {}
    svc = _service(store, clock, child_probe=lambda eid: states.get(eid), principal_child_limit=2)
    root = await _admit(svc, session="shared-session")
    gate = _Gate()
    task = asyncio.ensure_future(root.run(gate.body()))
    await _bounded(gate.started.wait())
    # Bob's tab drives a spawn inside Alice's running turn: charged to Alice.
    child = await svc.admit_child(parent_session_key="shared-session", fallback_budget=B)
    assert child.admitted and child.work.budget_key == A
    await child.attach_child("sub1", session_key="subagent:sub1")
    grandchild = await svc.admit_child(parent_session_key="subagent:sub1", fallback_budget=B)
    assert grandchild.work.budget_key == A
    over = await svc.admit_child(parent_session_key="subagent:sub1", fallback_budget=B)
    assert over.rejected and over.reason is ReasonCode.PRINCIPAL_QUEUE_FULL
    await grandchild.abandon()
    gate.release.set()
    await _bounded(task)
    # The root ended but its child still runs: the root's slot is not free yet.
    assert store.get_sync(root.work.work_id).state is S.COMPLETED
    assert svc._scheduler.is_active(root.work.work_id)
    states["sub1"] = S.COMPLETED
    assert (await svc.tick())["children"] == 1
    assert not svc._scheduler.is_active(root.work.work_id)
    assert store.get_sync(child.work.work_id).state is S.COMPLETED


@pytest.mark.asyncio
async def test_unknown_callers_share_one_bucket_and_automation_is_bounded(tmp_path, clock):
    store = _open(tmp_path / "t.db", clock)
    svc = _service(store, clock, principal_concurrency=1)

    class _Req(dict):
        headers: dict = {}

    unknown1, unknown2 = _Req(), _Req()
    b1 = ag.actor_budget(unknown1)
    b2 = ag.actor_budget(unknown2)
    assert b1 == b2 == Principal.unknown().id
    assert (await _admit(svc, who=b1, session="u1")).admitted
    assert (await _admit(svc, who=b2, session="u2")).queued
    auto = ag.actor_budget(_Req(), automation=True)
    assert auto == ag.AUTOMATION_PRINCIPAL.id
    assert (await _admit(svc, who=auto, session="cron:1")).admitted
    assert (await _admit(svc, who=auto, session="cron:2")).queued
    actor = ActorContext(
        principal=Principal.system("p-real"), request_id="r", transport=Transport.HTTP
    )
    known = _Req({ACTOR_REQUEST_KEY: actor})
    assert ag.actor_budget(known) == actor.principal.id


@pytest.mark.asyncio
async def test_session_create_rate_is_per_principal(tmp_path, clock):
    store = _open(tmp_path / "t.db", clock)
    svc = _service(store, clock, session_creates_per_minute=2)
    assert svc.check_session_create(A) is None
    assert svc.check_session_create(A) is None
    refused = svc.check_session_create(A)
    assert refused is not None and refused.reason is ReasonCode.SESSION_CREATE_RATE
    assert int(refused.refusal_headers()["Retry-After"]) >= 1
    assert svc.check_session_create(B) is None
