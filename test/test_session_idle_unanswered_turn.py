"""Tests for recording unanswered turns when idle sweep expires a session (#26823)."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.chat_utils_completion import wire_session_subagent_probe
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.history import ConversationLog
from kiro_crew.session import SessionManager
from kiro_crew.session_parts.sweeps import (
    FAILED_TURN_TEXT,
    TURN_FAILED_META,
)


def _make_dummy_session(last_used: float) -> MagicMock:
    entry = MagicMock()
    entry.last_used = last_used
    entry.semaphore = asyncio.BoundedSemaphore(1)
    entry.cancelled = False
    entry.work_dir = None
    entry.token = None
    entry.provider = MagicMock()
    entry.provider.shutdown = AsyncMock()
    return entry


@pytest.fixture
def tmp_conv_log(tmp_path):
    log = ConversationLog(tmp_path)
    log.init()
    return log


@pytest.fixture
def session_mgr(tmp_conv_log):
    cfg = KiroCrewConfig()
    cfg.session.timeout_secs = 60
    mgr = SessionManager(cfg, conversation_log=tmp_conv_log)
    return mgr


@pytest.mark.asyncio
async def test_unanswered_disk_path(session_mgr, tmp_conv_log, caplog):
    key = "dashboard:test-slot-1"
    tmp_conv_log.append(key, role="user", content="Hello assistant", cls="msg msg-u")

    entry = _make_dummy_session(last_used=0.0)
    session_mgr._sessions[key] = entry

    with caplog.at_level(logging.ERROR, logger="kiro_crew.session"):
        await session_mgr._expire_idle(timeout_secs=60)

    assert key not in session_mgr._sessions
    assert any("expired with unanswered turn" in record.message for record in caplog.records)

    path = tmp_conv_log._path(key)
    tail = tmp_conv_log._read_tail_messages(path, 10, None)
    assert len(tail) >= 2
    last_row = tail[-1]
    assert last_row["role"] == "error"
    assert last_row["content"] == FAILED_TURN_TEXT
    assert last_row.get("cls") == "msg msg-err"
    assert last_row.get("meta", {}).get(TURN_FAILED_META) is True


@pytest.mark.asyncio
async def test_unanswered_live_slot_path(session_mgr, caplog):
    slot_key = "live-slot-2"
    session_key = f"dashboard:{slot_key}"

    slot = _ChatSlot(slot_key)
    slot.append("user", "Hello from live slot", cls="msg msg-u")

    session_mgr.set_live_slot_resolver(lambda k: slot if k == session_key else None)

    entry = _make_dummy_session(last_used=0.0)
    session_mgr._sessions[session_key] = entry

    with caplog.at_level(logging.ERROR, logger="kiro_crew.session"):
        await session_mgr._expire_idle(timeout_secs=60)

    assert session_key not in session_mgr._sessions
    assert any("expired with unanswered turn" in record.message for record in caplog.records)

    assert len(slot.messages) == 2
    last_msg = slot.messages[-1]
    assert last_msg["role"] == "error"
    assert last_msg["content"] == FAILED_TURN_TEXT
    assert last_msg["cls"] == "msg msg-err"
    assert last_msg["meta"].get(TURN_FAILED_META) is True


@pytest.mark.asyncio
async def test_orphan_axis(session_mgr, tmp_conv_log, caplog):
    key = "dashboard:orphan-slot-3"
    tmp_conv_log.append(key, role="user", content="Question before tab closed", cls="msg msg-u")

    cleanup = session_mgr._cleanup_boundary()
    cleanup.state.slot_owned_keys.add(key)
    cleanup.state.active_dashboard_slots = set()

    # last_used is fresh so it will not expire on idle timeout axis
    entry = _make_dummy_session(last_used=9999999999.0)
    session_mgr._sessions[key] = entry

    with caplog.at_level(logging.ERROR, logger="kiro_crew.session"):
        await session_mgr._expire_idle(timeout_secs=3600)

    assert key not in session_mgr._sessions
    assert any("expired with unanswered turn" in record.message for record in caplog.records)

    path = tmp_conv_log._path(key)
    tail = tmp_conv_log._read_tail_messages(path, 10, None)
    last_row = tail[-1]
    assert last_row["role"] == "error"
    assert last_row.get("meta", {}).get(TURN_FAILED_META) is True


@pytest.mark.asyncio
async def test_answered_session_unchanged(session_mgr, tmp_conv_log, caplog):
    key = "dashboard:answered-slot-4"
    tmp_conv_log.append(key, role="user", content="Hello", cls="msg msg-u")
    tmp_conv_log.append(key, role="assistant", content="Hi there!", cls="msg msg-a")

    entry = _make_dummy_session(last_used=0.0)
    session_mgr._sessions[key] = entry

    with caplog.at_level(logging.ERROR, logger="kiro_crew.session"):
        await session_mgr._expire_idle(timeout_secs=60)

    assert key not in session_mgr._sessions
    assert not any("expired with unanswered turn" in record.message for record in caplog.records)

    path = tmp_conv_log._path(key)
    tail = tmp_conv_log._read_tail_messages(path, 10, None)
    assert tail[-1]["role"] == "assistant"


@pytest.mark.asyncio
async def test_last_row_stop_event_or_system(session_mgr, tmp_conv_log, caplog):
    key = "dashboard:stop-slot-5"
    tmp_conv_log.append(key, role="user", content="Cancelled prompt", cls="msg msg-u")
    tmp_conv_log.append(key, role="system", content="Turn stopped by user", cls="msg msg-sys")

    entry = _make_dummy_session(last_used=0.0)
    session_mgr._sessions[key] = entry

    with caplog.at_level(logging.ERROR, logger="kiro_crew.session"):
        await session_mgr._expire_idle(timeout_secs=60)

    assert key not in session_mgr._sessions
    assert not any("expired with unanswered turn" in record.message for record in caplog.records)

    path = tmp_conv_log._path(key)
    tail = tmp_conv_log._read_tail_messages(path, 10, None)
    assert tail[-1]["role"] == "system"


@pytest.mark.asyncio
async def test_steer_and_inject_rows_ignored(session_mgr, tmp_conv_log, caplog):
    # Case a: steer meta
    key_steer = "dashboard:steer-slot"
    tmp_conv_log.append(
        key_steer,
        role="user",
        content="Steer message",
        cls="msg msg-u",
        extra_meta={"steer": True},
    )
    entry_a = _make_dummy_session(last_used=0.0)
    session_mgr._sessions[key_steer] = entry_a

    # Case b: subagent completion event
    key_subagent = "dashboard:subagent-slot"
    tmp_conv_log.append(
        key_subagent,
        role="user",
        content="[Subagent completion event] subagent finished",
        cls="msg msg-u",
    )
    entry_b = _make_dummy_session(last_used=0.0)
    session_mgr._sessions[key_subagent] = entry_b

    # Case c: cron notification
    key_cron = "dashboard:cron-slot"
    tmp_conv_log.append(
        key_cron,
        role="user",
        content="[Cron notification from backup] job done",
        cls="msg msg-u",
    )
    entry_c = _make_dummy_session(last_used=0.0)
    session_mgr._sessions[key_cron] = entry_c

    with caplog.at_level(logging.ERROR, logger="kiro_crew.session"):
        await session_mgr._expire_idle(timeout_secs=60)

    assert not any("expired with unanswered turn" in record.message for record in caplog.records)

    for k in [key_steer, key_subagent, key_cron]:
        path = tmp_conv_log._path(k)
        tail = tmp_conv_log._read_tail_messages(path, 10, None)
        assert tail[-1]["role"] == "user"


@pytest.mark.asyncio
async def test_empty_transcript(session_mgr, tmp_conv_log, caplog):
    key = "dashboard:empty-slot-7"
    tmp_conv_log.init()

    entry = _make_dummy_session(last_used=0.0)
    session_mgr._sessions[key] = entry

    with caplog.at_level(logging.ERROR, logger="kiro_crew.session"):
        await session_mgr._expire_idle(timeout_secs=60)

    assert key not in session_mgr._sessions
    assert not any("expired with unanswered turn" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_reset_declined_writes_nothing(session_mgr, tmp_conv_log, caplog):
    key = "dashboard:declined-slot-8"
    tmp_conv_log.append(key, role="user", content="Prompt waiting", cls="msg msg-u")

    entry = _make_dummy_session(last_used=0.0)
    # Lock the semaphore so reset(skip_if_busy=True) will decline
    await entry.semaphore.acquire()
    session_mgr._sessions[key] = entry

    with caplog.at_level(logging.ERROR, logger="kiro_crew.session"):
        await session_mgr._expire_idle(timeout_secs=60)

    # Session stays alive because it was busy
    assert key in session_mgr._sessions
    assert not any("expired with unanswered turn" in record.message for record in caplog.records)

    path = tmp_conv_log._path(key)
    tail = tmp_conv_log._read_tail_messages(path, 10, None)
    assert tail[-1]["role"] == "user"


@pytest.mark.asyncio
async def test_probe_timeout_expires_as_before(session_mgr, tmp_conv_log, monkeypatch, caplog):
    key = "dashboard:timeout-slot-9"
    tmp_conv_log.append(key, role="user", content="Hanging probe prompt", cls="msg msg-u")

    entry = _make_dummy_session(last_used=0.0)
    session_mgr._sessions[key] = entry

    async def _hanging_probe(_key: str):
        await asyncio.sleep(10.0)
        return 100.0

    cleanup = session_mgr._cleanup_boundary()
    monkeypatch.setattr(cleanup, "_UNANSWERED_TURN_PROBE_TIMEOUT_SECS", 0.05)
    monkeypatch.setattr(session_mgr, "_unanswered_turn_age", _hanging_probe)

    with caplog.at_level(logging.ERROR, logger="kiro_crew.session"):
        await session_mgr._expire_idle(timeout_secs=60)

    # Expired as before despite timeout
    assert key not in session_mgr._sessions
    assert not any("expired with unanswered turn" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_idempotent_second_sweep(session_mgr, tmp_conv_log, caplog):
    key = "dashboard:idempotent-slot-10"
    tmp_conv_log.append(key, role="user", content="First prompt", cls="msg msg-u")

    entry1 = _make_dummy_session(last_used=0.0)
    session_mgr._sessions[key] = entry1

    with caplog.at_level(logging.ERROR, logger="kiro_crew.session"):
        await session_mgr._expire_idle(timeout_secs=60)

    path = tmp_conv_log._path(key)
    tail1 = tmp_conv_log._read_tail_messages(path, 10, None)
    assert tail1[-1]["role"] == "error"
    count1 = len(tail1)

    # Second sweep over the same session key
    entry2 = _make_dummy_session(last_used=0.0)
    session_mgr._sessions[key] = entry2

    caplog.clear()
    with caplog.at_level(logging.ERROR, logger="kiro_crew.session"):
        await session_mgr._expire_idle(timeout_secs=60)

    tail2 = tmp_conv_log._read_tail_messages(path, 10, None)
    assert len(tail2) == count1
    assert not any("expired with unanswered turn" in record.message for record in caplog.records)


def test_locked_reentrant_with_append_on_tmp_path(tmp_conv_log):
    key = "test-reentrancy"
    tmp_conv_log.append(key, role="user", content="Initial prompt", cls="msg msg-u")

    with tmp_conv_log._locked(key):
        tmp_conv_log.append(
            key,
            role="error",
            content=FAILED_TURN_TEXT,
            cls="msg msg-err",
            extra_meta={TURN_FAILED_META: True},
        )

    path = tmp_conv_log._path(key)
    tail = tmp_conv_log._read_tail_messages(path, 10, None)
    assert len(tail) == 2
    assert tail[-1]["role"] == "error"
    assert tail[-1]["content"] == FAILED_TURN_TEXT


def test_wire_session_subagent_probe_wires_resolver_and_conv_log():
    mock_sessions = MagicMock()
    mock_state = MagicMock()
    mock_state.sessions = mock_sessions
    mock_state.conversation_log = MagicMock()

    wire_session_subagent_probe(mock_state)

    mock_sessions.set_subagent_probe.assert_called_once()
    mock_sessions.set_live_slot_resolver.assert_called_once()
    mock_sessions.set_conversation_log.assert_called_once_with(mock_state.conversation_log)
