"""Opening a chat in kiro-cli: no other writer acts while the chat is handed over.

The invariant (``kiro_crew.kiro_cli_handoff``'s docstring): from the open's
hold until its record is gone, only the handover adds turns to the chat,
rewrites its history or changes its native conversation. A record left by a
restart, or by an import that failed after the hold was released, still counts.
Each writer that is not the handover brings the record's turns in first where it
can, and otherwise refuses with ``kiro_cli_import_pending``, changing nothing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import kiro_cli_handoff
from kiro_crew.dashboard import chat_utils
from kiro_crew.dashboard.handlers import kiro_cli_launch as kcl


@pytest.fixture(autouse=True)
def _records(_floor_monkeypatch, tmp_path):
    kiro_cli_handoff.reset_holds()
    _floor_monkeypatch.setattr(kiro_cli_handoff, "_records_dir", lambda: tmp_path / "opens")
    _floor_monkeypatch.setattr(kcl, "_recovery_locks", {})
    yield
    kiro_cli_handoff.reset_holds()


def _leave_record(key: str) -> None:
    kiro_cli_handoff.record_open(key, {"history_key": key, "created_at": "", "log_offset": 0})


def _recovery(*, fails: bool, imports: str = ""):
    """A recovery that keeps the record (a failed import) or adds *imports*
    to the chat as kiro-cli's reply and removes the record."""
    calls = []

    async def recover(state, slot, session_key):
        calls.append(session_key)
        if not fails:
            if imports:
                slot.append("assistant", imports, "msg msg-a")
            kiro_cli_handoff.forget_open(session_key)
        return True

    recover.calls = calls
    return recover


def _populated(state, name):
    slot = state.get_or_create_slot(name)
    slot.append("user", "first question", "msg msg-u", ts="2026-05-21T16:00:00Z")
    slot.append("assistant", "first answer", "msg msg-a", ts="2026-05-21T16:00:01Z")
    slot.drain()
    return slot


# ── the predicate every writer reads ───────────────────────────────────────


def test_a_left_record_with_no_hold_still_hands_the_chat_over():
    key = "dashboard:chat-left"
    assert kiro_cli_handoff.handover_refusal(key) is None
    _leave_record(key)
    assert kiro_cli_handoff.handover_refusal(key) == (
        kiro_cli_handoff.LEFT_OPEN_PENDING_REFUSAL,
        "kiro_cli_import_pending",
    )
    assert kiro_cli_handoff.hold(key, "t")
    assert kiro_cli_handoff.handover_refusal(key)[1] == "kiro_cli_open"


# ── rewind ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_rewind_is_refused_while_a_failed_import_keeps_the_record(tmp_path, monkeypatch):
    """A record with no hold (a restart, or an import that failed at release)
    refuses the rewind: letting it through would have the next turn's recovery
    add the discarded branch's kiro-cli turns after the edited message."""
    from chat_test_helpers import _make_app, _make_state

    monkeypatch.setattr("kiro_crew.dashboard.chat_rewind._run_chat", AsyncMock())
    state = _make_state(tmp_path)
    slot = _populated(state, "src")
    key = chat_utils.effective_session_key(slot)
    _leave_record(key)
    recover = _recovery(fails=True)
    monkeypatch.setattr(kcl, "recover_left_open", recover)
    before = [m["content"] for m in slot.messages]
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post(
            "/api/chat/slots/src/rewind", json={"at_message_index": 0, "content": "edited"}
        )
        assert resp.status == 409
        assert (await resp.json())["code"] == "kiro_cli_import_pending"
    assert recover.calls == [key], "the rewind tried to bring the turns in first"
    assert [m["content"] for m in slot.messages] == before
    state.sessions.discard_conversation.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_rewind_discards_the_left_turns_with_the_branch_they_belong_to(
    tmp_path, monkeypatch
):
    from chat_test_helpers import _make_app, _make_state

    monkeypatch.setattr("kiro_crew.dashboard.chat_rewind._run_chat", AsyncMock())
    state = _make_state(tmp_path)
    state.sessions._session_map.get = MagicMock(return_value="")
    slot = _populated(state, "src")
    key = chat_utils.effective_session_key(slot)
    _leave_record(key)
    monkeypatch.setattr(kcl, "recover_left_open", _recovery(fails=False, imports="from kiro-cli"))
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post(
            "/api/chat/slots/src/rewind", json={"at_message_index": 0, "content": "edited"}
        )
        assert resp.status == 200, await resp.text()
    assert [m["content"] for m in slot.messages] == ["edited"]
    assert not kiro_cli_handoff.has_left_open(key), "nothing left to add after the edit"
    if slot.task:
        slot.task.cancel()


# ── regenerate, variant switch, edit-resend ────────────────────────────────


def test_the_destructive_history_guard_refuses_a_left_record(monkeypatch):
    from types import SimpleNamespace

    from kiro_crew.dashboard import chat_regenerate

    key = "dashboard:chat-guard"
    monkeypatch.setattr(chat_regenerate, "effective_session_key", lambda slot: key)
    slot = SimpleNamespace(turn_running=False, running=False, is_closing=False)
    assert chat_regenerate._destructive_history_busy(slot) is None
    _leave_record(key)
    refused = chat_regenerate._destructive_history_busy(slot)
    assert refused.status == 409
    assert b"kiro_cli_import_pending" in refused.body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "body"),
    [
        ("edit-resend", {"index": 0, "content": "edited"}),
        ("regenerate", {}),
        ("switch-variant", {"index": 1, "variant": 0}),
    ],
)
async def test_each_regenerate_route_brings_the_turns_in_before_its_guard(
    tmp_path, monkeypatch, route, body
):
    from chat_regenerate_cov80_helpers import _client

    from kiro_crew.dashboard import chat_regenerate

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    from chat_test_helpers import _make_state

    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    slot = _populated(state, "s1")
    key = chat_utils.effective_session_key(slot)
    _leave_record(key)
    recover = _recovery(fails=True)
    monkeypatch.setattr(kcl, "recover_left_open", recover)
    before = [m["content"] for m in slot.messages]
    with patch.object(chat_regenerate, "_run_chat", new=AsyncMock()):
        async with _client(state) as client:
            resp = await client.post(f"/api/chat/slots/s1/{route}", json=body)
            assert resp.status == 409, await resp.text()
            assert (await resp.json())["code"] == "kiro_cli_import_pending"
    assert recover.calls == [key]
    assert [m["content"] for m in slot.messages] == before


# ── reset-conversation and the session manager's discard ───────────────────


@pytest.mark.asyncio
async def test_a_reset_conversation_is_refused_while_the_record_is_pending(
    _floor_monkeypatch, monkeypatch
):
    from kiro_crew.dashboard import chat_handlers
    from kiro_crew.dashboard.chat import api_chat_slot_reset_conversation
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

    slot = _ChatSlot("chat-reset")
    state = MagicMock(spec=DashboardState)
    state._slots = {slot.key: slot}
    state.sessions = MagicMock()
    state.sessions.discard_conversation = AsyncMock(return_value=True)
    state.sessions.get_provider = MagicMock(return_value=None)
    _floor_monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", MagicMock())
    _floor_monkeypatch.setattr(
        chat_handlers, "subagents_attached_async", AsyncMock(return_value=False)
    )
    _floor_monkeypatch.setattr(chat_handlers, "sel", lambda: MagicMock())
    key = chat_utils.effective_session_key(slot)
    _leave_record(key)
    recover = _recovery(fails=True)
    monkeypatch.setattr(kcl, "recover_left_open", recover)

    @web.middleware
    async def _auth(request, handler):
        request["app"] = ""
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = state
    app.router.add_post(
        "/api/chat/slots/{slot}/reset-conversation", api_chat_slot_reset_conversation
    )
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(f"/api/chat/slots/{slot.key}/reset-conversation")
        assert resp.status == 409
        assert (await resp.json())["code"] == "kiro_cli_import_pending"
    assert recover.calls == [key]
    state.sessions.discard_conversation.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_session_manager_refuses_to_discard_a_conversation_with_a_left_record(
    monkeypatch,
):
    from session_pool_helpers import _make_manager

    monkeypatch.setattr(kcl, "recover_left_open", _recovery(fails=True))

    key = "dashboard:chat-discard"
    mgr, _factory = _make_manager()
    _leave_record(key)
    assert await mgr.discard_conversation(key) is False
    assert await mgr.discard_conversation(key, skip_if_busy=True) is False
    kiro_cli_handoff.forget_open(key)
    assert await mgr.discard_conversation(key) is not None


# ── the send's last check and the pre-spawn ────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("handed_over", ["held", "record"])
@pytest.mark.parametrize("original_agent", ["", "agent-a"])
async def test_handover_refusal_preserves_the_chat_send_slot_agent(
    tmp_path, monkeypatch, handed_over, original_agent
):
    from chat_test_helpers import _make_app, _make_state

    from kiro_crew.dashboard import chat_handlers

    state = _make_state(tmp_path)
    slot = _populated(state, "chat-agent-held")
    slot.agent = original_agent
    key = chat_utils.effective_session_key(slot)
    if handed_over == "held":
        assert kiro_cli_handoff.hold(key, "t")
    else:
        _leave_record(key)
    recover = _recovery(fails=True)
    monkeypatch.setattr(kcl, "recover_left_open", recover)
    assigned = MagicMock()
    monkeypatch.setattr(chat_handlers, "_emit_agent_assignment", assigned)
    before = [m["content"] for m in slot.messages]
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post(
            "/api/chat", json={"slot": slot.key, "agent": "agent-b", "message": "hi"}
        )
        assert resp.status == 409, await resp.text()
        assert slot.agent == original_agent
        code = "kiro_cli_open" if handed_over == "held" else "kiro_cli_import_pending"
        assert (await resp.json())["code"] == code
    assert [m["content"] for m in slot.messages] == before
    assert recover.calls == ([] if handed_over == "held" else [key])
    assigned.assert_not_called()
    assert slot.task is None


@pytest.mark.asyncio
async def test_a_send_recovers_left_open_turns_before_adopting_its_agent(tmp_path, monkeypatch):
    from chat_test_helpers import _make_app, _make_state, drain_background_tasks

    from kiro_crew.dashboard import chat_handlers

    state = _make_state(tmp_path)
    slot = _populated(state, "chat-agent-recovered")
    slot.agent = ""
    key = chat_utils.effective_session_key(slot)
    _leave_record(key)
    recover = _recovery(fails=False, imports="from kiro-cli")

    async def recover_before_adoption(state_, slot_, session_key):
        if kiro_cli_handoff.has_left_open(session_key):
            assert slot_.agent == ""
        return await recover(state_, slot_, session_key)

    monkeypatch.setattr(kcl, "recover_left_open", recover_before_adoption)
    monkeypatch.setattr(chat_handlers, "_run_chat", AsyncMock())
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post(
            "/api/chat", json={"slot": slot.key, "agent": "agent-b", "message": "hi"}
        )
        assert resp.status == 200, await resp.text()
        await drain_background_tasks(state)
    assert slot.agent == "agent-b"
    assert not kiro_cli_handoff.has_left_open(key)
    contents = [m["content"] for m in slot.messages]
    assert contents.index("from kiro-cli") < contents.index("hi")


@pytest.mark.asyncio
async def test_a_record_left_during_a_sends_admission_refuses_it_before_the_row(
    tmp_path, monkeypatch
):
    """An open that started and ended during the send's awaits, its import
    failing, leaves a record and no hold: the send's last check, in the step
    that records the row, refuses it rather than putting it ahead of those turns."""
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard.chat import api_chat
    from kiro_crew.dashboard.state import _ChatSlot
    from kiro_crew.shared_crew import admission_entry

    state = _make_state(tmp_path)
    slot = _ChatSlot("chat-left-raced")
    state._slots[slot.key] = slot
    key = chat_utils.effective_session_key(slot)
    real_admit = admission_entry.admit_chat_turn

    async def admit_then_leave_a_record(*args, **kwargs):
        admission = await real_admit(*args, **kwargs)
        _leave_record(key)
        return admission

    monkeypatch.setattr(admission_entry, "admit_chat_turn", admit_then_leave_a_record)

    @web.middleware
    async def _auth(request, handler):
        request["app"] = ""
        request["user"] = "local-app"
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = state
    app.router.add_post("/api/chat/send", api_chat)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/chat/send", json={"slot": slot.key, "message": "hi"})
        assert resp.status == 409
        assert (await resp.json())["code"] == "kiro_cli_import_pending"
    assert slot.messages == [] and slot.task is None


def test_the_pre_spawn_stands_down_while_a_record_is_left():
    from kiro_crew.dashboard.chat_turn import eager_spawn

    key = "dashboard:chat-prespawn"
    assert not eager_spawn._open_in_kiro_cli(key)
    _leave_record(key)
    assert eager_spawn._open_in_kiro_cli(key)


# ── prompts started without a send: session_send, workflow, heartbeat ─────


@pytest.mark.asyncio
@pytest.mark.parametrize("handed_over", ["held", "record"])
async def test_a_prompt_for_a_handed_over_chat_is_queued_not_written_ahead(
    tmp_path, monkeypatch, handed_over
):
    """``enqueue_or_run_prompt`` (session_send, a workflow's auto-turn, the
    heartbeat) queues the prompt: running it would write its user row ahead of
    kiro-cli's turns and start a turn admission refuses, losing the prompt."""
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard.chat_turn import queue_drain

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-peer")
    key = chat_utils.effective_session_key(slot)
    if handed_over == "held":
        assert kiro_cli_handoff.hold(key, "t")
    else:
        _leave_record(key)
    drained = []

    async def drain(state_, keys):
        drained.append(keys)

    monkeypatch.setattr(queue_drain, "_drain_parked_queues", drain)
    monkeypatch.setattr("kiro_crew.dashboard.chat_runner._drain_parked_queues", drain)
    run_chat = AsyncMock()
    assert slot.enqueue_or_run_prompt("from a peer", run_chat, state) is False
    assert [e["content"] for e in slot._queue] == ["from a peer"]
    assert [m for m in slot.messages if m["role"] == "user"] == []
    run_chat.assert_not_called()
    for task in list(state._background_tasks):
        await task
    # A hold's release drains it; a record has no release, so the drain starts now.
    assert drained == ([] if handed_over == "held" else [[slot.key]])


# ── monitor wakes ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_monitor_wake_on_a_handed_over_chat_is_busy_and_writes_no_row(monkeypatch):
    from test_autonudge_banner import _loop, _orchestrator

    from kiro_crew.dashboard.chat_utils import effective_session_key

    monkeypatch.setattr(kcl, "recover_left_open", _recovery(fails=True))

    orch = _orchestrator()
    slot = MagicMock()
    slot.key = "chat-1-1785"
    slot.linked_session_key = ""
    slot.running = False
    orch.dashboard_state.get_slot = MagicMock(return_value=slot)
    _leave_record(effective_session_key(slot))
    run_chat = AsyncMock()
    with (
        patch("kiro_crew.dashboard.chat._run_chat", new=run_chat),
        patch(
            "kiro_crew.slack.gateway.compose_nudge_body",
            new=AsyncMock(side_effect=lambda message, sentinel, slot_key: message),
        ),
    ):
        result = await orch._fire_dashboard_nudge(_loop())
    assert result is False, "busy: the loop owes the wake and fires it later"
    slot.append.assert_not_called()
    run_chat.assert_not_called()


# ── channel links made after the open ──────────────────────────────────────


@pytest.mark.asyncio
async def test_a_slack_link_is_refused_while_the_chat_is_handed_over(tmp_path):
    """The plan refuses to open a chat a channel reaches; a link made after the
    open would route a channel's messages past the dashboard's admission."""
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard import chat_slack

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-slack")
    assert kiro_cli_handoff.hold(chat_utils.effective_session_key(slot), "t")
    with pytest.raises(chat_slack.SlackLinkError) as refused:
        await chat_slack.link_slot_to_slack(state, slot)
    assert refused.value.code == "kiro_cli_open"


@pytest.mark.asyncio
async def test_a_mirror_link_is_refused_while_the_chat_is_handed_over(tmp_path, monkeypatch):
    from chat_mirror_helpers import _fake_transport, _make_mirror_app, _prep

    monkeypatch.setattr(kcl, "recover_left_open", _recovery(fails=True))

    state = _prep(tmp_path, monkeypatch)
    transport = _fake_transport("telegram")
    state.register_channel_transport(transport)
    slot = state.get_slot("s1")
    _leave_record(chat_utils.effective_session_key(slot))
    async with TestClient(TestServer(_make_mirror_app(state))) as client:
        resp = await client.post(
            "/api/chat/slots/s1/mirror-link",
            json={"channel_type": "telegram", "target_id": "user:123"},
        )
        assert resp.status == 409, await resp.text()
        assert (await resp.json())["code"] == "kiro_cli_import_pending"
    assert state.sessions.get_mirror_link(chat_utils.effective_session_key(slot)) is None
    transport.send_message.assert_not_awaited()


def test_the_opens_link_read_waits_for_a_mirror_claim_in_flight():
    """The mirror claim checks the hold and writes inside the map's lock. The
    open reads the Slack link first (it takes that lock, so it waits for a claim
    in flight) and the mirror flag after it, which then sees the claim."""
    claimed = {"mirror": False}

    def get_slack_link(key):
        claimed["mirror"] = True  # the claim in flight commits while this waits
        return (None, None)

    sessions = MagicMock()
    sessions.get_slack_link = get_slack_link
    sessions.mirror_accepts_inbound = lambda key: claimed["mirror"]
    assert kcl._channel_reaches(sessions, "dashboard:chat-mirror") is True


@pytest.mark.asyncio
async def test_a_drain_stands_down_when_another_path_started_a_turn_during_its_awaits(
    tmp_path, monkeypatch
):
    """The kicked drain brings the left turns in (an await); a prompt started
    without the queue meanwhile owns the slot, and a second turn beside it would
    run two turns on one session."""
    import asyncio

    from chat_test_helpers import _make_state

    from kiro_crew.dashboard import chat_runner

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-drain")
    slot.queue_append("older, queued while kiro-cli held the chat")
    key = chat_utils.effective_session_key(slot)
    _leave_record(key)
    gate = asyncio.Event()

    async def recover_while_a_turn_starts(state_, slot_, session_key):
        kiro_cli_handoff.forget_open(session_key)
        slot_.task = asyncio.ensure_future(gate.wait())  # the newcomer's turn
        await asyncio.sleep(0)
        return True

    monkeypatch.setattr(kcl, "recover_left_open", recover_while_a_turn_starts)
    spawned = MagicMock()
    monkeypatch.setattr(chat_runner, "spawn_guarded_turn", spawned)
    try:
        assert await chat_runner._start_next_queued_turn(state, slot) is False
    finally:
        gate.set()
        await slot.task
    assert [e["content"] for e in slot._queue] == ["older, queued while kiro-cli held the chat"]
    spawned.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("send_path", ["responsive", "responsive_busy", "busy", "held", "teardown"])
@pytest.mark.parametrize("handed_over", ["held", "record"])
@pytest.mark.parametrize("passthrough", [False, True])
@pytest.mark.parametrize("suspension", ["admission", "closed_check"])
async def test_queued_send_refuses_handover_during_admission(
    tmp_path, monkeypatch, send_path, handed_over, passthrough, suspension
):
    """A handover during either admission await leaves no new queue or user row."""
    import asyncio
    import json

    from chat_test_helpers import _make_app, _make_state
    from test_shared_crew_turn_outcome import ALICE, _service

    from kiro_crew.dashboard import chat_handlers, chat_runner, responsive_chat
    from kiro_crew.shared_crew import admission_entry
    from kiro_crew.shared_crew.admission import OperationKind, OperationState, ReasonCode
    from kiro_crew.shared_crew.admission_gate import Admission

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-queued-handover")
    key = chat_utils.effective_session_key(slot)
    busy = None
    if send_path in {"busy", "responsive_busy"}:
        busy = slot.task = asyncio.get_running_loop().create_future()
    monkeypatch.setattr(
        chat_handlers, "subagents_hold_user_messages", lambda *_: send_path == "held"
    )
    monkeypatch.setattr(chat_handlers, "teardown_hold_active", lambda *_: send_path == "teardown")
    monkeypatch.setattr(responsive_chat, "_steer_ready", lambda *_: False)
    monkeypatch.setattr(chat_runner, "_start_next_queued_turn", AsyncMock(return_value=False))
    monkeypatch.setattr(chat_runner, "_drain_parked_queues", AsyncMock())
    monkeypatch.setattr(chat_handlers, "_arm_pending_reset_retry", lambda *_: None)
    monkeypatch.setattr(chat_handlers, "_wake_pending_reset_retry", lambda *_: None)
    service = _service(tmp_path)
    admission = Admission(None, "passthrough")

    def hand_over():
        if handed_over == "held":
            assert kiro_cli_handoff.hold(key, "t")
        else:
            _leave_record(key)

    async def admit(*args, **kwargs):
        nonlocal admission
        assert kwargs["session_key"] == key
        assert kiro_cli_handoff.handover_refusal(key) is None
        if not passthrough:
            admission = await service.admit(
                budget_key=ALICE.id,
                session_key=key,
                operation=OperationKind.CHAT_TURN,
                idempotency_key="queued-handover-send",
                park=True,
            )
            assert admission.queued
        if suspension == "admission":
            hand_over()
        return admission

    real_closed_check = admission_entry.refuse_if_slot_closed

    async def closed_check(*args):
        result = await real_closed_check(*args)
        assert result is None
        hand_over()
        return result

    monkeypatch.setattr(admission_entry, "admit_queued_send", admit)
    if suspension == "closed_check":
        monkeypatch.setattr(admission_entry, "refuse_if_slot_closed", closed_check)
    try:
        if send_path.startswith("responsive"):
            request = MagicMock(spec=web.Request)
            request.app = {"state": state}
            response = await responsive_chat.accept(request, slot, "hi", None)
            status, body = response.status, json.loads(response.body)
        else:
            async with TestClient(TestServer(_make_app(state))) as client:
                response = await client.post(
                    "/api/chat?ws=1", json={"slot": slot.key, "message": "hi"}
                )
                status, body = response.status, await response.json()
        code = "kiro_cli_open" if handed_over == "held" else "kiro_cli_import_pending"
        assert status == 409, body
        assert body == {"error": kiro_cli_handoff.handover_refusal(key)[0], "code": code}
        assert slot._queue == []
        assert slot.messages == []
        assert slot.task is busy
        if not passthrough:
            work = await service.current_work(admission.work.work_id)
            assert work.state is OperationState.CANCELLED
            assert work.reason is ReasonCode.CANCELLED_BY_USER
        assert service.capacity(ALICE.id).crew.queued == 0
    finally:
        if busy is not None:
            busy.cancel()
        service._store.tasks.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_slack_link_recovers_left_open_before_refusing(tmp_path, monkeypatch, fails):
    from chat_test_helpers import _make_state, drain_background_tasks
    from test_chat_slack import _make_slack_app

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    state.slack_client = MagicMock()
    state.slack_client.open_dm = AsyncMock(return_value="C123")
    state.slack_client.post_message = AsyncMock(return_value="ts123")
    state.owner_id = "U123"
    state.sessions.get_slack_link = MagicMock(return_value=(None, None))
    key = chat_utils.effective_session_key(slot)
    _leave_record(key)
    recover = _recovery(fails=fails)
    monkeypatch.setattr(kcl, "recover_left_open", recover)
    async with TestClient(TestServer(_make_slack_app(state))) as client:
        response = await client.post("/api/chat/slots/s1/slack-link", json={})
        assert response.status == (409 if fails else 200), await response.text()
        if fails:
            assert (await response.json())["code"] == "kiro_cli_import_pending"
            state.slack_client.post_message.assert_not_awaited()
        await drain_background_tasks(state)
    assert recover.calls == [key]
    assert kiro_cli_handoff.has_left_open(key) is fails


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_mirror_link_recovers_left_open_before_refusing(tmp_path, monkeypatch, fails):
    from chat_mirror_helpers import _fake_transport, _make_mirror_app, _prep

    state = _prep(tmp_path, monkeypatch)
    transport = _fake_transport("telegram")
    state.register_channel_transport(transport)
    key = chat_utils.effective_session_key(state.get_slot("s1"))
    _leave_record(key)
    recover = _recovery(fails=fails)
    monkeypatch.setattr(kcl, "recover_left_open", recover)
    async with TestClient(TestServer(_make_mirror_app(state))) as client:
        response = await client.post(
            "/api/chat/slots/s1/mirror-link",
            json={"channel_type": "telegram", "target_id": "user:123"},
        )
        assert response.status == (409 if fails else 200), await response.text()
        if fails:
            assert (await response.json())["code"] == "kiro_cli_import_pending"
            transport.send_message.assert_not_awaited()
    assert recover.calls == [key]
    assert kiro_cli_handoff.has_left_open(key) is fails


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_nudge_recovers_left_open_before_refusing(monkeypatch, fails):
    import asyncio

    from test_autonudge_banner import _loop, _orchestrator

    orch = _orchestrator()
    slot = MagicMock()
    slot.key = "chat-1-1785"
    slot.linked_session_key = ""
    slot.running = False
    orch.dashboard_state.get_slot = MagicMock(return_value=slot)
    key = chat_utils.effective_session_key(slot)
    _leave_record(key)
    recover = _recovery(fails=fails)
    monkeypatch.setattr(kcl, "recover_left_open", recover)
    run_chat = AsyncMock()
    with (
        patch("kiro_crew.dashboard.chat._run_chat", new=run_chat),
        patch(
            "kiro_crew.slack.gateway.compose_nudge_body",
            new=AsyncMock(side_effect=lambda message, sentinel, slot_key: message),
        ),
    ):
        result = await orch._fire_dashboard_nudge(_loop())
        await asyncio.gather(*list(orch.dashboard_state._background_tasks))
    assert result is (not fails)
    assert recover.calls == [key]
    assert kiro_cli_handoff.has_left_open(key) is fails
    if fails:
        slot.append.assert_not_called()
        run_chat.assert_not_called()
    else:
        run_chat.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_claim_recovers_left_open_before_refusing(monkeypatch, fails):
    from session_pool_helpers import _make_manager

    from kiro_crew.agent_sdk.drivers.acp_vocab import AcpError

    mgr, _ = _make_manager()
    boundary = MagicMock()
    boundary.get_or_create = AsyncMock(return_value=(None, False, False))
    monkeypatch.setattr(mgr, "_allocation_boundary", lambda: boundary)
    key = "dashboard:claim-left"
    _leave_record(key)
    recover = _recovery(fails=fails)
    monkeypatch.setattr(kcl, "recover_left_open", recover)
    if fails:
        with pytest.raises(AcpError, match="not in the chat yet"):
            await mgr.get_or_create(key)
        boundary.get_or_create.assert_not_awaited()
    else:
        assert await mgr.get_or_create(key) == (None, False, False)
    assert recover.calls == [key]
    assert kiro_cli_handoff.has_left_open(key) is fails


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_discard_recovers_left_open_before_refusing(monkeypatch, fails):
    from session_pool_helpers import _make_manager

    mgr, _ = _make_manager()
    key = "dashboard:discard-left"
    _leave_record(key)
    recover = _recovery(fails=fails)
    monkeypatch.setattr(kcl, "recover_left_open", recover)
    assert await mgr.discard_conversation(key) is (not fails)
    assert recover.calls == [key]
    assert kiro_cli_handoff.has_left_open(key) is fails


@pytest.mark.asyncio
@pytest.mark.parametrize("dashboard", [False, True])
@pytest.mark.parametrize("fails", [False, True])
async def test_recovery_without_state_keeps_the_terminal_turns(
    tmp_path, monkeypatch, dashboard, fails
):
    import asyncio

    from chat_test_helpers import _make_state

    from kiro_crew import history
    from kiro_crew.slack import handler

    state = _make_state(tmp_path)
    slot = _populated(state, "chat-recovery")
    key = chat_utils.effective_session_key(slot)
    log = state.conversation_log
    await asyncio.to_thread(log.append, key, "user", "first question")
    _leave_record(key)
    monkeypatch.setattr(handler, "get_dashboard_state", lambda: state if dashboard else None)
    monkeypatch.setattr(history, "ConversationLog", lambda: log)
    monkeypatch.setattr(
        kcl, "_left_open_turns", lambda record: ([("assistant", "terminal answer")], 0)
    )
    if fails:
        monkeypatch.setattr(kcl, "_import_turns", AsyncMock(return_value=False))
    assert await kcl.recover_left_open(None, None, key)
    assert kiro_cli_handoff.has_left_open(key) is fails
    if not fails:
        assert "terminal answer" in [m["content"] for m in log.read_messages(key)]
        if dashboard:
            assert "terminal answer" in [m["content"] for m in slot.messages]
