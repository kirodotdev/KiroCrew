"""File-change snapshots: the per-turn budget and what a persisted snapshot omits.

``_apply_turn_snapshot_budget`` keeps a turn's entries inside the aggregate budget
and row count, demoting the oldest and protecting the newest; a path is bounded at
admission, the turn accumulator keeps the first before of each path, and a side
stays inside its bound after redaction. Real turns through ``run_turn`` then show
which files a persisted snapshot omits and how they are counted. The snapshot
primitives and ``_flush_file_changes`` are in ``test_file_change_snapshots.py``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from file_change_snapshots_helpers import short_tmp_dir_fixture  # noqa: F401
from file_change_snapshots_helpers import (
    _LANDS,
    _make_slot_with_assistant_message,
    _persisted_meta,
    _snapshot_turn,
    _write_call,
)
from turn_harness import Raise

from kiro_crew.acp.types import (
    EVENT_TOOL_CALL,
    EVENT_TOOL_CALL_UPDATE,
    STOP_REASON_END_TURN,
    AcpEvent,
)
from kiro_crew.dashboard.chat_runner import (
    _MAX_SNAPSHOT,
    _MAX_SNAPSHOT_PATH_CHARS,
    _MAX_TURN_SNAPSHOT_CHARS,
    _MAX_TURN_SNAPSHOT_ENTRIES,
    _apply_turn_snapshot_budget,
    _flush_file_changes,
    _record_turn_snapshot,
    _snapshot_write_target,
    _truncate_snapshot,
    _turn_line_changes,
)
from kiro_crew.security import redact

# ── per-turn snapshot budget ────────────────────────────────────────────────


def _entry(path: str, chars: int, *, last_write: int | None = None) -> dict[str, object]:
    """One entry whose before+after spans ``chars`` and whose two sides differ."""
    body = chars
    half = body // 2
    entry: dict[str, object] = {
        "path": path,
        "before": "b" * half,
        "after": "a" * (body - half),
    }
    if last_write is not None:
        entry["_last_write"] = last_write
    return entry


def _noop_entry(path: str, chars: int, *, last_write: int | None = None) -> dict[str, object]:
    """An entry whose write changed nothing -- the format-on-save shape."""
    same = "s" * (chars // 2)
    entry: dict[str, object] = {"path": path, "before": same, "after": same}
    if last_write is not None:
        entry["_last_write"] = last_write
    return entry


# One snapshot side at its largest: the per-file cap plus its truncation marker.
_SIDE_AT_THE_CAP = _truncate_snapshot("x" * (_MAX_SNAPSHOT + 1)).content
# One entry at its worst case -- both sides at the cap -- which is what a file
# over the per-file cap on both sides stores. It exceeds the turn budget by its
# two markers, so as a NON-protected entry it is always demoted.
_MAXED_ENTRY_CHARS = 2 * len(_SIDE_AT_THE_CAP)


class TestTurnSnapshotBudget:
    @pytest.mark.parametrize("with_protected", [False, True])
    def test_path_only_rows_share_the_aggregate_budget(self, with_protected) -> None:
        paths = [
            f"/{i:03d}/" + "p" * (_MAX_SNAPSHOT_PATH_CHARS - 5)
            for i in range(_MAX_TURN_SNAPSHOT_ENTRIES)
        ]
        entries = [_entry(path, 0) for path in paths]
        if with_protected:
            entries.append(_entry("protected.py", _MAXED_ENTRY_CHARS))
        ordered, demoted, dropped = _apply_turn_snapshot_budget(entries)
        charged = [e for e in ordered if e["path"] != "protected.py"]
        fits = _MAX_TURN_SNAPSHOT_CHARS // _MAX_SNAPSHOT_PATH_CHARS
        assert [e["path"] for e in charged] == paths[-fits:]
        assert sum(len(e["path"]) for e in charged) <= _MAX_TURN_SNAPSHOT_CHARS
        assert (demoted, dropped) == (0, len(paths) - fits)
        if with_protected:
            protected = next(e for e in ordered if e["path"] == "protected.py")
            assert len(protected["before"]) + len(protected["after"]) == _MAXED_ENTRY_CHARS

    def test_demoted_paths_consume_the_aggregate_budget(self) -> None:
        paths = [
            f"/{i:03d}/" + "p" * (_MAX_SNAPSHOT_PATH_CHARS - 5)
            for i in range(_MAX_TURN_SNAPSHOT_ENTRIES)
        ]
        entries = [_entry(path, _MAXED_ENTRY_CHARS) for path in paths]
        entries.append(_entry("protected.py", 2))
        ordered, demoted, dropped = _apply_turn_snapshot_budget(entries)
        charged = [e for e in ordered if e.get("content_omitted")]
        fits = _MAX_TURN_SNAPSHOT_CHARS // _MAX_SNAPSHOT_PATH_CHARS
        assert [e["path"] for e in charged] == paths[-fits:]
        assert (demoted, dropped) == (fits, len(paths) - fits)
        assert all(e["before"] == e["after"] == "" for e in charged)
        assert sum(len(e["path"]) for e in charged) <= _MAX_TURN_SNAPSHOT_CHARS

    def test_path_and_content_exactly_fill_the_budget(self) -> None:
        entries = [
            _entry("old.py", 0),
            _entry("fits.py", _MAX_TURN_SNAPSHOT_CHARS - len("fits.py")),
            _entry("protected.py", 2),
        ]
        ordered, demoted, dropped = _apply_turn_snapshot_budget(entries)
        assert [e["path"] for e in ordered] == ["fits.py", "protected.py"]
        assert (demoted, dropped) == (0, 1)
        assert sum(len(ordered[0][key]) for key in ("path", "before", "after")) == (
            _MAX_TURN_SNAPSHOT_CHARS
        )

    def test_under_budget_keeps_every_entry_untouched(self) -> None:
        entries = [_entry("a.py", 100), _entry("b.py", 100)]
        ordered, demoted, dropped = _apply_turn_snapshot_budget(entries)
        assert demoted == 0
        assert [e["path"] for e in ordered] == ["a.py", "b.py"]
        assert all(e["before"] and e["after"] for e in ordered)
        assert all("content_omitted" not in e for e in ordered)

    def test_oldest_entries_lose_content_and_newest_keeps_it(self) -> None:
        # Three files at the worst case: the newest is protected and each of the
        # others alone exceeds the budget, so only the newest keeps its diff.
        entries = [
            _entry("oldest.py", _MAXED_ENTRY_CHARS),
            _entry("middle.py", _MAXED_ENTRY_CHARS),
            _entry("newest.py", _MAXED_ENTRY_CHARS),
        ]
        ordered, demoted, dropped = _apply_turn_snapshot_budget(entries)
        assert demoted == 2
        # The kept diff sorts first, so it lands inside the card's visible rows.
        assert [e["path"] for e in ordered] == ["newest.py", "oldest.py", "middle.py"]
        assert ordered[0]["before"] and ordered[0]["after"]
        for entry in ordered[1:]:
            assert entry["before"] == ""
            assert entry["after"] == ""
            assert entry["truncated"] is True
            assert entry["content_omitted"] is True
            assert entry["turn_budget_chars"] == _MAX_TURN_SNAPSHOT_CHARS

    def test_budget_follows_the_last_write_not_the_first(self) -> None:
        # The turn edits early.py, then late.py, then early.py again. Dedupe
        # order puts early.py first, but it is the file the turn touched last.
        entries = [
            _entry("early.py", _MAXED_ENTRY_CHARS, last_write=2),
            _entry("late.py", _MAXED_ENTRY_CHARS, last_write=1),
        ]
        ordered, demoted, dropped = _apply_turn_snapshot_budget(entries)
        assert demoted == 1
        assert ordered[0]["path"] == "early.py"
        assert ordered[0]["before"] and ordered[0]["after"]
        assert ordered[1]["path"] == "late.py"
        assert ordered[1]["content_omitted"] is True

    def test_an_idempotent_final_write_does_not_spend_the_protected_slot(self) -> None:
        # A format-on-save that changed nothing is the most recent entry; the
        # real change must keep its diff rather than fund a diff of nothing.
        entries = [
            _entry("real.py", _MAXED_ENTRY_CHARS, last_write=0),
            _noop_entry("formatted.py", _MAXED_ENTRY_CHARS, last_write=1),
        ]
        ordered, demoted, dropped = _apply_turn_snapshot_budget(entries)
        assert demoted == 1
        kept = next(e for e in ordered if not e.get("content_omitted"))
        assert kept["path"] == "real.py"
        assert kept["before"] and kept["after"]

    def test_newest_entry_survives_even_when_it_alone_exceeds_the_budget(self) -> None:
        # The protected entry is outside the budget, so its size neither drops
        # it nor charges the small older file, which keeps its diff too.
        entries = [_entry("old.py", 10), _entry("huge.py", _MAX_TURN_SNAPSHOT_CHARS + 1)]
        ordered, demoted, dropped = _apply_turn_snapshot_budget(entries)
        assert (demoted, dropped) == (0, 0)
        assert [e["path"] for e in ordered] == ["old.py", "huge.py"]
        assert all(e["before"] and e["after"] for e in ordered)

    def test_total_kept_content_stays_within_the_budget_plus_one_entry(self) -> None:
        # The protected entry is outside the budget, so the stored total is
        # bounded by the budget plus one entry at its worst case, never more.
        entries = [_entry(f"f{i}.py", 150_000) for i in range(12)]
        ordered, _demoted, _dropped = _apply_turn_snapshot_budget(entries)
        kept = sum(len(e["before"]) + len(e["after"]) for e in ordered)
        assert kept <= _MAX_TURN_SNAPSHOT_CHARS + _MAXED_ENTRY_CHARS
        assert kept > _MAX_TURN_SNAPSHOT_CHARS

    def test_a_noop_entry_cannot_push_the_kept_total_past_the_bound(self) -> None:
        # The no-op is not protected, so it is charged; the real diff is kept
        # outside the budget. Neither can exceed the budget-plus-one-entry bound.
        entries = [
            _entry("real.py", _MAXED_ENTRY_CHARS, last_write=0),
            _noop_entry("formatted.py", _MAXED_ENTRY_CHARS, last_write=1),
        ]
        ordered, _demoted, _dropped = _apply_turn_snapshot_budget(entries)
        kept = sum(len(e["before"]) + len(e["after"]) for e in ordered)
        assert kept <= _MAX_TURN_SNAPSHOT_CHARS + _MAXED_ENTRY_CHARS

    def test_a_maxed_protected_entry_does_not_demote_a_small_second_file(self) -> None:
        # The turn edits a file at the per-file cap on both sides and then a
        # two-character file. The protected entry is not charged, so the small
        # diff fits the budget and both are kept.
        side = _SIDE_AT_THE_CAP
        entries = [
            {"path": "small.py", "before": "x", "after": "y", "_last_write": 0},
            {"path": "big.py", "before": "b" + side[1:], "after": side, "_last_write": 1},
        ]
        ordered, demoted, dropped = _apply_turn_snapshot_budget(entries)
        assert (demoted, dropped) == (0, 0)
        assert [e["path"] for e in ordered] == ["small.py", "big.py"]
        assert all(e["before"] and e["after"] for e in ordered)

    def test_the_budget_holds_one_more_large_entry_beside_the_protected_one(self) -> None:
        # Two maxed files where the older one is at the cap on one side only:
        # it fits the two-cap budget and keeps its diff beside the protected
        # entry, so a turn rewriting two large files shows both.
        side = _SIDE_AT_THE_CAP
        entries = [
            {"path": "older.py", "before": side, "after": "a" * (_MAX_SNAPSHOT - 100)},
            {"path": "newest.py", "before": side, "after": "z" + side[1:]},
        ]
        ordered, demoted, dropped = _apply_turn_snapshot_budget(entries)
        assert (demoted, dropped) == (0, 0)
        assert [e["path"] for e in ordered] == ["older.py", "newest.py"]

    def test_a_turn_of_only_noop_writes_keeps_what_fits_and_demotes_the_rest(self) -> None:
        # Nothing has a real diff, so nothing is protected; the budget alone
        # decides, and the no-change captions that fit are kept.
        entries = [
            _noop_entry("a.py", _MAX_TURN_SNAPSHOT_CHARS - len("a.pyb.py"), last_write=0),
            _noop_entry("b.py", _MAX_TURN_SNAPSHOT_CHARS - len("a.pyb.py"), last_write=1),
        ]
        ordered, demoted, dropped = _apply_turn_snapshot_budget(entries)
        assert demoted == 1
        assert ordered[0]["path"] == "b.py"
        assert ordered[0]["before"] == ordered[0]["after"] != ""

    def test_a_demoted_entry_drops_the_per_file_limit_field(self) -> None:
        # A file both individually truncated AND demoted reports one reason:
        # the turn budget, because its content is gone for that reason.
        entries = [
            {
                "path": "old.py",
                "before": "b" + _SIDE_AT_THE_CAP[1:],
                "after": _SIDE_AT_THE_CAP,
                "truncated": True,
                "snapshot_limit_chars": _MAX_SNAPSHOT,
            },
            _entry("newest.py", _MAXED_ENTRY_CHARS),
        ]
        ordered, demoted, dropped = _apply_turn_snapshot_budget(entries)
        assert demoted == 1
        dropped = next(e for e in ordered if e.get("content_omitted"))
        assert dropped["path"] == "old.py"
        assert "snapshot_limit_chars" not in dropped

    def test_the_internal_write_order_key_never_reaches_a_stored_entry(self) -> None:
        entries = [_entry("a.py", 10, last_write=0), _entry("b.py", 10, last_write=1)]
        ordered, _demoted, _dropped = _apply_turn_snapshot_budget(entries)
        assert all("_last_write" not in e for e in ordered)

    def test_the_row_count_is_bounded_even_when_every_row_is_path_only(self) -> None:
        # A path-only row is not free, so the content budget alone would leave
        # the number of retained rows open.
        entries = [
            _entry(f"/f{i}.py", _MAXED_ENTRY_CHARS, last_write=i)
            for i in range(_MAX_TURN_SNAPSHOT_ENTRIES + 25)
        ]
        ordered, demoted, dropped = _apply_turn_snapshot_budget(entries)
        assert len(ordered) == _MAX_TURN_SNAPSHOT_ENTRIES
        assert dropped == 25
        assert demoted == _MAX_TURN_SNAPSHOT_ENTRIES - 1

    def test_the_rows_dropped_for_the_count_are_the_oldest(self) -> None:
        entries = [
            _entry(f"/f{i}.py", 10, last_write=i) for i in range(_MAX_TURN_SNAPSHOT_ENTRIES + 3)
        ]
        ordered, _demoted, dropped = _apply_turn_snapshot_budget(entries)
        assert dropped == 3
        kept_paths = {e["path"] for e in ordered}
        assert {"/f0.py", "/f1.py", "/f2.py"}.isdisjoint(kept_paths)

    def test_a_turn_within_the_count_keeps_a_path_for_every_file(self) -> None:
        # The older entry alone exceeds the content budget; it must still keep
        # its path rather than vanish from the card.
        entries = [
            _entry("/old.py", _MAXED_ENTRY_CHARS, last_write=0),
            _entry("/protected.py", _MAXED_ENTRY_CHARS, last_write=1),
        ]
        ordered, demoted, dropped = _apply_turn_snapshot_budget(entries)
        assert (demoted, dropped) == (1, 0)
        assert [e["path"] for e in ordered] == ["/protected.py", "/old.py"]

    def test_flush_records_paths_for_the_files_it_demotes(self, short_tmp_dir: Path) -> None:
        paths = []
        for index in range(3):
            target = short_tmp_dir / f"f{index}.txt"
            target.write_text("a" * _MAX_TURN_SNAPSHOT_CHARS)
            paths.append(target)
        slot = _make_slot_with_assistant_message()
        slot._file_changes = [
            {"path": str(p), "content": "b" * _MAX_TURN_SNAPSHOT_CHARS} for p in paths
        ]
        _flush_file_changes(slot)
        changes = slot.messages[-1]["meta"]["file_changes"]
        # Every file keeps a path; the last-written one keeps its diff and leads.
        assert {c["path"] for c in changes} == {str(p) for p in paths}
        assert changes[0]["path"] == str(paths[-1])
        assert [bool(c.get("content_omitted")) for c in changes] == [False, True, True]
        assert all("_last_write" not in c for c in changes)

    def test_flush_charges_the_budget_to_the_files_written_last(self, short_tmp_dir: Path) -> None:
        early = short_tmp_dir / "early.txt"
        late = short_tmp_dir / "late.txt"
        for target in (early, late):
            target.write_text("a" * _MAX_TURN_SNAPSHOT_CHARS)
        slot = _make_slot_with_assistant_message()
        # early is written, then late, then early again.
        slot._file_changes = [
            {"path": str(early), "content": "b" * _MAX_TURN_SNAPSHOT_CHARS},
            {"path": str(late), "content": "b" * _MAX_TURN_SNAPSHOT_CHARS},
            {"path": str(early), "content": "ignored second before"},
        ]
        _flush_file_changes(slot)
        changes = slot.messages[-1]["meta"]["file_changes"]
        kept = next(c for c in changes if not c.get("content_omitted"))
        assert kept["path"] == str(early)
        # The first before is still the one stored, not the second write's.
        assert kept["before"].startswith("b")


class TestSnapshotPathBound:
    def test_the_bound_is_the_windows_extended_length_ceiling(self) -> None:
        # Windows native is a supported platform, and with the extended-length
        # prefix a path reaches 32,767 characters; a lower bound refuses a
        # snapshot for a file the OS can open.
        assert _MAX_SNAPSHOT_PATH_CHARS == 32_767

    def test_a_path_no_os_can_open_is_refused_at_admission(self) -> None:
        # The path is LLM-supplied; an entry keeps it even when its content is
        # dropped, so an unbounded one would ride onto the message regardless.
        long_path = "/tmp/" + "a" * (_MAX_SNAPSHOT_PATH_CHARS + 1) + ".txt"
        assert (
            _snapshot_write_target({"command": "create", "path": long_path}, diff_old_text="")
            is None
        )

    def test_a_windows_long_path_within_the_ceiling_is_admitted(self) -> None:
        # Longer than any Linux pathname, shorter than the Windows ceiling: the
        # length check lets it through to the path validator. The validator
        # itself decides on the path's content, not its length, so the test
        # confirms the length gate alone did not refuse it.
        long_path = "\\\\?\\C:\\" + "a" * 8_000 + "\\file.txt"
        assert 4_096 < len(long_path) <= _MAX_SNAPSHOT_PATH_CHARS
        with patch("kiro_crew.dashboard.chat_runner.validate_file_path") as validate:
            validate.return_value = None
            assert (
                _snapshot_write_target({"command": "create", "path": long_path}, diff_old_text="")
                is None
            )
            validate.assert_called_once_with(long_path)

    def test_a_path_at_the_bound_is_still_captured(self, tmp_path: Path) -> None:
        target = tmp_path / "small.txt"
        target.write_text("after")
        assert len(str(target)) <= _MAX_SNAPSHOT_PATH_CHARS
        captured = _snapshot_write_target(
            {"command": "create", "path": str(target)}, diff_old_text="before"
        )
        assert captured is not None
        assert captured["path"] == str(target)


class TestTurnSnapshotAccumulator:
    def test_repeated_writes_leave_room_for_a_new_path(self, short_tmp_dir: Path) -> None:
        slot = _make_slot_with_assistant_message()
        early = short_tmp_dir / "early.py"
        late = short_tmp_dir / "late.py"
        for index in range(_MAX_TURN_SNAPSHOT_ENTRIES * 2):
            before = "first\n" if index == 0 else "intermediate\n"
            _record_turn_snapshot(slot, {"path": str(early), "content": before})
        _record_turn_snapshot(slot, {"path": str(late), "content": ""})
        early.write_text("last\n")
        late.write_text("created\n")
        assert _turn_line_changes(slot._file_changes) == 3
        _flush_file_changes(slot)
        stored = {fc["path"]: fc for fc in slot.messages[-1]["meta"]["file_changes"]}
        assert set(stored) == {str(early), str(late)}
        assert stored[str(early)]["before"] == "first\n"
        assert stored[str(early)]["after"] == "last\n"

    @pytest.mark.parametrize("row_cap", [2, 3])
    def test_repeat_recency_drives_the_budget_at_and_below_the_cap(
        self, short_tmp_dir: Path, monkeypatch, row_cap: int
    ) -> None:
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._MAX_TURN_SNAPSHOT_ENTRIES", row_cap)
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._MAX_TURN_SNAPSHOT_CHARS", 0)
        slot = _make_slot_with_assistant_message()
        early = short_tmp_dir / "early.py"
        other = short_tmp_dir / "other.py"
        for target in (early, other):
            target.write_text("after\n")
            _record_turn_snapshot(slot, {"path": str(target), "content": "first\n"})
        _record_turn_snapshot(
            slot,
            {"path": str(early), "content": "intermediate\n", "truncated": True},
        )
        _flush_file_changes(slot)
        [stored] = slot.messages[-1]["meta"]["file_changes"]
        assert stored["path"] == str(early)
        assert stored["before"] == "first\n"
        assert "truncated" not in stored
        assert "_last_write" not in stored

    def test_record_holds_every_distinct_path_and_keeps_first_objects(self) -> None:
        slot = _make_slot_with_assistant_message()
        first = {"path": "/first.py", "content": "first", "truncated": False}
        _record_turn_snapshot(slot, first)
        for index in range(_MAX_TURN_SNAPSHOT_ENTRIES * 5):
            _record_turn_snapshot(slot, {"path": "/first.py", "content": "replacement"})
        assert slot._file_changes == [first]
        distinct = _MAX_TURN_SNAPSHOT_ENTRIES * 2
        for index in range(distinct):
            _record_turn_snapshot(slot, {"path": f"/f{index}.py", "content": "b"})
            _record_turn_snapshot(slot, {"path": "/first.py", "content": "replacement"})
        assert len(slot._file_changes) == distinct + 1
        assert len({fc["path"] for fc in slot._file_changes}) == distinct + 1
        assert slot._file_changes[-1] is first
        assert first == {"path": "/first.py", "content": "first", "truncated": False}

    def test_record_returns_nothing_for_the_caller_to_consult(self) -> None:
        slot = _make_slot_with_assistant_message()
        assert _record_turn_snapshot(slot, {"path": "/a.py", "content": ""}) is None
        assert _record_turn_snapshot(slot, {"path": "/a.py", "content": "x"}) is None

    def test_two_spellings_of_one_path_keep_the_first_before(self) -> None:
        """``/work/f`` then ``/work/./f`` then ``/work/f`` is one file. Keyed by
        the raw path these would be two entries and a revisit under the first
        spelling would move the ORIGINAL behind the alias, so the flush (first
        entry per canonical key) would persist the alias's before and lose the
        true original. Keyed by canonical path they are one entry whose first
        before-snapshot survives every revisit."""
        slot = _make_slot_with_assistant_message()
        canon = "/work/f"
        _record_turn_snapshot(slot, {"path": "/work/f", "canonical_path": canon, "content": "zero"})
        _record_turn_snapshot(
            slot, {"path": "/work/./f", "canonical_path": canon, "content": "one"}
        )
        _record_turn_snapshot(
            slot, {"path": "/work/f", "canonical_path": canon, "content": "three"}
        )
        assert len(slot._file_changes) == 1
        assert slot._file_changes[0]["content"] == "zero"


# ── the per-side bound after redaction ──────────────────────────────────────

# One redaction unit: a short credential that the ``?token=`` pass rewrites to a
# 22-character tag, so a side made of these grows about threefold on redaction.
_CREDENTIAL_UNIT = "x ?token=a "
_SIDE_BOUND = len(_SIDE_AT_THE_CAP)


def _credential_side(pad: str, offset: int) -> str:
    """A side of exactly ``_MAX_SNAPSHOT`` chars, dense with short credentials.

    ``offset`` pad chars lead the side, so two pads give two different sides,
    and it shifts where the post-redaction cut lands relative to the tags.
    """
    body = pad * offset + _CREDENTIAL_UNIT * (_MAX_SNAPSHOT // len(_CREDENTIAL_UNIT) + 1)
    return body[:_MAX_SNAPSHOT]


_SNAPSHOT_MARKER = _SIDE_AT_THE_CAP[_MAX_SNAPSHOT:]


class TestRedactedSideBound:
    # The post-redaction cut lands between two tags at offset 1 and inside a
    # tag at offset 15.
    @pytest.mark.parametrize("offset", [1, 15])
    def test_the_protected_entry_stays_within_the_per_side_bound(
        self, short_tmp_dir: Path, offset: int
    ) -> None:
        # The protected entry is the one the turn budget never charges, so the
        # per-side bound is all that holds its size once redaction expands it.
        target = short_tmp_dir / "creds.txt"
        target.write_text(_credential_side("a", offset))
        slot = _make_slot_with_assistant_message()
        raw = {"path": str(target), "content": _credential_side("b", offset), "truncated": False}
        slot._file_changes = [raw]
        _flush_file_changes(slot)
        [stored] = slot.messages[-1]["meta"]["file_changes"]
        sources = {"before": raw["content"], "after": target.read_text()}
        for side in ("before", "after"):
            assert len(stored[side]) <= _SIDE_BOUND, (side, len(stored[side]))
            # The bound is a plain cut of the redacted text at the per-side cap.
            assert stored[side] == redact(sources[side])[:_MAX_SNAPSHOT] + _SNAPSHOT_MARKER
        assert stored["truncated"] is True
        assert stored["snapshot_limit_chars"] == _MAX_SNAPSHOT
        # The flag lands on the stored copy only: the accumulator entry the
        # line count reads keeps its own flag.
        assert raw["truncated"] is False

    def test_a_side_within_the_bound_after_redaction_is_stored_unchanged(
        self, short_tmp_dir: Path
    ) -> None:
        target = short_tmp_dir / "few.txt"
        target.write_text("after " + _CREDENTIAL_UNIT)
        slot = _make_slot_with_assistant_message()
        slot._file_changes = [{"path": str(target), "content": "before " + _CREDENTIAL_UNIT}]
        _flush_file_changes(slot)
        [stored] = slot.messages[-1]["meta"]["file_changes"]
        assert stored["before"] == "before x ?token=[REDACTED: credential] "
        assert stored["after"] == "after x ?token=[REDACTED: credential] "
        assert "truncated" not in stored

    def test_a_side_truncated_before_redaction_keeps_its_one_marker(
        self, short_tmp_dir: Path
    ) -> None:
        target = short_tmp_dir / "big.txt"
        target.write_text("y" * (_MAX_SNAPSHOT + 10))
        slot = _make_slot_with_assistant_message()
        slot._file_changes = [{"path": str(target), "content": "b"}]
        _flush_file_changes(slot)
        [stored] = slot.messages[-1]["meta"]["file_changes"]
        assert stored["after"] == _truncate_snapshot("y" * (_MAX_SNAPSHOT + 10)).content

    def test_a_redacted_path_stays_within_the_path_bound(self) -> None:
        path = ("/q ?token=a" * (_MAX_SNAPSHOT_PATH_CHARS // 11 + 1))[:_MAX_SNAPSHOT_PATH_CHARS]
        slot = _make_slot_with_assistant_message()
        slot._file_changes = [{"path": path, "content": "before"}]
        _flush_file_changes(slot)
        [stored] = slot.messages[-1]["meta"]["file_changes"]
        assert len(stored["path"]) <= _MAX_SNAPSHOT_PATH_CHARS
        assert stored["path"].endswith("...")


# ── omitted files on the persisted snapshot ────────────────────────────────


def _write_update(index: int, path: Path, before: str) -> list[AcpEvent]:
    """A write whose arguments arrive only on a later TOOL_CALL_UPDATE."""
    return [
        AcpEvent(
            kind=EVENT_TOOL_CALL,
            tool_call_id=f"call-{index}",
            tool_name="fs_write",
            tool_kind="edit",
            title="Write",
        ),
        AcpEvent(
            kind=EVENT_TOOL_CALL_UPDATE,
            tool_call_id=f"call-{index}",
            raw_tool_params={"command": "create", "path": str(path)},
            diff_old_text=before,
            diff_path=str(path),
        ),
    ]


_WRITES = {"call": _write_call, "update": _write_update}


#: An exit that never lands: no reply, so the finally's flush is the only one
#: and its chips ride a content-less anchor.
_DIES = [Raise(RuntimeError("boom"))]


def _files(directory: Path, count: int, *, disk: str = "after\n") -> list[Path]:
    paths = [directory / f"f{index}.py" for index in range(count)]
    for path in paths:
        path.write_text(disk, encoding="utf-8")
    return paths


class TestOmittedSnapshotFiles:
    @pytest.mark.parametrize("multiple", [2, 10])
    def test_large_flush_visits_dropped_indices_at_most_once(self, monkeypatch, multiple):
        from kiro_crew.dashboard import chat_runner

        total = _MAX_TURN_SNAPSHOT_ENTRIES * multiple
        visited = 0

        def tracked_set(indices=()):
            def counted_indices():
                nonlocal visited
                for index in indices:
                    visited += 1
                    yield index

            return set(counted_indices())

        monkeypatch.setattr(chat_runner, "set", tracked_set, raising=False)
        monkeypatch.setattr(
            chat_runner, "_safe_read_snapshot", lambda path: _truncate_snapshot("after\n")
        )
        slot = _make_slot_with_assistant_message()
        slot._file_changes = [
            {"path": f"/f{index}.py", "content": "before\n"} for index in range(total)
        ]
        _flush_file_changes(slot)
        meta = slot.messages[-1]["meta"]
        dropped = total - _MAX_TURN_SNAPSHOT_ENTRIES
        assert [fc["path"] for fc in meta["file_changes"]] == [
            f"/f{index}.py" for index in range(dropped, total)
        ]
        assert all(
            fc["before"] == "before\n" and fc["after"] == "after\n" for fc in meta["file_changes"]
        )
        assert meta["file_changes_omitted_files"] == dropped
        assert slot._file_changes == []
        # Count index traversal work, not elapsed time or constructor calls.
        assert visited <= dropped

    @pytest.mark.asyncio
    @pytest.mark.parametrize("site", ["call", "update"])
    async def test_a_turn_past_the_row_cap_keeps_the_files_it_wrote_last(
        self, monkeypatch, short_tmp_dir, site
    ):
        # Through the real turn; a cap of 3 instead of 200 keeps the script short.
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._MAX_TURN_SNAPSHOT_ENTRIES", 3)
        paths = _files(short_tmp_dir, 5)
        writes = [
            event
            for index, path in enumerate(paths)
            for event in _WRITES[site](index, path, "before\n")
        ]
        record = await _snapshot_turn(*writes, *_LANDS)
        assert record.stop_reason == STOP_REASON_END_TURN
        # The landed turn runs the success flush, then the finally flush: the
        # persisted count is what both left.
        meta = _persisted_meta(record, "done")
        assert {fc["path"] for fc in meta["file_changes"]} == {str(path) for path in paths[2:]}
        assert meta["file_changes_omitted_files"] == 2
        assert type(meta["file_changes_omitted_files"]) is int

    @pytest.mark.asyncio
    async def test_repeating_a_path_adds_no_row_and_keeps_the_first_before(self, short_tmp_dir):
        [target] = _files(short_tmp_dir, 1, disk="last\n")
        record = await _snapshot_turn(
            *_write_call(0, target, "first\n"),
            AcpEvent(
                kind=EVENT_TOOL_CALL_UPDATE,
                tool_call_id="call-0",
                raw_tool_params={"command": "create", "path": str(target)},
                diff_old_text="second\n",
                diff_path=str(target),
            ),
            *_write_call(1, target, "third\n"),
            *_LANDS,
        )
        meta = _persisted_meta(record, "done")
        [stored] = meta["file_changes"]
        assert stored["before"] == "first\n"
        assert stored["after"] == "last\n"
        assert "file_changes_omitted_files" not in meta

    @pytest.mark.asyncio
    @pytest.mark.parametrize("site", ["call", "update"])
    async def test_a_rewritten_path_moves_to_the_end_and_keeps_its_first_before(
        self, short_tmp_dir, site
    ):
        # P, then Q, then P again through the SAME site: the chips follow the
        # order the files were last written, and P keeps the content it had
        # before the turn first touched it.
        p_path, q_path = short_tmp_dir / "p.py", short_tmp_dir / "q.py"
        p_path.write_text("P-after\n", encoding="utf-8")
        q_path.write_text("Q-after\n", encoding="utf-8")
        write = _WRITES[site]
        record = await _snapshot_turn(
            *write(0, p_path, "P1\n"),
            *write(1, q_path, "Q1\n"),
            *write(2, p_path, "P2\n"),
            *_LANDS,
        )
        meta = _persisted_meta(record, "done")
        assert [fc["path"] for fc in meta["file_changes"]] == [str(q_path), str(p_path)]
        assert meta["file_changes"][1]["before"] == "P1\n"

    @pytest.mark.asyncio
    async def test_row_cap_and_path_budget_drops_are_counted_as_files(
        self, monkeypatch, short_tmp_dir
    ):
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._MAX_TURN_SNAPSHOT_ENTRIES", 2)
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._MAX_TURN_SNAPSHOT_CHARS", 0)
        paths = _files(short_tmp_dir, 3)
        writes = [
            event
            for index, path in enumerate(paths)
            for event in (
                *_write_call(index, path, "before\n"),
                AcpEvent(
                    kind=EVENT_TOOL_CALL_UPDATE,
                    tool_call_id=f"call-{index}",
                    raw_tool_params={"command": "create", "path": str(path)},
                    diff_old_text="before\n",
                    diff_path=str(path),
                ),
            )
        ]
        # A turn that never lands: only the finally's flush runs, and only the
        # forced save after it makes the anchor durable.
        record = await _snapshot_turn(*writes, *_DIES, seeded=False)
        meta = _persisted_meta(record, "")
        assert [fc["path"] for fc in meta["file_changes"]] == [str(paths[2])]
        assert meta["file_changes_omitted_files"] == 2

    @pytest.mark.asyncio
    async def test_the_count_is_absent_when_nothing_was_omitted(self, short_tmp_dir):
        a_path, b_path = _files(short_tmp_dir, 2)
        record = await _snapshot_turn(
            *_write_call(0, a_path, "before\n"), *_write_update(1, b_path, "before\n"), *_LANDS
        )
        meta = _persisted_meta(record, "done")
        assert {fc["path"] for fc in meta["file_changes"]} == {str(a_path), str(b_path)}
        assert "file_changes_omitted_files" not in meta

    @pytest.mark.asyncio
    async def test_demoted_files_keep_their_path_and_are_not_counted(
        self, monkeypatch, short_tmp_dir
    ):
        # A demoted file stays on the message as a path, so it is not omitted.
        # The synthetic anchor of a turn with no reply carries no zero count.
        paths = _files(short_tmp_dir, 3)
        # Room for two paths beside the protected entry, none of their content.
        budget = 2 * len(str(paths[0])) + 5
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._MAX_TURN_SNAPSHOT_CHARS", budget)
        writes = [e for i, path in enumerate(paths) for e in _write_call(i, path, "x" * 4096)]
        record = await _snapshot_turn(*writes, *_DIES, seeded=False)
        meta = _persisted_meta(record, "")
        assert len(meta["file_changes"]) == 3
        assert sum(bool(fc.get("content_omitted")) for fc in meta["file_changes"]) == 2
        assert "file_changes_omitted_files" not in meta

    @pytest.mark.asyncio
    async def test_the_count_is_dropped_files_only_beside_a_demoted_one(
        self, monkeypatch, short_tmp_dir
    ):
        # Room for one path beside the protected entry: f1 is demoted to its
        # path, f0 is dropped, and only the dropped one is counted.
        paths = _files(short_tmp_dir, 3)
        budget = len(str(paths[0])) + 5
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._MAX_TURN_SNAPSHOT_CHARS", budget)
        writes = [e for i, path in enumerate(paths) for e in _write_call(i, path, "x" * 4096)]
        record = await _snapshot_turn(*writes, *_LANDS)
        meta = _persisted_meta(record, "done")
        assert sorted(fc["path"] for fc in meta["file_changes"]) == [str(paths[1]), str(paths[2])]
        assert meta["file_changes_omitted_files"] == 1
        # The earlier exchange's reply is not this turn's anchor.
        assert "file_changes" not in _persisted_meta(record, "earlier answer")

    def test_a_flush_without_drops_clears_an_earlier_count_on_its_message(self, monkeypatch):
        slot = _make_slot_with_assistant_message()
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._MAX_TURN_SNAPSHOT_ENTRIES", 1)
        for path in ("/a.py", "/b.py"):
            _record_turn_snapshot(slot, {"path": path, "content": "before"})
        _flush_file_changes(slot)
        assert slot.messages[-1]["meta"]["file_changes_omitted_files"] == 1
        _record_turn_snapshot(slot, {"path": "/c.py", "content": "before"})
        _flush_file_changes(slot)
        meta = slot.messages[-1]["meta"]
        assert [fc["path"] for fc in meta["file_changes"]] == ["/c.py"]
        assert "file_changes_omitted_files" not in meta

    def test_line_count_covers_the_latest_writes_past_the_row_cap(self, short_tmp_dir):
        slot = _make_slot_with_assistant_message()
        total = _MAX_TURN_SNAPSHOT_ENTRIES + 5
        for index in range(total):
            target = short_tmp_dir / f"f{index}.py"
            target.write_text("after\n")
            _record_turn_snapshot(slot, {"path": str(target), "content": "before\n"})
        # One removed and one added line per file, the latest five included.
        assert _turn_line_changes(slot._file_changes) == 2 * total
        latest = slot._file_changes[-5:]
        assert _turn_line_changes(latest) == 10

    def test_the_spec_describes_the_flush_side_cap_and_the_file_count(self):
        spec = (
            Path(__file__).parents[1] / "docs/system-specs/modules/learn-cron-dashboard.md"
        ).read_text(encoding="utf-8")
        paragraph = next(p for p in spec.split("\n\n") if p.startswith("The in-turn accumulator"))
        assert "every distinct path" in paragraph
        assert "file_changes_omitted_files" in paragraph
        assert "file_changes_omitted_writes" not in paragraph
        assert "_MAX_OMITTED_WRITES" not in paragraph
        assert "_TurnOverflowLines" not in paragraph
        snapshots = next(p for p in spec.split("\n\n") if p.startswith("**File-change snapshots**"))
        assert f"`_MAX_SNAPSHOT` ({_MAX_SNAPSHOT:,}) bounds one snapshot side" in snapshots
        assert (
            f"`_MAX_TURN_SNAPSHOT_CHARS` ({_MAX_TURN_SNAPSHOT_CHARS:,}) bounds the aggregate "
            "path+before+after characters outside the protected entry"
        ) in snapshots
        assert _MAX_TURN_SNAPSHOT_CHARS == 2 * _MAX_SNAPSHOT
        assert (
            "protects the most recent one whose content differs, or is truncated and so may "
            "differ past its cut, without charging it (a one-file turn therefore never shows "
            "the turn-budget notice); "
            f"the total character bound is therefore {_MAX_TURN_SNAPSHOT_CHARS:,} plus that "
            "entry's two snapshot sides, truncation markers and path."
        ) in snapshots
