"""File-change snapshots: how an edit's diff is settled, windowed and bounded.

A pending ``strReplace`` snapshot is resolved at flush time, directly and through
the real ``_run_chat``; an edit past the per-file cut of a large file keeps a
windowed diff, whatever its shape, with whole-file reads off the event loop and
windows cut from redacted text; ``_excerpt_pair`` diffs only what differs and gives
up past its budget; a one-file turn keeps its entry; and a write that did not change
its file never shows an edit. The snapshot primitives and ``_flush_file_changes``
are in ``test_file_change_snapshots.py``.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from file_change_snapshots_helpers import short_tmp_dir_fixture  # noqa: F401
from file_change_snapshots_helpers import (
    _LANDS,
    _make_slot_with_assistant_message,
    _persisted_meta,
    _snapshot_turn,
    _write_call,
)
from turn_harness import Do, Raise, TurnRecord

from kiro_crew.acp.types import (
    EVENT_TOOL_CALL,
    EVENT_TOOL_CALL_UPDATE,
    EVENT_TOOL_RESULT,
    STOP_REASON_END_TURN,
    AcpEvent,
)
from kiro_crew.dashboard.chat_runner import (
    _MAX_SNAPSHOT,
    _apply_turn_snapshot_budget,
    _flush_file_changes,
    _record_turn_snapshot,
    _snapshot_write_target,
    _turn_after_reads,
)
from kiro_crew.dashboard.state import _ChatSlot


class TestPendingStrReplaceResolution:
    """An undecidable strReplace snapshot is settled at flush time.

    ``newStr ⊂ oldStr`` (drop a line next to a kept one) and ``oldStr ⊂
    newStr`` (add a line next to a kept one) leave the disk file valid as
    BOTH states at snapshot time, whichever side of the write the snapshot
    lands on. A fragment-only before would render the chip as ``-1 +N``. Only
    the PRE-write hypothesis is settled: the turn-end file is compared to the
    forward substitution, and a match yields the content actually captured from
    disk at snapshot time. A resolved before is therefore never synthesised
    from the turn-end state, so a refused write (file unchanged), a later write,
    or a concurrent restore cannot make it fabricate — it simply fails the
    comparison and keeps the fragment.
    """

    PARAMS = {
        "command": "strReplace",
        "oldStr": "export A=0\nexport B=1\n",
        "newStr": "export B=1\n",
    }
    BEFORE = "line 1\nexport A=0\nexport B=1\n"
    AFTER = "line 1\nexport B=1\n"

    def _snapshot(self, path: Path, on_disk: str, slot: _ChatSlot) -> dict:
        path.write_text(on_disk)
        snap = _snapshot_write_target(
            {**self.PARAMS, "path": str(path)},
            diff_old_text=self.PARAMS["oldStr"],
            diff_path=str(path),
        )
        assert snap is not None
        slot._file_changes = [snap]
        return snap

    def _flush(self, slot: _ChatSlot, path: Path, on_disk_at_flush: str) -> dict:
        path.write_text(on_disk_at_flush)
        _flush_file_changes(slot)
        return slot.messages[-1]["meta"]["file_changes"][0]

    def test_snapshot_taken_before_the_write_resolves_to_disk_content(self, short_tmp_dir: Path):
        f = short_tmp_dir / "rc"
        slot = _make_slot_with_assistant_message()
        snap = self._snapshot(f, self.BEFORE, slot)
        # Undecidable at snapshot time: fragment now, two hypotheses pending.
        assert snap["content"] == self.PARAMS["oldStr"]
        assert snap["pending_str_replace"]["if_post_write"].content == self.BEFORE
        assert snap["pending_str_replace"]["if_pre_write"].content == self.AFTER
        assert "before_if_post" not in snap["pending_str_replace"]
        # Turn-end disk == if_pre_write (the forward substitution): the snapshot
        # was pre-write, so the before is the captured disk content.
        entry = self._flush(slot, f, self.AFTER)
        assert entry["before"] == self.BEFORE
        assert entry["after"] == self.AFTER

    def test_unchanged_file_keeps_the_fragment(self, short_tmp_dir: Path):
        """A refused/failed/cancelled write (or a snapshot taken before a
        permission-gated write that is then denied) leaves the file equal to
        the snapshot content (== if_post_write != if_pre_write), so the
        pre-write comparison does not fire and the fragment stands — no
        completion gate needed."""
        f = short_tmp_dir / "rc"
        slot = _make_slot_with_assistant_message()
        self._snapshot(f, self.BEFORE, slot)
        entry = self._flush(slot, f, self.BEFORE)
        assert entry["before"] == self.PARAMS["oldStr"]
        assert entry["after"] == self.BEFORE

    def test_file_changed_again_keeps_the_fragment(self, short_tmp_dir: Path):
        f = short_tmp_dir / "rc"
        slot = _make_slot_with_assistant_message()
        self._snapshot(f, self.BEFORE, slot)
        entry = self._flush(slot, f, "something else entirely\n")
        assert entry["before"] == self.PARAMS["oldStr"]

    def test_pending_payload_never_reaches_message_meta(self, short_tmp_dir: Path):
        f = short_tmp_dir / "rc"
        slot = _make_slot_with_assistant_message()
        self._snapshot(f, self.BEFORE, slot)
        entry = self._flush(slot, f, self.AFTER)
        assert set(entry) == {"path", "before", "after"}

    def test_append_style_edit_keeps_the_fragment(self, short_tmp_dir: Path):
        """oldStr ⊂ newStr (the mirror shape): here the POST-write disk state is
        the one valid as both, so settling it would need a reverse substitution
        against the turn-end disk — the one branch a shell or cross-slot restore
        could turn into a fabrication. Only the pre-write hypothesis is settled,
        so this direction keeps the fragment rather than resolving."""
        f = short_tmp_dir / "cfg"
        after = "head\nvalue = 1  # tuned\ntail\n"
        f.write_text(after)
        params = {
            "command": "strReplace",
            "path": str(f),
            "oldStr": "value = 1",
            "newStr": "value = 1  # tuned",
        }
        snap = _snapshot_write_target(params, diff_old_text="value = 1", diff_path=str(f))
        assert snap is not None and "pending_str_replace" in snap
        assert "before_if_post" not in snap["pending_str_replace"]
        slot = _make_slot_with_assistant_message()
        slot._file_changes = [snap]
        entry = self._flush(slot, f, after)
        assert entry["before"] == "value = 1"
        assert entry["after"] == after

    def test_decided_snapshot_carries_no_pending(self, short_tmp_dir: Path):
        """A provable state (newStr absent → pre-write) needs no deferral."""
        f = short_tmp_dir / "plain"
        f.write_text("OLD\n")
        snap = _snapshot_write_target(
            {"command": "strReplace", "path": str(f), "oldStr": "OLD", "newStr": "NEW"},
            diff_old_text="OLD",
            diff_path=str(f),
        )
        assert snap is not None
        assert "pending_str_replace" not in snap
        assert snap["content"] == "OLD\n"

    def test_restore_to_snapshot_content_never_fabricates_a_before(self, short_tmp_dir: Path):
        """The payload carries no reverse substitution and the post-write branch
        is not settled, so a turn-end file equal to the snapshot content —
        however it got there (a shell `git checkout`, a concurrent write from
        another session, a refused write) — keeps the fragment instead of
        fabricating a full-file before that was never on disk."""
        f = short_tmp_dir / "rc"
        slot = _make_slot_with_assistant_message()
        snap = self._snapshot(f, self.BEFORE, slot)
        assert "before_if_post" not in snap["pending_str_replace"]
        entry = self._flush(slot, f, self.BEFORE)
        assert entry["before"] == self.PARAMS["oldStr"]
        assert entry["after"] == self.BEFORE

    def test_second_identical_edit_by_another_writer_keeps_the_fragment(self, short_tmp_dir: Path):
        """buluoray's wrong-before case, closed by the writer-id gate. The
        snapshot is undecidable (disk valid as both pre- and post-write), so it
        is deferred. A SECOND identical forward substitution in the same turn —
        a DIFFERENT tool call — would, under content-only resolution, let a
        turn-end file equal to ``if_pre_write`` settle to ``if_post_write`` (the
        after-first-edit content) as the before, under-reporting with no flag.
        Because the two writes carry different ``tool_call_id``s,
        ``_record_turn_snapshot`` drops the deferred ``pending_str_replace`` at
        record time, so the flush keeps the fragment instead of a wrong
        before."""
        f = short_tmp_dir / "rc"
        f.write_text(self.BEFORE)
        slot = _make_slot_with_assistant_message()
        first = _snapshot_write_target(
            {**self.PARAMS, "path": str(f)},
            diff_old_text=self.PARAMS["oldStr"],
            diff_path=str(f),
        )
        assert first is not None and "pending_str_replace" in first
        _record_turn_snapshot(slot, first, "writer-A")
        # Second identical substitution by a DIFFERENT tool call.
        second = _snapshot_write_target(
            {**self.PARAMS, "path": str(f)},
            diff_old_text=self.PARAMS["oldStr"],
            diff_path=str(f),
        )
        assert second is not None
        _record_turn_snapshot(slot, second, "writer-B")
        # The gate must have dropped the deferral on the held entry.
        assert len(slot._file_changes) == 1
        assert "pending_str_replace" not in slot._file_changes[0]
        # Turn-end disk equals if_pre_write (the forward substitution): without
        # the gate this would wrongly resolve; with it, the fragment stands.
        f.write_text(self.AFTER)
        _flush_file_changes(slot)
        entry = slot.messages[-1]["meta"]["file_changes"][0]
        assert entry["before"] == self.PARAMS["oldStr"]
        assert entry["before"] != self.BEFORE

    def test_second_identical_edit_by_the_same_writer_still_resolves(self, short_tmp_dir: Path):
        """The gate is scoped to a CHANGE of writer: a single tool call whose
        snapshot is recorded twice (same ``tool_call_id``) keeps the deferral
        resolvable, so the ordinary single-writer resolution is unaffected."""
        f = short_tmp_dir / "rc"
        f.write_text(self.BEFORE)
        slot = _make_slot_with_assistant_message()
        snap = _snapshot_write_target(
            {**self.PARAMS, "path": str(f)},
            diff_old_text=self.PARAMS["oldStr"],
            diff_path=str(f),
        )
        assert snap is not None and "pending_str_replace" in snap
        _record_turn_snapshot(slot, snap, "writer-A")
        _record_turn_snapshot(slot, dict(snap), "writer-A")
        assert len(slot._file_changes) == 1
        assert "pending_str_replace" in slot._file_changes[0]
        f.write_text(self.AFTER)
        _flush_file_changes(slot)
        entry = slot.messages[-1]["meta"]["file_changes"][0]
        assert entry["before"] == self.BEFORE
        assert entry["after"] == self.AFTER

    def test_shell_activity_in_the_turn_keeps_the_fragment(self, short_tmp_dir: Path):
        """GPT 6.1's wrong-before case: a shell command editing the file after a
        post-write snapshot is never seen by the write-tool gate (a shell does
        not pass through ``_record_turn_snapshot``), so it cannot be gated per
        path. When a shell ran this turn, ``_flush_file_changes`` skips pending
        resolution entirely (``turn_had_shell``) and keeps the fragment, rather
        than trusting a turn-end read a shell could have shaped into a false
        pre-write match. Compare ``test_...resolves...``: identical setup, the
        only difference is the shell flag."""
        f = short_tmp_dir / "rc"
        f.write_text(self.BEFORE)
        slot = _make_slot_with_assistant_message()
        snap = self._snapshot(f, self.BEFORE, slot)
        assert "pending_str_replace" in snap
        # Turn-end disk equals if_pre_write, which WOULD resolve — but a shell
        # ran this turn, so resolution is skipped and the fragment stands.
        f.write_text(self.AFTER)
        _flush_file_changes(slot, turn_had_shell=True)
        entry = slot.messages[-1]["meta"]["file_changes"][0]
        assert entry["before"] == self.PARAMS["oldStr"]
        assert entry["after"] == self.AFTER
        # Control: without the shell flag the same setup resolves, proving the
        # flag is what keeps the fragment (not some other mismatch).
        f.write_text(self.BEFORE)
        slot2 = _make_slot_with_assistant_message()
        self._snapshot(f, self.BEFORE, slot2)
        f.write_text(self.AFTER)
        _flush_file_changes(slot2, turn_had_shell=False)
        assert slot2.messages[-1]["meta"]["file_changes"][0]["before"] == self.BEFORE

    def test_alias_spelling_in_the_same_turn_stays_one_resolvable_entry(self, short_tmp_dir: Path):
        """The deferred-resolution path is itself why the canonical bucket is
        in scope. An undecidable strReplace deferred under one spelling, then a
        second write to the SAME file under an alias spelling in the same turn,
        must stay ONE entry: the flush reads one turn-end ``after`` per bucket
        and resolves the deferred snapshot against it. Keyed by the raw path the
        two spellings would split into two cards — the deferred one resolving
        against a bucket the alias write never updated — so the alias-split is
        reachable precisely for this fix's undecidable shape. Keyed by
        ``canonical_path`` they are one entry whose pre-write hypothesis settles
        against the real turn-end content."""
        f = short_tmp_dir / "rc"
        f.write_text(self.BEFORE)
        canon = os.path.realpath(str(f))
        slot = _make_slot_with_assistant_message()
        # First write: the undecidable strReplace, snapshotted under /dir/rc.
        snap = _snapshot_write_target(
            {**self.PARAMS, "path": str(f)},
            diff_old_text=self.PARAMS["oldStr"],
            diff_path=str(f),
        )
        assert snap is not None
        assert snap["canonical_path"] == canon
        assert "pending_str_replace" in snap
        _record_turn_snapshot(slot, snap, "writer-1")
        # Second write to the same file under the alias spelling /dir/./rc:
        # same canonical path, so it rides the first bucket rather than opening
        # a second card. Same known writer id, so the gate keeps the deferral
        # resolvable (the single-known-writer case).
        alias = f.parent / "." / f.name
        alias_snap = _snapshot_write_target(
            {"command": "insert", "path": str(alias)},
            diff_old_text=self.AFTER,
            diff_path=str(alias),
        )
        assert alias_snap is not None
        assert alias_snap["canonical_path"] == canon
        _record_turn_snapshot(slot, alias_snap, "writer-1")
        assert len(slot._file_changes) == 1, "two spellings of one file split the deferral"
        # Turn-end disk is the forward substitution: the deferred snapshot was
        # pre-write, so the one card resolves to the captured disk content.
        f.write_text(self.AFTER)
        _flush_file_changes(slot)
        entries = slot.messages[-1]["meta"]["file_changes"]
        assert len(entries) == 1
        assert entries[0]["before"] == self.BEFORE
        assert entries[0]["after"] == self.AFTER
        assert "pending_str_replace" not in entries[0]


class TestPendingStrReplaceThroughTheTurnLoop:
    """The deferred-resolution call sites wired through the real ``_run_chat``.

    The unit tests above drive the helpers directly; these prove the turn loop
    snapshots a write and the flush resolves it, running end to end without
    error, and that the chip still surfaces. The harness streams events without
    touching disk between
    them, so a turn's file is static across the snapshot and the flush; a static
    disk never satisfies the pre-write branch (that needs the turn-end file to
    equal the forward substitution of the snapshot content), so these turns keep
    the fragment. The resolution itself is proven in the unit class above, which
    can set the pre- and post-write disk states independently.
    """

    AFTER = "head\nvalue = 1  # tuned\ntail\n"
    PARAMS = {
        "command": "strReplace",
        "oldStr": "value = 1",
        "newStr": "value = 1  # tuned",
    }

    def _file_change(self, record: TurnRecord) -> dict:
        changes = [
            row["meta"]["file_changes"]
            for row in record.rows("assistant")
            if (row.get("meta") or {}).get("file_changes")
        ]
        assert changes, "no file_changes meta persisted"
        assert len(changes[-1]) == 1
        return changes[-1][0]

    @pytest.mark.asyncio
    async def test_bind_and_settle_run_and_the_chip_surfaces(self, short_tmp_dir: Path):
        """A completed ambiguous strReplace is snapshotted on tool_call and
        resolved at flush; the chip surfaces and never carries the internal
        pending payload. Static disk, so the fragment stands."""
        f = short_tmp_dir / "cfg"
        f.write_text(self.AFTER)
        record = await _snapshot_turn(
            AcpEvent(
                kind=EVENT_TOOL_CALL,
                tool_call_id="call-sr",
                tool_name="fs_write",
                tool_kind="edit",
                title="Write",
                raw_tool_params={**self.PARAMS, "path": str(f)},
                diff_old_text=self.PARAMS["oldStr"],
                diff_path=str(f),
            ),
            AcpEvent(
                kind=EVENT_TOOL_RESULT,
                tool_call_id="call-sr",
                tool_output="ok",
                tool_final=True,
                tool_status="completed",
            ),
            *_LANDS,
        )
        assert record.stop_reason == STOP_REASON_END_TURN
        entry = self._file_change(record)
        assert entry["before"] == self.PARAMS["oldStr"]
        assert "pending_str_replace" not in entry

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["refused", "failed"])
    async def test_a_write_that_did_not_complete_claims_no_change(
        self, short_tmp_dir: Path, status
    ):
        # Its fragment before ("value = 1" against the whole file) would show an
        # edit that may never have happened, and neither status word proves the
        # file was left alone: the path, with no hunk and no "no changes".
        f = short_tmp_dir / "cfg"
        f.write_text(self.AFTER)
        record = await _snapshot_turn(
            AcpEvent(
                kind=EVENT_TOOL_CALL,
                tool_call_id="call-sr",
                tool_name="fs_write",
                tool_kind="edit",
                title="Write",
                raw_tool_params={**self.PARAMS, "path": str(f)},
                diff_old_text=self.PARAMS["oldStr"],
                diff_path=str(f),
            ),
            AcpEvent(
                kind=EVENT_TOOL_RESULT,
                tool_call_id="call-sr",
                tool_output=status,
                tool_status=status,
            ),
            *_LANDS,
        )
        assert record.stop_reason == STOP_REASON_END_TURN
        changes = [
            (row.get("meta") or {}).get("file_changes")
            for row in record.rows("assistant")
            if (row.get("meta") or {}).get("file_changes")
        ]
        (entry,) = changes[-1]
        _assert_unverified(entry)


# ── a small edit past the per-file cut of a large file ─────────────────────


def _large_source(lines: int = 20_000) -> str:
    """About 1 MB of distinct lines, far past ``_MAX_SNAPSHOT``."""
    return "".join(f"line {i:06d} " + "x" * 40 + "\n" for i in range(lines))


def _changed_lines(before: str, after: str) -> tuple[int, int]:
    import difflib

    added = removed = 0
    for line in difflib.ndiff(before.splitlines(), after.splitlines()):
        if line.startswith("+ "):
            added += 1
        elif line.startswith("- "):
            removed += 1
    return added, removed


def _flush_with_reads(slot: _ChatSlot) -> None:
    """Flush as the turn-completion path does: after reads made first, then applied."""
    _flush_file_changes(slot, after_reads=_turn_after_reads(slot._file_changes, False))


class TestLargeFileEditPastTheCut:
    """An edit past char ``_MAX_SNAPSHOT`` of a large file keeps its diff.

    The per-file cap kept each side's FIRST ``_MAX_SNAPSHOT`` chars, so both cuts
    matched, the entry looked unchanged, lost the turn budget's protection, and
    a one-file turn showed "too large to keep every file's diff".
    """

    @staticmethod
    def _edit(text: str) -> tuple[str, str, str]:
        # 80 lines well past the first _MAX_SNAPSHOT chars.
        lines = text.splitlines(keepends=True)
        old = "".join(lines[18_000:18_080])
        new = "".join(line.replace("x" * 40, "y" * 40) for line in lines[18_000:18_080])
        assert text.index(old) > 4 * _MAX_SNAPSHOT
        return old, new, text.replace(old, new, 1)

    def _assert_kept_diff(self, entry: dict, old: str, new: str) -> None:
        assert not entry.get("content_omitted")
        assert not entry.get("truncated")
        assert entry["before"] != entry["after"]
        assert old in entry["before"] and new in entry["after"]
        assert _changed_lines(entry["before"], entry["after"]) == (80, 80)
        for side in ("before", "after"):
            assert len(entry[side]) <= _MAX_SNAPSHOT

    @pytest.mark.parametrize("command", ["strReplace", "insert"])
    def test_small_edit_keeps_a_correct_diff(self, short_tmp_dir: Path, command: str):
        f = short_tmp_dir / "big.py"
        text = _large_source()
        f.write_text(text)
        old, new, edited = self._edit(text)
        params = {"command": command, "path": str(f)}
        if command == "strReplace":
            params.update(oldStr=old, newStr=new)
        snapshot = _snapshot_write_target(params)
        assert snapshot is not None and snapshot["truncated"]
        slot = _make_slot_with_assistant_message()
        slot._file_changes = []
        _record_turn_snapshot(slot, snapshot)
        f.write_text(edited)
        _flush_with_reads(slot)
        meta = slot.messages[-1]["meta"]
        assert "file_changes_omitted_files" not in meta
        (entry,) = meta["file_changes"]
        self._assert_kept_diff(entry, old, new)
        assert entry["before"].startswith("... (")
        assert "lines not shown)" in entry["before"]

    def test_large_and_small_file_both_keep_their_diffs(self, short_tmp_dir: Path):
        big = short_tmp_dir / "big.py"
        small = short_tmp_dir / "small.py"
        text = _large_source()
        big.write_text(text)
        small.write_text("a\n")
        old, new, edited = self._edit(text)
        slot = _make_slot_with_assistant_message()
        slot._file_changes = []
        _record_turn_snapshot(
            slot,
            _snapshot_write_target(
                {"command": "strReplace", "path": str(big), "oldStr": old, "newStr": new}
            ),
        )
        _record_turn_snapshot(
            slot, _snapshot_write_target({"command": "insert", "path": str(small)})
        )
        big.write_text(edited)
        small.write_text("b\n")
        _flush_with_reads(slot)
        changes = {Path(c["path"]).name: c for c in slot.messages[-1]["meta"]["file_changes"]}
        self._assert_kept_diff(changes["big.py"], old, new)
        assert (changes["small.py"]["before"], changes["small.py"]["after"]) == ("a\n", "b\n")

    def test_unchanged_large_file_keeps_the_top_cut(self, short_tmp_dir: Path):
        f = short_tmp_dir / "big.py"
        f.write_text(_large_source())
        slot = _make_slot_with_assistant_message()
        slot._file_changes = []
        _record_turn_snapshot(slot, _snapshot_write_target({"command": "insert", "path": str(f)}))
        _flush_with_reads(slot)
        meta = slot.messages[-1]["meta"]
        (entry,) = meta["file_changes"]
        # Whole sides compared equal: a no-change entry, not a truncated one, so
        # it cannot take the protected slot from a real change.
        assert (entry["before"], entry["after"]) == ("", "")
        assert not entry.get("truncated") and not entry.get("content_omitted")
        assert "file_changes_omitted_files" not in meta

    def test_proven_unchanged_large_file_leaves_protection_to_the_real_change(
        self, short_tmp_dir: Path
    ):
        same, edited = short_tmp_dir / "same.py", short_tmp_dir / "edited.py"
        text = _large_source()
        same.write_text(text)
        edited.write_text(text)
        slot = _make_slot_with_assistant_message()
        slot._file_changes = []
        _record_turn_snapshot(
            slot, _snapshot_write_target({"command": "insert", "path": str(edited)})
        )
        # A change too large to window: its entry is the top cut on both sides,
        # over the turn budget unless it is the protected one.
        edited.write_text(text.replace("x" * 40, "z" * 40))
        # The unchanged rewrite is the newest write.
        _record_turn_snapshot(
            slot, _snapshot_write_target({"command": "insert", "path": str(same)})
        )
        _flush_with_reads(slot)
        changes = {Path(c["path"]).name: c for c in slot.messages[-1]["meta"]["file_changes"]}
        assert changes["edited.py"]["truncated"]
        assert not changes["edited.py"].get("content_omitted")
        assert not changes["same.py"].get("content_omitted")

    def test_change_larger_than_the_cap_keeps_the_top_cut(self, short_tmp_dir: Path):
        f = short_tmp_dir / "big.py"
        text = _large_source()
        f.write_text(text)
        slot = _make_slot_with_assistant_message()
        slot._file_changes = []
        _record_turn_snapshot(slot, _snapshot_write_target({"command": "insert", "path": str(f)}))
        f.write_text(text.replace("x" * 40, "z" * 40))
        _flush_with_reads(slot)
        (entry,) = slot.messages[-1]["meta"]["file_changes"]
        assert entry["truncated"]
        assert entry["before"].startswith("line 000000 ")

    def test_fragment_before_is_never_windowed(self, short_tmp_dir: Path):
        # replaceAll declines reconstruction, so the before is the content
        # block's fragment. It fits the cap but is not the file: diffing it as
        # one would invent tens of thousands of added lines.
        f = short_tmp_dir / "big.txt"
        block = "a\n" * 30_000
        f.write_text(block * 4)
        fragment = block
        snapshot = _snapshot_write_target(
            {
                "command": "strReplace",
                "path": str(f),
                "oldStr": block,
                "newStr": block[:-2] + "b\n",
                "replaceAll": True,
            },
            diff_old_text=fragment,
            diff_path=str(f),
        )
        assert snapshot is not None and "full_content" not in snapshot
        slot = _make_slot_with_assistant_message()
        slot._file_changes = []
        _record_turn_snapshot(slot, snapshot)
        f.write_text((block[:-2] + "b\n") * 4)
        _flush_with_reads(slot)
        (entry,) = slot.messages[-1]["meta"]["file_changes"]
        assert entry["truncated"]
        assert "lines not shown)" not in (entry["before"] + entry["after"])

    def test_held_whole_befores_stay_within_the_turn_bound(self, short_tmp_dir: Path, monkeypatch):
        import kiro_crew.dashboard.chat_runner as cr

        size = len(_large_source())
        monkeypatch.setattr(cr, "_MAX_TURN_FULL_CONTENT_CHARS", 2 * size)
        slot = _make_slot_with_assistant_message()
        slot._file_changes = []
        for name in ("a.py", "b.py", "c.py"):
            f = short_tmp_dir / name
            f.write_text(_large_source())
            _record_turn_snapshot(
                slot, _snapshot_write_target({"command": "insert", "path": str(f)})
            )
        held = [Path(e["path"]).name for e in slot._file_changes if "full_content" in e]
        assert held == ["b.py", "c.py"]


def _x_to_y(line: str) -> str:
    return line.replace("x" * 40, "y" * 40)


class TestLargeFileEditShapes:
    """Edit shapes past the cut: far apart, already on disk, oldStr inside newStr."""

    def test_two_far_apart_edits_both_show(self, short_tmp_dir: Path):
        f = short_tmp_dir / "big.py"
        lines = _large_source().splitlines(keepends=True)
        f.write_text("".join(lines))
        slot = _make_slot_with_assistant_message()
        slot._file_changes = []
        for index, at in enumerate((7000, 17000)):
            old = lines[at]
            _record_turn_snapshot(
                slot,
                _snapshot_write_target(
                    {"command": "strReplace", "path": str(f), "oldStr": old, "newStr": _x_to_y(old)}
                ),
                f"writer-{index}",
            )
            lines[at] = _x_to_y(old)
            f.write_text("".join(lines))
        _flush_with_reads(slot)
        (entry,) = slot.messages[-1]["meta"]["file_changes"]
        assert not entry.get("truncated") and not entry.get("content_omitted")
        assert _changed_lines(entry["before"], entry["after"]) == (2, 2)
        # Three left-out runs: above, between and below the two windows.
        assert entry["before"].count("lines not shown)") == 3
        assert max(len(entry["before"]), len(entry["after"])) <= _MAX_SNAPSHOT

    def test_auto_approve_path_rebuilds_the_before_in_reverse(self, short_tmp_dir: Path):
        # kiro-cli writes before the dashboard sees the call: the disk already
        # holds the after, and the before is the reverse substitution.
        f = short_tmp_dir / "big.py"
        text = _large_source()
        old, new, edited = TestLargeFileEditPastTheCut._edit(text)
        f.write_text(edited)
        snapshot = _snapshot_write_target(
            {"command": "strReplace", "path": str(f), "oldStr": old, "newStr": new},
            diff_old_text=old,
            diff_path=str(f),
        )
        assert snapshot is not None and snapshot["full_content"] == text
        slot = _make_slot_with_assistant_message()
        slot._file_changes = []
        _record_turn_snapshot(slot, snapshot)
        _flush_with_reads(slot)
        (entry,) = slot.messages[-1]["meta"]["file_changes"]
        TestLargeFileEditPastTheCut()._assert_kept_diff(entry, old, new)

    def test_old_inside_new_settles_on_whole_texts(self, short_tmp_dir: Path):
        # oldStr is inside newStr and the disk already holds newStr once, so
        # the file is valid as both states. The capped hypotheses agree past
        # the cut; the whole-text pre-write test settles it.
        f = short_tmp_dir / "big.py"
        lines = _large_source().splitlines(keepends=True)
        old, new = lines[18000], lines[18000] + lines[18001]
        text = "".join(lines)
        f.write_text(text)
        snapshot = _snapshot_write_target(
            {"command": "strReplace", "path": str(f), "oldStr": old, "newStr": new},
            diff_old_text=old,
            diff_path=str(f),
        )
        assert "whole_candidate" in snapshot["pending_str_replace"]
        slot = _make_slot_with_assistant_message()
        slot._file_changes = []
        _record_turn_snapshot(slot, snapshot, "writer")
        f.write_text(text.replace(old, new, 1))
        _flush_with_reads(slot)
        (entry,) = slot.messages[-1]["meta"]["file_changes"]
        assert not entry.get("truncated")
        assert _changed_lines(entry["before"], entry["after"]) == (1, 0)

    def test_whole_candidate_refused_after_a_shell(self, short_tmp_dir: Path):
        f = short_tmp_dir / "big.py"
        lines = _large_source().splitlines(keepends=True)
        old, new = lines[18000], lines[18000] + lines[18001]
        text = "".join(lines)
        f.write_text(text)
        snapshot = _snapshot_write_target(
            {"command": "strReplace", "path": str(f), "oldStr": old, "newStr": new},
            diff_old_text=old,
            diff_path=str(f),
        )
        slot = _make_slot_with_assistant_message()
        slot._file_changes = []
        _record_turn_snapshot(slot, snapshot, "writer")
        f.write_text(text.replace(old, new, 1))
        reads = _turn_after_reads(slot._file_changes, True)
        _flush_file_changes(slot, turn_had_shell=True, after_reads=reads)
        (entry,) = slot.messages[-1]["meta"]["file_changes"]
        assert entry["truncated"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["completed", "refused", "failed"])
    async def test_old_inside_new_already_written_keeps_the_top_cut(
        self, short_tmp_dir: Path, status
    ):
        # The reviewer's repro on the auto-approve path: the disk already holds
        # the after when the dashboard reads it. The only before left is a
        # reverse substitution, which a concurrent restore of the captured bytes
        # would make false, so a completed edit keeps the top cut (a known
        # limit; a pre-write snapshot would lift it), protected as the one entry
        # that may differ. A refused or failed one has no pre-call snapshot and
        # may have written: path only, no diff claimed.
        f = short_tmp_dir / "big.py"
        lines = _large_source().splitlines(keepends=True)
        old, new = lines[18000], lines[18000] + "added line\n"
        f.write_text("".join(lines).replace(old, new, 1))
        record = await _snapshot_turn(
            AcpEvent(
                kind=EVENT_TOOL_CALL,
                tool_call_id="call-big",
                tool_name="fs_write",
                tool_kind="edit",
                title="Write",
                raw_tool_params={
                    "command": "strReplace",
                    "path": str(f),
                    "oldStr": old,
                    "newStr": new,
                },
                diff_old_text=old,
                diff_path=str(f),
            ),
            AcpEvent(
                kind=EVENT_TOOL_RESULT,
                tool_call_id="call-big",
                tool_output="ok" if status == "completed" else status,
                tool_status=status,
                tool_final=status == "completed",
            ),
            *_LANDS,
        )
        (entry,) = _persisted_meta(record, "done")["file_changes"]
        if status != "completed":
            _assert_unverified(entry)
            return
        assert entry["truncated"] and not entry.get("content_omitted")
        assert "lines not shown)" not in (entry["before"] + entry["after"])


class TestLargeFileEditOffTheLoop:
    """Whole-file reads and diffs run off the event loop, never inline."""

    def test_inline_flush_never_reads_the_whole_file(self, short_tmp_dir: Path, monkeypatch):
        import kiro_crew.dashboard.chat_runner as cr

        f = short_tmp_dir / "big.py"
        text = _large_source()
        f.write_text(text)
        old, new, edited = TestLargeFileEditPastTheCut._edit(text)
        slot = _make_slot_with_assistant_message()
        slot._file_changes = []
        _record_turn_snapshot(
            slot,
            _snapshot_write_target(
                {"command": "strReplace", "path": str(f), "oldStr": old, "newStr": new}
            ),
        )
        f.write_text(edited)

        real = cr._safe_read_file_text

        def no_whole_read(path, *, whole):
            assert not whole, "whole-file read on the flush's own thread"
            return real(path, whole=whole)

        def no_diff(before, after):
            raise AssertionError("diff on the flush's own thread")

        monkeypatch.setattr(cr, "_safe_read_file_text", no_whole_read)
        monkeypatch.setattr(cr, "_excerpt_pair", no_diff)
        _flush_file_changes(slot)
        (entry,) = slot.messages[-1]["meta"]["file_changes"]
        assert entry["truncated"]

    @pytest.mark.asyncio
    async def test_real_turn_keeps_the_diff_and_reads_off_the_loop(
        self, short_tmp_dir: Path, monkeypatch
    ):
        import threading

        import kiro_crew.dashboard.chat_runner as cr

        f = short_tmp_dir / "big.py"
        text = _large_source()
        f.write_text(text)
        old, new, edited = TestLargeFileEditPastTheCut._edit(text)
        real = cr._read_turn_afters
        on_main: list[bool] = []

        def tracked(changes, turn_had_shell=False):
            on_main.append(threading.current_thread() is threading.main_thread())
            return real(changes, turn_had_shell)

        monkeypatch.setattr(cr, "_read_turn_afters", tracked)
        record = await _snapshot_turn(
            AcpEvent(
                kind=EVENT_TOOL_CALL,
                tool_call_id="call-big",
                tool_name="fs_write",
                tool_kind="edit",
                title="Write",
                raw_tool_params={
                    "command": "strReplace",
                    "path": str(f),
                    "oldStr": old,
                    "newStr": new,
                },
                diff_old_text=old,
                diff_path=str(f),
            ),
            Do(lambda ctx: f.write_text(edited)),
            *_LANDS,
        )
        assert on_main == [False]
        (entry,) = _persisted_meta(record, "done")["file_changes"]
        TestLargeFileEditPastTheCut()._assert_kept_diff(entry, old, new)

    def test_hidden_last_line_without_newline_is_counted(self):
        from kiro_crew.dashboard.chat_runner import _excerpt_pair

        tail = "".join(f"t{i}\n" for i in range(99)) + "last"
        pair = _excerpt_pair("head\nx\n" + tail, "head\ny\n" + tail)
        assert pair is not None
        # 100 tail lines, the first 20 shown as context: 80 hidden, "last" included.
        assert pair[0].endswith("... (80 lines not shown)\n")


class TestLargeFileEditRedaction:
    """Windows are cut from REDACTED whole texts, so no window splits a secret."""

    @staticmethod
    def _pem(lines: int) -> list[str]:
        import base64
        import hashlib

        body = [
            base64.b64encode(hashlib.sha512(f"k{i}".encode()).digest()).decode()[:64] + "\n"
            for i in range(lines)
        ]
        # Built from parts so the source carries no literal key delimiter.
        label = "RSA PRIVATE " + "KEY-----\n"
        return ["-----BEGIN " + label, *body, "-----END " + label]

    def test_edit_just_below_a_private_key_keeps_the_key_out(self, short_tmp_dir: Path):
        f = short_tmp_dir / "big.conf"
        lines = _large_source().splitlines(keepends=True)
        key = self._pem(40)
        at = 18000
        before = "".join(lines[:at] + key + lines[at:])
        old = lines[at]
        new = _x_to_y(old)
        f.write_text(before)
        snapshot = _snapshot_write_target(
            {"command": "strReplace", "path": str(f), "oldStr": old, "newStr": new}
        )
        slot = _make_slot_with_assistant_message()
        slot._file_changes = []
        _record_turn_snapshot(slot, snapshot)
        f.write_text(before.replace(old, new, 1))
        _flush_with_reads(slot)
        (entry,) = slot.messages[-1]["meta"]["file_changes"]
        assert not entry.get("truncated")
        assert _changed_lines(entry["before"], entry["after"]) == (1, 1)
        stored = entry["before"] + entry["after"]
        # The window's 20 context lines above the edit fall inside the key body.
        assert not any(line.strip() in stored for line in key[1:-1])
        assert key[-1].strip() not in stored

    def test_flush_redaction_leaves_a_redacted_window_unchanged(self):
        from kiro_crew.security import redact as redact_text

        text = "".join(self._pem(5)) + "token = AKIAIOSFODNN7EXAMPLE\n"
        once = redact_text(text)
        assert redact_text(once) == once


# ── bounded line diff, failed writes, one-file protection ──────────────────


def _repeated_rows(lines: int = 40_000, period: int = 200) -> list[str]:
    """About 1.3 MB of rows that each repeat ``lines / period`` times.

    Logs, CSVs and fixture data look like this: no row is rare, and none is
    common enough (1%) for difflib's autojunk to skip it, so a whole-file line
    diff is superlinear on it.
    """
    return [f"row {i % period:05d},alpha,beta,gamma,delta\n" for i in range(lines)]


class _DiffWork:
    """Counts the line visits ``SequenceMatcher.find_longest_match`` is asked for.

    Deterministic stand-in for time: each call walks every line of its ``a``
    range against each place that line occurs in ``b``. Past ``ceiling`` it
    raises, so an unbounded diff fails in milliseconds instead of hanging.
    """

    def __init__(self, monkeypatch, ceiling: int = 50_000_000) -> None:
        import difflib

        self.visits = 0
        real = difflib.SequenceMatcher.find_longest_match
        work = self

        def counted(matcher, alo=0, ahi=None, blo=0, bhi=None):
            ahi = len(matcher.a) if ahi is None else ahi
            occurs = matcher.b2j
            work.visits += sum(len(occurs.get(line, ())) for line in matcher.a[alo:ahi])
            if work.visits > ceiling:
                raise AssertionError(f"line diff passed {ceiling} visits")
            return real(matcher, alo, ahi, blo, bhi)

        monkeypatch.setattr(difflib.SequenceMatcher, "find_longest_match", counted)


class TestExcerptDiffWork:
    """``_excerpt_pair`` diffs only what differs, and gives up past its budget."""

    @pytest.mark.parametrize(
        "edit",
        [
            pytest.param(lambda rows: rows.__setitem__(30_000, "edited\n"), id="one-line"),
            pytest.param(
                lambda rows: rows.__setitem__(slice(25_000, 25_000), ["x\n"] * 3), id="insert"
            ),
            pytest.param(lambda rows: rows.__delitem__(slice(25_000, 25_003)), id="delete"),
            pytest.param(lambda rows: rows.append("tail\n"), id="append-at-end"),
        ],
    )
    def test_a_small_edit_in_repeated_rows_diffs_only_the_changed_lines(self, monkeypatch, edit):
        from kiro_crew.dashboard.chat_runner import _excerpt_pair

        rows = _repeated_rows()
        before = "".join(rows)
        edit(rows)
        work = _DiffWork(monkeypatch)
        pair = _excerpt_pair(before, "".join(rows))
        assert pair is not None and pair[0] != pair[1]
        # Untrimmed, the first match alone visits ~40,000 lines x 200 places.
        assert work.visits < 10_000
        assert max(len(pair[0]), len(pair[1])) <= _MAX_SNAPSHOT

    def test_many_spread_edits_stop_at_the_budget(self, monkeypatch):
        import kiro_crew.dashboard.chat_runner as cr

        rows = _repeated_rows()
        before = "".join(rows)
        for i in range(0, len(rows), 50):
            rows[i] = f"changed {i}\n"
        work = _DiffWork(monkeypatch)
        assert cr._excerpt_pair(before, "".join(rows)) is None
        # The charge is taken before each call's walk, so the walk never runs
        # past the budget: what was visited is at most the budget itself.
        assert work.visits <= cr._EXCERPT_DIFF_BUDGET

    def test_two_far_edits_in_distinct_lines_still_window(self, monkeypatch):
        from kiro_crew.dashboard.chat_runner import _excerpt_pair

        lines = _large_source().splitlines(keepends=True)
        before = "".join(lines)
        lines[1_000], lines[19_000] = "edit A\n", "edit B\n"
        work = _DiffWork(monkeypatch)
        pair = _excerpt_pair(before, "".join(lines))
        assert pair is not None
        assert _changed_lines(*pair) == (2, 2)
        assert work.visits < 100_000

    def test_a_length_gap_past_the_cap_skips_the_diff(self, monkeypatch):
        from kiro_crew.dashboard.chat_runner import _excerpt_pair

        work = _DiffWork(monkeypatch, ceiling=0)
        assert _excerpt_pair("a\n", "a\n" + "b" * (_MAX_SNAPSHOT + 1)) is None
        assert work.visits == 0

    def test_over_budget_edit_keeps_the_top_cut_and_stays_protected(
        self, short_tmp_dir: Path, monkeypatch
    ):
        import kiro_crew.dashboard.chat_runner as cr

        monkeypatch.setattr(cr, "_EXCERPT_DIFF_BUDGET", 0)
        f = short_tmp_dir / "big.py"
        text = _large_source()
        f.write_text(text)
        old, new, edited = TestLargeFileEditPastTheCut._edit(text)
        slot = _make_slot_with_assistant_message()
        slot._file_changes = []
        _record_turn_snapshot(
            slot,
            _snapshot_write_target(
                {"command": "strReplace", "path": str(f), "oldStr": old, "newStr": new}
            ),
        )
        f.write_text(edited)
        _flush_with_reads(slot)
        meta = slot.messages[-1]["meta"]
        (entry,) = meta["file_changes"]
        assert entry["truncated"] and not entry.get("content_omitted")
        assert "file_changes_omitted_files" not in meta


class TestOneFileTurnKeepsItsEntry:
    """A truncated entry may differ past its cut, so it can be the protected one."""

    def test_equal_top_cuts_on_a_truncated_entry_are_protected(self) -> None:
        cut = "x" * _MAX_SNAPSHOT
        entries = [{"path": "/big.log", "before": cut, "after": cut, "truncated": True}]
        kept, demoted, dropped = _apply_turn_snapshot_budget(entries)
        assert (demoted, dropped) == (0, 0)
        assert kept[0]["before"] == cut and not kept[0].get("content_omitted")

    def test_inline_flush_of_a_large_edit_is_protected(self, short_tmp_dir: Path):
        # The cancel and error exits flush without off-loop reads, so no window.
        f = short_tmp_dir / "big.py"
        text = _large_source()
        f.write_text(text)
        old, new, edited = TestLargeFileEditPastTheCut._edit(text)
        slot = _make_slot_with_assistant_message()
        slot._file_changes = []
        _record_turn_snapshot(
            slot,
            _snapshot_write_target(
                {"command": "strReplace", "path": str(f), "oldStr": old, "newStr": new}
            ),
        )
        f.write_text(edited)
        _flush_file_changes(slot)
        meta = slot.messages[-1]["meta"]
        (entry,) = meta["file_changes"]
        assert entry["truncated"] and not entry.get("content_omitted")
        assert "file_changes_omitted_files" not in meta

    def test_edit_in_a_file_past_the_whole_read_bound_is_protected(
        self, short_tmp_dir: Path, monkeypatch
    ):
        import kiro_crew.dashboard.chat_runner as cr

        f = short_tmp_dir / "big.py"
        text = _large_source()
        monkeypatch.setattr(cr, "_MAX_RECONSTRUCT_BYTES", len(text) // 2)
        f.write_text(text)
        slot = _make_slot_with_assistant_message()
        slot._file_changes = []
        snapshot = _snapshot_write_target({"command": "insert", "path": str(f)})
        assert "full_content" not in snapshot
        _record_turn_snapshot(slot, snapshot)
        f.write_text(text + "appended\n")
        _flush_with_reads(slot)
        meta = slot.messages[-1]["meta"]
        (entry,) = meta["file_changes"]
        assert entry["truncated"] and not entry.get("content_omitted")
        assert "file_changes_omitted_files" not in meta


def _str_replace_call(call_id: str, path: Path, old: str, new: str) -> AcpEvent:
    return AcpEvent(
        kind=EVENT_TOOL_CALL,
        tool_call_id=call_id,
        tool_name="fs_write",
        tool_kind="edit",
        title="Write",
        raw_tool_params={"command": "strReplace", "path": str(path), "oldStr": old, "newStr": new},
        diff_old_text=old,
        diff_path=str(path),
    )


def _result(call_id: str, status: str) -> AcpEvent:
    return AcpEvent(
        kind=EVENT_TOOL_RESULT,
        tool_call_id=call_id,
        tool_output=status,
        tool_status=status,
        tool_final=status == "completed",
    )


def _assert_unverified(entry: dict) -> None:
    """A failed write whose earlier file is unknown: its path, and no diff claimed."""
    assert (entry["before"], entry["after"]) == ("", "")
    assert entry["truncated"] and entry["content_omitted"]
    assert entry["omitted_reason"] == "unverified_write"
    assert "snapshot_limit_chars" not in entry


# Credential-shaped call ids, built from parts so the source carries no literal.
_KEY_ID_A = "AKIA" + "IOSFODNN7EXAMPLE"
_KEY_ID_B = "AKIA" + "I44QH8DHBEXAMPLE"


class TestFailedWriteRecordsNoChange:
    """A write that did not change its file never shows an edit, and a write
    that may have changed it is never hidden."""

    # An earlier turn already applied this edit: the disk holds newStr once and
    # no oldStr, so the snapshot is the reverse substitution, a whole before.
    DISK = "head\nvalue = 2\ntail\n"

    @pytest.mark.asyncio
    async def test_a_refused_reissue_shows_no_invented_hunk(self, short_tmp_dir):
        # The backend's `refused` is its word alone: no pre-call snapshot
        # proves the file was untouched, so the path stays, with no hunk.
        f = short_tmp_dir / "cfg"
        f.write_text(self.DISK)
        record = await _snapshot_turn(
            _str_replace_call("call-1", f, "value = 1", "value = 2"),
            _result("call-1", "refused"),
            *_LANDS,
        )
        (entry,) = _persisted_meta(record, "done")["file_changes"]
        _assert_unverified(entry)

    @pytest.mark.asyncio
    async def test_a_failed_reissue_shows_no_invented_hunk(self, short_tmp_dir):
        # The disk already holds the edit. Whether this call wrote it cannot be
        # told, so neither the reverse-substitution hunk nor "no change" is
        # shown: only the path, as an unverified write.
        f = short_tmp_dir / "cfg"
        f.write_text(self.DISK)
        record = await _snapshot_turn(
            _str_replace_call("call-1", f, "value = 1", "value = 2"),
            _result("call-1", "failed"),
            *_LANDS,
        )
        (entry,) = _persisted_meta(record, "done")["file_changes"]
        _assert_unverified(entry)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["failed", "refused"])
    @pytest.mark.parametrize("initial_input", [True, False], ids=["raw-input", "no-raw-input"])
    async def test_a_terminal_write_refinement_claims_no_diff(
        self, short_tmp_dir, status, initial_input
    ):
        f = short_tmp_dir / "cfg"
        f.write_text(self.DISK)
        call = _str_replace_call("call-1", f, "value = 1", "value = 2")
        if not initial_input:
            call = AcpEvent(
                kind=EVENT_TOOL_CALL,
                tool_call_id="call-1",
                tool_name="fs_write",
                tool_kind="edit",
                title="Write",
            )
        refinement = _str_replace_call("call-1", f, "value = 1", "value = 2")
        refinement.kind = EVENT_TOOL_CALL_UPDATE
        # One terminal frame can emit its result before its rawInput refinement.
        record = await _snapshot_turn(call, _result("call-1", status), refinement, *_LANDS)

        (entry,) = _persisted_meta(record, "done")["file_changes"]
        assert entry["path"] == str(f)
        _assert_unverified(entry)
        assert f.read_text() == self.DISK

    @pytest.mark.asyncio
    async def test_a_torn_write_read_as_pre_write_claims_no_diff(self, short_tmp_dir):
        # The write landed in part before the dashboard read the file: the torn
        # prefix holds oldStr once and no newStr, so it reads as pre-write, and
        # the call then fails with the file left that way. A disk read is no
        # pre-call snapshot, so no diff is claimed and the path stays.
        f = short_tmp_dir / "cfg"
        f.write_text("head\nold\nlong")
        record = await _snapshot_turn(
            _str_replace_call("call-1", f, "old\n", "old\nlong added text\n"),
            _result("call-1", "failed"),
            *_LANDS,
        )
        (entry,) = _persisted_meta(record, "done")["file_changes"]
        _assert_unverified(entry)

    @pytest.mark.asyncio
    async def test_a_failed_strreplace_after_a_disk_change_claims_no_diff(self, short_tmp_dir):
        # Read pre-write, then the file changed before the call reported failed.
        f = short_tmp_dir / "cfg"
        f.write_text(self.DISK)
        record = await _snapshot_turn(
            _str_replace_call("call-1", f, "value = 2", "value = 7"),
            Do(lambda ctx: f.write_text("head\nval")),
            _result("call-1", "failed"),
            *_LANDS,
        )
        (entry,) = _persisted_meta(record, "done")["file_changes"]
        _assert_unverified(entry)

    @pytest.mark.asyncio
    async def test_an_unverified_write_never_takes_the_protected_slot(self, short_tmp_dir):
        # A real change too large to window, then a newer failed write: the real
        # change stays whole and the failed write is path only.
        big, cfg = short_tmp_dir / "big.py", short_tmp_dir / "cfg"
        text = _large_source()
        big.write_text(text)
        cfg.write_text(self.DISK)
        record = await _snapshot_turn(
            _str_replace_call("call-1", big, "x" * 40, "z" * 40),
            Do(lambda ctx: big.write_text(text.replace("x" * 40, "z" * 40))),
            _result("call-1", "completed"),
            _str_replace_call("call-2", cfg, "value = 1", "value = 2"),
            _result("call-2", "failed"),
            *_LANDS,
        )
        changes = {Path(c["path"]).name: c for c in _persisted_meta(record, "done")["file_changes"]}
        assert not changes["big.py"].get("content_omitted")
        _assert_unverified(changes["cfg"])

    @pytest.mark.asyncio
    @pytest.mark.parametrize("changed", [False, True])
    async def test_a_failed_create_is_judged_by_its_content_block(self, short_tmp_dir, changed):
        f = short_tmp_dir / "new.py"
        f.write_text("old\n")
        events: list[AcpEvent | Do] = [_write_call(1, f, "old\n")[0]]
        if changed:
            events.append(Do(lambda ctx: f.write_text("half")))
        record = await _snapshot_turn(*events, _result("call-1", "failed"), *_LANDS)
        meta = _persisted_meta(record, "done")
        if not changed:
            assert "file_changes" not in meta
            return
        (entry,) = meta["file_changes"]
        assert (entry["before"], entry["after"]) == ("old\n", "half")

    @pytest.mark.asyncio
    async def test_the_same_edit_completing_still_shows_its_diff(self, short_tmp_dir):
        f = short_tmp_dir / "cfg"
        f.write_text(self.DISK)
        record = await _snapshot_turn(
            _str_replace_call("call-1", f, "value = 1", "value = 2"),
            _result("call-1", "completed"),
            *_LANDS,
        )
        (entry,) = _persisted_meta(record, "done")["file_changes"]
        assert (entry["before"], entry["after"]) == ("head\nvalue = 1\ntail\n", self.DISK)

    @pytest.mark.asyncio
    async def test_a_retry_after_a_proven_no_change_failure_keeps_its_own_before(
        self, short_tmp_dir
    ):
        # The failed create left the file as its content block says, so its
        # entry is gone before the retry records, and the retry's own (true)
        # before is the one kept.
        f = short_tmp_dir / "cfg"
        f.write_text("head\nvalue = 3\ntail\n")
        record = await _snapshot_turn(
            _write_call(1, f, "head\nvalue = 3\ntail\n")[0],
            _result("call-1", "failed"),
            _str_replace_call("call-2", f, "value = 5", "value = 3"),
            _result("call-2", "completed"),
            *_LANDS,
        )
        (entry,) = _persisted_meta(record, "done")["file_changes"]
        assert entry["before"] == "head\nvalue = 5\ntail\n"

    @pytest.mark.asyncio
    async def test_a_retry_after_an_unproven_failure_shows_its_own_diff(self, short_tmp_dir):
        # The failed call left the path unverified; the retry's own snapshot
        # replaces it, so its real diff shows and counts, not the notice.
        f = short_tmp_dir / "cfg"
        f.write_text(self.DISK)
        record = await _snapshot_turn(
            _str_replace_call("call-1", f, "value = 1", "value = 2"),
            _result("call-1", "failed"),
            _str_replace_call("call-2", f, "value = 2", "value = 3"),
            Do(lambda ctx: f.write_text("head\nvalue = 3\ntail\n")),
            _result("call-2", "completed"),
            *_LANDS,
        )
        (entry,) = _persisted_meta(record, "done")["file_changes"]
        assert "omitted_reason" not in entry
        assert (entry["before"], entry["after"]) == (self.DISK, "head\nvalue = 3\ntail\n")

    @pytest.mark.asyncio
    async def test_a_failure_on_a_path_another_call_wrote_keeps_the_entry(self, short_tmp_dir):
        f = short_tmp_dir / "cfg"
        f.write_text("head\nvalue = 2\ntail\n")
        record = await _snapshot_turn(
            _str_replace_call("call-1", f, "value = 1", "value = 2"),
            _result("call-1", "completed"),
            _str_replace_call("call-2", f, "value = 2", "value = 9"),
            _result("call-2", "refused"),
            *_LANDS,
        )
        (entry,) = _persisted_meta(record, "done")["file_changes"]
        assert entry["before"] == "head\nvalue = 1\ntail\n"

    @pytest.mark.asyncio
    async def test_a_refusal_under_a_collapsed_id_keeps_another_calls_change(self, short_tmp_dir):
        # Both ids are credential-shaped, so redaction turns them into one
        # constant and one identity key. The refused call cannot be told from
        # the successful one, so nothing is dropped.
        good, bad = short_tmp_dir / "good", short_tmp_dir / "bad"
        good.write_text(self.DISK)
        bad.write_text("x\n")
        record = await _snapshot_turn(
            _str_replace_call(_KEY_ID_A, good, "value = 1", "value = 2"),
            _result(_KEY_ID_A, "completed"),
            _str_replace_call(_KEY_ID_B, bad, "x", "z"),
            _result(_KEY_ID_B, "refused"),
            *_LANDS,
        )
        changes = {Path(c["path"]).name: c for c in _persisted_meta(record, "done")["file_changes"]}
        # The successful call keeps its real diff, not a path-only entry.
        assert "omitted_reason" not in changes["good"]
        assert changes["good"]["before"] == "head\nvalue = 1\ntail\n"

    @pytest.mark.asyncio
    async def test_a_collapsed_refinement_keeps_another_calls_diff(self, short_tmp_dir):
        bad, good = short_tmp_dir / "bad", short_tmp_dir / "good"
        bad.write_text(self.DISK)
        good.write_text(self.DISK)
        refinement = _str_replace_call(_KEY_ID_B, good, "value = 2", "value = 3")
        refinement.kind = EVENT_TOOL_CALL_UPDATE
        record = await _snapshot_turn(
            _str_replace_call(_KEY_ID_A, bad, "value = 1", "value = 2"),
            _result(_KEY_ID_A, "failed"),
            _str_replace_call(_KEY_ID_B, good, "value = 2", "value = 3"),
            Do(lambda ctx: good.write_text("head\nvalue = 3\ntail\n")),
            refinement,
            _result(_KEY_ID_B, "completed"),
            *_LANDS,
        )
        changes = {Path(c["path"]).name: c for c in _persisted_meta(record, "done")["file_changes"]}
        _assert_unverified(changes["bad"])
        assert "omitted_reason" not in changes["good"]
        assert (changes["good"]["before"], changes["good"]["after"]) == (
            self.DISK,
            "head\nvalue = 3\ntail\n",
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "cancel_point",
        [
            "steer",
            "settlement",
            "app-render",
            "post-tool-hook",
            "compaction-notice",
            "stream",
            "after-reads",
        ],
    )
    async def test_cancelled_failed_write_keeps_unproven_paths_only(
        self, short_tmp_dir, monkeypatch, cancel_point
    ):
        import asyncio

        import turn_harness

        from kiro_crew.dashboard import chat_runner

        reached = []

        def cancel(*args, **kwargs):
            reached.append(cancel_point)
            if cancel_point == "settlement":
                (entries,) = args
                assert len(entries) == 1 and entries[0].get("pre_call_digest")
            raise asyncio.CancelledError

        async def cancel_async(*args, **kwargs):
            cancel(*args, **kwargs)

        if cancel_point == "steer":
            monkeypatch.setattr(
                chat_runner.RepeatLoopTracker, "note_result", lambda *a, **k: "repeat"
            )
            monkeypatch.setattr(chat_runner, "_steer_repeat_loop_notice", cancel_async)
        elif cancel_point == "settlement":
            monkeypatch.setattr(chat_runner, "_settle_failed_write", cancel)
        elif cancel_point == "app-render":
            monkeypatch.setattr(chat_runner.mcp_apps_render, "handle_tool_result", cancel_async)
        elif cancel_point == "post-tool-hook":

            async def fire(self, event, *args, **kwargs):
                if event == chat_runner.HOOK_EVENT_POST_TOOL_USE:
                    cancel()
                return []

            monkeypatch.setattr(turn_harness._NoHooks, "fire", fire)
        elif cancel_point == "compaction-notice":
            monkeypatch.setattr(chat_runner, "_notice_compaction_after_turn", cancel_async)
        elif cancel_point == "after-reads":
            monkeypatch.setattr(chat_runner, "_turn_after_reads", cancel)

        f, other = short_tmp_dir / "cfg", short_tmp_dir / "other"
        f.write_text(self.DISK)
        other.write_text("old\n")
        refinement = _write_call(1, other, "old\n")[0]
        refinement.kind = EVENT_TOOL_CALL_UPDATE
        ending = (
            [Do(lambda ctx: reached.append(cancel_point)), Raise(asyncio.CancelledError())]
            if cancel_point == "stream"
            else _LANDS
        )
        record = await _snapshot_turn(
            _str_replace_call("call-1", f, "value = 1", "value = 2"),
            refinement,
            _result("call-1", "failed"),
            *ending,
        )
        assert reached == [cancel_point]
        entries = [
            c
            for row in record.rows("assistant")
            for c in row.get("meta", {}).get("file_changes", [])
        ]
        changes = {Path(c["path"]).name: c for c in entries}
        assert len(entries) == len(changes)
        assert set(changes) == (
            {"cfg", "other"} if cancel_point in ("steer", "settlement") else {"cfg"}
        )
        for entry in entries:
            _assert_unverified(entry)
        assert f.read_text() == self.DISK
        assert other.read_text() == "old\n"

    def test_an_unknown_writer_selects_nothing(self) -> None:
        from kiro_crew.dashboard.chat_runner import _failed_write_entries

        slot = MagicMock()
        slot._file_changes = [{"path": "/a", "content": "x"}]
        assert _failed_write_entries(slot, "") == []

    def test_a_failed_write_counts_no_lines(self) -> None:
        from kiro_crew.dashboard.chat_runner import _line_change_input, _Snapshot

        fc = {"path": "/a", "content": "x\n", "unverified_write": True}
        assert _line_change_input(fc, _Snapshot("y\n", False)) is None
