"""Opening a chat in kiro-cli: the transcript stays in the order things were said.

After a restart, the turns made in kiro-cli wait in the open's record until a
recovery brings them in. A recovery that fails keeps the record to retry, and
until it succeeds nothing newer may land in the transcript ahead of those older
turns: a send is refused (its draft kept), a queued entry stays queued, and a
turn that got past both is refused at admission. The refusal rows carry their
own gateway-notice tokens, so they are shown in the dashboard's language.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import kiro_cli_handoff
from kiro_crew.dashboard import chat_utils
from kiro_crew.dashboard import gateway_notice as gn
from kiro_crew.dashboard.handlers import kiro_cli_launch as kcl
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS


@pytest.fixture(autouse=True)
def _records(_floor_monkeypatch, tmp_path):
    kiro_cli_handoff.reset_holds()
    _floor_monkeypatch.setattr(kiro_cli_handoff, "_records_dir", lambda: tmp_path / "opens")
    _floor_monkeypatch.setattr(kcl, "_recovery_locks", {})
    yield
    kiro_cli_handoff.reset_holds()


def _leave_record(key: str) -> None:
    kiro_cli_handoff.record_open(key, {"history_key": key, "created_at": "", "log_offset": 0})


def _recovery(fails: bool):
    """A recovery that finds the record and either keeps it (a transient
    failure) or brings the turns in and removes it."""
    calls = []

    async def recover(state, slot, session_key):
        calls.append(session_key)
        if not fails:
            kiro_cli_handoff.forget_open(session_key)
        return True

    recover.calls = calls
    return recover


def _send_app(state):
    from kiro_crew.dashboard.chat import api_chat

    @web.middleware
    async def _auth(request, handler):
        request["app"] = ""
        request["user"] = "local-app"
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = state
    app.router.add_post("/api/chat/send", api_chat)
    return app


@pytest.mark.asyncio
async def test_a_send_is_refused_while_a_failed_recovery_keeps_the_record(tmp_path, monkeypatch):
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard.state import _ChatSlot

    state = _make_state(tmp_path)
    slot = _ChatSlot("chat-pending")
    slot.append("user", "before kiro-cli", "msg msg-u")
    state._slots[slot.key] = slot
    key = chat_utils.effective_session_key(slot)
    _leave_record(key)
    before = list(slot.messages)
    recover = _recovery(fails=True)
    monkeypatch.setattr(kcl, "recover_left_open", recover)
    async with TestClient(TestServer(_send_app(state))) as client:
        resp = await client.post("/api/chat/send", json={"slot": slot.key, "message": "next"})
        assert resp.status == 409
        assert await resp.json() == {
            "error": kiro_cli_handoff.LEFT_OPEN_PENDING_REFUSAL,
            "code": "kiro_cli_import_pending",
        }
    assert recover.calls == [key], "the send tried the recovery first"
    assert slot.messages == before, "nothing recorded ahead of kiro-cli's turns"
    assert slot.task is None


@pytest.mark.asyncio
async def test_a_send_goes_through_once_the_recovery_brought_the_turns_in(tmp_path, monkeypatch):
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard.state import _ChatSlot

    state = _make_state(tmp_path)
    slot = _ChatSlot("chat-recovered")
    state._slots[slot.key] = slot
    _leave_record(chat_utils.effective_session_key(slot))
    monkeypatch.setattr(kcl, "recover_left_open", _recovery(fails=False))
    # The send is accepted, then stopped at the turn: the point is the 409 is gone.
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_api.chat_send._schedule_chat_task", MagicMock(), raising=False
    )
    async with TestClient(TestServer(_send_app(state))) as client:
        resp = await client.post("/api/chat/send", json={"slot": slot.key, "message": "next"})
        assert resp.status != 409, await resp.text()


async def _run(state, slot, text):
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.dashboard.chat_runner import _run_chat

    with (
        patch("kiro_crew.dashboard.chat_runner.KiroCrewConfig.load", return_value=KiroCrewConfig()),
        patch("kiro_crew.autonudge.get_instance", return_value=MagicMock()),
    ):
        await _run_chat(state, slot, text)


def _admission_state(tmp_path):
    from chat_test_helpers import _make_state

    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    reached = []

    async def get_or_create(*args, **kwargs):
        reached.append(True)
        raise RuntimeError("stop here: the turn reached provider allocation")

    state.sessions.get_or_create = get_or_create
    state.sessions.record_failure = AsyncMock()
    return state, reached


@pytest.mark.asyncio
async def test_a_turn_past_the_send_is_refused_while_the_record_is_pending(tmp_path, monkeypatch):
    state, reached = _admission_state(tmp_path)
    slot = state.get_or_create_slot("chat-admitted")
    slot.append("user", "hello", "msg msg-u")
    _leave_record(chat_utils.effective_session_key(slot))
    monkeypatch.setattr(kcl, "recover_left_open", _recovery(fails=True))
    await _run(state, slot, "hello")
    [row] = [m for m in slot.messages if m["role"] == "error"]
    assert row["content"] == kiro_cli_handoff.LEFT_OPEN_PENDING_REFUSAL
    assert row["meta"][gn.GATEWAY_NOTICE_META_KEY] == gn.GATEWAY_NOTICE_KIRO_CLI_IMPORT_PENDING
    assert reached == [], "the model never answered ahead of kiro-cli's turns"


@pytest.mark.asyncio
async def test_the_held_turn_row_carries_its_notice_token(tmp_path):
    state, reached = _admission_state(tmp_path)
    slot = state.get_or_create_slot("chat-held-token")
    slot.append("user", "hello", "msg msg-u")
    kiro_cli_handoff.hold(chat_utils.effective_session_key(slot), "t")
    await _run(state, slot, "hello")
    [row] = [m for m in slot.messages if m["role"] == "error"]
    assert row["content"] == kiro_cli_handoff.HELD_TURN_REFUSAL
    assert row["meta"][gn.GATEWAY_NOTICE_META_KEY] == gn.GATEWAY_NOTICE_KIRO_CLI_OPEN
    assert reached == []


@pytest.mark.asyncio
async def test_a_queue_waits_while_the_record_is_pending_and_drains_once_it_is_in(
    tmp_path, monkeypatch
):
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard import chat_runner

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-queued")
    key = chat_utils.effective_session_key(slot)
    _leave_record(key)
    entry = {"id": "q1", "text": "queued while kiro-cli held the chat"}
    slot._queue.append(entry)
    failing = _recovery(fails=True)
    monkeypatch.setattr(kcl, "recover_left_open", failing)
    assert await chat_runner._start_next_queued_turn(state, slot) is False
    assert slot._queue == [entry], "the entry waits, its row unwritten"
    assert failing.calls == [key], "the drain tried the recovery once more"
    monkeypatch.setattr(kcl, "recover_left_open", _recovery(fails=False))
    assert await chat_runner._queue_awaits_kiro_cli_import(state, slot) is False
    assert not kiro_cli_handoff.has_left_open(key)


def _queued_drain(tmp_path, monkeypatch):
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard import chat_runner

    state = _make_state(tmp_path)
    state.subagents = None
    slot = state.get_or_create_slot("chat-drain-reservation")
    slot.queue_append("queued message")
    spawn = MagicMock()
    run = MagicMock()
    monkeypatch.setattr(chat_runner, "spawn_guarded_turn", spawn)
    monkeypatch.setattr(chat_runner, "_run_chat", run)
    return state, slot, spawn, run


@pytest.mark.asyncio
async def test_a_queue_without_a_left_open_record_does_not_offload_before_dequeue(
    tmp_path, monkeypatch
):
    from kiro_crew.dashboard import chat_runner

    state, slot, spawn, run = _queued_drain(tmp_path, monkeypatch)
    key = chat_utils.effective_session_key(slot)
    has_left_open = kiro_cli_handoff.has_left_open
    assert not has_left_open(key)
    loop = asyncio.get_running_loop()
    reads = []

    def read_inline(session_key):
        assert asyncio.get_running_loop() is loop, "the record check must not be offloaded"
        assert slot._queue, "the record check precedes dequeue"
        reads.append(session_key)
        return has_left_open(session_key)

    monkeypatch.setattr(kiro_cli_handoff, "has_left_open", read_inline)
    recover = AsyncMock()
    monkeypatch.setattr(kcl, "recover_left_open", recover)

    assert await chat_runner._start_next_queued_turn(state, slot) is True

    assert reads == [key]
    recover.assert_not_awaited()
    assert slot._queue == []
    spawn.assert_called_once()
    run.assert_called_once()
    assert run.call_args.args[2] == "queued message"


@pytest.mark.asyncio
@pytest.mark.parametrize("reserved_at_entry", [False, True])
async def test_a_recovered_queue_respects_only_a_reservation_acquired_during_import(
    tmp_path, monkeypatch, reserved_at_entry
):
    from kiro_crew.dashboard import chat_runner

    state, slot, spawn, run = _queued_drain(tmp_path, monkeypatch)
    key = chat_utils.effective_session_key(slot)
    _leave_record(key)
    before = list(slot._queue)
    slot._turn_admission_reserved = reserved_at_entry
    own_task = slot.task

    async def recover(state, recovering_slot, session_key):
        assert recovering_slot is slot
        assert session_key == key
        assert slot._turn_admission_reserved is reserved_at_entry
        slot._turn_admission_reserved = True
        kiro_cli_handoff.forget_open(session_key)
        return True

    monkeypatch.setattr(kcl, "recover_left_open", recover)
    started = await chat_runner._start_next_queued_turn(state, slot)

    assert not kiro_cli_handoff.has_left_open(key), "the recovery succeeded"
    assert slot._turn_admission_reserved is True, "the caller owns its reservation"
    assert started is reserved_at_entry
    if reserved_at_entry:
        assert slot._queue == []
        spawn.assert_called_once()
        run.assert_called_once()
        assert run.call_args.args[2] == "queued message"
    else:
        assert slot._queue == before
        assert slot.task is own_task
        assert slot.messages == []
        spawn.assert_not_called()
        run.assert_not_called()


@pytest.mark.asyncio
async def test_a_recovery_never_settles_an_open_that_took_its_hold_during_the_read(
    monkeypatch,
):
    """The record read runs in a worker. An open can take its hold and write
    its own record meanwhile: that live record is the open's, and putting its
    session file back or removing its launch copy would give the terminal the
    agent's pre-approved tools."""
    key = "dashboard:chat-racing"

    def read_while_an_open_lands(session_key):
        # The read checked for a hold first and found none; the open lands now.
        kiro_cli_handoff.hold(session_key, "t")
        _leave_record(session_key)
        return {"history_key": session_key, "created_at": "", "log_offset": 0}

    settled = []
    # A stale record is what sends recovery to read at all.
    _leave_record(key)
    monkeypatch.setattr(kiro_cli_handoff, "read_left_open", read_while_an_open_lands)
    monkeypatch.setattr(kcl, "_left_open_turns", lambda record: settled.append(record) or ([], 0))
    slot = MagicMock()
    assert await kcl.recover_left_open(MagicMock(), slot, key) is False
    assert settled == [], "the live open's files were left alone"
    assert kiro_cli_handoff.has_left_open(key), "and its record kept"


@pytest.mark.asyncio
async def test_the_terminals_cleanup_never_drops_the_hold_while_settle_runs():
    """The terminal's cleanup (``arelease``) can land while the release's settle
    step is still importing kiro-cli's turns. The hold must stand until that
    import is done, or a chat turn could start ahead of the turns it brings in."""
    key = "dashboard:chat-settling"
    importing = asyncio.Event()
    finish = asyncio.Event()
    held_during_import = []

    async def record_turns(turns, left_out):
        importing.set()
        await finish.wait()
        held_during_import.append(kiro_cli_handoff.is_held(key))
        return True

    launch = kcl.KiroCliLaunch(
        argv=["kiro-cli"], cwd="/", session_key=key, slot_key="chat-settling", terminal_id="t"
    )
    launch._announced = True
    launch.engine = kcl.ENGINE_V3
    launch.record_turns = record_turns
    assert kiro_cli_handoff.hold(
        key, "t", on_release=launch.on_released, before_release=launch.settle
    )
    with patch.object(kcl.kiro_cli_launch_v3, "read_turns", lambda *a: ([("user", "hi")], 0)):
        releasing = asyncio.ensure_future(kiro_cli_handoff.settle_and_release(key, "t"))
        await asyncio.wait_for(importing.wait(), timeout=FALLBACK_TIMEOUT_SECS)
        await launch.arelease()
        assert kiro_cli_handoff.is_held(key), "the cleanup left the settling hold alone"
        finish.set()
        assert await asyncio.wait_for(releasing, timeout=FALLBACK_TIMEOUT_SECS) is True
    assert held_during_import == [True]
    assert not kiro_cli_handoff.is_held(key), "the settle's own release freed the chat"


def test_a_hold_is_one_hold_under_either_spelling_of_the_chats_key():
    """A legacy bare Slack key and ``slack:<thread>`` name one chat: a hold taken
    under one spelling is seen, owned and released under the other."""
    from kiro_crew.messaging.link import canonical_key

    bare = "1712793600.123456"
    spelled = canonical_key(bare)
    assert spelled != bare
    assert kiro_cli_handoff.hold(bare, "t")
    assert kiro_cli_handoff.is_held(spelled)
    assert kiro_cli_handoff.holder_of(spelled) == "t"
    assert not kiro_cli_handoff.hold(spelled, "u"), "a second open of the same chat is refused"
    assert kiro_cli_handoff.release(spelled, "t")
    assert not kiro_cli_handoff.is_held(bare)
