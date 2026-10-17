"""A crew's first write carries the pre-projection ledger files into its crew log.

This file pins the one-shot carry of ``crews/<crew_id>/<n>.json`` and ``skipped.json``:
items and passes land with their own stamps, and the repository's passes are carried
by one crew only; every rejected approach the fold would keep lands in the file's
order, while approaches past its ``tried`` bound and unusable ``tried`` rows are counted
on the carry line rather than carried; an oversized row is shrunk to fit and an
oversized ``ci_state`` member clipped; a row that still cannot fit, cannot be read or
holds an out-of-range number refuses the write and leaves the carry unmarked, and so
does a carry the writer has not landed or a begun marker that cannot be written; a
re-run emits only the rows the record lacks; a crew that has already recorded carries
nothing; and a read before the first write previews the pending carry. The fold's own
bounds live in ``test_issue_radar_crew_store_fold_bounds.py`` and the shared skip index
in ``test_issue_radar_crew_store_skip_index.py``.
"""

import json
from pathlib import Path

import pytest
from issue_radar_crew_store_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    OWNER,
    REPO,
    _entries,
    _isolated_crew_log,
    _live_crew,
    _record,
    _sweep,
)

from kiro_crew.apps.builtins.issue_radar.backend import crew_runtime
from kiro_crew.apps.builtins.issue_radar.backend import crew_store as cs
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log_projection


def test_the_carry_marks_its_files_only_after_every_entry_landed(tmp_path, monkeypatch):
    """A drained writer is not proof: it can refuse an entry and drain quietly. The
    marker is written only after each carried entry is read back, so a carry that
    did not land is run again on the next write instead of being lost for good."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    item_file = _legacy_item(tmp_path, cid, 2251)
    from kiro_crew.crew_log import emit as crew_log_emit

    real_recorded = crew_log_emit.on_radar_recorded
    calls = {"n": 0}

    def drop_the_first_carry(session_id, data):
        calls["n"] += 1
        if data.get("carried") and calls["n"] == 1:
            return  # the writer "refused" it: nothing appended, nothing raised
        real_recorded(session_id, data)

    monkeypatch.setattr(crew_log_emit, "on_radar_recorded", drop_the_first_carry)
    # The write that triggered the carry is REFUSED: the record it would fold is
    # short of the row the writer refused, and folding it could admit a second editor.
    with pytest.raises(cs.CrewLedgerNotRecorded):
        _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")
    marker = item_file.parent / cs._ITEMS_CARRIED_MARKER
    assert not marker.exists(), "a carry that did not land must not be marked done"
    assert not [
        e for e in _entries(sid) if e.data.get("carried") is not True
    ], "the refused write appended nothing of its own"
    # An item's history is several entries, so a refusal can take one and leave
    # another; each entry re-states the whole record, so what DID land folds with its
    # real phase rather than as a bare placeholder the crew would read as live work.
    landed = cs.read_work_item(OWNER, REPO, cid, 2251, tmp_path)
    assert landed is not None and landed["phase"] == "implementing"

    # The next write carries it again; this time it lands, the marker appears and
    # the write itself goes through.
    monkeypatch.setattr(crew_log_emit, "on_radar_recorded", real_recorded)
    crew_log_projection.forget_slot_folds()
    _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")
    assert marker.is_file()
    carried = cs.read_work_item(OWNER, REPO, cid, 2251, tmp_path)
    assert carried is not None and carried["phase"] == "implementing"
    # Every approach is recovered, including the one the writer refused the first
    # time. Compared as a SET: a row recovered by a later run is appended when it
    # arrives, so the file's order survives a clean carry (pinned in the carry test)
    # but not a refused one -- and having the approach in the wrong place beats not
    # having it, since the files are never read again once the carry is marked done.
    assert {t["approach"] for t in carried["tried"]} == {"hasattr guard", "pywin32"}
    assert cs.read_work_item(OWNER, REPO, cid, 7, tmp_path) is not None


def test_a_carry_still_queued_in_the_writer_refuses_the_write_that_triggered_it(
    tmp_path, monkeypatch
):
    """The carry's rows are emitted, the flush times out: the refold this write would
    take is missing them -- a carried editing item among them -- so the write is
    refused, the crew is marked undrained, and the files stay unmarked. Once the
    writer drains, the same write goes through against the carried record."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    item_file = _legacy_item(tmp_path, cid, 2251)  # phase implementing: an EDITING item
    from kiro_crew.crew_log import emit as crew_log_emit

    real_flush = crew_log_emit.flush
    behind = {"on": True}

    def flush(timeout=None):
        drained = real_flush(timeout=timeout)
        return False if behind["on"] else drained

    monkeypatch.setattr(crew_log_emit, "flush", flush)
    # Issue 7 entering an editing phase would be a SECOND editor beside carried 2251.
    with pytest.raises(cs.CrewLedgerNotRecorded):
        _record(tmp_path, cid, sid, 7, {"phase": "implementing"}, "implement", "on #7")
    assert not (item_file.parent / cs._ITEMS_CARRIED_MARKER).exists()
    assert any(key[1] == cid for key in cs._undrained), "the crew is marked undrained"

    behind["on"] = False
    with pytest.raises(cs.CrewStoreError, match="already editing #2251"):
        _record(tmp_path, cid, sid, 7, {"phase": "implementing"}, "implement", "on #7")
    assert (item_file.parent / cs._ITEMS_CARRIED_MARKER).is_file()
    assert not any(key[1] == cid for key in cs._undrained)


# ── carrying the pre-projection files forward ───────────────────────────────
#
# The pre-projection ledger files -- ``crews/<crew_id>/<n>.json``, ``events.jsonl``,
# ``skipped.json`` -- are read ONCE more, on a crew's first write after the
# upgrade, and re-stated into its crew log. They are never written again.


def _legacy_item(root, cid, number, **fields):
    path = cs.work_item_path(OWNER, REPO, cid, number, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "schema": 1,
        "crew_id": cid,
        "owner": OWNER,
        "repo": REPO,
        "number": number,
        "phase": "implementing",
        "next": "add the Windows branch",
        "tried": [
            {
                "approach": "hasattr guard",
                "rejected_because": "loses the ACL",
                "at": "2026-01-01T00:00:00Z",
            },
            {"approach": "pywin32", "rejected_because": "", "at": "2026-01-02T00:00:00Z"},
        ],
        "worktree": "/w/2251",
        "branch": "fix/2251",
        "pr_number": 2271,
        "claimed_at": "2026-01-01T00:00:00Z",
        "last_progress_at": "2026-01-02T00:00:00Z",
        "finished_at": None,
        **fields,
    }
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def _legacy_skips(root, rows):
    path = cs.skips_path(OWNER, REPO, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows), encoding="utf-8")
    return path


def test_a_crews_first_write_carries_its_items_and_the_repositorys_passes_forward(tmp_path):
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    item_file = _legacy_item(tmp_path, cid, 2251)
    skips_file = _legacy_skips(
        tmp_path,
        {
            "42": {
                "number": "42",
                "reason": "dup of #41",
                "scope": "duplicate",
                "crew_id": "c_deadbeef",
                "decided_at": "2025-12-31T00:00:00Z",
            },
        },
    )
    item_bytes, skips_bytes = item_file.read_bytes(), skips_file.read_bytes()

    _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")

    carried = cs.read_work_item(OWNER, REPO, cid, 2251, tmp_path)
    assert carried is not None, "the pre-projection item was not carried"
    assert carried["phase"] == "implementing" and carried["next"] == "add the Windows branch"
    assert carried["pr_number"] == 2271 and carried["branch"] == "fix/2251"
    # Its own stamps, not fresh ones: re-stamping would make every carried claim
    # look freshly made and reset the TTL the record had already aged against.
    assert carried["claimed_at"] == "2026-01-01T00:00:00Z"
    assert carried["last_progress_at"] == "2026-01-02T00:00:00Z"
    # EVERY rejected approach is carried, in the file's own order, so the newest is
    # still last -- the field exists so the crew does not repeat what it ruled out.
    assert [t["approach"] for t in carried["tried"]] == ["hasattr guard", "pywin32"]
    assert [t["rejected_because"] for t in carried["tried"]] == ["loses the ACL", ""]
    lines = cs.read_events(OWNER, REPO, tmp_path, crew_id=cid)
    carry_line = next(line for line in lines if line.get("number") == 2251)
    assert "carried forward" in carry_line["text"]
    assert "not carried" not in carry_line["text"], "nothing was left behind to say"
    # The repository's passes, with the crew and time that decided them.
    row = cs.read_skips(OWNER, REPO, tmp_path)["42"]
    assert row == {
        "number": 42,
        "reason": "dup of #41",
        "scope": "duplicate",
        "crew_id": "c_deadbeef",
        "decided_at": "2025-12-31T00:00:00Z",
        "deferred": False,
    }
    # A carried pass creates NO work item for the carrying crew.
    assert {i["number"] for i in cs.list_work_items(OWNER, REPO, cid, tmp_path)} == {7, 2251}
    # The files are left exactly as they were, and marked so they are never read twice.
    assert (item_file.read_bytes(), skips_file.read_bytes()) == (item_bytes, skips_bytes)
    assert (item_file.parent / cs._ITEMS_CARRIED_MARKER).is_file()
    assert (skips_file.parent / cs._SKIPS_CARRIED_MARKER).is_file()
    # The crew's own write landed too, after the carry.
    assert cs.read_work_item(OWNER, REPO, cid, 7, tmp_path)["phase"] == "claimed"


def test_the_passes_are_carried_by_one_crew_only(tmp_path):
    """Whichever crew of the repository writes first carries the shared index; the
    marker stops a second crew re-carrying it, so the row stays one row."""
    a, sid_a = _live_crew(tmp_path, name="Andromeda")
    b, sid_b = _live_crew(tmp_path, name="Whirlpool")
    _legacy_skips(
        tmp_path, {"42": {"number": 42, "reason": "r", "scope": "other", "crew_id": "c_deadbeef"}}
    )

    _record(tmp_path, a["id"], sid_a, 7, {"phase": "claimed"}, "claim", "took #7")
    _record(tmp_path, b["id"], sid_b, 9, {"phase": "claimed"}, "claim", "took #9")

    assert "42" in cs.read_ledger(OWNER, REPO, a["id"], tmp_path)["skips"]
    assert "42" not in cs.read_ledger(OWNER, REPO, b["id"], tmp_path)["skips"]
    assert cs.read_skips(OWNER, REPO, tmp_path)["42"]["crew_id"] == "c_deadbeef"
    # One carried entry, in the first crew's log; the second appended only its own.
    assert len(_entries(sid_a)) == 2 and len(_entries(sid_b)) == 1


def test_a_crew_whose_fold_is_not_empty_does_not_carry(tmp_path):
    """The carry runs on a crew's FIRST write only. A file appearing later -- a
    restore from a backup -- is not silently merged over a live record."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")
    _legacy_item(tmp_path, cid, 2251)

    _record(tmp_path, cid, sid, 7, {"phase": "implementing"}, "implement", "fixing")

    assert cs.read_work_item(OWNER, REPO, cid, 2251, tmp_path) is None
    assert not (
        cs.work_item_path(OWNER, REPO, cid, 2251, tmp_path).parent / cs._ITEMS_CARRIED_MARKER
    ).exists()


def test_a_crew_level_sweep_as_the_first_write_carries_the_legacy_items(tmp_path):
    """An idle sweep is a routine first write after the upgrade. It carries the
    pre-projection files exactly as an item write would, so a crew that only sweeps
    is not read as owing nothing while its legacy open items sit unread."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _legacy_item(tmp_path, cid, 2251)

    _sweep(tmp_path, cid, sid)

    carried = cs.read_work_item(OWNER, REPO, cid, 2251, tmp_path)
    assert carried is not None and carried["phase"] == "implementing"
    assert cs.open_slot_count(OWNER, REPO, cid, tmp_path) == 1
    marker = cs.work_item_path(OWNER, REPO, cid, 2251, tmp_path).parent / cs._ITEMS_CARRIED_MARKER
    assert marker.is_file()


def test_a_begun_marker_that_cannot_be_written_refuses_the_write_before_any_carry(
    tmp_path, monkeypatch
):
    """The begun marker is what makes a half-done carry run again; a partial carry
    without it leaves a non-empty fold that never carries the rest. So a marker that
    cannot be written refuses the write before a single row is emitted."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    item_file = _legacy_item(tmp_path, cid, 2251)
    real_touch = Path.touch

    def touch(self, *a, **k):
        if self.name == cs._ITEMS_CARRY_BEGUN_MARKER:
            raise OSError("read-only")
        return real_touch(self, *a, **k)

    monkeypatch.setattr(Path, "touch", touch)
    with pytest.raises(cs.CrewLedgerNotRecorded):
        _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")
    assert _entries(sid) == [], "nothing was emitted"
    assert not (item_file.parent / cs._ITEMS_CARRY_BEGUN_MARKER).exists()
    # Nothing was carried, and the read says exactly that by previewing the file
    # rather than answering that the item does not exist -- the pre-projection record
    # is still on disk and still owed a carry.
    previewed = cs.read_work_item(OWNER, REPO, cid, 2251, tmp_path)
    assert previewed is not None and previewed["phase"] == "implementing"


def test_an_oversized_legacy_item_is_shrunk_to_fit_and_carried(tmp_path):
    """A row of long non-ASCII text can serialize past the entry ceiling at the text
    clamp the fold applies; the carry shrinks its free text until it fits rather
    than dropping the record."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    wide = "\u4e2d" * 4000  # 4000 chars, 12-24 KB serialized -- six fields of it overflow
    _legacy_item(
        tmp_path,
        cid,
        2251,
        why=wide,
        decision=wide,
        next=wide,
        worktree=wide,
        branch=wide,
        base_sha=wide,
    )

    _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")

    carried = cs.read_work_item(OWNER, REPO, cid, 2251, tmp_path)
    assert carried is not None and carried["phase"] == "implementing"
    assert 0 < len(carried["why"]) < 4000, "shrunk, not dropped"
    marker = cs.work_item_path(OWNER, REPO, cid, 2251, tmp_path).parent / cs._ITEMS_CARRIED_MARKER
    assert marker.is_file()


def test_a_legacy_number_outside_the_tools_range_is_unfit_not_a_lost_row(tmp_path):
    """The carry bounds a number the same way, so a pre-projection file cannot introduce
    one the fold would then drop -- which on an item row would fold it as a crew-level
    line, losing the row in silence. Such a file is unfit: named, left unmarked, and
    the write refused."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _legacy_item(tmp_path, cid, 2251)
    over = cs.work_item_path(OWNER, REPO, cid, 2251, tmp_path)
    record = json.loads(over.read_text(encoding="utf-8"))
    record["number"] = 10**12  # above the record tool's own max_val
    over.write_text(json.dumps(record), encoding="utf-8")
    renamed = over.with_name("1000000000000.json")
    over.rename(renamed)

    with pytest.raises(cs.CrewLedgerNotRecorded) as refusal:
        _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")
    assert renamed.name in str(refusal.value), "the out-of-range row is named"
    assert not (
        renamed.parent / cs._ITEMS_CARRIED_MARKER
    ).exists(), "an out-of-range row leaves the carry unfinished"


def test_a_legacy_row_that_cannot_fit_refuses_the_write(tmp_path, monkeypatch):
    """A valid row the log will not take is not carried, the carry is NOT marked
    finished -- the finished marker would discard stored state for good -- and the
    write that triggered the carry is REFUSED: folding a record missing that row
    could admit a second editor. The rows that fit land; the next write, once the
    row can be carried, appends only what is still missing."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _legacy_item(tmp_path, cid, 2251)
    _legacy_item(tmp_path, cid, 2252)
    real_fits = crew_log_emit.radar_entry_fits
    monkeypatch.setattr(
        crew_log_emit, "radar_entry_fits", lambda d: d.get("number") != 2252 and real_fits(d)
    )

    with pytest.raises(cs.CrewLedgerNotRecorded) as refusal:
        _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")
    assert "2252.json" in str(refusal.value), "the refusal names the file"

    crew_dir = cs.work_item_path(OWNER, REPO, cid, 2251, tmp_path).parent
    assert cs.read_work_item(OWNER, REPO, cid, 2251, tmp_path) is not None, "the fitting row landed"
    assert cs.read_work_item(OWNER, REPO, cid, 2252, tmp_path) is None
    assert (
        cs.read_work_item(OWNER, REPO, cid, 7, tmp_path) is None
    ), "the refused write did not fold"
    assert not (
        crew_dir / cs._ITEMS_CARRIED_MARKER
    ).exists(), "an unfit row leaves the carry unfinished"
    assert (crew_dir / cs._ITEMS_CARRY_BEGUN_MARKER).exists()

    monkeypatch.setattr(crew_log_emit, "radar_entry_fits", real_fits)
    _record(tmp_path, cid, sid, 7, {"next": "still on it"}, "claim", "progress")
    assert cs.read_work_item(OWNER, REPO, cid, 2252, tmp_path) is not None
    assert (crew_dir / cs._ITEMS_CARRIED_MARKER).is_file()
    assert (
        len([r for r in cs.list_work_items(OWNER, REPO, cid, tmp_path) if r["number"] == 2251]) == 1
    )


def test_an_unreadable_legacy_row_refuses_the_write_and_is_named(tmp_path, caplog):
    """A legacy file the reader cannot decode is not skipped: skipping it would let
    the carry finish with that record left behind for good, and folding without it
    would leave an item that holds an editing phase out of the record the one-editor
    rule reads. The readable row lands, the unreadable one is named, the write is
    refused, and once the file reads again the next write carries it and finishes."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _legacy_item(tmp_path, cid, 2251)
    torn = _legacy_item(tmp_path, cid, 2252)
    good_bytes = torn.read_bytes()
    torn.write_bytes(good_bytes[: len(good_bytes) // 2])

    with caplog.at_level("WARNING", logger=cs.logger.name):
        with pytest.raises(cs.CrewLedgerNotRecorded):
            _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")

    crew_dir = torn.parent
    assert (
        cs.read_work_item(OWNER, REPO, cid, 2251, tmp_path) is not None
    ), "the readable row landed"
    assert cs.read_work_item(OWNER, REPO, cid, 2252, tmp_path) is None
    assert (
        cs.read_work_item(OWNER, REPO, cid, 7, tmp_path) is None
    ), "the refused write did not fold"
    assert not (
        crew_dir / cs._ITEMS_CARRIED_MARKER
    ).exists(), "an unreadable row leaves the carry unfinished"
    assert (crew_dir / cs._ITEMS_CARRY_BEGUN_MARKER).exists()
    assert any("2252.json" in rec.getMessage() for rec in caplog.records), "the row is named"

    torn.write_bytes(good_bytes)
    _record(tmp_path, cid, sid, 7, {"next": "still on it"}, "claim", "progress")
    assert cs.read_work_item(OWNER, REPO, cid, 2252, tmp_path) is not None
    assert (crew_dir / cs._ITEMS_CARRIED_MARKER).is_file()


def test_a_legacy_row_without_a_number_takes_it_from_the_file_name(tmp_path):
    """The pre-projection writer named each item file by its number, so a record
    whose number field is missing still carries under the name it was filed as."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    path = _legacy_item(tmp_path, cid, 2251)
    record = json.loads(path.read_text(encoding="utf-8"))
    del record["number"]
    path.write_text(json.dumps(record), encoding="utf-8")

    _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")

    carried = cs.read_work_item(OWNER, REPO, cid, 2251, tmp_path)
    assert carried is not None and carried["phase"] == "implementing"
    assert (path.parent / cs._ITEMS_CARRIED_MARKER).is_file()


def test_a_skip_index_that_is_not_a_record_leaves_the_carry_unfinished(tmp_path):
    """The shared index file decoding to something other than a mapping is a row
    that could not be carried, not an empty index: the write is refused and the passes
    marker withheld, so the file is read again once it holds records."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    skips_file = _legacy_skips(tmp_path, {})
    skips_file.write_text("[]", encoding="utf-8")

    with pytest.raises(cs.CrewLedgerNotRecorded):
        _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")

    assert not (skips_file.parent / cs._SKIPS_CARRIED_MARKER).exists()
    assert (skips_file.parent / cs._SKIPS_CARRY_BEGUN_MARKER).exists()
    _legacy_skips(tmp_path, {"42": {"number": 42, "reason": "r", "scope": "other"}})
    _record(tmp_path, cid, sid, 7, {"next": "on it"}, "claim", "progress")
    assert cs.read_skips(OWNER, REPO, tmp_path)["42"]["reason"] == "r"
    assert (skips_file.parent / cs._SKIPS_CARRIED_MARKER).is_file()


def test_a_crew_that_only_swept_does_not_carry_a_file_that_appears_later(tmp_path):
    """The carry is keyed on whether ANY radar entry has folded into the crew's
    record, not on whether the record holds items or passes: a crew whose every
    write was an idle sweep holds none for good, and a legacy file present after
    its first write is not merged over the record it has moved on from."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _sweep(tmp_path, cid, sid)
    assert cs.read_ledger(OWNER, REPO, cid, tmp_path)["items"] == []
    _legacy_item(tmp_path, cid, 2251)

    _sweep(tmp_path, cid, sid, text="queue still empty")
    _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")

    assert cs.read_work_item(OWNER, REPO, cid, 2251, tmp_path) is None
    assert not (
        cs.work_item_path(OWNER, REPO, cid, 2251, tmp_path).parent / cs._ITEMS_CARRIED_MARKER
    ).exists()
    assert not (
        cs.work_item_path(OWNER, REPO, cid, 2251, tmp_path).parent / cs._ITEMS_CARRY_BEGUN_MARKER
    ).exists()


def test_every_rejected_approach_is_carried_in_the_files_own_order(tmp_path):
    """``tried`` is the crew's memory of what it already ruled out, so carrying only
    the newest deleted the field's purpose for every pre-upgrade item -- permanently,
    since the files are never read again once the carry is marked finished. Each
    approach is its own entry, in the file's order, so the folded list still ends with
    the newest."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _legacy_item(
        tmp_path,
        cid,
        2251,
        tried=[
            {"approach": "first", "rejected_because": "a", "at": "2026-01-01T00:00:00Z"},
            {"approach": "second", "rejected_because": "b", "at": "2026-01-02T00:00:00Z"},
            {"approach": "third", "rejected_because": "", "at": "2026-01-03T00:00:00Z"},
        ],
    )

    _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")

    carried = cs.read_work_item(OWNER, REPO, cid, 2251, tmp_path)
    assert [t["approach"] for t in carried["tried"]] == ["first", "second", "third"]
    assert [t["rejected_because"] for t in carried["tried"]] == ["a", "b", ""]
    # One entry per approach, each re-stating the record, so a refusal of one leaves
    # the others folding with the item's real phase instead of a placeholder.
    rows = [e for e in _entries(sid) if e.data.get("carried") is True]
    assert len(rows) == 3
    assert [r.data["tried"]["approach"] for r in rows] == ["first", "second", "third"]
    assert {r.data["phase"] for r in rows} == {"implementing"}
    # The item ENTERS its phase once, however long its history is.
    lines = cs.read_events(OWNER, REPO, tmp_path, crew_id=cid)
    with_phase = [line for line in lines if line.get("number") == 2251 and "phase" in line]
    assert len(with_phase) == 1


def test_an_approach_the_fold_would_evict_is_counted_not_silently_dropped(tmp_path):
    """The fold keeps an item's newest ``RADAR_TRIED_LIMIT`` approaches, so a longer
    history cannot be carried whole. The rows that cannot survive the read are left
    out at the source and SAID in the event, rather than costing an entry each to be
    discarded on the way in."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    limit = cs._projection().RADAR_TRIED_LIMIT
    _legacy_item(
        tmp_path,
        cid,
        2251,
        tried=[{"approach": f"a{n}", "rejected_because": ""} for n in range(limit + 3)],
    )

    _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")

    carried = cs.read_work_item(OWNER, REPO, cid, 2251, tmp_path)
    approaches = [t["approach"] for t in carried["tried"]]
    assert len(approaches) == limit
    assert approaches[-1] == f"a{limit + 2}", "the newest survived"
    assert approaches[0] == "a3", "the three oldest were the ones left behind"
    line = next(
        line
        for line in cs.read_events(OWNER, REPO, tmp_path, crew_id=cid)
        if line.get("number") == 2251 and "not carried" in line["text"]
    )
    assert "3" in line["text"]


def test_a_tried_row_the_fold_would_drop_is_counted_not_carried(tmp_path):
    """A ``tried`` row without a usable approach is not a row the fold would keep
    either, so it is counted rather than emitted as an entry that folds to nothing."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _legacy_item(
        tmp_path,
        cid,
        2251,
        tried=[
            {"approach": "real", "rejected_because": "r"},
            {"approach": "   "},
            "not a row",
            {"rejected_because": "no approach at all"},
        ],
    )

    _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")

    carried = cs.read_work_item(OWNER, REPO, cid, 2251, tmp_path)
    assert [t["approach"] for t in carried["tried"]] == ["real"]
    assert len([e for e in _entries(sid) if e.data.get("carried") is True]) == 1
    line = next(
        line
        for line in cs.read_events(OWNER, REPO, tmp_path, crew_id=cid)
        if line.get("number") == 2251
    )
    assert "not carried: 3" in line["text"]


def test_the_reads_an_upgraded_crews_first_turn_makes_answer_the_pending_carry(tmp_path):
    """THE READ HALF OF THE CARRY. A carry can only run from a write, but the reads
    that happen first are the ones the crew acts on: the pre-investigate nudge
    snapshot and the repository's shared skip index. Answering the empty record there
    tells a crew it has no open items and that no issue has been passed on, and it
    acts on that with forge comments that cannot be taken back. So a read previews
    what the pending carry would fold to, and appends nothing."""
    crew, sid = _live_crew(tmp_path, max_open=3)
    cid = crew["id"]
    item_file = _legacy_item(tmp_path, cid, 2251)
    skips_file = _legacy_skips(
        tmp_path, {"42": {"number": 42, "reason": "dup of #41", "scope": "duplicate"}}
    )
    before = (item_file.read_bytes(), skips_file.read_bytes())

    # No write has happened: the log is empty and the carry has not run.
    assert _entries(sid) == []

    # The two nudge-snapshot reads the reviewer named.
    assert [i["number"] for i in cs.list_work_items(OWNER, REPO, cid, tmp_path)] == [2251]
    assert cs.open_slot_count(OWNER, REPO, cid, tmp_path) == 1
    # The shared skip index, which a crew is told to check BEFORE it investigates.
    assert sorted(cs.read_skips(OWNER, REPO, tmp_path)) == ["42"]
    # And the snapshot itself, assembled from them.
    snapshot = crew_runtime.build_snapshot(OWNER, REPO, crew, tmp_path)
    assert snapshot["open_count"] == 1
    assert [i["number"] for i in snapshot["items"]] == [2251]

    # A READ, all of it: nothing appended, no marker written, the files untouched.
    assert _entries(sid) == []
    assert not (item_file.parent / cs._ITEMS_CARRIED_MARKER).exists()
    assert not (item_file.parent / cs._ITEMS_CARRY_BEGUN_MARKER).exists()
    assert not (skips_file.parent / cs._SKIPS_CARRIED_MARKER).exists()
    assert (item_file.read_bytes(), skips_file.read_bytes()) == before

    # The preview is the post-carry record BY CONSTRUCTION: the write's own carry
    # answers the same, and from then on the preview stops.
    previewed = cs.read_ledger(OWNER, REPO, cid, tmp_path)
    _record(tmp_path, cid, sid, 2251, {"phase": "implementing"}, "implement", "on it")
    assert (item_file.parent / cs._ITEMS_CARRIED_MARKER).is_file()
    landed = cs.read_work_item(OWNER, REPO, cid, 2251, tmp_path)
    for field in ("phase", "next", "branch", "pr_number", "claimed_at", "last_progress_at"):
        assert landed[field] == previewed["items"][0][field], field
    assert [t["approach"] for t in landed["tried"]] == [
        t["approach"] for t in previewed["items"][0]["tried"]
    ]


def test_a_crew_that_only_swept_is_not_previewed_for_a_file_that_appears_later(tmp_path):
    """The preview is reached on the write path's OWN carry trigger, so the two agree
    on when a carry is owed. A crew that has recorded -- even only an idle sweep -- has
    moved onto the log, and a file appearing beside it afterwards is not an upgrade to
    preview: previewing it would re-state retired work as live on every read."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _sweep(tmp_path, cid, sid, "queue empty")
    # The control: the crew has RECORDED, which is the write path's own carry trigger
    # -- the fold's crew_id is set by the first entry naming the crew, sweep included.
    assert cs.read_ledger(OWNER, REPO, cid, tmp_path)["crew_id"] == cid
    assert not cs._carry_pending(OWNER, REPO, cid, tmp_path)

    _legacy_item(tmp_path, cid, 4242)
    crew_log_projection.forget_slot_folds()

    assert cs.read_work_item(OWNER, REPO, cid, 4242, tmp_path) is None
    assert cs.list_work_items(OWNER, REPO, cid, tmp_path) == []
    assert cs.open_slot_count(OWNER, REPO, cid, tmp_path) == 0


def test_a_preview_that_cannot_be_built_reads_as_the_folded_record(tmp_path, monkeypatch):
    """A read must not raise into the crew page or the pre-investigate briefing, so a
    preview that fails answers the record as folded rather than propagating."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _legacy_item(tmp_path, cid, 2251)

    def explode(*a, **k):
        raise RuntimeError("the pre-projection files cannot be read")

    monkeypatch.setattr(cs, "_legacy_carry_rows", explode)
    assert cs.read_ledger(OWNER, REPO, cid, tmp_path)["items"] == []
    assert cs.open_slot_count(OWNER, REPO, cid, tmp_path) == 0


def test_a_re_run_carry_does_not_re_emit_a_row_the_record_already_holds(tmp_path):
    """A carried entry re-states the file's fields as an update. A carry run again --
    its rows landed but the finished marker did not, which is what an interrupted
    carry leaves on disk -- therefore emits only rows the record LACKS: re-emitting a
    row that landed would set an item the crew has since worked back to its
    pre-projection state."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _legacy_item(tmp_path, cid, 2251)
    _legacy_item(tmp_path, cid, 2252)
    _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")
    crew_dir = cs.work_item_path(OWNER, REPO, cid, 2251, tmp_path).parent
    assert (crew_dir / cs._ITEMS_CARRIED_MARKER).is_file(), "the control: the carry finished"
    assert cs.read_work_item(OWNER, REPO, cid, 2251, tmp_path)["phase"] == "implementing"

    # The state an interrupted carry leaves: the rows are folded, the BEGUN marker
    # stands and the finished one does not, so the next write carries again over a
    # record that already holds them.
    (crew_dir / cs._ITEMS_CARRIED_MARKER).unlink()
    cs._mark_carry_begun(crew_dir / cs._ITEMS_CARRY_BEGUN_MARKER)
    assert cs._carry_pending(OWNER, REPO, cid, tmp_path), "the control: it will carry again"

    # The crew works the carried item: live state past the file's.
    _record(
        tmp_path,
        cid,
        sid,
        2251,
        {"phase": "awaiting-ci", "next": "watch CI"},
        "implement",
        "pushed",
    )

    live = cs.read_work_item(OWNER, REPO, cid, 2251, tmp_path)
    assert live["phase"] == "awaiting-ci" and live["next"] == "watch CI", "live state kept"
    assert cs.read_work_item(OWNER, REPO, cid, 2252, tmp_path) is not None, "the other row stands"
    carried_2251 = [
        e for e in _entries(sid) if e.data.get("carried") is True and e.data.get("number") == 2251
    ]
    # One entry per rejected approach the file held, and the re-run added none: the
    # count is the file's `tried` length, not the number of carry runs.
    assert len(carried_2251) == 2, "the row that landed was emitted once, once per approach"
    assert (crew_dir / cs._ITEMS_CARRIED_MARKER).is_file()


def test_an_oversized_legacy_ci_member_is_bounded_not_a_lost_row(tmp_path):
    """A legacy file's ``ci_state`` can hold anything. One oversized member would push
    an otherwise valid row past the entry ceiling and leave it uncarried; the carry
    bounds each member the way the record tool bounds it on the way in, so the row
    lands with the member clipped and the ill-typed members dropped."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _legacy_item(
        tmp_path,
        cid,
        2251,
        ci_state={
            "state": "x" * 300_000,  # far past the entry ceiling on its own
            "passed": 3.7,  # fractional: not a counter
            "total": "9",  # a string: not a counter
            "round": 10**9,  # past the tool's ceiling
            "inherited_reds": 2,
            "stray": {"deep": ["not", "a", "member"]},
        },
    )
    _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")
    item = cs.read_work_item(OWNER, REPO, cid, 2251, tmp_path)
    assert item is not None, "the row was carried"
    assert item["ci_state"] == {"state": "x" * 32, "inherited_reds": 2}
    crew_dir = cs.work_item_path(OWNER, REPO, cid, 2251, tmp_path).parent
    assert (crew_dir / cs._ITEMS_CARRIED_MARKER).is_file()


def test_a_carried_item_that_was_finished_stays_finished(tmp_path):
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _legacy_item(
        tmp_path,
        cid,
        2251,
        phase="resolved",
        outcome="merged in #2271",
        finished_at="2026-01-03T00:00:00Z",
    )
    _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")
    carried = cs.read_work_item(OWNER, REPO, cid, 2251, tmp_path)
    assert carried["phase"] == "resolved"
    assert carried["finished_at"] == "2026-01-03T00:00:00Z"
    assert carried["outcome"] == "merged in #2271"
    assert cs.open_slot_count(OWNER, REPO, cid, tmp_path) == 1


def test_a_malformed_pre_projection_file_refuses_the_write_and_is_named(tmp_path):
    """A malformed pre-projection file is neither skipped nor fatal to the STORE: the
    write is refused, retryably, with the file named, and nothing of the record is
    lost. Skipping such a file would discard its stored state for good once the carry
    was marked finished, and folding without it could let a second item into an
    editing phase the omitted row still holds."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    bad = cs.work_item_path(OWNER, REPO, cid, 1, tmp_path)
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("{ not json", encoding="utf-8")
    (bad.parent / "2.json").write_text('"just a string"', encoding="utf-8")
    _legacy_skips(tmp_path, {"x": {"reason": "no number"}, "42": "not a row"})

    with pytest.raises(cs.CrewLedgerNotRecorded) as refusal:
        _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")
    assert "1.json" in str(refusal.value), "the malformed file is named"

    assert cs.read_skips(OWNER, REPO, tmp_path) == {}
    assert cs.list_work_items(OWNER, REPO, cid, tmp_path) == [], "the refused write did not fold"

    # Repaired, the same update goes through and the carry finishes.
    bad.unlink()
    (bad.parent / "2.json").unlink()
    _legacy_skips(tmp_path, {"42": {"number": 42, "reason": "r", "scope": "other"}})
    result = _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")
    assert result["item"]["phase"] == "claimed"
    assert [i["number"] for i in cs.list_work_items(OWNER, REPO, cid, tmp_path)] == [7]
