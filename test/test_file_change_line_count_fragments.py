"""The turn's line count never diffs a content-block fragment as a whole file.

For a strReplace the classifier declines (``replaceAll``, an empty-``newStr``
deletion) and for an ``insert``, the only before ``_snapshot_write_target`` has is
the content block's ``oldText``: the replaced fragment, not the file. Diffed
against the whole file after the write, every line outside the fragment would
count as added, so ``_turn_line_changes`` leaves such a path uncounted (the count
is a floor, never fabricated). The file-change chip is not changed here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from file_change_snapshots_helpers import short_tmp_dir_fixture  # noqa: F401
from file_change_snapshots_helpers import _make_slot_with_assistant_message

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_runner import (
    _flush_file_changes,
    _record_turn_snapshot,
    _snapshot_write_target,
    _turn_line_changes,
)

_FILE = "".join(f"line{i}\n" for i in range(50))


def _count_one_write(target: Path, params: dict, fragment: str, after: str) -> int:
    """Snapshot one write from its content block, apply it, count the turn."""
    snapshot = _snapshot_write_target(params, diff_old_text=fragment)
    assert snapshot is not None
    slot = _make_slot_with_assistant_message()
    _record_turn_snapshot(slot, snapshot, "call-1")
    target.write_text(after)
    return _turn_line_changes(slot._file_changes)


@pytest.mark.parametrize(
    ("params", "fragment", "after"),
    [
        pytest.param(
            {"command": "strReplace", "oldStr": "line7", "newStr": "LINE7", "replaceAll": True},
            "line7",
            _FILE.replace("line7\n", "LINE7\n"),
            id="replace-all",
        ),
        pytest.param(
            {"command": "strReplace", "oldStr": "line7\n", "newStr": ""},
            "line7\n",
            _FILE.replace("line7\n", ""),
            id="empty-new-str-deletion",
        ),
        pytest.param(
            {"command": "insert", "insertLine": 3, "newStr": "added\n"},
            "",
            _FILE.replace("line3\n", "line3\nadded\n"),
            id="insert",
        ),
    ],
)
def test_a_fragment_before_leaves_the_file_uncounted(
    short_tmp_dir: Path, params: dict, fragment: str, after: str
) -> None:
    target = short_tmp_dir / "mod.py"
    target.write_text(_FILE)

    assert _count_one_write(target, {**params, "path": str(target)}, fragment, after) == 0


def test_a_whole_file_before_from_the_content_block_still_counts(short_tmp_dir: Path) -> None:
    """create's content block is the whole previous file, so it is diffed."""
    target = short_tmp_dir / "mod.py"
    target.write_text(_FILE)
    params = {"command": "create", "path": str(target), "fileText": _FILE + "tail\n"}

    assert _count_one_write(target, params, _FILE, _FILE + "tail\n") == 1


def test_a_fragment_with_a_pending_str_replace_is_left_to_the_pair_builder(
    short_tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deferred strReplace can still be settled, so the skip does not take it."""
    target = short_tmp_dir / "mod.py"
    before = "a\nb\n" + _FILE
    target.write_text(before)
    params = {"command": "strReplace", "path": str(target), "oldStr": "a\nb\n", "newStr": "a\n"}
    snapshot = _snapshot_write_target(params, diff_old_text="a\nb\n")
    assert snapshot is not None and "pending_str_replace" in snapshot
    slot = _make_slot_with_assistant_message()
    _record_turn_snapshot(slot, snapshot, "call-1")
    seen: list[dict] = []
    monkeypatch.setattr(chat_runner, "_line_change_input", lambda fc, after: seen.append(fc))

    _turn_line_changes(slot._file_changes)

    assert seen == [slot._file_changes[0]]


def test_a_fragment_stays_uncounted_after_a_second_writer_drops_its_settling(
    short_tmp_dir: Path,
) -> None:
    """A second writer voids the deferred strReplace; the fragment is left behind."""
    target = short_tmp_dir / "mod.py"
    before = "a\nb\n" + _FILE
    target.write_text(before)
    params = {"command": "strReplace", "path": str(target), "oldStr": "a\nb\n", "newStr": "a\n"}
    snapshot = _snapshot_write_target(params, diff_old_text="a\nb\n")
    assert snapshot is not None and "pending_str_replace" in snapshot
    slot = _make_slot_with_assistant_message()
    _record_turn_snapshot(slot, snapshot, "call-1")
    # Same canonical key as the first write: the raw path can differ from it
    # (a Windows short-name temp dir), and the accumulator matches on the key.
    second = {"path": str(target), "canonical_path": snapshot["canonical_path"], "content": ""}
    _record_turn_snapshot(slot, second, "call-2")
    assert "pending_str_replace" not in slot._file_changes[0]
    target.write_text("a\n" + _FILE)

    assert _turn_line_changes(slot._file_changes) == 0


def _stored_entry(target: Path, params: dict, fragment: str, after: str) -> dict:
    """Snapshot one write, apply it, flush the turn: the entry the transcript keeps."""
    snapshot = _snapshot_write_target({**params, "path": str(target)}, diff_old_text=fragment)
    assert snapshot is not None
    slot = _make_slot_with_assistant_message()
    slot._file_changes = []
    _record_turn_snapshot(slot, snapshot, "call-1")
    target.write_text(after)
    _flush_file_changes(slot)
    (entry,) = slot.messages[-1]["meta"]["file_changes"]
    return entry


def test_an_unsettled_fragment_before_is_marked_on_the_stored_entry(short_tmp_dir: Path) -> None:
    """A reader of the stored pair (the Files tab's Show changes) must not diff it."""
    target = short_tmp_dir / "mod.py"
    target.write_text(_FILE)
    params = {"command": "strReplace", "oldStr": "line7", "newStr": "LINE7", "replaceAll": True}

    entry = _stored_entry(target, params, "line7", _FILE.replace("line7\n", "LINE7\n"))

    assert entry["before"] == "line7" and entry["before_fragment"] is True


def test_a_settled_or_whole_before_is_marked_whole(short_tmp_dir: Path) -> None:
    """False, not absent: an entry with no mark was written before the mark existed."""
    target = short_tmp_dir / "mod.py"
    target.write_text("a\nb\n" + _FILE)
    settled = {"command": "strReplace", "oldStr": "a\nb\n", "newStr": "a\n"}

    entry = _stored_entry(target, settled, "a\nb\n", "a\n" + _FILE)

    assert entry["before"] == "a\nb\n" + _FILE and entry["before_fragment"] is False
    target.write_text(_FILE)
    create = {"command": "create", "fileText": _FILE + "tail\n"}
    assert _stored_entry(target, create, _FILE, _FILE + "tail\n")["before_fragment"] is False
