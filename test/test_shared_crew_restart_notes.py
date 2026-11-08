"""A chat turn a gateway crash ended is surfaced in its own session, with its prompt.

* The accepted prompt rides with the work as its payload, and reconciliation
  hands it to the restart note before the terminal write clears it.
* The note waits for the startup restore, so a session the restore builds
  receives it; a session the crash left without an open-tab record is opened
  from disk, gets the accepted prompt back as its user row and the note.
* Nothing is replayed.

Real ``TaskStore`` and ``ConversationLog`` in ``tmp_path``, ``ManualClock``;
every wait is an explicit event, nothing sleeps.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kiro_crew.config.sections_shared_crew import effective_limits, shared_crew_config_from
from kiro_crew.shared_crew import admission_entry
from kiro_crew.shared_crew import admission_gate as ag
from kiro_crew.shared_crew import host_hooks
from kiro_crew.shared_crew.admission import (
    Liveness,
    OperationKind,
    OperationState,
    RuntimeOwnershipRef,
)
from kiro_crew.shared_crew.contracts import ACTOR_REQUEST_KEY, ActorContext, Principal, Transport
from kiro_crew.shared_crew.work_store import WorkStore
from kiro_crew.taskq.store import TaskStore
from kiro_crew.testing.clock import ManualClock

S = OperationState
BOUND = 30.0
ALICE = Principal.system("alice-r3b")
PROMPT = "running at crash"


def _open(path, clock) -> WorkStore:
    return WorkStore(TaskStore(path, clock=clock.time).open(), clock=clock.time)


def _service(store, clock, **kw) -> ag.AdmissionService:
    limits = effective_limits(
        shared_crew_config_from(
            {"admission_enabled": True, "principal_concurrency": 1, "crew_concurrency": 1}
        ),
        auto_crew_concurrency=8,
    )
    return ag.AdmissionService(
        store, limits=limits, clock=clock.time, liveness=lambda ref: Liveness.STOPPED, **kw
    )


class _Req(dict):
    headers: dict[str, str] = {}


def _request() -> _Req:
    req = _Req()
    req[ACTOR_REQUEST_KEY] = ActorContext(
        principal=ALICE, request_id="r3b", transport=Transport.HTTP
    )
    return req


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(start=90_000.0)


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


async def _restart_with_listener(path, clock, told):
    store = _open(path, clock)
    svc = _service(store, clock)
    svc.set_reconcile_listener(lambda work, payload: told.append((work, payload)))
    await svc.ensure_ready()
    return svc


@pytest.mark.asyncio
async def test_reconciliation_hands_the_accepted_prompt_to_the_note(tmp_path, clock, monkeypatch):
    path = tmp_path / "t.db"
    svc = _service(_open(path, clock), clock)

    async def _current():
        return svc

    monkeypatch.setattr(ag, "current_service", _current)
    running = await admission_entry.admit_chat_turn(
        _request(), session_key="dashboard:l-run", slot_key="l-run", message=PROMPT
    )
    queued = await admission_entry.admit_chat_turn(
        _request(), session_key="dashboard:l-queued", slot_key="l-queued", message="queued"
    )
    assert running.admitted and queued.queued
    store = svc._store
    store.advance_sync(running.work, S.RUNNING, runtime=RuntimeOwnershipRef(running.work.work_id))
    store.tasks.close()
    told: list = []
    svc2 = await _restart_with_listener(path, clock, told)
    by_session = {work.session_key: (work.state, payload) for work, payload in told}
    run_state, run_payload = by_session["dashboard:l-run"]
    assert run_state is S.UNKNOWN_OUTCOME
    assert run_payload[admission_entry.CHAT_PROMPT_PAYLOAD_KEY] == PROMPT
    assert run_payload[admission_entry.CHAT_AUTHOR_PAYLOAD_KEY]["id"] == ALICE.id
    queued_state, queued_payload = by_session["dashboard:l-queued"]
    assert queued_state is S.CANCELLED
    assert queued_payload[admission_entry.CHAT_PROMPT_PAYLOAD_KEY] == "queued"
    # The terminal write cleared the stored prompt.
    for work, _payload in told:
        assert svc2._store.payload_sync(work.work_id) is None


def _work(session_key: str, state: OperationState):
    return SimpleNamespace(
        operation=OperationKind.CHAT_TURN,
        state=state,
        extra={},
        reason=None,
        session_key=session_key,
    )


def _payload(prompt: str) -> dict:
    return {
        admission_entry.CHAT_PROMPT_PAYLOAD_KEY: prompt,
        admission_entry.CHAT_AUTHOR_PAYLOAD_KEY: ALICE.to_public_dict(),
    }


@pytest.mark.asyncio
async def test_a_session_the_crash_left_unopened_opens_with_its_prompt_and_note(
    tmp_path, dash_state
):
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard.chat_utils import slot_history_key

    # Before the crash: the session's metadata line is on disk, nothing else.
    slot = dash_state.get_or_create_slot("l-run")
    assert await asyncio.wait_for(host_hooks.ensure_slot_recorded(dash_state, slot), BOUND)
    key = slot_history_key(slot)
    assert dash_state.conversation_log.read_messages_chained(key) == []

    # After the restart: a fresh state that holds no slot for it.
    after = _make_state(tmp_path)
    after.broadcast_ws = MagicMock()
    after.push_slots_update = MagicMock()
    after.owner_id = ""
    assert "l-run" not in after._slots
    note = host_hooks.reconcile_note(after)
    note(_work("dashboard:l-run", S.UNKNOWN_OUTCOME), _payload(PROMPT))
    delivery = getattr(after, "_shared_crew_note_delivery")
    assert not delivery.done()
    host_hooks.mark_sessions_restored(after)
    await asyncio.wait_for(delivery, BOUND)

    opened = after._slots.get("l-run")
    assert opened is not None
    rows = [(m["role"], m["content"]) for m in opened.messages]
    assert rows == [("user", PROMPT), ("error", host_hooks.NOTE_UNKNOWN)]
    assert opened.messages[0]["meta"]["author"]["id"] == ALICE.id


@pytest.mark.asyncio
async def test_the_note_waits_for_the_restore_and_lands_in_the_restored_slot(dash_state):
    note = host_hooks.reconcile_note(dash_state)
    # Reconciliation runs at gateway start, before the restore built the slot.
    note(_work("dashboard:l-saved", S.CANCELLED), _payload("queued at crash"))
    restored = dash_state.get_or_create_slot("l-saved")
    restored.append("user", "queued at crash", "msg msg-u", broadcast=False)
    host_hooks.mark_sessions_restored(dash_state)
    await asyncio.wait_for(getattr(dash_state, "_shared_crew_note_delivery"), BOUND)
    rows = [(m["role"], m["content"]) for m in restored.messages]
    # The saved user row is not duplicated; the note follows it.
    assert rows == [("user", "queued at crash"), ("error", host_hooks.NOTE_NOT_STARTED)]


@pytest.mark.asyncio
async def test_no_note_and_no_replay_for_work_that_is_not_a_lost_chat_turn(dash_state):
    note = host_hooks.reconcile_note(dash_state)
    slot = dash_state.get_or_create_slot("other")
    parked = _work("dashboard:other", S.CANCELLED)
    parked.extra = {ag.PARKED_KEY: True}
    note(parked, _payload("parked"))
    task = _work("dashboard:other", S.UNKNOWN_OUTCOME)
    task.operation = OperationKind.TASK
    note(task, None)
    assert getattr(dash_state, "_shared_crew_note_delivery", None) is None
    host_hooks.mark_sessions_restored(dash_state)
    assert slot.messages == []


@pytest.mark.asyncio
@pytest.mark.parametrize("saved_id", ["earlier-send", "accepted-send", None])
async def test_restart_matches_send_identity_not_an_earlier_identical_prompt(
    tmp_path, clock, dash_state, monkeypatch, saved_id
):
    svc = _service(_open(tmp_path / "identity.db", clock), clock)

    async def current():
        return svc

    monkeypatch.setattr(ag, "current_service", current)
    accepted = await admission_entry.admit_chat_turn(
        _request(),
        session_key="dashboard:repeated",
        slot_key="repeated",
        message="continue",
        send_id="accepted-send",
    )
    payload = await asyncio.to_thread(svc._store.payload_sync, accepted.work.work_id)
    assert payload["sendId"] == "accepted-send"
    slot = dash_state.get_or_create_slot("repeated")
    slot.append("user", "continue", "msg msg-u", meta={"sendId": saved_id})
    note = host_hooks.reconcile_note(dash_state)
    note(_work("dashboard:repeated", S.CANCELLED), payload)
    host_hooks.mark_sessions_restored(dash_state)
    await asyncio.wait_for(getattr(dash_state, "_shared_crew_note_delivery"), BOUND)
    users = [row for row in slot.messages if row["role"] == "user"]
    assert len(users) == (1 if saved_id == "accepted-send" else 2)
    assert users[-1]["meta"]["sendId"] == "accepted-send"
    assert slot.messages[-1]["content"] == host_hooks.NOTE_NOT_STARTED


@pytest.mark.asyncio
@pytest.mark.parametrize("send_id", ["accepted-send", None, "invalid id"])
async def test_chat_send_payload_keeps_the_normalized_user_row_identity(
    tmp_path, clock, dash_state, monkeypatch, send_id
):
    from unittest.mock import AsyncMock

    from chat_test_helpers import drain_background_tasks
    from test_handlers_taskrunner_coverage import _request as chat_request

    from kiro_crew.dashboard import chat_handlers as chat

    svc = _service(_open(tmp_path / "send.db", clock), clock)
    monkeypatch.setattr(ag, "current_service", AsyncMock(return_value=svc))
    monkeypatch.setattr(ag, "_SERVICE", svc)
    slot = dash_state.get_or_create_slot("send")
    release = asyncio.Event()

    async def run(*args, **kwargs):
        await asyncio.wait_for(release.wait(), BOUND)

    monkeypatch.setattr(chat, "_run_chat", run)
    monkeypatch.setattr(chat, "_maybe_auto_title", AsyncMock())
    monkeypatch.setattr(chat, "maybe_auto_tag", AsyncMock())
    monkeypatch.setattr(chat, "maybe_auto_link_slack", AsyncMock())
    request = chat_request(
        dash_state,
        path="/api/chat?ws=1",
        json_body={"slot": "send", "message": "continue", "meta": {"sendId": send_id}},
    )
    try:
        response = await asyncio.wait_for(chat.api_chat(request), BOUND)
        assert response.status == 200
        [work] = await svc.list_work(session_key="dashboard:send")
        payload = await asyncio.to_thread(svc._store.payload_sync, work.work_id)
        if send_id == "accepted-send":
            assert payload["sendId"] == slot.messages[0]["meta"]["sendId"] == send_id
        else:
            assert "sendId" not in payload
    finally:
        release.set()
        await asyncio.wait_for(drain_background_tasks(dash_state), BOUND)
