"""The startup recent-session restore builds a person's tab as a sidebar row.

Only the in-memory window waits: the transcript on disk is never touched, and
the window that loads on first read is the one an eager restore builds, so older
history still pages in from disk in full.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.dashboard import chat_persistence as cp
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import ConversationLog

ROWS = 600


def _make_state(tmp_path: Path) -> DashboardState:
    sessions = MagicMock(count=0)
    sessions.get_pid = MagicMock(return_value=None)
    sessions.remove = AsyncMock()
    return DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )


def _write_session(tmp_path: Path, name: str, rows: int, meta: dict | None = None) -> Path:
    path = tmp_path / f"dashboard_{name}.jsonl"
    head = {"_type": "metadata", "created_at": "2026-03-23T10:00:00", "title": name}
    head.update(meta or {})
    lines = [json.dumps(head)]
    for i in range(rows):
        role = "user" if i % 2 == 0 else "assistant"
        lines.append(
            json.dumps(
                {
                    "role": role,
                    "content": f"m{i}",
                    "ts": f"2026-03-23T10:{i // 60:02d}:{i % 60:02d}",
                }
            )
        )
    # newline="\n": Windows text mode would write \r\n, which a transcript never holds.
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return path


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr(cp, "live_loop_slot_keys", lambda: set())
    return tmp_path


@pytest.mark.asyncio
async def test_a_recent_person_tab_is_restored_as_a_row_and_loads_in_full(home):
    path = _write_session(home, "long", ROWS)
    # Only the message rows: the restore backfills a tab id into the metadata line.
    before = path.read_bytes().split(b"\n", 1)[1]
    state = _make_state(home)

    assert await cp.restore_recent_sessions_async(state, window_minutes=60) == 1

    slot = state._slots["long"]
    assert slot.window_pending is True
    assert slot._messages == []
    assert len(slot._lazy_tail) == cp.LAZY_TAIL_ROWS
    assert slot._lazy_tail[-1]["content"] == f"m{ROWS - 1}"
    assert path.read_bytes().split(b"\n", 1)[1] == before, "a row restore touched the rows"

    window = slot.messages
    assert slot.window_pending is False
    assert [m["content"] for m in window] == [f"m{i}" for i in range(ROWS - 500, ROWS)]
    assert slot._disk_older_count == ROWS - 500
    # The rows older than the window are still all on disk, for the older-page read.
    on_disk = state.conversation_log.read_messages_chained("dashboard:long")
    assert [m["content"] for m in on_disk] == [f"m{i}" for i in range(ROWS)]
    assert path.read_bytes().split(b"\n", 1)[1] == before


@pytest.mark.asyncio
async def test_a_tab_a_loop_drives_keeps_its_window(home, monkeypatch):
    monkeypatch.setattr(cp, "live_loop_slot_keys", lambda: {"looped"})
    _write_session(home, "looped", 20)
    state = _make_state(home)

    assert await cp.restore_recent_sessions_async(state, window_minutes=60) == 1

    slot = state._slots["looped"]
    assert slot.window_pending is False
    assert len(slot._messages) == 20


@pytest.mark.asyncio
async def test_an_unknown_loop_set_keeps_every_window(home, monkeypatch):
    monkeypatch.setattr(cp, "live_loop_slot_keys", lambda: None)
    monkeypatch.setattr(cp, "stored_loop_slot_keys", lambda: None)
    _write_session(home, "unknown", 20)
    state = _make_state(home)

    assert await cp.restore_recent_sessions_async(state, window_minutes=60) == 1
    assert state._slots["unknown"].window_pending is False


@pytest.mark.asyncio
async def test_a_tab_something_else_drives_keeps_its_window(home):
    _write_session(home, "linked", 20, meta={"linked_session_key": "cron:abc"})
    state = _make_state(home)

    assert await cp.restore_recent_sessions_async(state, window_minutes=60) == 1
    assert state._slots["linked"].window_pending is False


@pytest.mark.asyncio
async def test_a_foldered_row_saves_back_byte_for_byte_after_it_loads(home):
    path = _write_session(home, "kept", ROWS, meta={"folder_id": "f1"})
    state = _make_state(home)
    await cp.restore_recent_sessions_async(state, window_minutes=0)
    slot = state._slots["kept"]
    assert slot.window_pending is True

    slot.append("user", "new", "msg msg-u", ts="2026-03-24T00:00:00")
    slot.drain()
    cp._save_slot_to_history(state, slot, force=True)

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()[1:]]
    assert [r["content"] for r in rows] == [f"m{i}" for i in range(ROWS)] + ["new"]
