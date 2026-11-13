"""File-change snapshots for kiro-cli's v3 write tools, whose arguments name no command.

The v3 engine sends ``fs_write`` / ``fs_append`` as ``{path, text}`` and
``str_replace`` as ``{path, oldStr, newStr, replace_all}``, and names the file in
its diff content block as a ``file://`` URI. The frames below copy the shapes a
live kiro-cli 2.35.1 ``--agent-engine v3`` session wrote for a create, a replace
and an append of one file (only the path and the session ids are changed). The
older ``command`` shapes are covered in ``test_file_change_snapshots.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from file_change_snapshots_helpers import short_tmp_dir_fixture  # noqa: F401
from file_change_snapshots_helpers import _LANDS, _persisted_meta, _snapshot_turn

from kiro_crew.acp._dispatch_parts.session_update_parser import parse_session_update
from kiro_crew.acp.types import (
    EVENT_TOOL_CALL,
    EVENT_TOOL_CALL_UPDATE,
    STOP_REASON_END_TURN,
    AcpEvent,
)
from kiro_crew.dashboard.chat_runner import _snapshot_write_target

BEFORE = "one\ntwo\nthree\n"
REPLACED = "one\nTWO\nthree\n"
AFTER = "one\nTWO\nthree\nfour"


def _create_frames(path: Path) -> list[dict[str, Any]]:
    args = {"path": str(path), "text": BEFORE}
    return [
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "call-write",
            "title": "Write File",
            "kind": "edit",
            "status": "pending",
            "rawInput": args,
            "locations": [{"path": str(path)}],
        },
        {"sessionUpdate": "tool_call_update", "toolCallId": "call-write", "rawInput": args},
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "call-write",
            "title": "Write File",
            "status": "completed",
            "rawInput": args,
            "content": [{"type": "diff", "path": path.as_uri(), "oldText": "", "newText": BEFORE}],
        },
    ]


def _replace_frames(path: Path) -> list[dict[str, Any]]:
    args = {"path": str(path), "oldStr": "two", "newStr": "TWO", "replace_all": False}
    return [
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "call-replace",
            "title": "Replace in File",
            "kind": "edit",
            "status": "pending",
            "rawInput": {"path": str(path), "oldStr": "two", "newStr": "TWO"},
            "locations": [{"path": str(path)}],
        },
        # The placeholder block a replace sends before its real one: no path,
        # empty texts. Read as a before it would claim the file was created.
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "call-replace",
            "rawInput": args,
            "content": [{"type": "diff", "path": "", "oldText": "", "newText": ""}],
        },
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "call-replace",
            "title": "Replace in File",
            "status": "completed",
            "rawInput": args,
            "content": [
                {"type": "diff", "path": path.as_uri(), "oldText": BEFORE, "newText": REPLACED}
            ],
        },
    ]


def _append_frames(path: Path) -> list[dict[str, Any]]:
    args = {"path": str(path), "text": "four"}
    return [
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "call-append",
            "title": "Append to File",
            "kind": "edit",
            "status": "pending",
            "rawInput": {"path": str(path)},
        },
        {"sessionUpdate": "tool_call_update", "toolCallId": "call-append", "rawInput": args},
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "call-append",
            "title": "Append to File",
            "status": "completed",
            "rawInput": args,
            "content": [
                {"type": "diff", "path": path.as_uri(), "oldText": REPLACED, "newText": AFTER}
            ],
        },
    ]


def _events(frames: list[dict[str, Any]]) -> list[AcpEvent]:
    events: list[AcpEvent] = []
    for frame in frames:
        parsed = parse_session_update(frame)
        assert parsed, frame
        events.extend(parsed)
    return events


def _snapshots(frames: list[dict[str, Any]]) -> list[dict | None]:
    return [
        _snapshot_write_target(
            e.raw_tool_params, diff_old_text=e.diff_old_text, diff_path=e.diff_path
        )
        for e in _events(frames)
        if e.kind in (EVENT_TOOL_CALL, EVENT_TOOL_CALL_UPDATE)
    ]


class TestCommandlessWriteSnapshot:
    @pytest.mark.parametrize(
        ("frames", "before"),
        [(_create_frames, ""), (_replace_frames, BEFORE), (_append_frames, REPLACED)],
        ids=["create", "replace", "append"],
    )
    def test_only_the_block_naming_the_file_gives_the_before(
        self, short_tmp_dir: Path, frames, before
    ):
        # The file already holds the finished write: a disk read on any frame
        # would record it as the before. Only the content block may.
        f = short_tmp_dir / "a.txt"
        f.write_text(AFTER)
        *earlier, last = _snapshots(frames(f))
        assert earlier == [None] * len(earlier)
        assert last is not None
        assert last["path"] == str(f)
        assert last["content"] == before
        assert last["full_content"] == before
        assert last["truncated"] is False

    def test_a_crlf_before_is_folded_like_the_disk_after(self, short_tmp_dir: Path):
        # The turn-end after is read from disk with newlines folded to LF, so
        # an unfolded CRLF before would mark every line changed.
        f = short_tmp_dir / "a.txt"
        snap = _snapshot_write_target(
            {"path": str(f), "oldStr": "two", "newStr": "TWO", "replace_all": False},
            diff_old_text="one\r\ntwo\r\nthree\r",
            diff_path=f.as_uri(),
        )
        assert snap is not None
        assert snap["content"] == "one\ntwo\nthree\n"
        assert snap["full_content"] == "one\ntwo\nthree\n"

    def test_a_crlf_create_block_is_folded_too(self, short_tmp_dir: Path):
        f = short_tmp_dir / "a.txt"
        snap = _snapshot_write_target(
            {"command": "create", "path": str(f)}, diff_old_text="one\r\ntwo\r\n", diff_path=str(f)
        )
        assert snap is not None
        assert snap["content"] == "one\ntwo\n"
        assert snap["full_content"] == "one\ntwo\n"

    def test_a_home_relative_path_matches_its_absolute_block(
        self, short_tmp_dir: Path, monkeypatch
    ):
        monkeypatch.setenv("HOME", str(short_tmp_dir))
        monkeypatch.setenv("USERPROFILE", str(short_tmp_dir))
        f = short_tmp_dir / "a.txt"
        snap = _snapshot_write_target(
            {"path": "~/a.txt", "text": "x"}, diff_old_text="old\n", diff_path=f.as_uri()
        )
        assert snap is not None and snap["content"] == "old\n"

    def test_a_block_naming_another_file_records_nothing(self, short_tmp_dir: Path):
        f = short_tmp_dir / "a.txt"
        other = short_tmp_dir / "b.txt"
        snap = _snapshot_write_target(
            {"path": str(f), "text": "x"}, diff_old_text="", diff_path=other.as_uri()
        )
        assert snap is None

    @pytest.mark.parametrize(
        "diff_path",
        ["file://other-host/tmp/a.txt", "https://example.invalid/a.txt", ""],
        ids=["remote-host", "other-scheme", "empty"],
    )
    def test_a_block_with_no_local_path_records_nothing(self, short_tmp_dir: Path, diff_path: str):
        f = short_tmp_dir / "a.txt"
        assert (
            _snapshot_write_target(
                {"path": str(f), "text": "x"}, diff_old_text="", diff_path=diff_path
            )
            is None
        )

    def test_a_plain_path_block_is_read_like_a_uri(self, short_tmp_dir: Path):
        f = short_tmp_dir / "a.txt"
        snap = _snapshot_write_target(
            {"path": str(f), "text": "x"}, diff_old_text="old\n", diff_path=str(f)
        )
        assert snap is not None and snap["content"] == "old\n"

    def test_a_space_in_the_path_survives_the_uri(self, short_tmp_dir: Path):
        f = short_tmp_dir / "my notes.txt"
        snap = _snapshot_write_target(
            {"path": str(f), "text": "x"}, diff_old_text="old\n", diff_path=f.as_uri()
        )
        assert snap is not None and snap["path"] == str(f)

    @pytest.mark.parametrize(
        "params",
        [
            {"file_path": "/tmp/a.txt", "content": "x"},
            {"path": "/tmp/a.txt", "text": "x", "mode": "overwrite"},
            {"path": "", "text": "x"},
            {"path": 3, "text": "x"},
        ],
        ids=["other-harness-keys", "unknown-key", "empty-path", "non-string-path"],
    )
    def test_other_shapes_still_record_nothing(self, params: dict):
        assert (
            _snapshot_write_target(params, diff_old_text="", diff_path="file:///tmp/a.txt") is None
        )

    def test_a_sensitive_path_records_nothing(self):
        path = str(Path.home() / ".aws" / "credentials")
        snap = _snapshot_write_target(
            {"path": path, "text": "x"}, diff_old_text="secret", diff_path=Path(path).as_uri()
        )
        assert snap is None


class TestCommandlessWriteTurn:
    @pytest.mark.asyncio
    async def test_a_v3_turn_attaches_the_changed_file_card(self, short_tmp_dir: Path):
        f = short_tmp_dir / "a.txt"
        f.write_text(AFTER)
        # Each completed frame parses into the call's result and its last
        # refinement, so the events below carry their own results.
        record = await _snapshot_turn(
            *_events(_create_frames(f)),
            *_events(_replace_frames(f)),
            *_events(_append_frames(f)),
            *_LANDS,
        )
        assert record.stop_reason == STOP_REASON_END_TURN
        changes = _persisted_meta(record, "done").get("file_changes")
        assert changes is not None, "a v3 turn that wrote a file must record it"
        [entry] = changes
        assert entry["path"] == str(f)
        # The first write's before is kept: the card shows the whole turn's change.
        assert entry["before"] == ""
        assert entry["after"] == AFTER
