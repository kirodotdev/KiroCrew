"""Messages sent to a busy session face fair admission, and a retry never runs twice.

* A message parked behind its session's running turn counts against the
  sender's and the Crew's queue depth and the request size, and shows in
  capacity.
* A retry under the same ``Idempotency-Key`` answers the recorded work: once
  queued it is not queued again, once finished it writes no second row and the
  answer ends.

Every test drives a real ``TaskStore`` in ``tmp_path`` with a ``ManualClock``;
nothing sleeps and nothing touches the host's systemd.
"""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config import sections_shared_crew
from kiro_crew.config.sections_shared_crew import effective_limits, shared_crew_config_from
from kiro_crew.shared_crew import admission_entry as entry
from kiro_crew.shared_crew import admission_gate as ag
from kiro_crew.shared_crew import turn_gate, work_store
from kiro_crew.shared_crew.admission import Liveness, OperationKind, OperationState, ReasonCode
from kiro_crew.shared_crew.containment import ContainmentReport, SliceManager
from kiro_crew.shared_crew.contracts import (
    ACTOR_REQUEST_KEY,
    ActorContext,
    DisplaySnapshot,
    EnforcementLevel,
    Principal,
    PrincipalKind,
    Provenance,
    Transport,
    principal_id_for,
)
from kiro_crew.shared_crew.scheduler import (
    Decision,
    FairScheduler,
    SchedulerLimits,
    Ticket,
)
from kiro_crew.shared_crew.work_store import WorkStore
from kiro_crew.taskq.store import TaskStore
from kiro_crew.testing.clock import ManualClock

S = OperationState
BOUND = 30.0


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
BOB = _person("bob@example.test")


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


def _service(tmp_path, clock, **cfg) -> ag.AdmissionService:
    raw = {"admission_enabled": True, "principal_concurrency": 1, "crew_concurrency": 2}
    fixed = {k.upper(): cfg.pop(k) for k in list(cfg) if hasattr(sections_shared_crew, k.upper())}
    raw.update(cfg)
    with patch.dict(sections_shared_crew.__dict__, fixed):
        limits = effective_limits(shared_crew_config_from(raw), auto_crew_concurrency=8)
    store = WorkStore(TaskStore(tmp_path / "t.db", clock=clock.time).open(), clock=clock.time)
    return ag.AdmissionService(
        store,
        limits=limits,
        clock=clock.time,
        containment_probe=_report,
        slices=SliceManager(
            runner=lambda cmd: subprocess.CompletedProcess(cmd, 0, "", ""),
            systemctl="/usr/bin/systemctl",
        ),
        liveness=lambda ref: Liveness.STOPPED,
    )


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(start=70_000.0)


async def _park(svc, who=ALICE.id, key="park-key-0001", text="hi", session="dashboard:s1"):
    return await svc.admit(
        budget_key=who,
        session_key=session,
        operation=OperationKind.CHAT_TURN,
        idempotency_key=key,
        digest=entry.chat_digest("s1", text),
        request_bytes=len(text.encode()),
        park=True,
    )


# -- the scheduler counts parked work ------------------------------------------------


def test_parked_tickets_count_against_queue_depth_and_never_dispatch():
    sched = FairScheduler(
        SchedulerLimits(
            principal_active=1,
            principal_children=1,
            principal_queue=2,
            crew_active=4,
            crew_children=4,
            crew_queue=3,
        )
    )
    assert sched.park(Ticket("p1", "a", "s")).decision is Decision.QUEUED
    assert sched.park(Ticket("p2", "a", "s")).decision is Decision.QUEUED
    third = sched.park(Ticket("p3", "a", "s"))
    assert third.decision is Decision.REJECTED and third.reason is ReasonCode.PRINCIPAL_QUEUE_FULL
    principal, crew = sched.usage("a")
    assert (principal.active, principal.queued, crew.queued) == (0, 2, 2)
    # A parked ticket is not dispatched even with room everywhere.
    assert not sched.is_active("p1")
    # The Crew queue counts parked work too.
    sched.set_limits(replace(sched.limits, crew_active=1))
    assert sched.submit(Ticket("x", "b", "s2")).decision is Decision.ADMITTED
    assert sched.submit(Ticket("y", "b", "s2")).decision is Decision.QUEUED
    full = sched.submit(Ticket("z", "c", "s3"))
    assert full.decision is Decision.REJECTED and full.reason is ReasonCode.CREW_QUEUE_FULL
    assert sched.cancel_queued("y") is not None
    sched.release("x")
    # Its drain hands it to the fair queue: admitted at once, slot taken.
    started = sched.submit_parked("p1", expires_at=1e12)
    assert started is not None and started.decision is Decision.ADMITTED
    assert sched.is_active("p1") and not sched.is_parked("p1")
    assert sched.cancel_queued("p2") is not None
    assert sched.usage("a")[0].queued == 0


# -- the service bounds and dedupes queued sends --------------------------------------


@pytest.mark.asyncio
async def test_queued_sends_are_bounded_sized_and_visible(tmp_path, clock):
    svc = _service(tmp_path, clock, principal_queue_depth=2, max_request_bytes=1024)
    first = await _park(svc, key="park-key-0001", text="one")
    second = await _park(svc, key="park-key-0002", text="two")
    assert first.queued and second.queued
    third = await _park(svc, key="park-key-0003", text="three")
    assert third.rejected and third.reason is ReasonCode.PRINCIPAL_QUEUE_FULL
    assert third.refusal_status() == 429
    cap = svc.capacity(ALICE.id)
    assert cap.principal.queued == 2 and cap.crew.queued == 2 and cap.principal.active == 0
    big = await _park(svc, who=BOB.id, key="park-key-0004", text="x" * 4096)
    assert big.rejected and big.reason is ReasonCode.REQUEST_TOO_LARGE
    assert big.refusal_status() == 413


@pytest.mark.asyncio
async def test_a_queued_retry_is_answered_not_queued_again(tmp_path, clock):
    svc = _service(tmp_path, clock)
    first = await _park(svc)
    svc.note_queue_id(first.work.work_id, "q-1")
    again = await _park(svc)
    assert again.replayed and again.work.work_id == first.work.work_id
    assert svc.capacity(ALICE.id).principal.queued == 1
    looked = await svc.lookup(
        budget_key=ALICE.id,
        session_key="dashboard:s1",
        idempotency_key="park-key-0001",
        digest=entry.chat_digest("s1", "hi"),
    )
    assert looked is not None and looked.replayed
    body = entry.chat_replay_response(looked, slot_key="s1")
    assert body.status == 200
    import json

    payload = json.loads(body.body)
    assert payload["queued"] is True and payload["queue_id"] == "q-1"
    assert payload["replayed"] is True
    other = await svc.lookup(
        budget_key=ALICE.id,
        session_key="dashboard:s1",
        idempotency_key="park-key-0001",
        digest=entry.chat_digest("s1", "different text"),
    )
    assert other is not None and other.reason is ReasonCode.IDEMPOTENCY_CONFLICT


@pytest.mark.asyncio
async def test_a_replayed_handle_never_runs_its_work(tmp_path, clock):
    svc = _service(tmp_path, clock)
    first = await _park(svc)
    again = await _park(svc)
    ran: list[int] = []

    async def body():
        ran.append(1)

    with pytest.raises(ag.AdmissionEnded):
        await asyncio.wait_for(again.run(body()), BOUND)
    assert ran == [] and first.work is not None


@pytest.mark.asyncio
async def test_the_drained_turn_runs_as_the_parked_work_and_merges_followers(tmp_path, clock):
    svc = _service(tmp_path, clock)
    a = await _park(svc, key="park-key-0001", text="a")
    b = await _park(svc, key="park-key-0002", text="b")
    started = await svc.start_parked([a.work.work_id, b.work.work_id])
    assert started is not None and started.admitted
    assert started.work.work_id == a.work.work_id
    assert await asyncio.wait_for(started.run(asyncio.sleep(0, result=5)), BOUND) == 5
    store = svc._store
    assert store.get_sync(a.work.work_id).state is S.COMPLETED
    merged = store.get_sync(b.work.work_id)
    assert merged.state is S.COMPLETED
    assert merged.extra[ag.MERGED_INTO_KEY] == a.work.work_id
    cap = svc.capacity(ALICE.id)
    assert (cap.principal.active, cap.principal.queued) == (0, 0)
    # Nothing parked is left: a later drain of the same ids admits afresh.
    assert await svc.start_parked([a.work.work_id]) is None


@pytest.mark.asyncio
async def test_a_parked_message_waits_while_its_card_exists_and_ends_once_gone(tmp_path, clock):
    svc = _service(tmp_path, clock, queue_expiry_seconds=60)
    parked = await _park(svc)
    present = {"yes": True}
    svc.set_parked_probe(lambda work_id: present["yes"])
    clock.advance(61)
    assert (await svc.tick())["parked"] == 0
    assert svc._store.get_sync(parked.work.work_id).state is S.QUEUED
    present["yes"] = False
    clock.advance(61)
    assert (await svc.tick())["parked"] == 1
    gone = svc._store.get_sync(parked.work.work_id)
    assert gone.state is S.CANCELLED and gone.extra[ag.RECONCILE_DETAIL_KEY] == "queue_entry_gone"
    assert svc.capacity(ALICE.id).principal.queued == 0


@pytest.mark.asyncio
async def test_a_card_cancelled_through_the_work_api_does_not_run(tmp_path, clock):
    svc = _service(tmp_path, clock)
    parked = await _park(svc)
    await svc.cancel(parked.work.work_id, expected_revision=parked.work.revision)
    refused = await svc.start_parked([parked.work.work_id])
    assert refused is not None and refused.rejected
    assert refused.reason is ReasonCode.CANCELLED_BY_USER


# -- the send handler ----------------------------------------------------------------


def _actor_middleware(principal: Principal):
    @web.middleware
    async def mw(request: web.Request, handler):
        request["app"] = ""
        request["user"] = "local-app"
        request[ACTOR_REQUEST_KEY] = ActorContext(
            principal=principal, request_id="r", transport=Transport.HTTP
        )
        return await handler(request)

    return mw


@pytest.fixture
def dash_state(tmp_path, monkeypatch):
    from chat_test_helpers import _make_state

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    st.broadcast_ws_owners = MagicMock()
    st.push_slots_update = MagicMock()
    st.owner_id = ""
    return st


@pytest.fixture
def installed(tmp_path, clock, monkeypatch):
    (tmp_path / "svc").mkdir()
    svc = _service(tmp_path / "svc", clock, principal_queue_depth=2, max_request_bytes=1024)

    async def _current():
        return svc

    monkeypatch.setattr(ag, "current_service", _current)
    monkeypatch.setattr(ag, "_SERVICE", svc)
    yield svc
    entry._PENDING_SLOTS.clear()
    work_store.set_change_listener(None)


def _client(state, principal: Principal) -> TestClient:
    from kiro_crew.dashboard.chat_handlers import api_chat

    app = web.Application(middlewares=[_actor_middleware(principal)])
    app["state"] = state
    app.router.add_post("/api/chat", api_chat)
    return TestClient(TestServer(app))


def _user_rows(slot) -> list[dict]:
    return [m for m in slot.messages if m.get("role") == "user"]


@pytest.mark.asyncio
async def test_sends_to_a_busy_session_are_bounded_and_deduped(dash_state, installed):
    slot = dash_state.get_or_create_slot("s1")
    busy = MagicMock()
    busy.done.return_value = False
    slot.task = busy
    with patch("kiro_crew.dashboard.chat_handlers._maybe_auto_title", new=AsyncMock()):
        async with _client(dash_state, BOB) as client:

            async def send(text: str, key: str):
                resp = await client.post(
                    "/api/chat?ws=1",
                    json={"slot": "s1", "message": text},
                    headers={"Idempotency-Key": key},
                )
                return resp.status, await resp.json()

            st1, first = await asyncio.wait_for(send("one", "retry-key-00001"), BOUND)
            st2, retry = await asyncio.wait_for(send("one", "retry-key-00001"), BOUND)
            st3, second = await asyncio.wait_for(send("two", "retry-key-00002"), BOUND)
            st4, third = await asyncio.wait_for(send("three", "retry-key-00003"), BOUND)
            st5, big = await asyncio.wait_for(send("x" * 4096, "retry-key-00004"), BOUND)
    assert st1 == 200 and first["queued"] is True
    assert st2 == 200 and retry["replayed"] is True and retry["queue_id"] == first["queue_id"]
    assert st3 == 200 and second["queued"] is True
    assert st4 == 429 and third["code"] == ReasonCode.PRINCIPAL_QUEUE_FULL.value
    assert st5 == 413 and big["code"] == ReasonCode.REQUEST_TOO_LARGE.value
    assert len(slot._queue) == 2
    work_ids = entry.queued_work_ids(slot._queue)
    assert len(work_ids) == 2
    cap = installed.capacity(BOB.id)
    assert cap.principal.queued == 2


@pytest.mark.asyncio
async def test_a_finished_send_retried_answers_once_and_ends(dash_state, installed):
    slot = dash_state.get_or_create_slot("s1")
    run_chat = AsyncMock()
    with (
        patch("kiro_crew.dashboard.chat_handlers._run_chat", new=run_chat),
        patch("kiro_crew.dashboard.chat_handlers._maybe_auto_title", new=AsyncMock()),
    ):
        async with _client(dash_state, ALICE) as client:
            resp = await client.post(
                "/api/chat?ws=1",
                json={"slot": "s1", "message": "hello"},
                headers={"Idempotency-Key": "done-key-000001"},
            )
            assert resp.status == 200, await resp.text()
            await asyncio.wait_for(asyncio.shield(slot.task), BOUND)
            work_id = (await resp.json())["admission"]["work"]["work_id"]
            assert installed._store.get_sync(work_id).state is S.COMPLETED

            async def retry():
                again = await client.post(
                    "/api/chat",
                    json={"slot": "s1", "message": "hello"},
                    headers={"Idempotency-Key": "done-key-000001"},
                )
                return again.status, again.headers, await again.json()

            status, headers, body = await asyncio.wait_for(retry(), BOUND)
    assert status == 200
    assert body["replayed"] is True
    assert body["admission"]["state"] == S.COMPLETED.value
    assert headers[entry.WORK_ID_HEADER] == work_id
    assert len(_user_rows(slot)) == 1
    assert run_chat.await_count == 1


# -- the drain runs the parked work ----------------------------------------------------


@pytest.mark.asyncio
async def test_gate_turn_runs_the_parked_work_it_was_handed(installed):
    parked = await _park(installed, who=BOB.id)
    slot = SimpleNamespace(key="s1", linked_session_key="", messages=[], total_messages=0)
    slot.append = MagicMock()
    ran: list[str] = []

    async def run():
        ran.append(turn_gate.admitted_work())

    entries = [{"id": "q", "meta": {entry.QUEUED_WORK_META_KEY: parked.work.work_id}}]
    state = SimpleNamespace(push_slots_update=lambda: None)
    with patch("kiro_crew.dashboard.chat_slack.slot_is_live", lambda _s, _slot: True):
        with turn_gate.initiated_by(BOB.id), turn_gate.queued_work(entries):
            task = asyncio.ensure_future(turn_gate.gate_turn(state, slot, "hi", {}, run))
        await asyncio.wait_for(task, BOUND)
    assert ran == [parked.work.work_id]
    assert installed._store.get_sync(parked.work.work_id).state is S.COMPLETED
    # One record for the message, not a second admission for its turn.
    assert len(installed._store.list_sync()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("who", [BOB.id, ""], ids=["edited-body", "merged-people"])
async def test_a_drained_turn_unlike_its_parked_work_is_admitted_afresh(installed, who):
    parked = await _park(installed, who=BOB.id)
    slot = SimpleNamespace(key="s1", linked_session_key="", messages=[], total_messages=0)
    slot.append = MagicMock()
    ran: list[str] = []

    async def run():
        ran.append(turn_gate.admitted_work())

    entries = [{"id": "q", "meta": {entry.QUEUED_WORK_META_KEY: parked.work.work_id}}]
    text = "x" * (installed._limits.max_request_bytes + 1) if who else "hi"
    state = SimpleNamespace(push_slots_update=lambda: None)
    with patch("kiro_crew.dashboard.chat_slack.slot_is_live", lambda _s, _slot: True):
        with turn_gate.initiated_by(who), turn_gate.queued_work(entries):
            task = asyncio.ensure_future(turn_gate.gate_turn(state, slot, text, {}, run))
        await asyncio.wait_for(task, BOUND)
    assert installed._store.get_sync(parked.work.work_id).state is S.CANCELLED
    if who:  # the edited body is held to the size limit like a fresh send
        assert ran == [] and "request too large" in slot.append.call_args.args[1]
    else:  # mixed authors run on their own fresh admission, not the first sender's
        (fresh,) = [w for w in installed._store.list_sync() if w.work_id != parked.work.work_id]
        assert ran == [fresh.work_id] and fresh.budget_key != BOB.id


# -- a removed person's parked prompt, a late start, a failed terminal write --------


@pytest.mark.asyncio
async def test_a_removed_persons_parked_prompt_is_refused_before_any_merge(tmp_path, clock):
    svc = _service(tmp_path, clock)
    removed = await _park(svc, who=ALICE.id, key="park-key-0001", text="alice")
    staying = await _park(svc, who=BOB.id, key="park-key-0002", text="bob")
    await svc.cancel(
        removed.work.work_id,
        expected_revision=removed.work.revision,
        reason=ReasonCode.ACCESS_REMOVED,
    )
    # The drain's per-entry check drops the removed person's entry, with its reason.
    alone = await svc._cancelled_parked([removed.work.work_id])
    assert alone is not None and alone.reason is ReasonCode.ACCESS_REMOVED
    # A merge that still names it never runs the removed person's text.
    merged = await svc.start_parked([removed.work.work_id, staying.work.work_id])
    assert merged is not None and merged.rejected
    assert merged.reason is ReasonCode.ACCESS_REMOVED


@pytest.mark.asyncio
async def test_work_cancelled_while_its_start_runs_stops_the_started_execution(tmp_path, clock):
    svc = _service(tmp_path, clock)
    stopped: list[str] = []
    svc.register_cancel(
        OperationKind.WORKFLOW, lambda work: stopped.append(str(work.extra["execution_id"]))
    )
    admission = await svc.admit(
        budget_key=ALICE.id,
        session_key="workflow",
        operation=OperationKind.WORKFLOW,
        idempotency_key="wf-key-0001",
        digest="d",
        allow_queue=False,
    )
    assert admission.admitted and admission.work is not None
    # Cancelled from the work API while the start is still in flight.
    await svc.cancel(admission.work.work_id, expected_revision=admission.work.revision)
    assert svc.capacity(ALICE.id).principal.active == 0
    await admission.attach_child("run-1")
    assert stopped == ["run-1"]
    assert svc._store.get_sync(admission.work.work_id).state is S.CANCELLED
    # A second attach (a retry) stops nothing twice.
    await admission.attach_child("run-1")
    assert stopped == ["run-1"]


@pytest.mark.asyncio
async def test_a_queued_turn_whose_caller_is_cancelled_frees_its_place(tmp_path, clock):
    svc = _service(tmp_path, clock, principal_concurrency=1, crew_concurrency=1)
    running = await svc.admit(
        budget_key=ALICE.id,
        session_key="dashboard:a",
        operation=OperationKind.CHAT_TURN,
        idempotency_key="run-key-0001",
        digest="a",
    )
    queued = await svc.admit(
        budget_key=BOB.id,
        session_key="dashboard:b",
        operation=OperationKind.CHAT_TURN,
        idempotency_key="run-key-0002",
        digest="b",
    )
    assert running.admitted and queued.queued and queued.work is not None
    work_id = queued.work.work_id
    waiter = asyncio.ensure_future(queued.run(asyncio.sleep(0)))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert svc._store.get_sync(work_id).state is S.CANCELLED
    assert not svc._scheduler.is_queued(work_id) and not svc._scheduler.is_active(work_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "site", ["promotion", "start", "settle", "cancel", "expiry", "attach", "deadline"]
)
async def test_a_failed_admission_write_fails_closed(tmp_path, clock, caplog, site):
    """Each store write that fails frees the slot, stops the child and wakes waiters."""
    svc = _service(tmp_path, clock, principal_concurrency=1, crew_concurrency=1)
    stopped: list[str] = []
    svc.register_cancel(
        OperationKind.WORKFLOW, lambda work: stopped.append(str(work.extra["execution_id"]))
    )
    operation = (
        OperationKind.WORKFLOW if site in ("attach", "deadline") else OperationKind.CHAT_TURN
    )
    first = await svc.admit(
        budget_key=ALICE.id,
        session_key="dashboard:a",
        operation=operation,
        idempotency_key="run-key-0001",
        digest="a",
    )
    second = await svc.admit(
        budget_key=BOB.id,
        session_key="dashboard:b",
        operation=OperationKind.CHAT_TURN,
        idempotency_key="run-key-0002",
        digest="b",
    )
    assert first.admitted and second.queued and first.work and second.work
    if site == "deadline":
        await first.attach_child("run-3")
    failing = {
        "promotion": S.ADMITTED,
        "start": S.RUNNING,
        "settle": S.COMPLETED,
        "attach": S.RUNNING,
        "deadline": S.CANCELLING,
    }.get(site)
    real = svc._store.advance

    async def locked(work, state, *args, **kwargs):
        if failing is None or state is failing:
            raise RuntimeError("database is locked")
        return await real(work, state, *args, **kwargs)

    target = first.work.work_id
    with patch.object(svc._store, "advance", side_effect=locked):
        if site == "promotion":
            target = second.work.work_id
            waiter = asyncio.ensure_future(second.run(asyncio.sleep(0)))
            await asyncio.sleep(0)
            await svc.cancel(first.work.work_id, expected_revision=first.work.revision)
            await svc.drain_promotions()
            with pytest.raises(ag.AdmissionEnded):
                await waiter
        elif site == "start":
            with pytest.raises(RuntimeError):
                await first.run(asyncio.sleep(0))
        elif site == "settle":
            assert await first.run(asyncio.sleep(0, result=7)) == 7
        elif site in ("cancel", "expiry"):
            target = second.work.work_id
            if site == "cancel":
                await svc.cancel(target, expected_revision=second.work.revision)
            else:
                clock.advance(sections_shared_crew.QUEUE_EXPIRY_SECONDS + 1)
                assert (await svc.tick())["expired"] == 1
        elif site == "attach":
            with pytest.raises(RuntimeError):
                await first.attach_child("run-2")
        else:
            clock.advance(sections_shared_crew.ROOT_DEADLINE_SECONDS + 1)
            await svc.tick()
    assert "write for" in caplog.text and "failed" in caplog.text
    if site in ("attach", "deadline"):
        assert stopped == ["run-2" if site == "attach" else "run-3"]
    if site != "deadline":
        assert not svc._scheduler.is_queued(target)
        live = svc._live.get(target)
        if live is not None:
            # Ended in memory, but the slot is held until the terminal write lands.
            assert live.unpersisted is not None and live.work.state in ag.TERMINAL
        # The store is writable again: the next pass records the end and frees the slot.
        await svc.tick()
        assert target not in svc._live
        assert not svc._scheduler.is_active(target)
        durable = await svc._store.get(target)
        assert durable is not None and durable.state in ag.TERMINAL


@pytest.mark.asyncio
async def test_a_failed_attach_holds_the_slot_until_the_child_has_stopped(tmp_path, clock):
    """The terminal write landing is not enough: the stopped child must be gone too."""
    svc = _service(tmp_path, clock, principal_concurrency=1, crew_concurrency=1)
    stopped: list[str] = []
    alive = {"run-2": True}
    svc.register_cancel(OperationKind.WORKFLOW, lambda work: stopped.append("run-2"))
    svc.register_probe(
        OperationKind.WORKFLOW,
        lambda exec_id: None if alive.get(exec_id) else OperationState.CANCELLED,
    )
    first = await svc.admit(
        budget_key=ALICE.id,
        session_key="dashboard:a",
        operation=OperationKind.WORKFLOW,
        idempotency_key="run-key-0001",
        digest="a",
    )
    assert first.admitted and first.work
    real = svc._store.advance

    async def locked(work, state, *args, **kwargs):
        if state is OperationState.RUNNING:
            raise RuntimeError("database is locked")
        return await real(work, state, *args, **kwargs)

    with patch.object(svc._store, "advance", side_effect=locked):
        with pytest.raises(RuntimeError):
            await first.attach_child("run-2")
    target = first.work.work_id
    assert stopped == ["run-2"]
    await svc.tick()
    # Written, and the record names the execution, but the child still runs.
    durable = await svc._store.get(target)
    assert durable is not None and durable.state in ag.TERMINAL
    assert durable.extra.get("execution_id") == "run-2"
    assert target in svc._live and svc._scheduler.is_active(target)
    alive["run-2"] = False
    await svc.tick()
    assert target not in svc._live and not svc._scheduler.is_active(target)
