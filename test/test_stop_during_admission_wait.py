"""A Stop while a dashboard turn still waits in Shared Crew fair admission.

With fair admission on, a send that has to wait for capacity is dispatched as
a task that waits in ``admission_entry.admitted_chat_turn`` before its
``_run_chat`` starts, so it holds no provider turn and no preparing runner yet,
and ``stop_turn`` answers ``"idle"``. A queue drain's turn waits in
``turn_gate.gate_turn`` inside its started runner, which a dashboard Stop
already ends (``test_stop_during_turn_preparation.py``). These tests drive the real send handler (``POST /api/chat``), the
real Stop (``stop_slot_turn``) and Interrupt (``POST .../interrupt``) handlers
and the real ``_run_chat`` against a real ``AdmissionService`` over a work
store in ``tmp_path``. Faked: the provider edge (``get_or_create``, the
provider's stream and the session manager's Stop record) and the queue
hand-off's dispatch, which records the successor instead of running it.

* A Stop or Interrupt pressed during the wait ends the turn at once: the work
  is cancelled by the user, the provider is never prompted, no capacity note
  is written and the message queued behind it is handed off once.
* A Stop recorded on the session from another surface during the wait keeps
  the turn from running when the wait ends, on the send path and on the
  ``gate_turn`` path.
* A send that waits and is granted with no Stop runs once, also when the
  session's Stop count was reset by a teardown during the wait.
* An agent-started turn waiting in ``gate_turn`` still runs after a session
  Stop: only its own open stream can cancel its cycle.
* A send whose turn ends after a later send on the same slot began to wait
  leaves that send's entry, so a Stop still ends the later send.
"""

from __future__ import annotations

import asyncio
import inspect
import subprocess
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from dashboard_chat_helpers import _provider_mock

from kiro_crew.acp.types import STOP_REASON_END_TURN
from kiro_crew.config.sections_shared_crew import effective_limits, shared_crew_config_from
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent
from kiro_crew.shared_crew import admission_entry as entry
from kiro_crew.shared_crew import admission_gate as ag
from kiro_crew.shared_crew import turn_gate
from kiro_crew.shared_crew.admission import Liveness, OperationKind, OperationState, ReasonCode
from kiro_crew.shared_crew.containment import ContainmentReport, SliceManager
from kiro_crew.shared_crew.contracts import (
    ACTOR_REQUEST_KEY,
    ActorContext,
    EnforcementLevel,
    Provenance,
    Transport,
)
from kiro_crew.shared_crew.identity import tailnet_principal
from kiro_crew.shared_crew.work_store import WorkStore
from kiro_crew.taskq import lanes
from kiro_crew.taskq.store import TaskStore
from kiro_crew.testing.clock import ManualClock
from kiro_crew.testing.wait import async_wait_until

ALICE = tailnet_principal("alice@example.com", "laptop.tail.ts.net", Provenance.TAILSCALE_SERVE)
#: Bound, in seconds, on every wait a test releases itself.
BOUND = 30.0
S = OperationState


@pytest.fixture(autouse=True)
def _quiet_audit(_floor_monkeypatch: pytest.MonkeyPatch) -> Any:
    # The stop handlers and the runner write their audit lines through ``sel``.
    from kiro_crew.dashboard import chat_handlers, chat_runner

    _floor_monkeypatch.setattr(chat_handlers, "sel", lambda: MagicMock())
    _floor_monkeypatch.setattr(chat_runner, "sel", lambda: MagicMock())
    _floor_monkeypatch.setattr(chat_handlers, "_maybe_auto_title", AsyncMock())
    entry._PENDING_SLOTS.clear()
    turn_gate._UNADMITTED_RUNNING.clear()
    yield
    entry._PENDING_SLOTS.clear()
    turn_gate._UNADMITTED_RUNNING.clear()


@pytest.fixture
def service(tmp_path, monkeypatch) -> ag.AdmissionService:
    """A real admission service: one turn at a time per person, two queued."""
    (tmp_path / "svc").mkdir()
    clock = ManualClock(start=80_000.0)
    store = WorkStore(
        TaskStore(tmp_path / "svc" / "t.db", clock=clock.time).open(), clock=clock.time
    )
    limits = effective_limits(
        shared_crew_config_from(
            {
                "admission_enabled": True,
                "principal_concurrency": 1,
                "crew_concurrency": 2,
                "principal_queue_depth": 2,
            }
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

    async def _current() -> ag.AdmissionService:
        return svc

    monkeypatch.setattr(ag, "current_service", _current)
    monkeypatch.setattr(ag, "_SERVICE", svc)
    monkeypatch.setattr(lanes, "_LANE_RESOLVER", svc.budget_for_session)
    return svc


class _Edge:
    """The provider edge: prompts it was sent, Stops recorded, successors handed off."""

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.stops: dict[str, int] = {}
        self.successors: list[str] = []

    # The session manager's Stop record (``SessionManager.note_stop`` and
    # ``stop_generation``), and a ``stop_turn`` that records the Stop and finds
    # no provider turn to cancel, as it does for a session with no turn open.
    def note_stop(self, key: str) -> bool:
        self.stops[key] = self.stops.get(key, 0) + 1
        return True

    def stop_generation(self, key: str) -> int:
        return self.stops.get(key, 0)

    async def stop_turn(self, key: str, **_kwargs: Any) -> str:
        self.note_stop(key)
        return "idle"


@pytest.fixture
def edge() -> _Edge:
    return _Edge()


@pytest.fixture
def state(tmp_path, monkeypatch, edge: _Edge) -> Any:
    from kiro_crew.dashboard import chat_runner

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    st.broadcast_ws_owners = MagicMock()
    st.push_slots_update = MagicMock()
    st.owner_id = ""
    st.context_builder = None
    st.consolidator = None
    st._hook_store = None
    st._yolo = False

    async def _stream(message: str, **_kwargs: Any):
        edge.prompts.append(message)
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="the reply")
        yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

    client = _provider_mock()
    client.context_usage_pct = MagicMock(return_value=10.0)
    client.stream = _stream
    client.stream_command = _stream
    st.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    st.sessions.note_stop = edge.note_stop
    st.sessions.stop_generation = edge.stop_generation
    st.sessions.stop_turn = edge.stop_turn
    st.sessions.is_compacting = lambda *_a, **_k: False

    def _record_successor(_state: Any, _slot: Any, coro: Any, **_kw: Any) -> Any:
        # The queue-exit guard wrapping the successor ``_run_chat``: its ``args``
        # local is that call's (state, slot, message, ...) after the guard's own.
        bound = inspect.getcoroutinelocals(coro)
        coro.close()
        args = tuple(bound.get("args", ()))
        edge.successors.append(str(args[0]) if args else "")
        done = asyncio.get_running_loop().create_future()
        done.set_result(None)
        return done

    monkeypatch.setattr(chat_runner, "spawn_guarded_turn", _record_successor)
    return st


def _actor_middleware():
    @web.middleware
    async def mw(request: web.Request, handler):
        request["app"] = ""
        request["user"] = "local-app"
        request[ACTOR_REQUEST_KEY] = ActorContext(
            principal=ALICE, request_id="r", transport=Transport.HTTP
        )
        return await handler(request)

    return mw


def _client(state: Any) -> TestClient:
    from kiro_crew.dashboard.chat_handlers import api_chat, api_chat_slot_interrupt

    app = web.Application(middlewares=[_actor_middleware()])
    app["state"] = state
    app.router.add_post("/api/chat", api_chat)
    app.router.add_post("/api/chat/slots/{slot}/interrupt", api_chat_slot_interrupt)
    return TestClient(TestServer(app))


async def _hold_capacity(service: ag.AdmissionService, release: asyncio.Event) -> Any:
    """Alice's one turn slot is held by other work of hers until *release* is set."""

    async def _held() -> None:
        await asyncio.wait_for(release.wait(), 2 * BOUND)

    with turn_gate.initiated_by(ALICE.id):
        holder = asyncio.ensure_future(
            turn_gate.run_admitted(_held(), session_key="elsewhere", text="other work")
        )
    await async_wait_until(
        lambda: service.scheduler.usage(ALICE.id)[0].active == 1,
        describe=lambda: service.scheduler.usage(ALICE.id),
    )
    return holder


async def _send_queued(client: TestClient, service: ag.AdmissionService, text: str) -> str:
    """Send *text* to slot s1 while Alice's capacity is held: it waits. Its work id."""
    resp = await asyncio.wait_for(
        client.post("/api/chat?ws=1", json={"slot": "s1", "message": text}), BOUND
    )
    # 202: accepted, and waiting for capacity.
    assert resp.status == 202, await resp.text()
    body = await resp.json()
    work_id = body["admission"]["work"]["work_id"]
    await async_wait_until(lambda: entry.admission_pending("s1"))
    assert service.live_work(work_id).state is S.QUEUED
    return work_id


async def _ended(task: Any, timeout: float) -> bool:
    done, _ = await asyncio.wait({task}, timeout=timeout)
    return bool(done)


def _error_rows(slot: Any) -> list[str]:
    return [str(m.get("content", "")) for m in slot.messages if m.get("role") == "error"]


async def _press_stop(client: TestClient, state: Any, slot: Any) -> Any:
    from kiro_crew.dashboard.chat_handlers import stop_slot_turn

    return await stop_slot_turn(state, slot)


async def _press_interrupt(client: TestClient, state: Any, slot: Any) -> Any:
    chosen = slot.queue_append("chosen card")
    resp = await client.post(f"/api/chat/slots/{slot.key}/interrupt", json={"queue_id": chosen})
    assert resp.status == 200, await resp.text()
    return await resp.json()


async def _press_interrupt_on_restored(client: TestClient, state: Any, slot: Any) -> Any:
    # A card restored after a restart: the drain holds it unless this click chose it.
    from kiro_crew.dashboard.chat_handlers import RESTORED_QUEUE_KEY

    chosen = slot.queue_append("chosen card")
    slot._queue[-1][RESTORED_QUEUE_KEY] = True
    resp = await client.post(f"/api/chat/slots/{slot.key}/interrupt", json={"queue_id": chosen})
    assert resp.status == 200, await resp.text()
    return await resp.json()


_PRESSES = {
    "stop": _press_stop,
    "interrupt": _press_interrupt,
    "interrupt-restored": _press_interrupt_on_restored,
}


@pytest.mark.asyncio
@pytest.mark.timeout(90)
@pytest.mark.parametrize("press", sorted(_PRESSES))
async def test_a_press_during_the_wait_ends_the_turn_unstarted(press, service, state, edge):
    release = asyncio.Event()
    holder = await _hold_capacity(service, release)
    try:
        async with _client(state) as client:
            work_id = await _send_queued(client, service, "hello")
            slot = state._slots["s1"]
            turn = slot.task
            slot.queue_append("sent while it waited")
            reply = await asyncio.wait_for(_PRESSES[press](client, state, slot), BOUND)
            # The press itself ended the wait: no capacity had to free first.
            # The holder outlasts this wait, so only the press can end the turn.
            ended_before_release = await _ended(turn, BOUND)
            release.set()
            await asyncio.wait_for(holder, BOUND)
            await asyncio.wait_for(asyncio.gather(turn, return_exceptions=True), BOUND)
            await async_wait_until(lambda: not slot.running, describe=lambda: slot.task)
    finally:
        release.set()
    assert ended_before_release is True
    assert reply == ({"ok": True} if press == "stop" else {"ok": True, "outcome": "idle"})
    record = service._store.get_sync(work_id)
    assert (record.state, record.reason) == (S.CANCELLED, ReasonCode.CANCELLED_BY_USER)
    assert edge.prompts == []
    assert _error_rows(slot) == []
    assert slot._stop_state == "idle"
    # The chosen card starts once: the unstarted turn's hand-off drains it.
    expected = ["sent while it waited"] if press == "stop" else ["chosen card"]
    assert edge.successors == expected
    assert not entry.admission_pending("s1")


async def _wait_in_gate_turn(state: Any, service: ag.AdmissionService) -> tuple[Any, str]:
    """A drained turn on slot s1 waiting in ``gate_turn``'s own admission."""
    from kiro_crew.dashboard.chat_handlers import _run_chat
    from kiro_crew.dashboard.chat_utils import effective_session_key

    slot = state.get_or_create_slot("s1")
    slot.append("user", "drained", "msg msg-u")
    with turn_gate.initiated_by(ALICE.id):
        turn = asyncio.ensure_future(_run_chat(state, slot, "drained"))
    slot.task = turn
    await async_wait_until(lambda: entry.admission_pending("s1"))
    key = effective_session_key(slot)
    [work] = [w for w in service._store.list_sync() if w.state is S.QUEUED]
    assert work.session_key == key
    return turn, work.work_id


@pytest.mark.asyncio
@pytest.mark.timeout(90)
@pytest.mark.parametrize("path", ["send", "gate_turn"])
async def test_a_session_stop_during_the_wait_keeps_the_turn_from_running(
    path, service, state, edge
):
    from kiro_crew.dashboard.chat_utils import effective_session_key

    release = asyncio.Event()
    holder = await _hold_capacity(service, release)
    try:
        async with _client(state) as client:
            if path == "send":
                work_id = await _send_queued(client, service, "hello")
                slot = state._slots["s1"]
                turn = slot.task
            else:
                turn, work_id = await _wait_in_gate_turn(state, service)
                slot = state._slots["s1"]
            slot.queue_append("sent while it waited")
            # A linked channel's stop records the Stop on the session, not the slot.
            assert state.sessions.note_stop(effective_session_key(slot)) is True
            release.set()
            await asyncio.wait_for(holder, BOUND)
            await asyncio.wait_for(asyncio.gather(turn, return_exceptions=True), BOUND)
            await async_wait_until(lambda: not slot.running, describe=lambda: slot.task)
    finally:
        release.set()
    record = service._store.get_sync(work_id)
    assert (record.state, record.reason) == (S.CANCELLED, ReasonCode.CANCELLED_BY_USER)
    assert edge.prompts == []
    assert _error_rows(slot) == []
    assert edge.successors == ["sent while it waited"]


@pytest.mark.asyncio
@pytest.mark.timeout(90)
async def test_an_agent_started_turn_still_runs_after_a_session_stop(service, state):
    from kiro_crew.dashboard.chat_utils import effective_session_key

    slot = state.get_or_create_slot("s1")
    ran: list[str] = []

    async def _agent_turn() -> None:
        ran.append("agent turn")

    release = asyncio.Event()
    holder = await _hold_capacity(service, release)
    try:
        with turn_gate.initiated_by(ALICE.id):
            turn = asyncio.ensure_future(
                turn_gate.gate_turn(state, slot, "", {"_agent_started": True}, _agent_turn)
            )
        await async_wait_until(lambda: entry.admission_pending("s1"))
        assert state.sessions.note_stop(effective_session_key(slot)) is True
        release.set()
        await asyncio.wait_for(holder, BOUND)
        await asyncio.wait_for(turn, BOUND)
    finally:
        release.set()
    assert ran == ["agent turn"]


@pytest.mark.asyncio
@pytest.mark.timeout(90)
@pytest.mark.parametrize("during_the_wait", ["nothing", "session_torn_down"])
async def test_a_send_granted_with_no_stop_runs_once(during_the_wait, service, state, edge):
    from kiro_crew.dashboard.chat_utils import effective_session_key

    key = effective_session_key(state.get_or_create_slot("s1"))
    # An earlier turn's Stop, already on the session before this send.
    edge.note_stop(key)
    release = asyncio.Event()
    holder = await _hold_capacity(service, release)
    try:
        async with _client(state) as client:
            work_id = await _send_queued(client, service, "hello")
            slot = state._slots["s1"]
            turn = slot.task
            if during_the_wait == "session_torn_down":
                # Teardown drops the session's Stop count back to 0: not a Stop.
                edge.stops.pop(key)
            release.set()
            await asyncio.wait_for(holder, BOUND)
            await asyncio.wait_for(asyncio.gather(turn, return_exceptions=True), BOUND)
    finally:
        release.set()
    assert edge.prompts == ["hello"]
    assert service._store.get_sync(work_id).state is S.COMPLETED
    assert _error_rows(slot) == []


@pytest.mark.asyncio
@pytest.mark.timeout(90)
async def test_an_ending_send_keeps_a_later_sends_stop_entry(service):
    async def _admit(name: str) -> ag.Admission:
        return await service.admit(
            budget_key=ALICE.id,
            session_key="sess-s1",
            operation=OperationKind.CHAT_TURN,
            idempotency_key=f"successor-test-{name}",
        )

    first = await _admit("a")
    # Other work of Alice's queued ahead of the later send takes the capacity A frees.
    other = await _admit("other")
    successor = await _admit("b")
    assert first.admitted and other.queued and successor.queued
    ran: list[str] = []
    ended: list[str] = []
    tasks: list[Any] = []

    async def _successor_turn() -> None:
        ran.append("b")

    async def _first_turn() -> str:
        # B is sent on the same slot and starts waiting while A's turn is still finishing.
        tasks.append(
            asyncio.ensure_future(
                entry.admitted_chat_turn(
                    "s1",
                    successor,
                    _successor_turn(),
                    still_live=lambda: True,
                    on_ended=lambda: ended.append("b"),
                    stoppable=True,
                )
            )
        )
        await async_wait_until(lambda: entry._WAITING_SENDS.get("s1") is successor)
        return "a"

    result = await asyncio.wait_for(
        entry.admitted_chat_turn(
            "s1",
            first,
            _first_turn(),
            still_live=lambda: True,
            on_ended=lambda: None,
            stoppable=True,
        ),
        BOUND,
    )
    assert result == "a"
    [b_turn] = tasks
    try:
        assert await asyncio.wait_for(entry.cancel_waiting_send("s1"), BOUND) is True
        assert await asyncio.wait_for(b_turn, BOUND) is None
    finally:
        b_turn.cancel()
        live = service.live_work(other.work.work_id)
        if live is not None:
            await service.cancel(other.work.work_id, expected_revision=live.revision)
    assert ran == [] and ended == ["b"]
    record = service._store.get_sync(successor.work.work_id)
    assert (record.state, record.reason) == (S.CANCELLED, ReasonCode.CANCELLED_BY_USER)
    assert not entry.admission_pending("s1")
