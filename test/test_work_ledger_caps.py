"""Conductor work ledger store: the caps and input bounds refuse, and a refusal writes nothing.

The item and open-item caps, the goal's concurrency cap (a hard refusal) and its item total
(an advisory line, not a refusal), and the field bounds every record is held to -- each pinned
at its RFC value, and each refusal proven to leave the prior bytes untouched.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
import work_ledger_helpers
from work_ledger_helpers import CONDUCTOR, _bytes_on_disk, _lift_goal_cap, _new_item

from kiro_crew import work_ledger as wl

# The shared fixtures, bound here by name so pytest registers them for this module.
_isolated_home = work_ledger_helpers._isolated_home


# ── caps refuse, and a refusal changes no bytes ───────────────────────────


def test_the_item_cap_refuses_the_thirty_third_item():
    wl.ensure_conductor(CONDUCTOR, goal="g")
    _lift_goal_cap()
    for index in range(wl.MAX_ITEMS_PER_CONDUCTOR):
        wl.apply_conductor_action(CONDUCTOR, "create", title=f"t{index}", acceptance={})
    before = sorted(p.name for p in wl.items_dir(CONDUCTOR).iterdir())
    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.apply_conductor_action(CONDUCTOR, "create", title="one too many", acceptance={})
    assert caught.value.code == wl.CODE_ITEM_CAP_EXCEEDED
    assert sorted(p.name for p in wl.items_dir(CONDUCTOR).iterdir()) == before


# ── the goal's two caps: concurrency (hard) and item total (advisory) ─────────


def _create_n(count: int, prefix: str = "t") -> list[str]:
    ids = []
    for index in range(count):
        made = wl.apply_conductor_action(
            CONDUCTOR, "create", title=f"{prefix}{index}", acceptance={}
        )
        ids.append(made["item"].item_id)
    return ids


def _create_and_close_n(count: int, prefix: str = "t") -> None:
    """*count* items minted and closed one at a time, so no fan-out cap is in the way."""
    for index in range(count):
        made = wl.apply_conductor_action(
            CONDUCTOR, "create", title=f"{prefix}{index}", acceptance={}
        )
        wl.apply_conductor_action(
            CONDUCTOR, "close", item_id=made["item"].item_id, state="accepted"
        )


def _refused_create(title: str = "one too many") -> wl.WorkLedgerError:
    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.apply_conductor_action(CONDUCTOR, "create", title=title, acceptance={})
    return caught.value


def test_the_concurrency_cap_refuses_the_seventh_open_item_by_default():
    """The bound on parallel work: six open at once, and the seventh is refused.

    Red before the cap existed: a goal board admitted a seventh open item.
    """
    wl.ensure_conductor(CONDUCTOR)
    wl.apply_conductor_action(CONDUCTOR, "goal", goal="g")
    header = wl.read_conductor(CONDUCTOR)
    assert header.concurrency_cap == wl.DEFAULT_GOAL_CONCURRENCY_CAP == 6
    _create_n(6)
    before = sorted(p.name for p in wl.items_dir(CONDUCTOR).iterdir())
    header_before = wl.read_conductor(CONDUCTOR).to_dict()
    refused = _refused_create("seventh")
    assert refused.code == wl.CODE_GOAL_CONCURRENCY_CAP_REACHED == "goal_concurrency_cap_reached"
    assert refused.field == "concurrency_cap"
    message = str(refused)
    assert "open items" in message and "action=goal concurrency_cap=" in message
    # A refusal writes nothing.
    assert sorted(p.name for p in wl.items_dir(CONDUCTOR).iterdir()) == before
    assert wl.read_conductor(CONDUCTOR).to_dict() == header_before


@pytest.mark.parametrize("terminal_state", sorted(wl.TERMINAL_ITEM_STATES))
def test_closing_an_item_frees_a_seat_under_the_concurrency_cap(terminal_state: str):
    wl.ensure_conductor(CONDUCTOR)
    wl.apply_conductor_action(CONDUCTOR, "goal", goal="g")
    ids = _create_n(6)
    assert _refused_create().code == wl.CODE_GOAL_CONCURRENCY_CAP_REACHED
    wl.apply_conductor_action(CONDUCTOR, "close", item_id=ids[0], state=terminal_state)
    minted = wl.apply_conductor_action(CONDUCTOR, "create", title="next", acceptance={})
    assert minted["item"].state == "open"
    # Six open again, so the cap bites again.
    assert _refused_create().code == wl.CODE_GOAL_CONCURRENCY_CAP_REACHED


def test_closed_items_never_count_toward_the_concurrency_cap():
    """A goal that has created far more than its cap still dispatches, so long as
    no more than the cap is running: the cap is on parallel work, not on history."""
    wl.ensure_conductor(CONDUCTOR)
    wl.apply_conductor_action(CONDUCTOR, "goal", goal="g")
    _create_and_close_n(30)
    header = wl.read_conductor(CONDUCTOR)
    assert header.goal_items_used == 30 and header.created_total == 30
    _create_n(6, "live")
    assert _refused_create().code == wl.CODE_GOAL_CONCURRENCY_CAP_REACHED


def test_a_raised_concurrency_cap_admits_more_open_items_and_keeps_the_count():
    wl.ensure_conductor(CONDUCTOR)
    wl.apply_conductor_action(CONDUCTOR, "goal", goal="g")
    _create_n(6)
    assert _refused_create().code == wl.CODE_GOAL_CONCURRENCY_CAP_REACHED
    # Raising the cap is a goal write; it must not restart the goal's item count.
    wl.apply_conductor_action(CONDUCTOR, "goal", concurrency_cap=8)
    header = wl.read_conductor(CONDUCTOR)
    assert header.concurrency_cap == 8 and header.goal_items_used == 6
    _create_n(2, "more")
    assert _refused_create().code == wl.CODE_GOAL_CONCURRENCY_CAP_REACHED


def test_a_lowered_concurrency_cap_refuses_until_enough_items_land():
    wl.ensure_conductor(CONDUCTOR)
    wl.apply_conductor_action(CONDUCTOR, "goal", goal="g")
    ids = _create_n(4)
    wl.apply_conductor_action(CONDUCTOR, "goal", concurrency_cap=2)
    assert _refused_create().code == wl.CODE_GOAL_CONCURRENCY_CAP_REACHED
    for item_id in ids[:2]:
        wl.apply_conductor_action(CONDUCTOR, "close", item_id=item_id, state="accepted")
    # Two open against a cap of two: still full.
    assert _refused_create().code == wl.CODE_GOAL_CONCURRENCY_CAP_REACHED
    wl.apply_conductor_action(CONDUCTOR, "close", item_id=ids[2], state="accepted")
    wl.apply_conductor_action(CONDUCTOR, "create", title="fits", acceptance={})


def test_the_stores_open_cap_still_wins_at_the_concurrency_ceiling():
    """A concurrency cap at its ceiling equals the store's own open bound, so the
    thirty-third open item is refused by the store, under its own code."""
    wl.ensure_conductor(CONDUCTOR)
    wl.apply_conductor_action(
        CONDUCTOR, "goal", goal="g", concurrency_cap=wl.MAX_GOAL_CONCURRENCY_CAP
    )
    assert wl.MAX_GOAL_CONCURRENCY_CAP == wl.MAX_ITEMS_PER_CONDUCTOR
    _create_n(wl.MAX_ITEMS_PER_CONDUCTOR)
    assert _refused_create().code == wl.CODE_ITEM_CAP_EXCEEDED


def test_a_board_that_never_records_a_goal_has_no_goal_concurrency_cap():
    """A queue board (pipeline, security) writes no ``goal``; only the store's open
    cap bounds its fan-out."""
    wl.ensure_conductor(CONDUCTOR)
    _create_n(wl.DEFAULT_GOAL_CONCURRENCY_CAP + 1, "q")
    open_items = [i for i in wl.list_work_items(CONDUCTOR) if i.state == "open"]
    assert len(open_items) == wl.DEFAULT_GOAL_CONCURRENCY_CAP + 1


@pytest.mark.parametrize("bad", [0, -1, wl.MAX_GOAL_CONCURRENCY_CAP + 1, "six", 2.5])
def test_an_out_of_range_concurrency_cap_is_refused_and_writes_nothing(bad):
    wl.ensure_conductor(CONDUCTOR)
    wl.apply_conductor_action(CONDUCTOR, "goal", goal="g")
    before = wl.read_conductor(CONDUCTOR).to_dict()
    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.apply_conductor_action(CONDUCTOR, "goal", concurrency_cap=bad)
    assert caught.value.code == wl.CODE_INVALID_VALUE
    assert caught.value.field == "concurrency_cap"
    assert wl.read_conductor(CONDUCTOR).to_dict() == before


def test_a_stored_concurrency_cap_out_of_range_reads_as_the_default():
    for raw in (None, 0, wl.MAX_GOAL_CONCURRENCY_CAP + 1, "x"):
        record = wl.ConductorRecord.from_dict({"concurrency_cap": raw})
        assert record.concurrency_cap == wl.DEFAULT_GOAL_CONCURRENCY_CAP
    assert wl.ConductorRecord.from_dict({"concurrency_cap": 9}).concurrency_cap == 9


def test_a_header_from_before_the_concurrency_cap_is_bounded_by_the_default():
    """An upgraded board gets the default cap, not an unbounded fan-out."""
    wl.ensure_conductor(CONDUCTOR)
    wl.apply_conductor_action(CONDUCTOR, "goal", goal="g")
    header_path = wl.conductor_dir(CONDUCTOR) / "conductor.json"
    stored = json.loads(header_path.read_text(encoding="utf-8"))
    stored.pop("concurrency_cap")
    header_path.write_text(json.dumps(stored), encoding="utf-8")
    assert wl.read_conductor(CONDUCTOR).concurrency_cap == wl.DEFAULT_GOAL_CONCURRENCY_CAP
    _create_n(6)
    assert _refused_create().code == wl.CODE_GOAL_CONCURRENCY_CAP_REACHED
    # The first write stores the key.
    assert "concurrency_cap" in json.loads(header_path.read_text(encoding="utf-8"))


def test_the_concurrency_cap_holds_under_concurrent_creates():
    wl.ensure_conductor(CONDUCTOR)
    wl.apply_conductor_action(CONDUCTOR, "goal", goal="g")
    refused: list[str] = []

    def create() -> None:
        for _ in range(3):
            try:
                wl.apply_conductor_action(CONDUCTOR, "create", title="t", acceptance={})
            except wl.WorkLedgerError as exc:
                refused.append(exc.code)

    threads = [threading.Thread(target=create) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert len(wl.list_work_items(CONDUCTOR)) == wl.DEFAULT_GOAL_CONCURRENCY_CAP
    assert refused and set(refused) == {wl.CODE_GOAL_CONCURRENCY_CAP_REACHED}


# ── the goal's item cap: an advisory line, not a refusal ─────────────────────


def test_the_twenty_first_create_is_admitted_and_flagged_by_default():
    """The goal's total is reported, never refused.

    Red before: the twenty-first create was refused ``goal_item_cap_reached``.
    """
    wl.ensure_conductor(CONDUCTOR)
    wl.apply_conductor_action(CONDUCTOR, "goal", goal="g")
    assert wl.read_conductor(CONDUCTOR).item_cap == wl.DEFAULT_GOAL_ITEM_CAP == 20
    _create_and_close_n(20)
    header = wl.read_conductor(CONDUCTOR)
    # At the line, not past it: twenty of twenty is what the cap allowed.
    assert header.goal_items_used == 20 and header.cap_exceeded is False
    minted = wl.apply_conductor_action(CONDUCTOR, "create", title="twenty-first", acceptance={})
    assert minted["item"].state == "open"
    header = wl.read_conductor(CONDUCTOR)
    assert header.goal_items_used == 21 and header.cap_exceeded is True
    assert not hasattr(wl, "CODE_GOAL_ITEM_CAP_REACHED"), "the refusal is gone, not dormant"


def test_closed_items_still_count_toward_the_item_cap_line():
    """The line is about spend, so a closed item is still one the goal paid for."""
    wl.ensure_conductor(CONDUCTOR)
    wl.apply_conductor_action(CONDUCTOR, "goal", goal="g", item_cap=2)
    _create_and_close_n(2)
    assert wl.read_conductor(CONDUCTOR).cap_exceeded is False
    wl.apply_conductor_action(CONDUCTOR, "create", title="third", acceptance={})
    assert wl.read_conductor(CONDUCTOR).cap_exceeded is True


def test_a_raised_item_cap_clears_the_flag_and_keeps_the_count():
    wl.ensure_conductor(CONDUCTOR)
    wl.apply_conductor_action(CONDUCTOR, "goal", goal="g", item_cap=2)
    _create_and_close_n(3)
    assert wl.read_conductor(CONDUCTOR).cap_exceeded is True
    wl.apply_conductor_action(CONDUCTOR, "goal", item_cap=5)
    header = wl.read_conductor(CONDUCTOR)
    assert (header.item_cap, header.goal_items_used, header.cap_exceeded) == (5, 3, False)


def test_a_round_only_or_same_text_goal_write_keeps_the_count():
    wl.ensure_conductor(CONDUCTOR)
    wl.apply_conductor_action(CONDUCTOR, "goal", goal="g", item_cap=1)
    _create_and_close_n(2)
    wl.apply_conductor_action(CONDUCTOR, "goal", round_number=2)
    wl.apply_conductor_action(CONDUCTOR, "goal", goal="g", round_number=3)
    header = wl.read_conductor(CONDUCTOR)
    assert header.goal_items_used == 2 and header.cap_exceeded is True


def test_a_new_goal_starts_a_new_item_count():
    wl.ensure_conductor(CONDUCTOR)
    wl.apply_conductor_action(CONDUCTOR, "goal", goal="first goal", item_cap=3)
    _create_and_close_n(4)
    version = wl.read_conductor(CONDUCTOR).goal_version
    assert wl.read_conductor(CONDUCTOR).cap_exceeded is True
    wl.apply_conductor_action(CONDUCTOR, "goal", goal="second goal", new_goal=True)
    header = wl.read_conductor(CONDUCTOR)
    assert header.goal_version == version + 1
    assert header.goal_items_used == 0 and header.created_total == 4
    assert header.cap_exceeded is False
    _create_and_close_n(3, "second")
    assert wl.read_conductor(CONDUCTOR).cap_exceeded is False
    _create_n(1, "past")
    assert wl.read_conductor(CONDUCTOR).cap_exceeded is True


def test_a_new_goal_on_a_board_from_before_the_counter_starts_after_its_records():
    """A header with no create counter still holds its records; a new goal skips them."""
    wl.ensure_conductor(CONDUCTOR)
    wl.apply_conductor_action(CONDUCTOR, "goal", goal="old goal")
    _create_n(3)
    header_path = wl.conductor_dir(CONDUCTOR) / "conductor.json"
    stored = json.loads(header_path.read_text(encoding="utf-8"))
    stored.pop("created_total")
    stored.pop("goal_items_base")
    header_path.write_text(json.dumps(stored), encoding="utf-8")
    wl.apply_conductor_action(CONDUCTOR, "goal", goal="new goal", item_cap=1, new_goal=True)
    header = wl.read_conductor(CONDUCTOR)
    assert (header.created_total, header.goal_items_used) == (3, 0)
    wl.apply_conductor_action(CONDUCTOR, "create", title="first of the new goal", acceptance={})
    assert wl.read_conductor(CONDUCTOR).cap_exceeded is False


def _strip_to_pre_goal_cap_header() -> None:
    """Rewrite the header as one written before the per-goal cap existed."""
    header_path = wl.conductor_dir(CONDUCTOR) / "conductor.json"
    stored = json.loads(header_path.read_text(encoding="utf-8"))
    stored.pop("item_cap")
    stored.pop("goal_items_base")
    header_path.write_text(json.dumps(stored), encoding="utf-8")


def test_an_upgraded_header_counts_its_goal_from_the_upgrade():
    """A header from before the per-goal cap does not charge earlier goals to this one.

    One goal made 22 items, then a second goal was recorded the old way. With the
    count start read as zero, the next create was flagged as if this goal had made
    22. Read as legacy, the goal starts counting at the upgrade.
    """
    wl.ensure_conductor(CONDUCTOR)
    wl.apply_conductor_action(CONDUCTOR, "goal", goal="first goal", item_cap=wl.MAX_GOAL_ITEM_CAP)
    _create_and_close_n(22)
    wl.apply_conductor_action(CONDUCTOR, "goal", goal="second goal")
    _strip_to_pre_goal_cap_header()
    header = wl.read_conductor(CONDUCTOR)
    assert header.created_total == 22 and header.goal_items_used == 0

    wl.apply_conductor_action(CONDUCTOR, "create", title="first after upgrade", acceptance={})

    header = wl.read_conductor(CONDUCTOR)
    assert (header.goal_items_base, header.goal_items_used) == (22, 1)
    assert header.cap_exceeded is False
    # The settled start is stored, and the default line now counts from it.
    assert "goal_items_base" in json.loads(
        (wl.conductor_dir(CONDUCTOR) / "conductor.json").read_text(encoding="utf-8")
    )
    _create_and_close_n(19, "rest")
    assert wl.read_conductor(CONDUCTOR).cap_exceeded is False
    _create_n(1, "21st")
    assert wl.read_conductor(CONDUCTOR).cap_exceeded is True


def test_a_goal_write_settles_an_upgraded_headers_count_start():
    wl.ensure_conductor(CONDUCTOR)
    wl.apply_conductor_action(CONDUCTOR, "goal", goal="g")
    _create_n(5)
    _strip_to_pre_goal_cap_header()
    wl.apply_conductor_action(CONDUCTOR, "goal", round_number=2)
    header = wl.read_conductor(CONDUCTOR)
    assert (header.goal_items_base, header.goal_items_used) == (5, 0)


def test_a_reworded_goal_does_not_reset_the_item_count():
    """Only an explicit ``new_goal`` restarts the count, so rewording cannot dodge it."""
    wl.ensure_conductor(CONDUCTOR)
    wl.apply_conductor_action(CONDUCTOR, "goal", goal="ship the feature", item_cap=1)
    _create_and_close_n(2)
    wl.apply_conductor_action(CONDUCTOR, "goal", goal="ship the feature, round two")
    header = wl.read_conductor(CONDUCTOR)
    assert header.goal == "ship the feature, round two" and header.goal_items_used == 2
    assert header.cap_exceeded is True


def test_new_goal_needs_the_goal_text_and_a_boolean():
    wl.ensure_conductor(CONDUCTOR)
    wl.apply_conductor_action(CONDUCTOR, "goal", goal="g")
    before = wl.read_conductor(CONDUCTOR).to_dict()
    for kwargs in ({"new_goal": True}, {"goal": "h", "new_goal": "yes"}):
        with pytest.raises(wl.WorkLedgerError) as caught:
            wl.apply_conductor_action(CONDUCTOR, "goal", **kwargs)
        assert (caught.value.code, caught.value.field) == (wl.CODE_INVALID_VALUE, "new_goal")
    assert wl.read_conductor(CONDUCTOR).to_dict() == before


def test_a_board_that_never_records_a_goal_has_no_item_cap_line():
    """A queue board (pipeline, security) writes no ``goal``: its count is its whole
    history, so there is no goal to measure and the flag never turns on."""
    wl.ensure_conductor(CONDUCTOR)
    _create_and_close_n(wl.DEFAULT_GOAL_ITEM_CAP + 1, "q")
    header = wl.read_conductor(CONDUCTOR)
    assert len(wl.list_work_items(CONDUCTOR)) == wl.DEFAULT_GOAL_ITEM_CAP + 1
    assert header.goal_items_used == wl.DEFAULT_GOAL_ITEM_CAP + 1
    assert header.cap_exceeded is False


@pytest.mark.parametrize("bad", [0, -1, wl.MAX_GOAL_ITEM_CAP + 1, "twenty", 2.5])
def test_an_out_of_range_item_cap_is_refused_and_writes_nothing(bad):
    wl.ensure_conductor(CONDUCTOR)
    wl.apply_conductor_action(CONDUCTOR, "goal", goal="g")
    before = wl.read_conductor(CONDUCTOR).to_dict()
    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.apply_conductor_action(CONDUCTOR, "goal", item_cap=bad)
    assert caught.value.code == wl.CODE_INVALID_VALUE
    assert caught.value.field == "item_cap"
    assert wl.read_conductor(CONDUCTOR).to_dict() == before


def test_a_stored_item_cap_out_of_range_reads_as_the_default():
    for raw in (None, 0, wl.MAX_GOAL_ITEM_CAP + 1, "x"):
        record = wl.ConductorRecord.from_dict({"item_cap": raw})
        assert record.item_cap == wl.DEFAULT_GOAL_ITEM_CAP
    assert wl.ConductorRecord.from_dict({"item_cap": 7}).item_cap == 7


@pytest.mark.parametrize("terminal_state", sorted(wl.TERMINAL_ITEM_STATES))
def test_closed_items_do_not_count_toward_the_item_cap(terminal_state: str):
    """The cap bounds LIVE fan-out: closing an item frees its seat.

    A queue conductor mints one item per ticket and closes each as it lands, so a
    cap that counted its closed history would refuse the 33rd ticket of the shift
    with nothing live behind the refusal.
    """
    wl.ensure_conductor(CONDUCTOR, goal="g")
    _lift_goal_cap()
    ids = [
        wl.apply_conductor_action(CONDUCTOR, "create", title=f"t{index}", acceptance={})[
            "item"
        ].item_id
        for index in range(wl.MAX_ITEMS_PER_CONDUCTOR)
    ]
    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.apply_conductor_action(CONDUCTOR, "create", title="one too many", acceptance={})
    assert caught.value.code == wl.CODE_ITEM_CAP_EXCEEDED

    wl.apply_conductor_action(CONDUCTOR, "close", item_id=ids[0], state=terminal_state)
    minted = wl.apply_conductor_action(CONDUCTOR, "create", title="next ticket", acceptance={})
    assert minted["item"].state == "open"

    # Thirty-two open again, so the next one is refused -- the cap still holds.
    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.apply_conductor_action(CONDUCTOR, "create", title="one too many", acceptance={})
    assert caught.value.code == wl.CODE_ITEM_CAP_EXCEEDED
    assert "open items" in str(caught.value)


def test_closed_items_stay_on_disk_listed_and_readable_past_the_cap():
    """Freeing a seat changes nothing about the closed item's own record."""
    wl.ensure_conductor(CONDUCTOR, goal="g")
    _lift_goal_cap()
    ids = [
        wl.apply_conductor_action(CONDUCTOR, "create", title=f"t{index}", acceptance={})[
            "item"
        ].item_id
        for index in range(wl.MAX_ITEMS_PER_CONDUCTOR)
    ]
    for item_id in ids:
        wl.apply_conductor_action(CONDUCTOR, "close", item_id=item_id, state="accepted")
    closed_bytes = {item_id: _bytes_on_disk(item_id) for item_id in ids}

    # A whole second shift's worth fits once the first is closed...
    second = [
        wl.apply_conductor_action(CONDUCTOR, "create", title=f"s{index}", acceptance={})[
            "item"
        ].item_id
        for index in range(wl.MAX_ITEMS_PER_CONDUCTOR)
    ]
    # ...and the cap bites again at thirty-two OPEN.
    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.apply_conductor_action(CONDUCTOR, "create", title="one too many", acceptance={})
    assert caught.value.code == wl.CODE_ITEM_CAP_EXCEEDED

    listed = wl.list_work_items(CONDUCTOR)
    assert len(listed) == 2 * wl.MAX_ITEMS_PER_CONDUCTOR
    assert {it.item_id for it in listed} == set(ids) | set(second)
    assert sum(it.state == "accepted" for it in listed) == wl.MAX_ITEMS_PER_CONDUCTOR
    for item_id in ids:
        stored = wl.read_work_item(CONDUCTOR, item_id)
        assert stored is not None and stored.state == "accepted"
        assert wl.item_path(CONDUCTOR, item_id).exists()
        assert _bytes_on_disk(item_id) == closed_bytes[item_id]
    brief = wl.read_work_brief(CONDUCTOR, ids[0])
    assert brief is not None and brief["item_id"] == ids[0]


def test_a_board_keeps_dispatching_past_256_lifetime_creates():
    """Closed history does not stop a board.

    A queue board (no goal, so no spend cap) creates and closes 260 items; every
    create succeeds, and the header's create
    counter still counts each one.
    """
    wl.ensure_conductor(CONDUCTOR)
    total = 260
    for index in range(total):
        item_id = wl.apply_conductor_action(CONDUCTOR, "create", title=f"t{index}", acceptance={})[
            "item"
        ].item_id
        wl.apply_conductor_action(CONDUCTOR, "close", item_id=item_id, state="accepted")
    header = wl.read_conductor(CONDUCTOR)
    assert header is not None and header.created_total == total
    assert len(wl.list_work_items(CONDUCTOR)) == total


def test_a_header_from_before_the_counter_is_seeded_from_its_records_once():
    """``created_total`` is zero on a header that predates it. The first create on
    such a board seeds the counter from the records the board holds, then bumps it,
    so the count starts where the board stands rather than at zero."""
    wl.ensure_conductor(CONDUCTOR, goal="g")
    for index in range(2):
        wl.apply_conductor_action(CONDUCTOR, "create", title=f"t{index}", acceptance={})
    header_path = wl.conductor_dir(CONDUCTOR) / "conductor.json"
    stored = json.loads(header_path.read_text(encoding="utf-8"))
    assert stored["created_total"] == 2
    del stored["created_total"]  # a header written before the field existed
    header_path.write_text(json.dumps(stored), encoding="utf-8")
    assert wl.read_conductor(CONDUCTOR).created_total == 0

    wl.apply_conductor_action(CONDUCTOR, "create", title="third", acceptance={})
    assert wl.read_conductor(CONDUCTOR).created_total == 3, "seeded from two records, then bumped"


@pytest.mark.parametrize(
    "kwargs,expected_field",
    [
        ({"status": "progress", "summary": "x" * (wl.MAX_SUMMARY_CHARS + 1)}, "summary"),
        (
            {
                "status": "progress",
                "summary": "ok",
                "artifacts": {f"k{i}": "v" for i in range(wl.MAX_ARTIFACT_KEYS + 1)},
            },
            "artifacts",
        ),
        (
            {
                "status": "progress",
                "summary": "ok",
                "artifacts": {"k" * (wl.MAX_ARTIFACT_KEY_CHARS + 1): "v"},
            },
            "artifacts",
        ),
        (
            {
                "status": "progress",
                "summary": "ok",
                "artifacts": {"k": "v" * (wl.MAX_ARTIFACT_VALUE_CHARS + 1)},
            },
            "artifacts",
        ),
    ],
)
def test_a_refused_worker_cap_leaves_both_files_byte_identical(kwargs, expected_field):
    item_id = _new_item()
    wl.apply_worker_report(CONDUCTOR, item_id, status="progress", summary="baseline")
    before = _bytes_on_disk(item_id)
    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.apply_worker_report(CONDUCTOR, item_id, **kwargs)
    assert caught.value.code == wl.CODE_FIELD_TOO_LONG
    assert caught.value.field == expected_field
    assert _bytes_on_disk(item_id) == before


def test_a_refused_conductor_cap_leaves_both_files_byte_identical():
    item_id = _new_item()
    before = _bytes_on_disk(item_id)
    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.apply_conductor_action(
            CONDUCTOR,
            "decide",
            item_id=item_id,
            decision="d" * (wl.MAX_DECISION_CHARS + 1),
        )
    assert (caught.value.code, caught.value.field) == (wl.CODE_FIELD_TOO_LONG, "decision")
    assert _bytes_on_disk(item_id) == before


def test_an_oversized_title_and_goal_are_refused_not_truncated():
    wl.ensure_conductor(CONDUCTOR, goal="g")
    for action, kwargs, name in (
        ("create", {"title": "t" * (wl.MAX_TITLE_CHARS + 1), "acceptance": {}}, "title"),
        ("goal", {"goal": "g" * (wl.MAX_GOAL_CHARS + 1)}, "goal"),
    ):
        with pytest.raises(wl.WorkLedgerError) as caught:
            wl.apply_conductor_action(CONDUCTOR, action, **kwargs)
        assert (caught.value.code, caught.value.field) == (wl.CODE_FIELD_TOO_LONG, name)
    record = wl.read_conductor(CONDUCTOR)
    assert record is not None and record.goal == "g"


def test_a_boundary_length_value_is_accepted():
    item_id = _new_item(title="t" * wl.MAX_TITLE_CHARS)
    wl.apply_worker_report(
        CONDUCTOR,
        item_id,
        status="progress",
        summary="s" * wl.MAX_SUMMARY_CHARS,
        artifacts={"k" * wl.MAX_ARTIFACT_KEY_CHARS: "v" * wl.MAX_ARTIFACT_VALUE_CHARS},
    )
    item = wl.read_work_item(CONDUCTOR, item_id)
    assert item is not None and len(item.summary) == wl.MAX_SUMMARY_CHARS


@pytest.mark.parametrize("bad_pr", [0, -1, wl.MAX_PR + 1, True, 1.5, float("nan"), "12", object()])
def test_pr_is_bounded_and_a_bool_is_not_an_integer(bad_pr):
    item_id = _new_item()
    before = _bytes_on_disk(item_id)
    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.apply_worker_report(CONDUCTOR, item_id, status="done", summary="s", pr=bad_pr)
    assert caught.value.field == "pr"
    assert _bytes_on_disk(item_id) == before


def test_an_unknown_status_is_refused_with_its_own_code():
    item_id = _new_item()
    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.apply_worker_report(CONDUCTOR, item_id, status="finished", summary="s")
    assert caught.value.code == wl.CODE_INVALID_STATUS


def test_wrong_typed_fields_are_refused_rather_than_coerced():
    item_id = _new_item()
    with pytest.raises(wl.WorkLedgerError):
        wl.apply_worker_report(CONDUCTOR, item_id, status="done", summary=17)
    with pytest.raises(wl.WorkLedgerError):
        wl.apply_worker_report(CONDUCTOR, item_id, status="done", summary="s", artifacts=["x"])
    with pytest.raises(wl.WorkLedgerError):
        wl.apply_worker_report(CONDUCTOR, item_id, status="done", summary="s", artifacts={"k": 1})
    with pytest.raises(wl.WorkLedgerError):
        wl.apply_conductor_action(CONDUCTOR, "create", title="t", acceptance=["nope"])
    with pytest.raises(wl.WorkLedgerError):
        wl.apply_conductor_action(CONDUCTOR, "verdict", item_id=item_id, verdict="fail", fails=-1)
    with pytest.raises(wl.WorkLedgerError):
        wl.apply_conductor_action(CONDUCTOR, "verdict", item_id=item_id, verdict="maybe")
    with pytest.raises(wl.WorkLedgerError):
        wl.apply_conductor_action(CONDUCTOR, "bind", item_id=item_id, worker_session_key="a\0b")


def test_acceptance_must_be_json_serialisable():
    wl.ensure_conductor(CONDUCTOR)
    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.apply_conductor_action(CONDUCTOR, "create", title="t", acceptance={"fn": lambda: None})
    assert caught.value.field == "acceptance"


def test_an_oversized_acceptance_is_refused(monkeypatch):
    wl.ensure_conductor(CONDUCTOR)
    # Above the header's own size (the header must still read back), below the blob's.
    monkeypatch.setattr(wl, "MAX_RECORD_BYTES", 360)
    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.apply_conductor_action(CONDUCTOR, "create", title="t", acceptance={"blob": "x" * 400})
    assert caught.value.code == wl.CODE_FIELD_TOO_LONG


def test_a_record_lands_at_the_size_the_ceiling_measured(monkeypatch):
    """The whole-file writer pins ``newline`` so the stored bytes are the measured bytes.

    :func:`_write_item_locked` and :func:`_read_json_record` both reason in the
    ``\\n`` form ``_serialize`` produced. With the default newline translation
    Windows writes ``\\r\\n``, one byte per line more, so a record measured just
    under ``MAX_RECORD_BYTES`` would land over it and read back as absent. POSIX
    cannot observe that growth, so the contract is pinned at the writer's boundary.
    """
    calls: list[dict] = []
    real = wl.atomic_write

    def recorder(path, content, **kwargs):
        calls.append({"path": Path(path), "content": content, "newline": kwargs.get("newline")})
        real(path, content, **kwargs)

    monkeypatch.setattr(wl, "atomic_write", recorder)
    wl.ensure_conductor(CONDUCTOR)
    _new_item(acceptance={"kind": "manual"})
    records = [c for c in calls if c["path"].suffix == ".json"]
    assert records, "no whole-file record was written"
    for call in records:
        assert call["newline"] == "\n", call["path"].name
        assert "\r" not in call["content"]
    # The header is written by the bootstrap and again by the create (its create
    # counter), so what is on disk is each path's LAST write.
    for call in {c["path"]: c for c in records}.values():
        assert call["path"].read_bytes() == call["content"].encode("utf-8")


def test_acceptance_is_stored_verbatim_and_never_interpreted():
    payload = {"kind": "pr_checks", "pr": 123, "repo": "owner/name", "extra": [1, {"a": None}]}
    item_id = _new_item(acceptance=payload)
    item = wl.read_work_item(CONDUCTOR, item_id)
    assert item is not None and item.acceptance == payload
