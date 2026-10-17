"""Open tabs past the newest few restore as sidebar rows whose window loads later.

A sidebar row needs a tab's metadata and its newest rows, not its whole
transcript. Past ``EAGER_WINDOW_RESTORES`` tabs the startup open-tab restore
builds the slot from the metadata line and a bounded tail only; the window loads
on the slot's first read of ``messages`` (or off the loop, when the tab is
opened). These tests pin that:

* only the eager tabs read their whole transcript at boot;
* the sidebar summary of a pending row matches the loaded one, without loading;
* the first read of ``messages`` loads exactly the window an eager restore builds;
* a save of a pending row never rewrites the transcript, and keeps a metadata edit;
* a row appended to a pending row lands after the full history;
* a failed load fails closed and retries.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard import chat_persistence as cp
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard import state as dashboard_state
from kiro_crew.dashboard.chat_utils import _history_key_for
from kiro_crew.dashboard.state import load_window_off_loop

_KEYS = ["chat-1-alpha", "chat-2-beta", "chat-3-gamma"]


def _seed(state, key: str, turns: int) -> None:
    log = state.conversation_log
    history_key = _history_key_for(key)
    for i in range(turns):
        log.append(history_key, "user", f"{key} question {i}")
        log.append(history_key, "assistant", f"{key} answer {i}")


def _write_snapshot(tmp_path, keys: list[str]) -> None:
    (tmp_path / "open_slots.json").write_text(json.dumps({"keys": keys, "ts": 0.0}))


def _count_full_reads(monkeypatch, conv_log) -> list[str]:
    reads: list[str] = []
    real = conv_log.read_messages_chained

    def _spy(key):
        reads.append(key)
        return real(key)

    monkeypatch.setattr(conv_log, "read_messages_chained", _spy)
    return reads


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    seed_state = _make_state(tmp_path / "sessions")
    for turns, key in enumerate(_KEYS, start=3):
        _seed(seed_state, key, turns)
    _write_snapshot(tmp_path, _KEYS)
    monkeypatch.setattr(cp, "EAGER_WINDOW_RESTORES", 1)
    return tmp_path


def _restore(tmp_path):
    state = _make_state(tmp_path / "sessions")
    restored = asyncio.run(cp.restore_open_slots_async(state))
    return state, restored


def test_only_the_eager_tabs_read_their_whole_transcript(seeded, monkeypatch):
    state = _make_state(seeded / "sessions")
    reads = _count_full_reads(monkeypatch, state.conversation_log)

    restored = asyncio.run(cp.restore_open_slots_async(state))

    assert restored == len(_KEYS)
    pending = [key for key in _KEYS if state._slots[key].window_pending]
    assert len(pending) == len(_KEYS) - 1
    assert len(reads) == 1
    # Serializing the sidebar does not load a pending row.
    state.serialize_slots()
    assert [key for key in _KEYS if state._slots[key].window_pending] == pending
    assert len(reads) == 1


def test_a_pending_row_reads_only_the_end_of_its_transcript(seeded, monkeypatch):
    from kiro_crew import history_projection

    whole_file_reads: list[str] = []
    projection = history_projection.TranscriptReadProjection
    for name in ("_read_messages", "_row_index"):
        real = getattr(projection, name)

        def _spy(self, read_key, *args, _real=real, **kwargs):
            whole_file_reads.append(read_key)
            return _real(self, read_key, *args, **kwargs)

        monkeypatch.setattr(projection, name, _spy)

    state, _ = _restore(seeded)

    pending = {_history_key_for(k) for k in _KEYS if state._slots[k].window_pending}
    assert pending
    assert not pending & set(whole_file_reads), "a sidebar row decoded its whole transcript"
    assert all(state._slots[k].sidebar_messages() for k in _KEYS)


def _append_ending(state, key: str, ending: str) -> None:
    log = state.conversation_log
    history_key = _history_key_for(key)
    if ending == "answered":
        return
    # A cycle that ends through the quiet-end directive: its tool row carries the
    # trusted identity, plus the flag the runner stamps only when it applied.
    log.append(history_key, "user", f"{key} patrol cycle")
    identity = {
        "tool_name": dashboard_state.QUIET_END_TOOL,
        "mcp_server": dashboard_state.QUIET_END_SERVER,
    }
    if ending == "quiet_end":
        identity["ends_turn"] = True
    log.append(history_key, "tool", "nothing_to_do", extra_meta=identity)


@pytest.mark.parametrize(
    ("ending", "interrupted"),
    [("answered", False), ("quiet_end", False), ("refused_quiet_end", True)],
)
def test_a_pending_rows_summary_matches_the_loaded_one(seeded, monkeypatch, ending, interrupted):
    seed_state = _make_state(seeded / "sessions")
    for key in _KEYS:
        _append_ending(seed_state, key, ending)
    monkeypatch.setattr(cp, "EAGER_WINDOW_RESTORES", 0)
    lazy_state, _ = _restore(seeded)
    assert all(lazy_state._slots[key].window_pending for key in _KEYS)
    lazy_rows = {row["key"]: row for row in lazy_state.serialize_slots()}

    for key in _KEYS:
        lazy_state._slots[key].load_window()
    loaded_rows = {row["key"]: row for row in lazy_state.serialize_slots()}

    for key in _KEYS:
        for field in ("title", "last_ts", "last_message", "waiting_for_input", "interrupted"):
            assert lazy_rows[key][field] == loaded_rows[key][field], (key, field)
        assert loaded_rows[key]["interrupted"] is interrupted, key
        assert lazy_rows[key]["messages"] > 0


def test_the_first_read_loads_the_window_an_eager_restore_builds(seeded, monkeypatch):
    lazy_state, _ = _restore(seeded)
    monkeypatch.setattr(cp, "EAGER_WINDOW_RESTORES", len(_KEYS))
    eager_state, _ = _restore(seeded)

    for key in _KEYS:
        lazy_slot = lazy_state._slots[key]
        eager_slot = eager_state._slots[key]
        assert not eager_slot.window_pending

        loaded = [(m["role"], m["content"], m["ts"]) for m in lazy_slot.messages]

        assert loaded == [(m["role"], m["content"], m["ts"]) for m in eager_slot.messages]
        assert not lazy_slot.window_pending
        assert lazy_slot._disk_window_len == eager_slot._disk_window_len
        assert lazy_slot._disk_older_count == eager_slot._disk_older_count
        assert lazy_slot._resumed_count == eager_slot._resumed_count


def test_opening_a_tab_loads_its_window_off_the_loop(seeded):
    state, _ = _restore(seeded)
    slot = next(s for s in state._slots.values() if s.window_pending)

    asyncio.run(load_window_off_loop(slot))

    assert not slot.window_pending
    assert slot.loaded_messages()


def test_a_pending_rows_save_keeps_its_transcript_and_its_metadata_edit(seeded):
    state, _ = _restore(seeded)
    key = next(k for k in _KEYS if state._slots[k].window_pending)
    slot = state._slots[key]
    history_key = _history_key_for(key)
    before = state.conversation_log.read_messages_chained(history_key)

    slot.pinned = True
    slot._dirty = True
    state._flush_dirty_slots()

    assert slot.window_pending, "a save must not load the window"
    assert state.conversation_log.read_messages_chained(history_key) == before
    assert state.conversation_log.get_metadata(history_key).get("pinned") is True


def test_a_metadata_edit_owed_before_the_load_is_still_owed_after_it(seeded):
    state, _ = _restore(seeded)
    slot = next(s for s in state._slots.values() if s.window_pending)
    slot._dirty = True

    slot.load_window()

    assert slot._dirty


def test_a_row_appended_to_a_pending_row_lands_after_its_history(seeded):
    state, _ = _restore(seeded)
    key = next(k for k in _KEYS if state._slots[k].window_pending)
    slot = state._slots[key]
    history_key = _history_key_for(key)
    before = [m["content"] for m in state.conversation_log.read_messages_chained(history_key)]

    slot.append("user", "a new question", "msg msg-u", broadcast=False)
    cp._save_slot_to_history(state, slot)

    after = [m["content"] for m in state.conversation_log.read_messages_chained(history_key)]
    assert after == [*before, "a new question"]


def test_a_failed_load_fails_closed_and_retries(seeded, monkeypatch):
    state, _ = _restore(seeded)
    slot = next(s for s in state._slots.values() if s.window_pending)
    real = state.conversation_log.read_messages_chained
    calls = {"n": 0}

    def _flaky(key):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("transient")
        return real(key)

    monkeypatch.setattr(state.conversation_log, "read_messages_chained", _flaky)

    with pytest.raises(OSError):
        _ = slot.messages
    assert slot.window_pending
    assert slot.loaded_messages() == []

    assert slot.messages
    assert not slot.window_pending


def test_a_window_snapshot_taken_before_the_load_stays_as_it_was(seeded):
    state, _ = _restore(seeded)
    slot = state._slots[_pending_key(state)]
    # What a flush-thread save holds while the loop replays the window.
    before = slot.loaded_messages()

    slot.load_window()

    assert before == [], "a save's snapshot must not fill in place with the replay"
    assert slot.loaded_messages() == slot.messages
    assert slot.messages


def test_a_reader_on_another_thread_waits_for_the_whole_window(seeded, monkeypatch):
    state, _ = _restore(seeded)
    key = _pending_key(state)
    slot = state._slots[key]
    conv_log = state.conversation_log
    expected = [m["content"] for m in conv_log.read_messages_chained(_history_key_for(key))]
    in_replay = threading.Event()
    release = threading.Event()
    second_reader_moved = threading.Event()
    armed = threading.Event()
    real_replay = cp._load_rehydrated_window
    real_read = conv_log.read_messages_chained

    def _paused(*args, **kwargs):
        in_replay.set()
        release.wait(5)
        return real_replay(*args, **kwargs)

    def _read(history_key):
        if armed.is_set():
            # The second reader found the row still pending and is loading it.
            second_reader_moved.set()
        return real_read(history_key)

    monkeypatch.setattr(cp, "_load_rehydrated_window", _paused)
    monkeypatch.setattr(conv_log, "read_messages_chained", _read)
    seen: list[str] = []

    def _second_reader():
        try:
            seen.extend(m["content"] for m in slot.messages)
        finally:
            second_reader_moved.set()

    first = threading.Thread(target=lambda: slot.messages)
    second = threading.Thread(target=_second_reader)
    first.start()
    try:
        assert in_replay.wait(5)
        # The first reader is mid-replay, so the window holds nothing yet.
        armed.set()
        second.start()
        assert second_reader_moved.wait(5)
    finally:
        release.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)

    assert seen == expected
    assert not slot.window_pending


@pytest.mark.asyncio
async def test_a_load_on_another_thread_never_holds_the_loop_behind_its_read(seeded, monkeypatch):
    state = await _restore_on_loop(seeded)
    slow_key, other_key = [k for k in _KEYS if state._slots[k].window_pending]
    conv_log = state.conversation_log
    real = conv_log.read_messages_chained
    reading = threading.Event()
    release = threading.Event()
    slow_read_done = threading.Event()

    def _slow(history_key):
        if history_key == _history_key_for(slow_key):
            reading.set()
            # Released only after the other tab has loaded; the bound only
            # keeps a loop held behind this read from hanging the run.
            release.wait(5)
            slow_read_done.set()
        return real(history_key)

    monkeypatch.setattr(conv_log, "read_messages_chained", _slow)
    loader = threading.Thread(target=lambda: state._slots[slow_key].messages)
    loader.start()
    try:
        assert await asyncio.to_thread(reading.wait, 5)
        await load_window_off_loop(state._slots[other_key])
        other_loaded_during_the_slow_read = not slow_read_done.is_set()
    finally:
        release.set()
        await asyncio.to_thread(loader.join, 5)

    assert not state._slots[other_key].window_pending
    assert other_loaded_during_the_slow_read, "the loop waited behind another tab's read"
    assert not state._slots[slow_key].window_pending


@pytest.mark.parametrize(
    ("meta", "can_wait"),
    [
        ({}, True),
        ({"origin": "user"}, True),
        ({"deferred_notes": [{"id": "n1"}]}, False),
        ({"turn_in_flight_generation": 3}, False),
        ({"linked_session_key": "cron:job-1"}, False),
        ({"app": "spec-builder"}, False),
        ({"created_by": "dashboard:chat-9"}, False),
        ({"remote_slot": "peer-chat-1"}, False),
        ({"origin": "cron"}, False),
        ({"origin": "system"}, False),
    ],
)
def test_only_a_persons_own_settled_tab_waits(meta, can_wait):
    assert cp._window_can_wait(meta) is can_wait


@pytest.mark.parametrize(
    ("past_eager", "looped", "as_row"),
    [
        (False, set(), False),
        (True, set(), True),
        (True, {"chat-2-beta"}, False),
        (True, None, False),
    ],
)
def test_a_loop_driven_tab_loads_at_boot(past_eager, looped, as_row):
    restored = cp.EAGER_WINDOW_RESTORES - (0 if past_eager else 1)
    assert cp._may_restore_as_row(restored, "chat-2-beta", looped) is as_row


_ON_LOOP = "on the event loop"


def _loads_on_loop(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if _ON_LOOP in r.getMessage()]


async def _restore_on_loop(tmp_path):
    state = _make_state(tmp_path / "sessions")
    await cp.restore_open_slots_async(state)
    return state


def _pending_key(state) -> str:
    return next(k for k in _KEYS if state._slots[k].window_pending)


def test_a_read_on_the_loop_still_loads_and_says_so(seeded, caplog):
    state, _ = _restore(seeded)
    slot = state._slots[_pending_key(state)]

    async def _read():
        return list(slot.messages)

    with caplog.at_level(logging.WARNING):
        rows = asyncio.run(_read())

    assert rows and not slot.window_pending
    assert _loads_on_loop(caplog)


@pytest.mark.asyncio
async def test_a_per_slot_route_loads_the_window_off_the_loop(seeded, caplog):
    state = await _restore_on_loop(seeded)
    key = _pending_key(state)

    with caplog.at_level(logging.WARNING):
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get(f"/api/chat/slots/{key}")
            assert resp.status == 200

    assert not state._slots[key].window_pending
    assert not _loads_on_loop(caplog)


@pytest.mark.asyncio
async def test_a_send_to_a_pending_row_loads_it_off_the_loop(seeded, caplog, monkeypatch):
    state = await _restore_on_loop(seeded)
    key = _pending_key(state)

    async def _reply(_state, slot, _message, **_kwargs):
        slot.append("assistant", "reply")
        slot.append("done", "", "done", broadcast=False)

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", _reply)
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._maybe_auto_title", AsyncMock())

    with caplog.at_level(logging.WARNING):
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat", json={"slot": key, "message": "hello"})
            assert resp.status == 200
            await resp.read()

    contents = [m["content"] for m in state._slots[key].messages]
    assert contents.index(f"{key} question 0") < contents.index("hello")
    assert not _loads_on_loop(caplog)


@pytest.mark.asyncio
async def test_a_bulk_model_switch_loads_a_pending_row_off_the_loop(seeded, caplog):
    # The bulk route names no slot, so no middleware loads the window first;
    # the session-switch notice has to read the history to see a started chat.
    from kiro_crew.dashboard.chat import api_chat_slots_model
    from kiro_crew.dashboard.system_notices import SESSION_SWITCH_KIND

    state = await _restore_on_loop(seeded)
    state.sessions.reset = AsyncMock()
    key = _pending_key(state)
    app = _make_app(state)
    app.router.add_post("/api/chat/slots/model", api_chat_slots_model)

    with caplog.at_level(logging.WARNING):
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/model", json={"model": "claude-opus-4.8"})
            assert resp.status == 200

    assert not _loads_on_loop(caplog)
    rows = state._slots[key].messages
    assert rows[0]["content"] == f"{key} question 0"
    assert (rows[-1].get("meta") or {}).get("kind") == SESSION_SWITCH_KIND


@pytest.mark.asyncio
async def test_bulk_model_switch_survives_an_unreadable_pending_window(seeded, caplog, monkeypatch):
    from kiro_crew.dashboard.chat import api_chat_slots_model
    from kiro_crew.dashboard.system_notices import SESSION_SWITCH_KIND

    state = await _restore_on_loop(seeded)
    key = _pending_key(state)
    slot = state._slots[key]
    later_key = next(k for k in _KEYS[_KEYS.index(key) + 1 :] if k in state._slots)
    # Make the pending slot the first iteration, so later success proves isolation.
    state._slots = {key: slot, later_key: state._slots[later_key]}
    path = state.conversation_log._path(_history_key_for(key))
    before = path.read_bytes().splitlines()[1:]
    read_messages = state.conversation_log.read_messages_chained

    def unreadable_window(history_key):
        if history_key == _history_key_for(key):
            # Fail at the strict decoder without racing restore's metadata writes.
            b"\xff".decode("utf-8")
        return read_messages(history_key)

    monkeypatch.setattr(state.conversation_log, "read_messages_chained", unreadable_window)
    state.sessions.reset = AsyncMock(return_value=True)
    push = MagicMock(wraps=state.push_slots_update)
    monkeypatch.setattr(state, "push_slots_update", push)
    app = _make_app(state)
    app.router.add_post("/api/chat/slots/model", api_chat_slots_model)
    target = "claude-opus-4.8"

    with caplog.at_level(logging.WARNING):
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/model", json={"model": target})
            assert resp.status == 200
            body = await resp.json()

    assert body["switched"] == [key, later_key]
    assert body["failed"] == []
    assert slot.model == state._slots[later_key].model == target
    assert state.sessions.reset.await_count == 2
    push.assert_called_once()
    assert slot.window_pending and slot.loaded_messages() == []
    assert path.read_bytes().splitlines()[1:] == before
    assert state._slots[later_key].messages[-1]["meta"]["kind"] == SESSION_SWITCH_KIND
    assert "Skipping session-switch notice" in caplog.text
    assert not _loads_on_loop(caplog)


_THREAD_TS = "1790000000.000100"


def _link_thread(state, key: str) -> None:
    slot = state._slots[key]
    slot._slack_linked = True
    slot._slack_thread_ts = _THREAD_TS
    slot._slack_channel = "C1"
    state._slack_to_slot[_THREAD_TS] = key


async def _reply_in_linked_thread(state, monkeypatch, text: str = "hello") -> bool:
    from kiro_crew.slack import handler

    monkeypatch.setattr("kiro_crew.dashboard.chat._run_chat", AsyncMock())
    slack = MagicMock(post_message=AsyncMock())
    with (
        patch.object(handler, "_dashboard_state", state),
        patch.object(handler, "is_allowed_user", return_value=True),
    ):
        return await handler.maybe_route_linked_thread(
            text, f"slack:{_THREAD_TS}", "U1", "C1", slack, _THREAD_TS
        )


@pytest.mark.asyncio
async def test_a_reply_in_a_linked_thread_loads_its_pending_row_off_the_loop(
    seeded, caplog, monkeypatch
):
    state = await _restore_on_loop(seeded)
    key = _pending_key(state)
    _link_thread(state, key)

    with caplog.at_level(logging.WARNING):
        routed = await _reply_in_linked_thread(state, monkeypatch)

    assert routed is True
    contents = [m["content"] for m in state._slots[key].messages]
    assert contents.index(f"{key} question 0") < contents.index("hello")
    assert not _loads_on_loop(caplog)


@pytest.mark.asyncio
async def test_a_thread_relinked_during_the_load_delivers_to_its_new_tab(
    seeded, caplog, monkeypatch
):
    state = await _restore_on_loop(seeded)
    first, second = [k for k in _KEYS if state._slots[k].window_pending]
    _link_thread(state, first)
    real = cp._PendingWindow.read

    def _relink_during_read(self, slot):
        if slot.key == first:
            state._slots[first]._slack_linked = False
            _link_thread(state, second)
        return real(self, slot)

    monkeypatch.setattr(cp._PendingWindow, "read", _relink_during_read)

    with caplog.at_level(logging.WARNING):
        routed = await _reply_in_linked_thread(state, monkeypatch)

    assert routed is True
    assert "hello" not in [m["content"] for m in state._slots[first].messages]
    contents = [m["content"] for m in state._slots[second].messages]
    assert contents.index(f"{second} question 0") < contents.index("hello")


def _seed_card_rows(seeded, surface: str) -> None:
    from kiro_crew.dashboard import chat_cards

    seed_state = _make_state(seeded / "sessions")
    status = chat_cards._PROPOSED[surface]
    for key in _KEYS:
        card = {"surface": surface, "id": f"card-{key}", "slot": key, "status": status}
        seed_state.conversation_log.append(
            _history_key_for(key), chat_cards.ROLE_CARD, "Proposal", extra_meta={"card": card}
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(("surface", "status"), [("change", "cancelled"), ("guide", "cancelled")])
async def test_a_card_outcome_for_a_pending_row_loads_it_off_the_loop(
    seeded, caplog, surface, status
):
    from kiro_crew.dashboard import chat_cards
    from kiro_crew.dashboard.handlers import change_cards, guide

    _seed_card_rows(seeded, surface)
    state = await _restore_on_loop(seeded)
    key = _pending_key(state)
    item = f"card-{key}"
    public = {"slot_key": key, "status": status}
    public.update({"id": item} if surface == "change" else {"guide_id": item})
    broadcast = change_cards._broadcast if surface == "change" else guide._broadcast

    with caplog.at_level(logging.WARNING):
        await broadcast(state, public)

    assert not _loads_on_loop(caplog)
    row = chat_cards.find_row(state._slots[key], surface, item)
    assert row is not None and row["meta"]["card"]["status"] == status
    assert not _loads_on_loop(caplog)


@pytest.mark.asyncio
async def test_a_session_control_target_loads_off_the_loop(seeded, caplog):
    state = await _restore_on_loop(seeded)
    key = _pending_key(state)

    with caplog.at_level(logging.WARNING):
        await sc.load_target_window(state, key)

    assert not state._slots[key].window_pending
    assert not _loads_on_loop(caplog)


class _Stop(Exception):
    pass


def _stopping_spy(calls: list):
    async def _spy(*args):
        calls.append(args)
        raise _Stop

    return _spy


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["send", "retry"])
async def test_a_delivering_verb_loads_its_target_before_its_gate(tmp_path, monkeypatch, verb):
    calls: list = []
    monkeypatch.setattr(sc, "load_target_window", _stopping_spy(calls))
    state = _make_state(tmp_path)

    with pytest.raises(_Stop):
        if verb == "send":
            await sc.send_to_target(
                state, caller_session_key="dashboard:caller", target="chat-x", message="hi"
            )
        else:
            await sc.retry_target(state, caller_session_key="dashboard:caller", target="chat-x")

    assert calls == [(state, "chat-x")]


@pytest.mark.asyncio
async def test_the_read_verb_loads_its_target_before_its_gate(tmp_path, monkeypatch):
    from kiro_crew.dashboard.handlers import session_control as sc_handlers

    calls: list = []
    monkeypatch.setattr(sc, "load_target_window", _stopping_spy(calls))
    monkeypatch.setattr(sc_handlers, "_require_internal", AsyncMock(return_value=None))
    app = web.Application()
    app["state"] = state = _make_state(tmp_path)
    request = make_mocked_request("GET", "/api/session-control/read?target=chat-x", app=app)

    with pytest.raises(_Stop):
        await sc_handlers.api_session_control_read(request)

    assert calls == [(state, "chat-x")]


@pytest.mark.asyncio
async def test_a_message_to_a_live_session_loads_it_first(monkeypatch):
    from kiro_crew.dashboard.handlers import messaging

    calls: list = []
    monkeypatch.setattr(messaging, "load_window_off_loop", _stopping_spy(calls))
    slot = MagicMock(window_pending=True)
    state = MagicMock()
    state.get_slot.return_value = slot
    monkeypatch.setattr(messaging, "_resolve_session_target", lambda *_a: ("chat-x", "job"))
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/send-message", messaging.api_send_message)

    with patch("kiro_crew.sel.sel"):
        async with TestClient(TestServer(app)) as client:
            await client.post(
                "/api/send-message",
                json={"text": "done", "session": "origin", "caller_session": "cron:job"},
            )

    assert calls == [(slot,)]


@pytest.mark.asyncio
async def test_a_workflow_result_loads_its_originating_tab_first(monkeypatch):
    from kiro_crew.dashboard import workflow_inject

    calls: list = []
    monkeypatch.setattr(workflow_inject, "load_window_off_loop", _stopping_spy(calls))
    slot = MagicMock()
    state = MagicMock()
    state.get_slot.return_value = slot

    with pytest.raises(_Stop):
        await workflow_inject.inject_bound_workflow_result(
            state, "run-1", {"session_key": "dashboard:chat-x"}
        )

    assert calls == [(slot,)]
    state.get_slot.assert_called_with("chat-x")


_FAKE_PAT = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


def test_a_pending_rows_tail_is_redacted_like_its_window(seeded, monkeypatch):
    seed_state = _make_state(seeded / "sessions")
    leak = f"pushed to https://{_FAKE_PAT}@github.com/acme/repo/pull/7"
    raw_row = {"role": "assistant", "content": leak, "ts": "2026-10-10T12:00:00+00:00"}
    for key in _KEYS:
        # Written past the append path, which redacts: a row from an older build
        # or a hand-edited transcript reaches disk raw.
        path = seed_state.conversation_log._path(_history_key_for(key))
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(raw_row) + "\n")
    state, _ = _restore(seeded)
    slot = state._slots[_pending_key(state)]

    tail = slot.sidebar_messages()
    row = next(r for r in state.serialize_slots() if r["key"] == slot.key)

    assert slot.window_pending
    assert any(str(m.get("content", "")).startswith("pushed to ") for m in tail)
    assert all(_FAKE_PAT not in str(m.get("content", "")) for m in tail)
    assert _FAKE_PAT not in json.dumps(row)


def test_the_card_seed_asks_a_pending_row_without_loading_it(seeded):
    from kiro_crew.dashboard.card_lifecycle import CardLifecycle

    state, _ = _restore(seeded)
    slot = state._slots[_pending_key(state)]
    cards = MagicMock(enabled=True, state=state)
    cards._eligible.return_value = False

    CardLifecycle.notify(cards, slot, "restored")

    assert slot.window_pending
    cards.publisher.forget.assert_called_once_with(slot.key)


@pytest.fixture
def outbox_dir():
    import shutil
    import tempfile
    from pathlib import Path

    # Not ``tmp_path``: its macOS per-user segment trips the outbox route's
    # path redaction (see test_outbox_binary.py). Everything after the mkdtemp
    # sits inside the ``try``, so no failure can leave the directory behind.
    base = Path(tempfile.mkdtemp())
    try:
        odir = base / "outbox"
        odir.mkdir()
        with patch("kiro_crew.config.loader.outbox_dir", return_value=odir):
            yield odir
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.mark.asyncio
async def test_a_file_sent_to_a_pending_row_loads_it_off_the_loop(seeded, outbox_dir, caplog):
    from kiro_crew.dashboard.handlers.files import api_outbox_notify

    state = await _restore_on_loop(seeded)
    key = _pending_key(state)
    report = outbox_dir / "report.txt"
    report.write_text("the report", encoding="utf-8")
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/outbox/notify", api_outbox_notify)

    with caplog.at_level(logging.WARNING), patch("kiro_crew.dashboard.handlers.files._sel"):
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/outbox/notify",
                json={"path": str(report), "filename": "report.txt", "size": 10},
                headers={"X-Session-Key": f"dashboard:{key}"},
            )
            assert resp.status == 200

    roles = [m["role"] for m in state._slots[key].messages]
    assert roles[-1] == "file" and roles[0] == "user"
    assert not _loads_on_loop(caplog)


def test_a_pending_rows_tail_keeps_only_bounded_summary_fields():
    huge = "head " + "x" * (cp.TAIL_TEXT_BOUND * 4) + "\n[OPTIONS: Ship it | Hold]"
    rows = [
        {
            "role": "assistant",
            "content": huge,
            "ts": "2026-10-10T12:00:00+00:00",
            "cls": "msg msg-a",
            "meta": {"kind": "note", "tool_input": "y" * 100_000, "variants": [{"a": 1}] * 50},
            "variants": ["z" * 10_000],
        }
    ]

    (row,) = cp._display_tail(rows)

    assert set(row) == {"role", "content", "ts", "cls", "meta"}
    assert row["meta"] == {"kind": "note"}
    assert len(row["content"]) <= cp.TAIL_TEXT_BOUND + 3
    assert row["content"].startswith("head ")
    assert row["content"].endswith("[OPTIONS: Ship it | Hold]")


@pytest.mark.asyncio
async def test_a_heartbeat_to_a_pending_row_loads_it_off_the_loop(seeded, caplog):
    from kiro_crew.slack.gateway import GatewayOrchestrator

    state = await _restore_on_loop(seeded)
    key = _pending_key(state)
    gateway = MagicMock(dashboard_state=state)

    with caplog.at_level(logging.WARNING):
        slot = await GatewayOrchestrator._resolve_loaded_slot(gateway, key)

    assert slot is state._slots[key]
    assert not slot.window_pending
    assert not _loads_on_loop(caplog)


@pytest.mark.asyncio
async def test_a_nudge_into_a_pending_row_loads_it_off_the_loop(seeded, caplog):
    from kiro_crew.slack.gateway import GatewayOrchestrator

    state = await _restore_on_loop(seeded)
    key = _pending_key(state)
    slot = state._slots[key]
    lookups = iter([slot, None])
    live = MagicMock(get_slot=lambda _key: next(lookups))
    gateway = MagicMock(dashboard_state=live)
    loop = MagicMock(id="loop-1", slot_key=key)

    with caplog.at_level(logging.WARNING):
        fired = await GatewayOrchestrator._fire_dashboard_nudge(gateway, loop)

    assert not slot.window_pending
    assert fired is False, "a tab closed during the load takes no nudge"
    assert not _loads_on_loop(caplog)


def test_a_banner_sweep_leaves_a_pending_row_pending(seeded):
    from kiro_crew.dashboard.chat_utils import _broadcast_expired_oauth_banners

    state, _ = _restore(seeded)
    slot = state._slots[_pending_key(state)]

    _broadcast_expired_oauth_banners(state, slot)

    assert slot.window_pending


def test_a_pending_rows_loader_keeps_no_raw_metadata_line(seeded):
    state, _ = _restore(seeded)
    slot = state._slots[_pending_key(state)]

    assert slot._lazy_window._applied.meta == {}
    slot.load_window()
    assert slot.messages and not slot.window_pending


@pytest.mark.asyncio
async def test_a_transfer_of_a_pending_row_loads_it_off_the_loop(seeded, caplog):
    from kiro_crew.dashboard.session_transfer import build_transfer_bundle_async

    state = await _restore_on_loop(seeded)
    key = _pending_key(state)
    slot = state._slots[key]

    with caplog.at_level(logging.WARNING):
        bundle = await build_transfer_bundle_async(state, slot)

    assert not slot.window_pending
    assert bundle is not None
    assert not _loads_on_loop(caplog)


@pytest.mark.asyncio
async def test_a_resume_of_a_live_pending_row_loads_it_off_the_loop(seeded, caplog):
    # Through the facade: the owner's functions run on its namespace.
    from kiro_crew.dashboard import chat_handlers

    state = await _restore_on_loop(seeded)
    key = _pending_key(state)
    slot = state._slots[key]

    with caplog.at_level(logging.WARNING):
        payload = await chat_handlers._live_slot_resume_payload(state, slot)

    assert payload["key"] == key and payload["messages"]
    assert not slot.window_pending
    assert not _loads_on_loop(caplog)


@pytest.mark.asyncio
async def test_closing_a_pending_row_never_loads_it(seeded, caplog):
    state = await _restore_on_loop(seeded)
    key = _pending_key(state)
    slot = state._slots[key]
    history_key = _history_key_for(key)
    before = state.conversation_log.read_messages_chained(history_key)

    with caplog.at_level(logging.WARNING):
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.delete(f"/api/chat/slots/{key}")
            assert resp.status == 200

    assert key not in state._slots
    assert slot.window_pending, "closing a tab must not load its transcript"
    assert state.conversation_log.read_messages_chained(history_key) == before
    assert state.conversation_log.get_metadata(history_key).get("closed") is True
    assert not _loads_on_loop(caplog)


def test_a_health_snapshot_leaves_a_pending_row_pending(seeded):
    from kiro_crew.dashboard.session_health import snapshot_slot

    state, _ = _restore(seeded)
    slot = state._slots[_pending_key(state)]

    snap = snapshot_slot(slot)

    assert slot.window_pending
    assert snap.last_message_ts
