"""A slot save that rewrites the transcript copies its older rows instead of holding them.

The rows older than the in-memory window form the frozen prefix. On platforms
that support streaming, a rewrite copies an unchanged prefix from the file and
builds only the rows after it, retaining the stored message count. A changed
prefix or a metadata rewrite with file-change rows in the window takes the
whole-file path so the retention bound applies. These tests check byte parity
with the whole-file path on every platform and streamed reads and counts where
streaming is supported.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_slot_save_append_tail import KEY, _primed, _row, _without_tab_id

from kiro_crew.dashboard import chat_persistence
from kiro_crew.dashboard.slot_persistence import transcript_merge
from kiro_crew.history import ConversationLog
from kiro_crew.transcript_count import count_and_store, count_messages, stored_count

WINDOW = 250


def _whole_reads(monkeypatch: pytest.MonkeyPatch, path: Path) -> list[int]:
    reads: list[int] = []
    real = Path.read_bytes

    def spy(p: Path) -> bytes:
        data = real(p)
        if p == path:
            reads.append(len(data))
        return data

    monkeypatch.setattr(Path, "read_bytes", spy)
    return reads


def _streamed_once(state, slot, path: Path) -> None:
    """One save that edits the newest row in place: copies the prefix and learns its CRC.

    The consolidation check has counted the file once before, as it does on a
    live gateway, so the prefix's row count can be derived.
    """
    count_and_store(path)
    slot.messages[-1]["content"] = "edited reply"
    slot._dirty = True
    assert chat_persistence._save_slot_to_history(state, slot) is True


def _meta_rewrite(state, slot, path: Path) -> None:
    ConversationLog(base_dir=path.parent).update_metadata_if(
        "dashboard:" + KEY.removeprefix("dashboard_"),
        {"turn_in_flight_generation": 7},
        lambda m: True,
    )


def _foreign_append(state, slot, path: Path) -> None:
    cron = {**_row(5_000, role="assistant"), "meta": {"mid": "cron-1"}}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(cron) + "\n")


def _edit_reply(state, slot, path: Path) -> None:
    slot.messages[-1]["content"] = "edited again"


@pytest.mark.parametrize(
    "change", [_meta_rewrite, _foreign_append, _edit_reply], ids=lambda f: f.__name__
)
def test_a_rewrite_copies_the_prefix_and_writes_what_the_full_path_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change
) -> None:
    outputs = []
    for streamed in (True, False):
        with monkeypatch.context() as mp:
            if not streamed:
                mp.setattr(transcript_merge, "streaming_supported", lambda: False)
            state, slot, path = _primed(tmp_path / str(streamed), mp, WINDOW)
            _streamed_once(state, slot, path)
            change(state, slot, path)
            slot.messages.append(_row(10_000, role="assistant"))
            slot._dirty = True
            reads = _whole_reads(mp, path)
            assert chat_persistence._save_slot_to_history(state, slot) is True
            if streamed and transcript_merge.streaming_supported():
                assert reads == []
                assert stored_count(path) == count_messages(path)
            outputs.append(_without_tab_id(path.read_bytes()))
            if change is _foreign_append:
                assert b'"cron-1"' in path.read_bytes()
    assert outputs[0] == outputs[1]


def test_a_prefix_changed_under_the_slot_takes_the_whole_file_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, slot, path = _primed(tmp_path, monkeypatch, WINDOW)
    _streamed_once(state, slot, path)
    # Another writer edits an old row and rewrites the file.
    head, first, rest = path.read_bytes().split(b"\n", 2)
    edited = json.loads(first)
    edited["content"] = "changed by someone else"
    path.write_bytes(head + b"\n" + json.dumps(edited).encode() + b"\n" + rest)
    slot.messages.append(_row(10_000, role="assistant"))
    slot._dirty = True
    reads = _whole_reads(monkeypatch, path)

    assert chat_persistence._save_slot_to_history(state, slot) is True

    assert reads  # the whole-file path ran
    assert b"changed by someone else" in path.read_bytes()
    assert json.loads(path.read_bytes().splitlines()[-1])["meta"]["mid"] == "m-10000"


@pytest.mark.skipif(
    not transcript_merge.streaming_supported(), reason="Prefix streaming is unsupported"
)
def test_a_file_change_in_the_window_after_metadata_rewrite_reads_the_whole_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, slot, path = _primed(tmp_path, monkeypatch, WINDOW)
    _streamed_once(state, slot, path)
    monkeypatch.setattr(transcript_merge, "_file_changes_keep_turns", lambda: 1)
    row = _row(10_000, role="assistant")
    row["meta"]["file_changes"] = [{"path": "/repo/a.py", "before": "x", "after": "y"}]
    slot.messages.append(row)
    _meta_rewrite(state, slot, path)
    slot._dirty = True
    reads = _whole_reads(monkeypatch, path)

    assert chat_persistence._save_slot_to_history(state, slot) is True

    assert reads  # the whole-file path applies the file-change retention bound
    written = json.loads(path.read_bytes().splitlines()[-1])
    assert written["meta"]["file_changes"] == row["meta"]["file_changes"]


@pytest.mark.skipif(
    not transcript_merge.streaming_supported(), reason="Prefix streaming is unsupported"
)
def test_an_append_keeps_the_stored_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state, slot, path = _primed(tmp_path, monkeypatch, WINDOW)
    _streamed_once(state, slot, path)
    assert stored_count(path) == count_messages(path)
    slot.messages.append(_row(10_000, role="assistant"))
    slot._dirty = True

    assert chat_persistence._save_slot_to_history(state, slot) is True

    assert stored_count(path) == count_messages(path)
