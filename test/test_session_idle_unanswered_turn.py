"""Tests for recording unanswered turns when idle sweep expires a session."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import session_cleanup_unanswered
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.chat_turn.queue_cycle import (
    FAILED_BEFORE_START_TEXT,
    TURN_FAILED_META,
    failed_before_start_meta,
)
from kiro_crew.dashboard.chat_utils_completion import wire_session_subagent_probe
from kiro_crew.dashboard.idle_unanswered import DashboardUnansweredHooks
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.history import ConversationLog
from kiro_crew.session import SessionManager


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
def slots():
    return {}


@pytest.fixture
def hooks(tmp_conv_log, slots):
    state = SimpleNamespace(conversation_log=tmp_conv_log, get_slot=slots.get)
    return DashboardUnansweredHooks(state)


@pytest.fixture
def session_mgr(hooks):
    cfg = KiroCrewConfig()
    cfg.session.timeout_secs = 60
    mgr = SessionManager(cfg)
    mgr.set_unanswered_turn_hooks(hooks)
    return mgr


async def _sweep(session_mgr, idle_secs: int = 60) -> None:
    """Run one idle sweep. *idle_secs* is the idle threshold the sessions'
    ``last_used`` is compared with, not a wait."""
    await session_mgr._expire_idle(timeout_secs=idle_secs)


def _tail(log: ConversationLog, key: str) -> list[dict]:
    return log._read_tail_messages(log._path(key), 10, None)


def _logged_unanswered(caplog) -> bool:
    return any("expired with unanswered turn" in record.message for record in caplog.records)


def _assert_failed_row(row: dict) -> None:
    assert row["role"] == "error"
    assert row["content"] == FAILED_BEFORE_START_TEXT
    assert row.get("cls") == "msg msg-err"
    for name, value in failed_before_start_meta().items():
        assert row.get("meta", {}).get(name) == value


@pytest.mark.asyncio
async def test_unanswered_disk_path(session_mgr, tmp_conv_log, caplog):
    key = "dashboard:test-slot-1"
    tmp_conv_log.append(key, role="user", content="Hello assistant", cls="msg msg-u")
    session_mgr._sessions[key] = _make_dummy_session(last_used=0.0)

    with caplog.at_level(logging.ERROR, logger="kiro_crew.session"):
        await _sweep(session_mgr)

    assert key not in session_mgr._sessions
    assert _logged_unanswered(caplog)
    tail = _tail(tmp_conv_log, key)
    assert len(tail) == 2
    _assert_failed_row(tail[-1])


@pytest.mark.asyncio
async def test_a_channel_prompt_without_a_dashboard_class_counts(session_mgr, tmp_conv_log, caplog):
    """Slack writes its user row with no ``cls``; it is still a prompt."""
    key = "slack:C1:1700000000.000100"
    tmp_conv_log.append(key, role="user", content="Hello from Slack")
    session_mgr._sessions[key] = _make_dummy_session(last_used=0.0)

    with caplog.at_level(logging.ERROR, logger="kiro_crew.session"):
        await _sweep(session_mgr)

    assert _logged_unanswered(caplog)
    _assert_failed_row(_tail(tmp_conv_log, key)[-1])


@pytest.mark.asyncio
async def test_unanswered_live_slot_path(session_mgr, slots, caplog):
    slot_key = "live-slot-2"
    session_key = f"dashboard:{slot_key}"
    slot = _ChatSlot(slot_key)
    slot.append("user", "Hello from live slot", cls="msg msg-u")
    slots[slot_key] = slot
    session_mgr._sessions[session_key] = _make_dummy_session(last_used=0.0)

    with caplog.at_level(logging.ERROR, logger="kiro_crew.session"):
        await _sweep(session_mgr)

    assert session_key not in session_mgr._sessions
    assert _logged_unanswered(caplog)
    assert len(slot.messages) == 2
    _assert_failed_row(slot.messages[-1])


@pytest.mark.asyncio
@pytest.mark.parametrize("busy", ["admission_reserved", "queued"])
async def test_a_slot_about_to_answer_is_not_unanswered(session_mgr, slots, caplog, busy):
    """A prompt reserved for admission, or with work queued behind it, will run."""
    slot_key = f"busy-slot-{busy}"
    session_key = f"dashboard:{slot_key}"
    slot = _ChatSlot(slot_key)
    slot.append("user", "Sent while the session was idle", cls="msg msg-u")
    if busy == "admission_reserved":
        slot._turn_admission_reserved = True
    else:
        slot._queue.append({"text": "queued prompt"})
    slots[slot_key] = slot
    session_mgr._sessions[session_key] = _make_dummy_session(last_used=0.0)

    with caplog.at_level(logging.ERROR, logger="kiro_crew.session"):
        await _sweep(session_mgr)

    assert not _logged_unanswered(caplog)
    assert [row["role"] for row in slot.messages] == ["user"]


@pytest.mark.asyncio
async def test_a_prompt_sent_after_the_probe_is_not_marked(hooks, tmp_conv_log, slots):
    """The recorder marks only the prompt the probe found, on both paths."""
    disk_key = "dashboard:replaced-disk"
    tmp_conv_log.append(disk_key, role="user", content="Old prompt", cls="msg msg-u")
    found = await hooks.probe(disk_key)
    assert found is not None
    tmp_conv_log.append(disk_key, role="assistant", content="Answer", cls="msg msg-a")
    tmp_conv_log.append(disk_key, role="user", content="New prompt", cls="msg msg-u")
    await hooks.mark_failed(disk_key, found.prompt)
    assert _tail(tmp_conv_log, disk_key)[-1]["content"] == "New prompt"

    slot = _ChatSlot("replaced-live")
    slot.append("user", "Old prompt", cls="msg msg-u")
    slots["replaced-live"] = slot
    found = await hooks.probe("dashboard:replaced-live")
    assert found is not None
    slot.append("assistant", "Answer", cls="msg msg-a")
    slot.append("user", "New prompt", cls="msg msg-u")
    await hooks.mark_failed("dashboard:replaced-live", found.prompt)
    assert slot.messages[-1]["content"] == "New prompt"


@pytest.mark.asyncio
async def test_orphan_axis(session_mgr, tmp_conv_log, caplog):
    key = "dashboard:orphan-slot-3"
    tmp_conv_log.append(key, role="user", content="Question before tab closed", cls="msg msg-u")

    cleanup = session_mgr._cleanup_boundary()
    cleanup.state.slot_owned_keys.add(key)
    cleanup.state.active_dashboard_slots = set()

    # last_used is fresh so it will not expire on idle timeout axis
    session_mgr._sessions[key] = _make_dummy_session(last_used=9999999999.0)

    with caplog.at_level(logging.ERROR, logger="kiro_crew.session"):
        await _sweep(session_mgr, 3600)

    assert key not in session_mgr._sessions
    assert _logged_unanswered(caplog)
    _assert_failed_row(_tail(tmp_conv_log, key)[-1])


@pytest.mark.asyncio
async def test_answered_session_unchanged(session_mgr, tmp_conv_log, caplog):
    key = "dashboard:answered-slot-4"
    tmp_conv_log.append(key, role="user", content="Hello", cls="msg msg-u")
    tmp_conv_log.append(key, role="assistant", content="Hi there!", cls="msg msg-a")
    session_mgr._sessions[key] = _make_dummy_session(last_used=0.0)

    with caplog.at_level(logging.ERROR, logger="kiro_crew.session"):
        await _sweep(session_mgr)

    assert key not in session_mgr._sessions
    assert not _logged_unanswered(caplog)
    assert _tail(tmp_conv_log, key)[-1]["role"] == "assistant"


@pytest.mark.asyncio
async def test_last_row_stop_event_or_system(session_mgr, tmp_conv_log, caplog):
    key = "dashboard:stop-slot-5"
    tmp_conv_log.append(key, role="user", content="Cancelled prompt", cls="msg msg-u")
    tmp_conv_log.append(key, role="system", content="Turn stopped by user", cls="msg msg-sys")
    session_mgr._sessions[key] = _make_dummy_session(last_used=0.0)

    with caplog.at_level(logging.ERROR, logger="kiro_crew.session"):
        await _sweep(session_mgr)

    assert key not in session_mgr._sessions
    assert not _logged_unanswered(caplog)
    assert _tail(tmp_conv_log, key)[-1]["role"] == "system"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "extra_meta"),
    [
        ("Steer message", {"steer": True}),
        ("[Subagent completion event] subagent finished", None),
        ("[Cron notification from backup] job done", None),
    ],
    ids=["steer", "subagent", "cron"],
)
async def test_steer_and_inject_rows_ignored(
    session_mgr, tmp_conv_log, caplog, content, extra_meta
):
    key = "dashboard:injected-slot"
    tmp_conv_log.append(key, role="user", content=content, cls="msg msg-u", extra_meta=extra_meta)
    session_mgr._sessions[key] = _make_dummy_session(last_used=0.0)

    with caplog.at_level(logging.ERROR, logger="kiro_crew.session"):
        await _sweep(session_mgr)

    assert not _logged_unanswered(caplog)
    assert _tail(tmp_conv_log, key)[-1]["role"] == "user"


@pytest.mark.asyncio
async def test_empty_transcript(session_mgr, caplog):
    key = "dashboard:empty-slot-7"
    session_mgr._sessions[key] = _make_dummy_session(last_used=0.0)

    with caplog.at_level(logging.ERROR, logger="kiro_crew.session"):
        await _sweep(session_mgr)

    assert key not in session_mgr._sessions
    assert not _logged_unanswered(caplog)


@pytest.mark.asyncio
async def test_reset_declined_writes_nothing(session_mgr, tmp_conv_log, caplog):
    key = "dashboard:declined-slot-8"
    tmp_conv_log.append(key, role="user", content="Prompt waiting", cls="msg msg-u")
    entry = _make_dummy_session(last_used=0.0)
    # Lock the semaphore so reset(skip_if_busy=True) will decline
    await entry.semaphore.acquire()
    session_mgr._sessions[key] = entry

    with caplog.at_level(logging.ERROR, logger="kiro_crew.session"):
        await _sweep(session_mgr)

    # Session stays alive because it was busy
    assert key in session_mgr._sessions
    assert not _logged_unanswered(caplog)
    assert _tail(tmp_conv_log, key)[-1]["role"] == "user"


@pytest.mark.asyncio
async def test_probe_timeout_expires_as_before(session_mgr, tmp_conv_log, monkeypatch, caplog):
    key = "dashboard:timeout-slot-9"
    tmp_conv_log.append(key, role="user", content="Hanging probe prompt", cls="msg msg-u")
    session_mgr._sessions[key] = _make_dummy_session(last_used=0.0)

    class _HangingHooks:
        async def probe(self, _key: str):
            await asyncio.Event().wait()  # never answers; the probe timeout cuts it off

        async def mark_failed(self, _key: str, _prompt: object) -> None:
            raise AssertionError("a timed-out probe found nothing to mark")

    monkeypatch.setattr(
        session_cleanup_unanswered,
        "PROBE_TIMEOUT_SECS",
        0.05,  # flake-ok: bounds a probe that never answers, so it always times out
    )
    session_mgr.set_unanswered_turn_hooks(_HangingHooks())

    with caplog.at_level(logging.ERROR, logger="kiro_crew.session"):
        await _sweep(session_mgr)

    assert key not in session_mgr._sessions
    assert not _logged_unanswered(caplog)


@pytest.mark.asyncio
async def test_no_hooks_records_nothing(session_mgr, tmp_conv_log, caplog):
    key = "dashboard:no-hooks"
    tmp_conv_log.append(key, role="user", content="Prompt", cls="msg msg-u")
    session_mgr._sessions[key] = _make_dummy_session(last_used=0.0)
    session_mgr.set_unanswered_turn_hooks(None)

    with caplog.at_level(logging.ERROR, logger="kiro_crew.session"):
        await _sweep(session_mgr)

    assert key not in session_mgr._sessions
    assert not _logged_unanswered(caplog)
    assert _tail(tmp_conv_log, key)[-1]["role"] == "user"


@pytest.mark.asyncio
async def test_idempotent_second_sweep(session_mgr, tmp_conv_log, caplog):
    key = "dashboard:idempotent-slot-10"
    tmp_conv_log.append(key, role="user", content="First prompt", cls="msg msg-u")
    session_mgr._sessions[key] = _make_dummy_session(last_used=0.0)

    with caplog.at_level(logging.ERROR, logger="kiro_crew.session"):
        await _sweep(session_mgr)

    tail1 = _tail(tmp_conv_log, key)
    assert tail1[-1]["role"] == "error"

    # Second sweep over the same session key
    session_mgr._sessions[key] = _make_dummy_session(last_used=0.0)
    caplog.clear()
    with caplog.at_level(logging.ERROR, logger="kiro_crew.session"):
        await _sweep(session_mgr)

    assert len(_tail(tmp_conv_log, key)) == len(tail1)
    assert not _logged_unanswered(caplog)


def test_the_failed_row_meta_names_the_turn_failure():
    assert failed_before_start_meta()[TURN_FAILED_META] is True


def test_wire_session_subagent_probe_installs_the_unanswered_hooks():
    mock_state = MagicMock()

    wire_session_subagent_probe(mock_state)

    mock_state.sessions.set_subagent_probe.assert_called_once()
    (hooks,), _ = mock_state.sessions.set_unanswered_turn_hooks.call_args
    assert isinstance(hooks, DashboardUnansweredHooks)
