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
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard import chat_persistence as cp
from kiro_crew.dashboard import session_control as sc
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


def test_a_pending_rows_summary_matches_the_loaded_one(seeded):
    lazy_state, _ = _restore(seeded)
    lazy_rows = {row["key"]: row for row in lazy_state.serialize_slots()}

    for key in _KEYS:
        lazy_state._slots[key].load_window()
    loaded_rows = {row["key"]: row for row in lazy_state.serialize_slots()}

    for key in _KEYS:
        for field in ("title", "last_ts", "last_message", "waiting_for_input", "interrupted"):
            assert lazy_rows[key][field] == loaded_rows[key][field], (key, field)
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
