"""A steady slot save appends only its new rows.

Before, every flush rebuilt, redacted and serialized the whole in-memory window
and replaced the whole transcript, so one save cost grew with the window and the
file. Now a save whose older rows are unchanged since its own last write, and whose
file nobody else touched, appends just the new rows. Anything else -- an edited or
reordered old row, another writer's append, a changed header line, a torn tail --
takes the full rewrite, which must produce exactly what it always did.

These tests drive the real save, ``_save_slot_to_history``, against a real file.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_transcript_file_change_bound import KEY, _make_state, _write

from kiro_crew import atomic_write as atomic_write_module
from kiro_crew.dashboard import chat_persistence, display_verdicts
from kiro_crew.dashboard.chat_persistence import _rehydrate_slot_from_history, _save_slot_to_history
from kiro_crew.dashboard.slot_persistence import transcript_merge
from kiro_crew.history import ConversationLog

PREFIX = 20
SIZES = (250, 1000, 2500, 6000)
_BASE = datetime(2026, 10, 11, 9, 0, 0)


def _row(i: int, role: str = "user") -> dict[str, Any]:
    return {
        "role": role,
        "content": f"message {i} with some words in it",
        "ts": (_BASE + timedelta(seconds=i)).isoformat(),
        "meta": {"mid": f"m-{i}"},
    }


def _primed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, window: int):
    """A slot holding the last *window* rows in memory, after one committed save."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    rows = [_row(i) for i in range(PREFIX + window)]
    path = _write(tmp_path, rows)
    state = _make_state(tmp_path)
    slot = _rehydrate_slot_from_history(state, KEY.removeprefix("dashboard_"))
    assert slot is not None
    slot.messages[:] = [json.loads(json.dumps(r)) for r in rows[PREFIX:]]
    slot._disk_older_count = PREFIX
    slot._disk_window_len = window
    slot._frozen_prefix_cache = None
    slot._dirty = True
    assert _save_slot_to_history(state, slot) is True
    return state, slot, path


class _Meter:
    """What one save writes, reads and builds."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
        self.written = 0
        self.read = 0
        self.full_writes = 0
        self.built = 0
        self.serialized = 0
        depth = [0]
        write_all = atomic_write_module._write_all

        def counting_write_all(fd, data, target):  # type: ignore[no-untyped-def]
            self.written += len(data)
            return write_all(fd, data, target)

        monkeypatch.setattr(atomic_write_module, "_write_all", counting_write_all)
        read_bytes = Path.read_bytes

        def counting_read_bytes(p: Path) -> bytes:
            data = read_bytes(p)
            if p == path:
                self.read += len(data)
            return data

        monkeypatch.setattr(Path, "read_bytes", counting_read_bytes)
        full = chat_persistence.atomic_write

        def counting_atomic_write(*a, **kw):  # type: ignore[no-untyped-def]
            self.full_writes += 1
            return full(*a, **kw)

        monkeypatch.setattr(chat_persistence, "atomic_write", counting_atomic_write)
        for name in ("_build_message_entry", "_build_message_entry_uncached"):
            original = getattr(chat_persistence, name)

            def counting(m, *, _original=original, **kw):  # type: ignore[no-untyped-def]
                depth[0] += 1
                try:
                    if depth[0] == 1:
                        self.built += 1
                    return _original(m, **kw)
                finally:
                    depth[0] -= 1

            monkeypatch.setattr(chat_persistence, name, counting)
        note = display_verdicts.note_written_lines

        def counting_note(key, lines):  # type: ignore[no-untyped-def]
            self.serialized += len(lines)
            return note(key, lines)

        monkeypatch.setattr(display_verdicts, "note_written_lines", counting_note)


def _force_full_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every save take the full rewrite."""
    monkeypatch.setattr(transcript_merge, "appendable_tail", lambda *a, **kw: None, raising=False)


def _full_rewrite_keeps(state, slot, path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A full rewrite of the slot's current state leaves the file byte for byte."""
    before = path.read_bytes()
    _force_full_path(monkeypatch)
    slot._frozen_prefix_cache = None
    slot._dirty = True
    assert _save_slot_to_history(state, slot) is True
    assert path.read_bytes() == before


@pytest.mark.parametrize("window", SIZES)
def test_a_steady_save_writes_only_the_new_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, window: int
) -> None:
    state, slot, path = _primed(tmp_path, monkeypatch, window)
    before = path.read_bytes()
    new = [_row(PREFIX + window + k, role="assistant") for k in range(3)]
    slot.messages.extend(json.loads(json.dumps(r)) for r in new)
    slot._dirty = True
    with monkeypatch.context() as mp:
        meter = _Meter(mp, path)
        assert _save_slot_to_history(state, slot) is True
    read = meter.read  # before this test's own reads below

    after = path.read_bytes()
    added = after[len(before) :]
    assert after.startswith(before)
    assert [json.loads(ln)["meta"]["mid"] for ln in added.splitlines()] == [
        r["meta"]["mid"] for r in new
    ]
    # Flat across window sizes: the cost is the new rows, not the window.
    assert (meter.full_writes, read) == (0, 0)
    assert meter.written == len(added)
    assert (meter.built, meter.serialized) == (3, 3)
    _full_rewrite_keeps(state, slot, path, monkeypatch)


def test_a_trim_at_the_window_cap_still_appends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = 300
    state, slot, path = _primed(tmp_path, monkeypatch, window)
    monkeypatch.setattr("kiro_crew.dashboard.state._MAX_SLOT_MESSAGES", window)
    before = path.read_bytes()
    older = slot._disk_older_count

    row = slot.append("assistant", "past the cap", ts=(_BASE + timedelta(days=1)).isoformat())
    assert slot._disk_older_count == older + 1  # the trim folded one row into the prefix
    with monkeypatch.context() as mp:
        meter = _Meter(mp, path)
        assert _save_slot_to_history(state, slot) is True
    read = meter.read  # before this test's own reads below

    added = path.read_bytes()[len(before) :]
    assert path.read_bytes().startswith(before)
    assert [json.loads(ln)["meta"]["mid"] for ln in added.splitlines()] == [row["meta"]["mid"]]
    assert (meter.full_writes, read, meter.built) == (0, 0, 1)
    assert meter.written == len(added)
    _full_rewrite_keeps(state, slot, path, monkeypatch)


def test_a_trim_that_moves_a_file_change_row_into_the_prefix_rewrites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a prefix read applies the file-change bound, so this trim must not append."""
    window = 300
    state, slot, path = _primed(tmp_path, monkeypatch, window)
    slot.messages[0]["meta"]["file_changes"] = [{"path": "/repo/a.py", "before": "x", "after": "y"}]
    slot._dirty = True
    assert _save_slot_to_history(state, slot) is True  # an old-row edit: full path
    monkeypatch.setattr("kiro_crew.dashboard.state._MAX_SLOT_MESSAGES", window)

    slot.append("assistant", "past the cap", ts=(_BASE + timedelta(days=1)).isoformat())
    meter = _Meter(monkeypatch, path)
    assert _save_slot_to_history(state, slot) is True
    # A full rewrite that re-reads the prefix, where the bound runs.
    assert meter.full_writes == 1 and meter.read > 0


def _without_tab_id(data: bytes) -> bytes:
    """The file with its header's per-slot random ``tab_id`` left out."""
    header, rest = data.split(b"\n", 1)
    meta = json.loads(header)
    meta.pop("tab_id", None)
    return json.dumps(meta).encode() + b"\n" + rest


def _edit_old_row(state, slot, path) -> None:
    slot.messages[3]["content"] = "edited in place"


def _reorder(state, slot, path) -> None:
    slot.messages[5], slot.messages[6] = slot.messages[6], slot.messages[5]


def _foreign_append(state, slot, path) -> None:
    # Another writer's row, with a fixed ``ts`` so both runs write the same bytes.
    cron = {**_row(5_000, role="assistant"), "meta": {"mid": "cron-1"}}
    atomic_write_module.append_line(path, json.dumps(cron))


def _header_change(state, slot, path) -> None:
    slot.reasoning_effort = "high"


@pytest.mark.parametrize(
    "change", [_edit_old_row, _reorder, _foreign_append, _header_change], ids=lambda f: f.__name__
)
def test_anything_but_new_rows_takes_the_full_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change
) -> None:
    outputs = []
    for forced in (False, True):
        with monkeypatch.context() as mp:
            if forced:
                _force_full_path(mp)
            state, slot, path = _primed(tmp_path / str(forced), mp, 250)
            header = path.read_bytes().split(b"\n", 1)[0]
            change(state, slot, path)
            slot.messages.append(_row(10_000, role="assistant"))
            slot._dirty = True
            meter = _Meter(mp, path)
            assert _save_slot_to_history(state, slot) is True
            assert meter.full_writes == 1
            outputs.append(_without_tab_id(path.read_bytes()))
            if change is _foreign_append:
                assert b'"cron-1"' in path.read_bytes()
            if change is _header_change:
                assert path.read_bytes().split(b"\n", 1)[0] != header
    # The full path writes what it always wrote.
    assert outputs[0] == outputs[1]


def test_a_torn_tail_is_skipped_on_load_and_repaired_by_the_next_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, slot, path = _primed(tmp_path, monkeypatch, 250)
    slot.messages.append(_row(9_000, role="assistant"))
    slot._dirty = True
    assert _save_slot_to_history(state, slot) is True
    # An append killed mid-write leaves the start of a line and no newline.
    with path.open("ab") as handle:
        handle.write(b'{"role": "assistant", "content": "cut sho')

    assert len(ConversationLog(base_dir=tmp_path)._read_messages(KEY)) == PREFIX + 251

    slot.messages.append(_row(9_001, role="assistant"))
    slot._dirty = True
    meter = _Meter(monkeypatch, path)
    assert _save_slot_to_history(state, slot) is True
    assert meter.full_writes == 1
    lines = path.read_bytes().splitlines()
    assert all(json.loads(ln) for ln in lines)
    assert b"cut sho" not in path.read_bytes()
    assert [json.loads(ln).get("meta", {}).get("mid") for ln in lines[-2:]] == ["m-9000", "m-9001"]
