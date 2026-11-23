"""A saved slot caches where its frozen prefix sits, not the prefix text.

The next save reads the range back, so the file it writes is the same and an
open slot holds no second copy of its older history.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.dashboard import chat_persistence as cp
from kiro_crew.dashboard.slot_persistence import transcript_merge
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


def _write_session(tmp_path: Path, name: str) -> Path:
    path = tmp_path / f"dashboard_{name}.jsonl"
    head = {"_type": "metadata", "created_at": "2026-03-23T10:00:00", "title": "é " + name}
    lines = [json.dumps(head)]
    for i in range(ROWS):
        role = "user" if i % 2 == 0 else "assistant"
        lines.append(json.dumps({"role": role, "content": f"m{i} é", "ts": f"t{i:04d}"}))
    # newline="\n": Windows text mode would write \r\n, which a transcript never holds.
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return path


def _contents(path: Path) -> list[str]:
    return [
        json.loads(line)["content"] for line in path.read_text(encoding="utf-8").splitlines()[1:]
    ]


def test_the_cache_holds_a_range_and_the_fast_path_writes_the_same_file(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    path = _write_session(tmp_path, "long")
    state = _make_state(tmp_path)
    slot = cp._rehydrate_slot_from_history(state, "long")
    assert slot is not None and slot._disk_older_count == ROWS - 500

    slot.append("user", "new", "msg msg-u", ts="t9999")
    slot.drain()
    cp._save_slot_to_history(state, slot, force=True)
    first = path.read_bytes()
    cache = slot._frozen_prefix_cache
    assert cache is not None and isinstance(cache[3], tuple)

    # The second save must take the fast path: a full read now fails the test.
    def _no_full_read(*_args, **_kwargs):
        raise AssertionError("the fast path read the whole file")

    monkeypatch.setattr("kiro_crew.history_projection._split_transcript_head", _no_full_read)
    cp._save_slot_to_history(state, slot, force=True)

    assert path.read_bytes() == first
    assert _contents(path) == [f"m{i} é" for i in range(ROWS)] + ["new"]


def _saved_slot(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    path = _write_session(tmp_path, "long")
    state = _make_state(tmp_path)
    slot = cp._rehydrate_slot_from_history(state, "long")
    assert slot is not None
    slot.append("user", "new", "msg msg-u", ts="t9999")
    slot.drain()
    cp._save_slot_to_history(state, slot, force=True)
    return state, slot, path


@pytest.mark.skipif(transcript_merge.os.linesep != "\n", reason="Windows saves the prefix as text")
def test_an_in_place_edit_copies_the_prefix_from_the_file(tmp_path, monkeypatch):
    state, slot, path = _saved_slot(tmp_path, monkeypatch)
    first = path.read_bytes()
    prefix_start, prefix_size = slot._frozen_prefix_cache[3]
    prefix = first[prefix_start : prefix_start + prefix_size]

    def _no_text_read(*_args, **_kwargs):
        raise AssertionError("the save read the frozen prefix as text")

    monkeypatch.setattr("kiro_crew.history_projection._split_transcript_head", _no_text_read)
    monkeypatch.setattr(transcript_merge, "read_prefix_range", _no_text_read)
    slot.messages[-1]["content"] = "edited in place"
    assert cp._save_slot_to_history(state, slot, force=True) is True

    written = path.read_bytes()
    assert prefix in written
    assert _contents(path) == [f"m{i} é" for i in range(ROWS)] + ["edited in place"]
    # The cached range names the prefix in the file just written.
    start, size = slot._frozen_prefix_cache[3]
    assert written[start : start + size] == prefix


@pytest.mark.skipif(transcript_merge.os.linesep != "\n", reason="Windows saves the prefix as text")
def test_the_copied_prefix_writes_the_bytes_the_text_path_writes(tmp_path, monkeypatch):
    def _edit_and_save(root: Path) -> bytes:
        root.mkdir()
        state, slot, path = _saved_slot(root, monkeypatch)
        slot.messages[-1]["content"] = "edited in place"
        assert cp._save_slot_to_history(state, slot, force=True) is True
        return path.read_bytes()

    copied = _edit_and_save(tmp_path / "copied")
    monkeypatch.setattr(transcript_merge, "_range_in_place", lambda *_a: None)
    text = _edit_and_save(tmp_path / "text")

    def _without_minted_ids(data: bytes) -> list[dict]:
        # tab_id and meta.mid are minted per slot, so the two runs differ there.
        rows = [json.loads(line) for line in data.splitlines()]
        for row in rows:
            row.pop("tab_id", None)
            if isinstance(row.get("meta"), dict):
                row["meta"].pop("mid", None)
        return rows

    assert len(text) == len(copied)
    assert _without_minted_ids(text) == _without_minted_ids(copied)


@pytest.mark.skipif(transcript_merge.os.linesep != "\n", reason="Windows saves the prefix as text")
def test_a_failed_copy_makes_the_next_save_read_the_prefix_again(tmp_path, monkeypatch):
    state, slot, path = _saved_slot(tmp_path, monkeypatch)
    before = path.read_bytes()

    def _fail(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(transcript_merge, "write_range_payload", _fail)
    slot.messages[-1]["content"] = "edited in place"
    with contextlib.suppress(OSError):
        cp._save_slot_to_history(state, slot, force=True)
    assert path.read_bytes() == before
    assert slot._frozen_prefix_cache is None


def test_a_prefix_that_no_longer_copies_is_written_as_text(tmp_path):
    from kiro_crew.atomic_write import atomic_write

    path = tmp_path / "f.jsonl"
    path.write_bytes(b'{"_type": "metadata"}\n{"a": "\\u00e9"}\r\n{"b": 2}\n')
    start = len(b'{"_type": "metadata"}\n')
    payload = transcript_merge.RangePayload(
        '{"_type": "metadata", "x": 1}\n', (start, len(b'{"a": "\\u00e9"}\r\n')), '{"c": 3}\n'
    )
    written = transcript_merge.write_range_payload(
        path, payload, write=atomic_write, replace_budget_s=None
    )
    assert written == '{"a": "\\u00e9"}\n'
    # The text write puts the platform's line ending on disk.
    expected = '{"_type": "metadata", "x": 1}\n{"a": "\\u00e9"}\n{"c": 3}\n'
    assert path.read_bytes() == expected.replace("\n", os.linesep).encode("utf-8")


@pytest.mark.skipif(transcript_merge.os.linesep != "\n", reason="Windows saves the prefix as text")
def test_a_text_fallback_caches_the_prefix_it_wrote(tmp_path, monkeypatch):
    state, slot, path = _saved_slot(tmp_path, monkeypatch)
    real_read = transcript_merge.read_prefix_range

    def _copy_fails(*_args):
        raise transcript_merge._PrefixMoved

    def _shorter_prefix(*args):
        # A prefix whose text is shorter than the cached range, as a folded
        # CRLF would be.
        return real_read(*args).split("\n", 1)[1]

    real_write_fd = transcript_merge._write_fd
    monkeypatch.setattr(transcript_merge, "_write_fd", _copy_fails)
    monkeypatch.setattr(transcript_merge, "read_prefix_range", _shorter_prefix)
    slot.messages[-1]["content"] = "edited once"
    assert cp._save_slot_to_history(state, slot, force=True) is True
    monkeypatch.setattr(transcript_merge, "_write_fd", real_write_fd)
    monkeypatch.setattr(transcript_merge, "read_prefix_range", real_read)

    written = path.read_bytes()
    start, size = slot._frozen_prefix_cache[3]
    assert written[start : start + size].decode("utf-8") == "".join(
        line + "\n" for line in written.decode("utf-8").splitlines()[1 : 1 + ROWS - 501]
    )
    # The next save copies the range it cached and keeps the window once.
    slot.messages[-1]["content"] = "edited twice"
    assert cp._save_slot_to_history(state, slot, force=True) is True
    assert _contents(path) == [f"m{i} é" for i in range(1, ROWS)] + ["edited twice"]


def test_a_range_that_no_longer_holds_lines_fails_the_save_write(tmp_path):
    from kiro_crew.atomic_write import atomic_write

    path = tmp_path / "f.jsonl"
    original = b'{"_type": "metadata"}\n{"a": 1}\n'
    path.write_bytes(original)
    # The file ended before the range did.
    payload = transcript_merge.RangePayload("{}\n", (len(original) - 3, 10), "")
    with pytest.raises(OSError, match="moved"):
        transcript_merge.write_range_payload(
            path, payload, write=atomic_write, replace_budget_s=None
        )
    assert path.read_bytes() == original


def test_a_range_copies_in_place_only_between_line_ends(tmp_path, monkeypatch):
    monkeypatch.setattr(transcript_merge.os, "linesep", "\n")
    path = tmp_path / "f.jsonl"
    path.write_bytes(b'{"_type": "metadata"}\n{"a": 1}\n{"b": 2}\n')
    start = len(b'{"_type": "metadata"}\n')
    size = len(b'{"a": 1}\n')

    assert transcript_merge._range_in_place(path, (start, size)) == (start, size)
    assert transcript_merge._range_in_place(path, (start + 1, size)) is None
    assert transcript_merge._range_in_place(path, (start, size - 1)) is None
    assert transcript_merge._range_in_place(path, (start, 0)) is None
    assert transcript_merge._range_in_place(path, "text") is None
    assert transcript_merge._range_in_place(tmp_path / "gone", (start, size)) is None
    monkeypatch.setattr(transcript_merge.os, "linesep", "\r\n")
    assert transcript_merge._range_in_place(path, (start, size)) is None


def test_a_range_that_does_not_read_back_as_lines_takes_the_full_read(tmp_path):
    path = tmp_path / "f.jsonl"
    path.write_bytes(b'{"_type": "metadata"}\n{"a": 1}\n{"b": 2}\n')
    start = len(b'{"_type": "metadata"}\n')
    size = len(b'{"a": 1}\n')

    assert transcript_merge.read_prefix_range(path, (start, size)) == '{"a": 1}\n'
    assert transcript_merge.read_prefix_range(path, (start + 1, size)) is None
    assert transcript_merge.read_prefix_range(path, (start, size - 1)) is None
    assert transcript_merge.read_prefix_range(path, (start, 10_000)) is None
    assert transcript_merge.read_prefix_range(tmp_path / "gone", (start, size)) is None
    assert transcript_merge.read_prefix_range(path, (start, 0)) == ""
    assert transcript_merge.read_prefix_range(path, "text") == "text"


def test_a_crlf_file_reads_back_as_the_text_written(tmp_path):
    path = tmp_path / "f.jsonl"
    path.write_bytes(b'{"_type": "metadata"}\r\n{"a": 1}\r\n{"b": 2}\r\n')
    start = len(b'{"_type": "metadata"}\r\n')
    size = len(b'{"a": 1}\r\n')

    assert transcript_merge.read_prefix_range(path, (start, size)) == '{"a": 1}\n'


@pytest.mark.parametrize("linesep, expected", [("\n", (4, 6)), ("\r\n", (6, 9))])
def test_prefix_range_counts_the_bytes_atomic_write_puts_down(monkeypatch, linesep, expected):
    monkeypatch.setattr(transcript_merge.os, "linesep", linesep)
    assert transcript_merge.prefix_range("ab\n\n", "é\nx\n\n") == expected
