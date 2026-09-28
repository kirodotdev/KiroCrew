"""What the provider decides, which the type checker cannot.

``mypy`` proves every required key is written and that nothing but ``str | Unsaid``
lands in a ``str | Unsaid`` field. It cannot prove the VALUE is honest, and three of
these values are exactly where the live board lied:

* the CHECKS cell showed ``ready`` / ``pending`` / ``next`` -- words where a fraction
  belongs, because nothing refused a word (:func:`test_a_word_never_reaches_the_checks_cell`);
* the per-column action line showed ``Raymond`` / ``chat-2176`` -- owner names where an
  instruction belongs (:func:`test_a_bare_name_never_reaches_the_action_line`);
* an age taken from the read clock is always about zero, which deletes staleness
  (:func:`test_age_is_measured_from_the_newest_work_entry`).

So the gates are pinned by behaviour, and each pin restores the exact value that was
wrong rather than a nearby one.

:func:`test_a_board_with_no_items_is_not_a_board_of_zeros` is the other half: absent and
zero are different facts, and a crew that has logged nothing must not render a complete
board of zeros.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from kiro_crew.pipeline_board_contract import (
    CONTRACT_VERSION,
    EMPTY_JUDGMENT,
    UNSAID,
    PipelineBoardJudgment,
    build_pipeline_board,
    panel_payload,
)
from kiro_crew.work_vocab import WORK_ITEM_STATES, WorkBoardItem, WorkBoardView

#: The newest entry's stamp, and a read clock exactly one hour later -- DERIVED from the
#: stamp rather than hand-computed, so the ages below are exact by construction and a
#: wrong epoch constant cannot make a clamped 0 look like a real measurement.
ENTRY_AT = "2026-09-27T12:00:00+00:00"
NOW = datetime.fromisoformat(ENTRY_AT).timestamp() + 3600.0


def _item(item_id: str, state: str = "open", **over: Any) -> WorkBoardItem:
    base: WorkBoardItem = {
        "schema": 1,
        "item_id": item_id,
        "title": "an item",
        "acceptance": {},
        "state": state,
        "verdict": None,
        "decision": "",
        "worker_session_key": "chat-99-worker",
        "round": 1,
        "fails": 0,
        "status": "progress",
        "summary": "",
        "artifacts": {},
        "pr": None,
        "last_report_at": None,
        "created_at": ENTRY_AT,
        "closed_at": None,
        "events": [],
    }
    base.update(over)  # type: ignore[typeddict-item]
    return base


def _view(items: list[WorkBoardItem], *, omitted: int = 0, rounds: int = 1) -> WorkBoardView:
    return {
        "conductor": {
            "schema": 1,
            "slot_key": "member-fleet",
            "goal": "ship it",
            "round": rounds,
            "goal_version": 1,
            "depth": 0,
            "parent_item": None,
            "created_at": ENTRY_AT,
            "entries": len(items),
            "first_entry_at": ENTRY_AT,
            "last_entry_at": ENTRY_AT,
            "generation": "g1",
        },
        "items": items,
        "omitted": omitted,
    }


def _build(view: WorkBoardView, judgment: PipelineBoardJudgment | None = None) -> Any:
    return build_pipeline_board(
        view,
        judgment if judgment is not None else EMPTY_JUDGMENT,
        name="Fleet",
        captured_at=ENTRY_AT,
        stale_after_seconds=1800,
        now_epoch=NOW,
    )


# --------------------------------------------------------------------------
# the two gates
# --------------------------------------------------------------------------


def test_a_word_never_reaches_the_checks_cell() -> None:
    """The exact four values the live board showed in a column headed CHECKS."""
    judgment: PipelineBoardJudgment = {
        "lede": "one line",
        "you": {},
        "notes": {},
        "checks": {
            f"it_{i}": word for i, word in enumerate(("ready", "pending", "next", "blocked"))
        },
    }
    panel = _build(_view([_item(f"it_{i}") for i in range(4)]), judgment)
    cells = [card["of"] for col in panel["columns"] for card in col["cards"]]
    assert cells == [UNSAID] * 4, cells


def test_a_fraction_does_reach_the_checks_cell() -> None:
    """The gate must not be a blanket refusal: a real tally is the point of the cell."""
    judgment: PipelineBoardJudgment = {
        "lede": UNSAID,
        "you": {},
        "notes": {},
        "checks": {"it_0": "94/94"},
    }
    panel = _build(_view([_item("it_0")]), judgment)
    cells = [card["of"] for col in panel["columns"] for card in col["cards"]]
    assert cells == ["94/94"]


def test_a_malformed_fraction_is_refused() -> None:
    """Only canonical non-negative ASCII integer fractions count as tallies."""
    for bad in (
        "a/b",
        "1/2/3",
        "/5",
        "5/",
        "94 / 94",
        "\u00b2/\u00b2",
        "\u00b9\u2070/\u00b9\u2070",
        " 94/94",
        "94/94 ",
        "+94/94",
        "94/+94",
        "-94/94",
        "94/-94",
        "094/94",
        "94/094",
        "9_4/94",
    ):
        judgment: PipelineBoardJudgment = {
            "lede": UNSAID,
            "you": {},
            "notes": {},
            "checks": {"it_0": bad},
        }
        panel = _build(_view([_item("it_0")]), judgment)
        got = [card["of"] for col in panel["columns"] for card in col["cards"]]
        assert got == [UNSAID], f"{bad!r} reached the cell as {got}"


def test_a_bare_name_never_reaches_the_action_line() -> None:
    """The exact two values the live board showed where an instruction belongs.

    Keyed by ITEM id, which is the key the provider reads. Keyed by column instead, the
    lookup simply misses and the cell is UNSAID because nothing was found -- so the
    test would pass without the gate ever running, and deleting the gate would not
    redden it.
    """
    for name in ("Raymond", "chat-2176", "--"):
        judgment: PipelineBoardJudgment = {
            "lede": UNSAID,
            "you": {"it_0": name},
            "notes": {},
            "checks": {},
        }
        panel = _build(_view([_item("it_0")]), judgment)
        opens = [col for col in panel["columns"] if col["name"] == "open"]
        assert opens[0]["cards"][0]["you"] == UNSAID, f"{name!r} reached the action line"


def test_a_real_instruction_does_reach_the_action_line() -> None:
    """The gate's other direction: a phrase gets through, so it refuses rather than
    blocks everything. Without this, returning UNSAID unconditionally would pass every
    test above it.
    """
    judgment: PipelineBoardJudgment = {
        "lede": UNSAID,
        "you": {"it_0": "approve PR 14111"},
        "notes": {},
        "checks": {},
    }
    panel = _build(_view([_item("it_0")]), judgment)
    opens = [col for col in panel["columns"] if col["name"] == "open"]
    assert opens[0]["cards"][0]["you"] == "approve PR 14111"


def test_an_action_for_one_item_does_not_appear_on_another() -> None:
    """Two items, one action, and it lands on exactly one card.

    A column key makes this impossible to get right: both cards read the same sentence,
    so two open items both advertise one item's action when only one of them is the
    item it names. That is the same misattribution as an owner name in this cell,
    arriving by shape rather than by value.
    """
    judgment: PipelineBoardJudgment = {
        "lede": UNSAID,
        "you": {"it_0": "approve PR 14111"},
        "notes": {},
        "checks": {},
    }
    panel = _build(_view([_item("it_0"), _item("it_1")]), judgment)
    opens = [col for col in panel["columns"] if col["name"] == "open"]
    actions = {card["id"]: card["you"] for card in opens[0]["cards"]}
    assert actions == {"it_0": "approve PR 14111", "it_1": UNSAID}


# --------------------------------------------------------------------------
# numbers come from the fold
# --------------------------------------------------------------------------


def test_the_publisher_cannot_reach_a_number() -> None:
    """The judgment type has no numeric field at all -- the structural version of the fix.

    Asserted on the type rather than on a value, because the guarantee is that there is
    nowhere to put a number, not that a particular number was ignored.
    """
    assert set(PipelineBoardJudgment.__annotations__) == {
        "lede",
        "you",
        "notes",
        "checks",
    }


def test_columns_are_named_from_the_closed_state_vocabulary() -> None:
    """A publisher cannot invent ``next`` or ``ready``: the names are the fold's."""
    panel = _build(_view([_item("it_0")]))
    assert [col["name"] for col in panel["columns"]] == list(WORK_ITEM_STATES)


def test_the_total_counts_items_and_dropped_entries_are_their_own_number() -> None:
    """``total`` is items. Dropped ENTRIES are reported beside it, never added into it.

    ``omitted`` counts entries the fold refused -- a straggler from a purged board, an
    entry naming no item -- and one dropped entry is not one missing item. Added
    together the total belongs to neither quantity, and every count derived from it
    (the segment remainder, the "unaccounted" band) inherits the error. The drops are a
    fact about the LOG, so they travel as their own count.
    """
    panel = _build(_view([_item("it_0"), _item("it_1")], omitted=3))
    assert panel["progress"]["total"] == 2
    assert panel["omitted"] == 3
    # And the segments now account for the whole total, so the template draws no
    # phantom remainder band.
    assert sum(seg["n"] for seg in panel["progress"]["segments"]) == panel["progress"]["total"]


def test_items_are_grouped_by_their_own_state() -> None:
    view = _view([_item("it_0", "open"), _item("it_1", "accepted"), _item("it_2", "accepted")])
    panel = _build(view)
    counts = {seg["name"]: seg["n"] for seg in panel["progress"]["segments"]}
    assert counts["open"] == 1 and counts["accepted"] == 2 and counts["rejected"] == 0


def test_a_pull_request_number_becomes_the_row_id() -> None:
    panel = _build(_view([_item("it_0", pr=14111)]))
    ids = [card["id"] for col in panel["columns"] for card in col["cards"]]
    assert ids == ["#14111"]


def test_an_item_with_no_pull_request_falls_back_to_its_id() -> None:
    panel = _build(_view([_item("it_abcd1234")]))
    ids = [card["id"] for col in panel["columns"] for card in col["cards"]]
    assert ids == ["it_abcd1234"]


# --------------------------------------------------------------------------
# age, and absent-versus-zero
# --------------------------------------------------------------------------


def test_age_is_measured_from_the_newest_work_entry() -> None:
    """One hour after the newest entry reads 3600, not ~0.

    A read-clock age would be about zero on every live read, so the whole idea of a
    stale board would quietly stop existing while the header still rendered a number.
    """
    panel = _build(_view([_item("it_0")]))
    assert panel["meta"]["age_seconds"] == 3600


def test_the_age_comes_from_the_boards_own_newest_entry() -> None:
    """The fold's entry stamp decides the age, not the newest ITEM stamp.

    An item carries only ``created_at``, ``last_report_at`` and ``closed_at``, and a
    conductor's own round touches none of them -- a decision, a verdict, an acceptance,
    a bind. Derived from item stamps, a board that just moved keeps ageing and
    eventually reads as stale while it is current. Here the entry stamp is half an hour
    newer than every item stamp, and the age follows it.
    """
    view = _view([_item("it_0")])
    view["conductor"]["last_entry_at"] = "2026-09-27T12:30:00+00:00"
    assert _build(view)["meta"]["age_seconds"] == 1800


def test_an_item_stamp_is_the_fallback_when_the_fold_carries_no_entry_stamp() -> None:
    """A checkpoint written before the fold carried the key still answers.

    Its items are stamped even though the board is not, so an older answer beats none.
    The newest item stamp wins, which is why the fallback cannot short-circuit on the
    first item it sees.
    """
    view = _view([_item("it_0"), _item("it_1", last_report_at="2026-09-27T12:30:00+00:00")])
    view["conductor"]["last_entry_at"] = ""
    assert _build(view)["meta"]["age_seconds"] == 1800


def test_a_board_with_no_items_is_not_a_board_of_zeros() -> None:
    """Absent and zero are different facts. Age has no answer; it is not 0.

    A crew that has logged nothing must not render a complete board, and an age of 0
    would say the information is current -- the most confident possible lie.
    """
    empty: WorkBoardView = _view([])
    empty["conductor"]["first_entry_at"] = ""
    empty["conductor"]["last_entry_at"] = ""
    panel = _build(empty)
    assert panel["meta"]["age_seconds"] == UNSAID
    assert panel["since"] == UNSAID
    assert panel["progress"]["total"] == 0


def test_a_damaged_timestamp_reads_as_unsaid_not_as_now() -> None:
    """These are bytes a reader does not control, and they stay on disk."""
    view = _view([_item("it_0", created_at="not-a-date", last_report_at=None)])
    view["conductor"]["last_entry_at"] = "not-a-date"
    assert _build(view)["meta"]["age_seconds"] == UNSAID


# --------------------------------------------------------------------------
# the single exit to the island
# --------------------------------------------------------------------------


def test_every_sentinel_becomes_null_in_the_payload() -> None:
    """The template renders ``null`` as "not said"; the sentinel would print as text."""
    payload = panel_payload(_build(_view([_item("it_0")])))
    assert payload["lede"] is None
    assert payload["columns"][0]["cards"][0]["of"] is None
    assert UNSAID not in repr(payload), "a sentinel survived into the island payload"


def test_the_payload_carries_the_contract_version() -> None:
    """A reader can tell which shape it was handed without guessing."""
    assert panel_payload(_build(_view([])))["contract_version"] == CONTRACT_VERSION


def test_the_payload_is_json_serialisable_with_no_nan() -> None:
    """The store serialises with ``allow_nan=False``; a value it refuses loses the panel."""
    import json

    text = json.dumps(panel_payload(_build(_view([_item("it_0")]))), allow_nan=False)
    assert '"contract_version": 1' in text
