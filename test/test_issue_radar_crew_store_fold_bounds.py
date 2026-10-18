"""The radar fold bounds and screens everything it retains from a crew's log.

This file pins what the fold keeps from the bytes it reads back: ``ci_state`` members,
labels, free text and numbers are held to the record tool's own limits, and ``tried``
rows, work items and passes to a named bound; an eviction is counted, never lands on
the item that holds the edit, and a retry after one puts the item back; a line naming
another crew is left out, and a number outside the tool's range is left out of the
fold and of the entry an update writes; and a unit recreated under its id folds cold.
The update entry itself lives in ``test_issue_radar_crew_store.py`` and the bounds the
pre-projection carry applies in ``test_issue_radar_crew_store_legacy_carry.py``.
"""

from issue_radar_crew_store_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    OWNER,
    REPO,
    _entries,
    _isolated_crew_log,
    _item,
    _live_crew,
    _record,
    _tick,
    _unit,
)

from kiro_crew.apps.builtins.issue_radar.backend import crew_store as cs
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log_projection
from kiro_crew.crew_log.schema import KIND_SESSION


def test_ci_members_and_labels_read_off_bytes_are_re_bounded_by_the_fold():
    """The entry type admits an object for ``ci_state`` and a list for labels, and the
    fold reads bytes off a file: every member is re-bounded to the record tool's own
    type and ceiling, and the label count is capped, so an oversized reading cannot
    make every retained item hold it."""
    from kiro_crew.crew_log import entry_types
    from kiro_crew.crew_log import projection as crew_log
    from kiro_crew.crew_log.schema import Entry

    data = {
        "crew_id": "c_0a1b2c3d",
        "owner": OWNER,
        "repo": REPO,
        "number": 7,
        "event": "ci",
        "event_kind": "ci",
        "ci_state": {
            "state": "x" * 5000,
            "passed": 10**9,  # past the tool's ceiling
            "total": "9",  # a string: not a counter
            "round": 3,
            "stray": {"deep": ["not", "a", "member"]},
        },
        "labels_applied": [f"crew:l{i}" for i in range(1000)],
    }
    value = crew_log.fold(
        "radar",
        [Entry(type=cs.LEDGER_ENTRY_TYPE, seq=1, time=1_789_000_000_000, src="gateway", data=data)],
    )
    (item,) = value["items"]
    assert item["ci_state"] == {"state": "x" * 32, "round": 3}
    assert len(item["labels_applied"]) == entry_types.RADAR_LABELS_LIMIT == 20


def test_rejected_approaches_are_bounded_per_item_newest_kept():
    """Every list the fold retains has a named bound; ``tried`` keeps the newest
    RADAR_TRIED_LIMIT rows of one item, which is what a resume reads."""
    from kiro_crew.crew_log import projection as crew_log
    from kiro_crew.crew_log.schema import Entry

    base = {
        "crew_id": "c_0a1b2c3d",
        "owner": OWNER,
        "repo": REPO,
        "number": 7,
        "event": "progress",
        "event_kind": "implement",
    }
    n = crew_log.RADAR_TRIED_LIMIT + 5
    entries = [
        Entry(
            type=cs.LEDGER_ENTRY_TYPE,
            seq=i + 1,
            time=1_789_000_000_000 + i,
            src="gateway",
            data={**base, "tried": {"approach": f"approach {i}", "rejected_because": "no"}},
        )
        for i in range(n)
    ]
    (item,) = crew_log.fold("radar", entries)["items"]
    assert len(item["tried"]) == crew_log.RADAR_TRIED_LIMIT
    assert item["tried"][-1]["approach"] == f"approach {n - 1}"
    assert item["tried"][0]["approach"] == "approach 5"


def _fold_entries(crew_log, datas):
    from kiro_crew.crew_log.schema import Entry

    return crew_log.fold(
        "radar",
        [
            Entry(
                type=cs.LEDGER_ENTRY_TYPE,
                seq=i + 1,
                time=1_789_000_000_000 + i,
                src="gateway",
                data=data,
            )
            for i, data in enumerate(datas)
        ],
    )


def test_work_items_are_bounded_finished_ones_evicted_first_and_counted(monkeypatch):
    """Past RADAR_ITEM_LIMIT the fold evicts: a finished item before any open one,
    oldest finish first, never the item just written; an evicted item takes its
    phase history along; and the count is reported so a bounded record is told
    from a complete one."""
    from kiro_crew.crew_log import projection as crew_log

    monkeypatch.setattr(crew_log, "RADAR_ITEM_LIMIT", 4)
    base = {"crew_id": "c_0a1b2c3d", "owner": OWNER, "repo": REPO, "event_kind": "implement"}
    datas = [
        {**base, "number": 1, "phase": "resolved", "event": "done 1"},  # finished, oldest
        {**base, "number": 2, "phase": "awaiting-ci", "event": "on 2"},  # open, not editing
        {**base, "number": 3, "phase": "resolved", "event": "done 3"},  # finished
        {**base, "number": 4, "phase": "awaiting-ci", "event": "on 4"},  # open, not editing
        {**base, "number": 5, "phase": "claimed", "event": "took 5"},  # open, 5th: evicts #1
        {**base, "number": 6, "phase": "claimed", "event": "took 6"},  # 5th again: evicts #3
    ]
    value = _fold_entries(crew_log, datas)
    kept = sorted(item["number"] for item in value["items"])
    assert kept == [2, 4, 5, 6], "the finished items went first, every open one stayed"
    assert value["counts"]["evicted_items"] == 2
    assert set(value["phase_lines"]) == {"2", "4", "5", "6"}, "an evicted item's history goes too"
    assert value["counts"]["open"] == 4

    # Only open items left: the one longest without progress goes, never the newest.
    value = _fold_entries(
        crew_log, datas + [{**base, "number": 7, "phase": "claimed", "event": "took 7"}]
    )
    assert sorted(item["number"] for item in value["items"]) == [4, 5, 6, 7]
    assert value["counts"]["evicted_items"] == 3


def test_a_retry_after_eviction_puts_the_item_back_rather_than_deduping_away(monkeypatch):
    """The dedup map and the item bound evict on different rules over different key
    sets, so an evicted item's digest can outlive the item. A retry matching that stale
    digest must APPLY: returning early would leave the crew told its update landed while
    the row is gone."""
    from kiro_crew.crew_log import projection as crew_log

    monkeypatch.setattr(crew_log, "RADAR_ITEM_LIMIT", 2)
    base = {"crew_id": "c_0a1b2c3d", "owner": OWNER, "repo": REPO, "event_kind": "implement"}
    first = {**base, "number": 1, "phase": "resolved", "event": "done 1"}
    datas = [
        first,
        {**base, "number": 2, "phase": "implementing", "event": "on 2"},
        {**base, "number": 3, "phase": "implementing", "event": "on 3"},
    ]
    value = _fold_entries(crew_log, datas)
    assert sorted(item["number"] for item in value["items"]) == [2, 3], "#1 was evicted"

    # The SAME update again -- a retry of the line the crew was told landed.
    value = _fold_entries(crew_log, datas + [first])
    assert 1 in {item["number"] for item in value["items"]}, "the retry did not put #1 back"


def test_this_crews_passes_are_bounded_earliest_decided_evicted_and_counted(monkeypatch):
    from kiro_crew.crew_log import projection as crew_log

    monkeypatch.setattr(crew_log, "RADAR_SKIP_LIMIT", 2)
    base = {"crew_id": "c_0a1b2c3d", "owner": OWNER, "repo": REPO, "event_kind": "skip"}
    datas = [
        {
            **base,
            "number": n,
            "phase": "skipped",
            "event": f"pass {n}",
            "skip": {"reason": f"r{n}", "scope": "duplicate"},
        }
        for n in (11, 12, 13)
    ]
    value = _fold_entries(crew_log, datas)
    assert sorted(value["skips"]) == ["12", "13"], "the earliest decided pass went"
    assert value["counts"]["evicted_skips"] == 1


def test_a_ci_reading_keeps_only_the_declared_member_keys():
    """``ci_state`` is merged key by key; an undeclared key would grow the item by
    key without bound, so the fold keeps the declared members and no other."""
    from kiro_crew.crew_log import entry_types
    from kiro_crew.crew_log import projection as crew_log

    base = {
        "crew_id": "c_0a1b2c3d",
        "owner": OWNER,
        "repo": REPO,
        "number": 7,
        "event_kind": "ci",
        "event": "ci",
    }
    datas = [
        {**base, "ci_state": {"state": "pending", "passed": 3, "total": 9, "shard_17": "red"}},
        {**base, "ci_state": {"round": 2, "shard_18": "red", "inherited_reds": 1}},
    ]
    (item,) = _fold_entries(crew_log, datas)["items"]
    assert item["ci_state"] == {
        "state": "pending",
        "passed": 3,
        "total": 9,
        "round": 2,
        "inherited_reds": 1,
    }
    assert set(item["ci_state"]) <= set(entry_types.RADAR_CI_KEYS)


def test_an_entry_naming_another_crew_is_left_out_of_the_fold(tmp_path):
    """Every unit of one slot belongs to ONE crew, so a line naming another is a
    planted or damaged one and must not patch a record it does not own -- nor may
    it break the reads that follow it."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _item(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim")
    planted = {
        "crew_id": "c_0badf00d",
        "owner": OWNER,
        "repo": REPO,
        "number": 7,
        "phase": "resolved",
        "event": "planted",
        "event_kind": "merge",
    }
    crew_log_emit.on_radar_recorded(sid, planted)
    assert crew_log_emit.flush(timeout=5.0)
    assert len(_entries(sid)) == 2, "the planted line is in the file"

    item = cs.read_work_item(OWNER, REPO, cid, 7, tmp_path)
    assert item["phase"] == "claimed" and item["finished_at"] is None
    assert [e["text"] for e in cs.read_events(OWNER, REPO, tmp_path, crew_id=cid)] == ["progress"]
    # And the crew keeps writing past it.
    assert _item(tmp_path, cid, sid, 7, {"phase": "implementing"})["phase"] == "implementing"


def test_a_number_outside_the_tools_range_is_not_retained_from_the_log(tmp_path):
    """A line the tool would have refused on magnitude is not retained on the way back
    in. A thousand-digit ``number`` read off a file is dropped exactly as a number of
    the wrong type is, so it cannot be kept as an item key, a skip key, or a
    ``pr_number``, and cannot ride in every later checkpoint and response."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")
    huge = int("9" * 4000)
    crew_log_emit.on_radar_recorded(
        sid,
        {
            "crew_id": cid,
            "owner": OWNER,
            "repo": REPO,
            "number": huge,
            "phase": "implementing",
            "event": "planted",
            "event_kind": "implement",
        },
    )
    # A second line whose NUMBER is legitimate, so the oversized ``pr_number`` reaches
    # the item path: without this the first line folds as a crew-level one and no item
    # is there to carry a pr_number at all.
    crew_log_emit.on_radar_recorded(
        sid,
        {
            "crew_id": cid,
            "owner": OWNER,
            "repo": REPO,
            "number": 7,
            "event": "planted a pr number",
            "event_kind": "implement",
            "pr_number": huge,
        },
    )
    assert crew_log_emit.flush(timeout=10.0)
    crew_log_projection.forget_slot_folds()

    ledger = cs.read_ledger(OWNER, REPO, cid, tmp_path)
    assert {i["number"] for i in ledger["items"]} == {7}, "the oversized number is not an item"
    assert (
        cs.read_work_item(OWNER, REPO, cid, 7, tmp_path)["pr_number"] is None
    ), "the oversized pr_number is not retained"
    assert str(huge) not in ledger["skips"]


def test_the_folds_text_clamp_is_the_cap_the_record_tool_itself_accepts(tmp_path):
    """The fold's clamp is a shape gate on bytes a reader does not control, so it must
    sit AT the largest value the record tool accepts, never below it. Below it, an
    ordinary field the tool took is shortened on every read -- silent corruption of
    state a resume acts on, not a bound."""
    from kiro_crew.crew_log import projection as crew_log
    from kiro_crew.validation import MAX_MEDIUM_STRING

    assert (
        crew_log.RADAR_TEXT_LIMIT == MAX_MEDIUM_STRING
    ), "the fold would truncate a next/decision/why/tried the tool accepted"
    assert (
        cs._MAX_CARRIED_TEXT == crew_log.RADAR_TEXT_LIMIT
    ), "the one-shot carry would permanently lose a legacy tail the fold would have kept"

    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _record(tmp_path, cid, sid, 2251, {"phase": "claimed"}, "claim", "took #2251")
    long_next = "n" * MAX_MEDIUM_STRING
    item = _item(tmp_path, cid, sid, 2251, {"next": long_next})
    assert item["next"] == long_next, "a field at the tool's own cap came back whole"


def test_an_out_of_range_number_does_not_erase_the_one_already_stored(tmp_path):
    """A number the tool would refuse on magnitude is left OUT of the entry rather than
    written into it. Written, it would append fine and then be dropped by the fold, so
    the update would land and silently erase the association the item already held --
    an omitted field reads as "unchanged", which is the honest answer."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _record(tmp_path, cid, sid, 2251, {"phase": "claimed"}, "claim", "took #2251")
    held = _item(tmp_path, cid, sid, 2251, {"pr_number": 2271, "claim_comment_id": 9911})
    assert (held["pr_number"], held["claim_comment_id"]) == (2271, 9911)

    # 0 is below the tool's floor of 1; the comment id is past its ceiling.
    after = _item(tmp_path, cid, sid, 2251, {"pr_number": 0, "claim_comment_id": 10**18 + 1})
    assert (after["pr_number"], after["claim_comment_id"]) == (
        2271,
        9911,
    ), "an out-of-range number erased the association already stored"
    last = _entries(sid)[-1].data
    assert (
        "pr_number" not in last and "claim_comment_id" not in last
    ), "the out-of-range number was written into the entry instead of left out"


def test_the_item_bound_never_evicts_the_item_that_holds_the_edit(monkeypatch):
    """The one-editor rule is decided by scanning the items the record still HOLDS, so
    evicting the item in an editing phase makes the rule answer "no editor" and admit a
    second one -- and an append-only log cannot retract the lines the two then write. So
    an editing item is never the victim even when no terminal item is available."""
    from kiro_crew.crew_log import projection as crew_log

    monkeypatch.setattr(crew_log, "RADAR_ITEM_LIMIT", 3)
    base = {"crew_id": "c_0a1b2c3d", "owner": OWNER, "repo": REPO, "event_kind": "implement"}
    # The editing item is the OLDEST, which is what an eviction rule that ranks by age
    # alone picks: every item is open, so there is no terminal victim and a pool that
    # falls back to all candidates lands on exactly it.
    datas = [
        {**base, "number": 1, "phase": "implementing", "event": "editing 1"},
        {**base, "number": 2, "phase": "claimed", "event": "took 2"},
        {**base, "number": 3, "phase": "claimed", "event": "took 3"},
        {**base, "number": 4, "phase": "claimed", "event": "took 4"},
    ]
    value = _fold_entries(crew_log, datas)
    kept = sorted(item["number"] for item in value["items"])
    assert 1 in kept, "the editing item survived the bound"
    assert kept == [1, 3, 4], "an ordinary open item went instead"
    assert value["counts"]["evicted_items"] == 1
    # And the record the one-editor rule reads still names it, which is the invariant
    # the eviction was breaking.
    editing = [i for i in value["items"] if i["phase"] in cs.RADAR_EDITING_PHASES]
    assert [i["number"] for i in editing] == [1]


def test_a_retry_whose_item_was_evicted_is_not_vouched_for_by_a_surviving_skip(monkeypatch):
    """Items and passes are bounded on SEPARATE rules and evict independently, so a
    pass can outlive the work item of the very update that wrote both. Asking whether
    EITHER survived let the surviving pass stand in for the evicted item, and the
    same-digest retry -- the one thing that would put the item back -- returned early
    and left it missing."""
    from kiro_crew.crew_log import projection as crew_log

    monkeypatch.setattr(crew_log, "RADAR_ITEM_LIMIT", 2)
    base = {"crew_id": "c_0a1b2c3d", "owner": OWNER, "repo": REPO, "event_kind": "skip"}
    # One update writing BOTH an item and a pass on #1. Not carried, so it is the
    # ordinary path: the item is recorded and the pass with it.
    both = {
        **base,
        "number": 1,
        "phase": "skipped",
        "event": "passed on 1",
        "skip": {"reason": "duplicate of #0", "scope": "duplicate"},
    }
    later = [
        {**base, "number": n, "phase": "claimed", "event": f"took {n}", "event_kind": "claim"}
        for n in (2, 3, 4)
    ]
    # The item bound evicts #1; the pass bound (5000) keeps its pass.
    value = _fold_entries(crew_log, [both, *later])
    assert 1 not in {item["number"] for item in value["items"]}, "the control: item evicted"
    assert "1" in value["skips"], "the control: its pass survived"

    # The retry of that same update. Three updates follow it here, so its digest is not
    # the item's last applied one; drive the exact reported case instead -- the update
    # is replayed as the newest one and then retried.
    replayed = _fold_entries(crew_log, [both, *later, both, *later, both])
    restored = [item for item in replayed["items"] if item["number"] == 1]
    assert len(restored) == 1, "the retry restored the item the skip had vouched for"
    assert restored[0]["phase"] == "skipped"
    assert "1" in replayed["skips"], "and its pass is still there"


def test_a_unit_recreated_under_its_id_folds_cold_however_far_its_seq_climbed(tmp_path):
    """The checkpoint cache is keyed by each unit's log identity, not by its seq
    alone: a log removed and created again under the same id, whose new entries take
    its seq PAST the cached one, is a different log, and the read must fold it from
    the start rather than continue the retired log's checkpoint.

    The new entries are appended straight through the emitter, because a store write
    folds before it appends -- so writing them would refresh the cache while the new
    log's seq was still below the cached one, and the stale checkpoint would never be
    offered to the comparison this pins.
    """
    from kiro_crew.crew_log import eager
    from kiro_crew.crew_log.store import segment_paths

    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _item(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim")
    warm = cs.read_ledger(OWNER, REPO, cid, tmp_path)
    assert {i["number"] for i in warm["items"]} == {7}, "the retired log's item is cached"
    retired_seq = cs._unit_last_seq(sid)

    crew_log_emit.reset_caches()
    # Removed the way retention removes a unit: with the eager folder held between
    # batches, so none of its folds has a segment open. Windows refuses to unlink a
    # file any handle holds, and the folder reads this unit on its own thread after
    # every entry.
    assert eager.drain(timeout=10.0), "the eager folder never settled"
    with eager.paused() as held:
        assert held, "the eager folder could not be held between batches"
        for path in segment_paths(KIND_SESSION, sid):
            path.unlink()
    _tick()
    _unit(cid, session_id=sid)
    for number in (11, 13, 15, 17):
        built = cs._legacy_item_entries(OWNER, REPO, cid, {"number": number, "phase": "claimed"})
        assert len(built) == 1
        crew_log_emit.on_radar_recorded(sid, built[0])
    assert crew_log_emit.flush(timeout=10.0)
    assert cs._unit_last_seq(sid) > retired_seq, "the control: the new log outran the cached seq"

    fresh = cs.read_ledger(OWNER, REPO, cid, tmp_path)
    assert {i["number"] for i in fresh["items"]} == {11, 13, 15, 17}, "no item of the retired log"
    crew_log_projection.forget_slot_folds()
    assert cs.read_ledger(OWNER, REPO, cid, tmp_path) == fresh


def test_the_radar_ci_bounds_and_label_limit_are_the_record_tools_own():
    """The fold's per-member CI bounds and label count are copied from the record
    tool's field specs; this pins the copy so the two cannot drift apart silently."""
    from kiro_crew.crew_log import entry_types
    from kiro_crew.validation import ISSUE_RADAR_CREW_RECORD_SCHEMA

    specs = {f.name: f for f in ISSUE_RADAR_CREW_RECORD_SCHEMA.fields}
    assert set(entry_types.RADAR_CI_BOUNDS) == set(entry_types.RADAR_CI_KEYS)
    for key, (kind, bound) in entry_types.RADAR_CI_BOUNDS.items():
        spec = specs[f"ci_{key}"]
        assert spec.type is kind
        assert bound == (spec.max_len if kind is str else spec.max_val)
    assert entry_types.RADAR_LABELS_LIMIT == specs["labels_applied"].max_items
    for field, (low, high) in entry_types.RADAR_NUMBER_BOUNDS.items():
        spec = specs[field]
        assert spec.type is int
        assert (low, high) == (spec.min_val, spec.max_val), field
