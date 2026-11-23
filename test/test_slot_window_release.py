"""Releasing the saved head of an idle chat window (``release_saved_head``).

A slot that stays open keeps every row it ever appended, up to the window cap.
Releasing folds the rows older than the newest window into the frozen prefix,
which leaves the slot holding what a restart would load for it. These tests pin
that the file stays exactly the transcript afterwards, through later saves of
either kind, and that older pages still read the released rows.
"""

from __future__ import annotations

import json

import pytest
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard.chat_persistence import _save_slot_to_history


def _file_rows(state, key: str) -> list[str]:
    rows = state.conversation_log.read_messages(f"dashboard:{key}")
    return [r["content"] for r in rows]


def _slot_with_saved_rows(tmp_path, monkeypatch, count: int, key: str = "big"):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot(key)
    for i in range(count):
        slot.append("user" if i % 2 == 0 else "assistant", f"row {i}")
    slot.drain()
    _save_slot_to_history(state, slot, force=True)
    slot._dirty = False
    return state, slot


def test_release_keeps_the_newest_rows_and_moves_the_prefix(tmp_path, monkeypatch):
    state, slot = _slot_with_saved_rows(tmp_path, monkeypatch, 30)

    assert slot.release_saved_head(10) == 20

    assert [m["content"] for m in slot.messages] == [f"row {i}" for i in range(20, 30)]
    assert slot._disk_older_count == 20
    assert slot._disk_older_durable_count == 20
    assert slot._disk_window_len == 10
    assert _file_rows(state, "big") == [f"row {i}" for i in range(30)]


@pytest.mark.parametrize("rewrite", [False, True])
def test_saves_after_a_release_keep_every_row_once(tmp_path, monkeypatch, rewrite):
    state, slot = _slot_with_saved_rows(tmp_path, monkeypatch, 30)
    assert slot.release_saved_head(10) == 20
    if rewrite:
        # The append basis gone: the next save rewrites the file from the
        # frozen prefix plus the window.
        slot._append_basis = None

    slot.append("user", "row 30")
    slot.append("assistant", "row 31")
    _save_slot_to_history(state, slot)

    assert _file_rows(state, "big") == [f"row {i}" for i in range(32)]


@pytest.mark.parametrize("rewrite", [False, True])
def test_a_transient_row_in_the_released_head_is_not_counted_as_a_line(
    tmp_path, monkeypatch, rewrite
):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("big")
    slot.append("user", "row 0")
    # A transient row: the window holds it, the transcript never does.
    slot.append("permission", "allow?", "msg msg-perm")
    for i in range(1, 20):
        slot.append("assistant", f"row {i}")
    slot.drain()
    _save_slot_to_history(state, slot, force=True)
    slot._dirty = False

    assert slot.release_saved_head(5) == 16
    assert slot._disk_older_count == 15, "the prefix counts transcript lines, not window rows"

    if rewrite:
        slot._append_basis = None
    slot.append("user", "row 20")
    _save_slot_to_history(state, slot)

    assert _file_rows(state, "big") == [f"row {i}" for i in range(21)]


@pytest.mark.parametrize(
    "unsafe",
    ["dirty", "unsaved_rows", "swap", "pending_rewrite", "metadata_write", "no_basis"],
)
def test_release_refuses_a_window_not_wholly_on_disk(tmp_path, monkeypatch, unsafe):
    state, slot = _slot_with_saved_rows(tmp_path, monkeypatch, 30)
    if unsafe == "dirty":
        slot._dirty = True
    elif unsafe == "unsaved_rows":
        slot._disk_window_len -= 1
    elif unsafe == "swap":
        slot._swap_in_flight = True
    elif unsafe == "pending_rewrite":
        slot._pending_rewrite = True
    elif unsafe == "metadata_write":
        slot._metadata_persist_inflight = 1
    else:
        slot._append_basis = None

    assert slot.release_saved_head(10) == 0
    assert len(slot.messages) == 30
    assert slot._disk_older_count == 0


def test_release_refuses_a_window_whose_region_holds_another_writers_row(tmp_path, monkeypatch):
    """A row another writer put inside the window region moves the line boundary."""
    state, slot = _slot_with_saved_rows(tmp_path, monkeypatch, 30)
    path = state.conversation_log._path("dashboard:big")
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    foreign = json.dumps({"role": "assistant", "content": "foreign", "ts": "0"}) + "\n"
    # Between window rows 0 and 1: line 0 is the metadata line.
    path.write_text("".join(lines[:2] + [foreign] + lines[2:]), encoding="utf-8")
    slot.append("user", "row 30")
    _save_slot_to_history(state, slot)
    slot._dirty = False
    assert "foreign" in _file_rows(state, "big"), "the save did not keep the foreign row"

    assert slot.release_saved_head(10) == 0
    assert len(slot.messages) == 31
    assert slot._disk_older_count == 0


def test_release_rebases_the_title_refresh_mark_like_a_restore(tmp_path, monkeypatch):
    """The refresh cadence counts user turns in the window, which the release shrinks."""
    from kiro_crew.dashboard.chat_title import _title_refresh_due

    state, slot = _slot_with_saved_rows(tmp_path, monkeypatch, 60)
    slot._title_refresh_mark = 30  # every user turn so far was counted
    assert slot.release_saved_head(10) == 50

    assert slot._title_refresh_mark == 24, "a spent built-in milestone stays spent"
    for i in range(60, 100):
        slot.append("user" if i % 2 == 0 else "assistant", f"row {i}")
    user_turns = sum(1 for m in slot.messages if m["role"] == "user")
    assert _title_refresh_due(
        slot._title_refresh_mark, user_turns, 5, ()
    ), "a cadence of 5 user turns never comes due after the release"


def test_release_leaves_a_short_window_alone(tmp_path, monkeypatch):
    _state, slot = _slot_with_saved_rows(tmp_path, monkeypatch, 8)
    assert slot.release_saved_head(10) == 0
    assert len(slot.messages) == 8


@pytest.mark.asyncio
async def test_older_pages_read_the_released_rows_from_disk(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    state, slot = _slot_with_saved_rows(tmp_path, monkeypatch, 30)
    assert slot.release_saved_head(10) == 20

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.get("/api/chat/slots/big?limit=10")
        data = json.loads(await resp.text())
        assert data["total"] == 30
        assert [m["content"] for m in data["messages"]] == [f"row {i}" for i in range(20, 30)]

        resp = await client.get("/api/chat/slots/big?limit=50&before=20")
        data = json.loads(await resp.text())
        assert [m["content"] for m in data["messages"]] == [f"row {i}" for i in range(20)]


def test_the_cap_trim_does_not_count_a_transient_row_as_a_line(tmp_path, monkeypatch):
    from kiro_crew.dashboard import state as state_mod

    monkeypatch.setattr(state_mod, "_MAX_SLOT_MESSAGES", 6)
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("big")
    slot.append("user", "row 0")
    slot.append("permission", "allow?", "msg msg-perm")
    for i in range(1, 5):
        slot.append("assistant", f"row {i}")
    slot.drain()
    _save_slot_to_history(state, slot, force=True)

    # Two appends past the cap fold "row 0" and the transient row.
    slot.append("user", "row 5")
    slot.append("assistant", "row 6")
    assert slot._disk_older_count == 1
    slot._append_basis = None
    _save_slot_to_history(state, slot)

    assert _file_rows(state, "big") == [f"row {i}" for i in range(7)]


def test_a_save_racing_a_release_does_not_count_the_released_rows(tmp_path, monkeypatch):
    """A fold that lands while a save writes leaves only the rows still held as saved."""
    from kiro_crew.dashboard.slot_persistence import write_guards

    state, slot = _slot_with_saved_rows(tmp_path, monkeypatch, 30)
    real = write_guards.paired_window_snapshot

    def _snapshot_then_release(*args, **kwargs):
        taken = real(*args, **kwargs)
        assert slot.release_saved_head(10) == 20
        return taken

    monkeypatch.setattr(write_guards, "paired_window_snapshot", _snapshot_then_release)
    _save_slot_to_history(state, slot, force=True)

    assert len(slot.messages) == 10
    assert slot._disk_window_len == 10, "the rows folded during the save still count as saved"
    assert _file_rows(state, "big") == [f"row {i}" for i in range(30)]


def _iso(epoch: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _idle_slot(state, key: str, rows: int, last_ts: str):
    slot = state.get_or_create_slot(key)
    for i in range(rows):
        slot.append("assistant", f"{key} {i}", ts=last_ts)
    slot.drain()
    _save_slot_to_history(state, slot, force=True)
    slot._dirty = False
    return slot


def test_the_pass_releases_only_idle_slots_past_a_window(tmp_path, monkeypatch):
    from kiro_crew.dashboard import chat_persistence
    from kiro_crew.dashboard import slot_window_release as swr

    monkeypatch.setattr(chat_persistence, "_WINDOW_ROWS", 5)
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    now = 1_900_000_000.0
    old = _iso(now - swr.RELEASE_IDLE_SECS - 60)
    recent = _iso(now - 30)

    idle = _idle_slot(state, "idle", 12, old)
    busy = _idle_slot(state, "busy", 12, recent)
    short = _idle_slot(state, "short", 4, old)
    running = _idle_slot(state, "running", 12, old)
    running._turn_admission_reserved = True

    assert swr.release_idle_windows(state, now=now) == 7

    assert len(idle.messages) == 5
    assert idle._disk_older_count == 7
    assert len(busy.messages) == 12
    assert len(short.messages) == 4
    assert len(running.messages) == 12
