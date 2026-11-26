"""The first save after a restart finds its frozen prefix chunk by chunk.

With no cached range, the save reads the prefix in chunks and keeps only the
rows after it in memory, and it writes the same file the full read writes.
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


def _row(i: int, *, text: str = "") -> dict:
    return {
        "role": "user" if i % 2 == 0 else "assistant",
        "content": f"m{i} é{text}",
        "ts": f"t{i:04d}",
    }


def _file_change_row(i: int) -> dict:
    row = _row(i)
    row["meta"] = {"file_changes": [{"path": f"f{i}.py", "before": "old", "after": "new"}]}
    return row


def _save_once(
    tmp_path: Path, monkeypatch, rows: list[dict], *, streamed: bool, before_save=None
) -> bytes:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    path = tmp_path / "dashboard_long.jsonl"
    head = {"_type": "metadata", "created_at": "2026-03-23T10:00:00", "title": "long"}
    lines = [json.dumps(head)] + [json.dumps(r, ensure_ascii=False) for r in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    state = _make_state(tmp_path)
    slot = cp._rehydrate_slot_from_history(state, "long")
    assert slot is not None and slot._disk_older_count > 0
    slot.append("user", "new", "msg msg-u", ts="t9999", meta={"mid": "m-new"}, mint_mid=False)
    slot.drain()
    if not streamed:
        monkeypatch.setattr(transcript_merge, "_streamed_prefix_source", lambda *a, **k: None)
    if before_save is not None:
        before_save()
    cp._save_slot_to_history(state, slot, force=True)
    return path.read_bytes()


def _body(data: bytes) -> bytes:
    return data.split(b"\n", 1)[1]


@pytest.mark.skipif(
    not transcript_merge.streaming_supported(), reason="Prefix streaming is unsupported"
)
def test_the_first_save_does_not_read_the_whole_file(tmp_path, monkeypatch) -> None:
    def no_full_read(*_args, **_kwargs):
        raise AssertionError("the first save read the whole file")

    data = _save_once(
        tmp_path,
        monkeypatch,
        [_row(i) for i in range(ROWS)],
        streamed=True,
        before_save=lambda: monkeypatch.setattr(
            "kiro_crew.history_projection._split_transcript_head", no_full_read
        ),
    )

    contents = [json.loads(line)["content"] for line in data.decode().splitlines()[1:]]
    assert contents == [f"m{i} é" for i in range(ROWS)] + ["new"]


def _same_file_both_ways(tmp_path, monkeypatch, rows: list[dict]) -> bytes:
    with monkeypatch.context() as m:
        streamed = _save_once(tmp_path / "s", m, rows, streamed=True)
    with monkeypatch.context() as m:
        full = _save_once(tmp_path / "f", m, rows, streamed=False)
    assert _body(streamed) == _body(full)
    return streamed


def test_plain_rows_are_written_the_same_both_ways(tmp_path, monkeypatch) -> None:
    for d in ("s", "f"):
        (tmp_path / d).mkdir()
    _same_file_both_ways(tmp_path, monkeypatch, [_row(i) for i in range(ROWS)])


def test_a_row_with_another_line_break_is_written_the_same_both_ways(tmp_path, monkeypatch) -> None:
    for d in ("s", "f"):
        (tmp_path / d).mkdir()
    rows = [_row(i, text="\u2028" if i == 3 else "") for i in range(ROWS)]
    _same_file_both_ways(tmp_path, monkeypatch, rows)


def test_older_file_change_copies_are_still_dropped(tmp_path, monkeypatch) -> None:
    for d in ("s", "f"):
        (tmp_path / d).mkdir()
    monkeypatch.setattr(transcript_merge, "_file_changes_keep_turns", lambda: 1)
    rows = [_file_change_row(i) if i in (2, 4) else _row(i) for i in range(ROWS)]
    data = _same_file_both_ways(tmp_path, monkeypatch, rows)
    third = json.loads(data.decode().splitlines()[3])
    assert third["meta"]["file_changes"][0]["before"] == ""
