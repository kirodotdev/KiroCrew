"""A saved slot caches where its frozen prefix sits, not the prefix text.

The next save reads the range back, so the file it writes is the same and an
open slot holds no second copy of its older history.
"""

from __future__ import annotations

import json
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
