"""The ``workstreams`` slot fold: a board per workstream, a cost per task.

The project report asks four questions of one fold -- what is the crew doing,
what did each task cost, what came of it, what needs me -- and the answer that
is NOT already in the ``work`` fold is the cost. A charge lives in the WORKER's
``usage`` entries, keyed by nothing but the unit it was appended to, so these
tests pin the join: a task's credits are its bound worker session's spend, an
unmeasured cost reports ``credits_reported: false`` rather than ``0``, and one
session bound to two items of a board is counted once in that board's total.

They also pin the shape against ``REPORT-SPEC.md``, which another worker's
template reads, and the agentic ``for_you`` value's trip through the existing
``dashboard_write`` type check.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from kiro_crew import dashboard_agentic as agentic
from kiro_crew.crew_log import projection
from kiro_crew.crew_log.entry_types import WORK_ENTRY_TYPE
from kiro_crew.crew_log.schema import Entry
from kiro_crew.dashboard_templates.manifest import FieldSpec, TemplateManifest

CONDUCTOR = "chat-1-lead"
WORKER_A = "chat-2-worker-a"
WORKER_B = "chat-3-worker-b"

#: 2026-10-03T12:00:00Z, so an hour key is predictable in the assertions.
NOON = 1_767_441_600_000
HOUR = 3_600_000

_seq = iter(range(1, 10_000))


def _entry(etype: str, data: dict[str, Any], time: int = NOON) -> Entry:
    return Entry(type=etype, seq=next(_seq), time=time, src="test", data=data)


def _opened(slot: str, time: int = NOON) -> Entry:
    """The one entry that names a unit's session slot."""
    return _entry("session/opened", {"slot": slot}, time)


def _work(action: str, slot: str = CONDUCTOR, time: int = NOON, **data: Any) -> Entry:
    return _entry(WORK_ENTRY_TYPE, {"action": action, "slot": slot, **data}, time)


def _turn(credits: float | None, time: int = NOON, duration_ms: int = 1_000) -> Entry:
    body: dict[str, Any] = {"duration_ms": duration_ms}
    if credits is not None:
        body["credits"] = credits
    return _entry("turn/completed", body, time)


def _fold(entries: list[Entry], slot: str = CONDUCTOR) -> dict[str, Any]:
    """*entries* folded from nothing, bound to *slot* the way a reader asks."""
    state = projection.initial("workstreams")
    bind = projection._FOLDS["workstreams"].bind_slot
    assert bind is not None
    bind(state.state, slot)
    return projection.projection_of(projection.advance(state, entries)).value


def _board(value: dict[str, Any], goal: str) -> dict[str, Any]:
    matched = [row for row in value["items"] if row["goal"] == goal]
    assert len(matched) == 1, f"{goal!r} not in {[r['goal'] for r in value['items']]}"
    return matched[0]


def _task(board: dict[str, Any], title: str) -> dict[str, Any]:
    matched = [row for row in board["tasks"] if row["title"] == title]
    assert len(matched) == 1, f"{title!r} not in {[r['title'] for r in board['tasks']]}"
    return matched[0]


# --------------------------------------------------------------------------- #
# the shape, which another template reads
# --------------------------------------------------------------------------- #


def test_the_fold_is_registered_as_a_slot_fold() -> None:
    """A SLOT fold, so the report reads the boards a crewmate's slot reaches.

    Session-keyed would answer for one unit's own log, and a board's spend is in
    its workers' logs rather than the conductor's.
    """
    assert "workstreams" in projection.SLOT_PROJECTION_NAMES
    assert "workstreams" not in projection.SESSION_FOLD_NAMES
    assert "workstreams" in projection.EAGER_SLOT_FOLD_NAMES


def test_an_empty_fold_states_the_contract_shape() -> None:
    """Nothing folded is still the report's shape, with zeroes rather than gaps."""
    value = _fold([])
    assert value["items"] == []
    assert value["omitted"] == 0
    assert value["series"] == []
    # A fold with no charge says so, which is the same rule the task rows keep.
    assert value["unattributed"] == {"credits": 0.0, "credits_reported": False}


def test_the_rendered_keys_are_the_spec_keys() -> None:
    """The item, task and series rows carry EXACTLY the spec's fields.

    Pinned as sets rather than as a subset check because the template reads these
    by name: a renamed field is a blank column on the page, and an extra one is a
    field no reader was told about.
    """
    value = _fold(
        [
            _opened(CONDUCTOR),
            _work("goal", goal="ship the report", round=4),
            _work("create", item_id="it_1", title="the fold", round=4),
            _work("bind", item_id="it_1", worker_session_key=WORKER_A),
            _opened(WORKER_A),
            _turn(2.5),
        ]
    )
    board = _board(value, "ship the report")
    assert set(board) == {
        "id",
        "goal",
        "round",
        "parent",
        "total",
        "accepted",
        "open",
        "needs_you",
        "credits",
        "credits_reported",
        "last_activity_at",
        "tasks",
        "tasks_omitted",
        "series",
    }
    assert set(_task(board, "the fold")) == {
        "item_id",
        "title",
        "state",
        "status",
        "summary",
        "verdict",
        "pr",
        "round",
        "spender",
        "credits",
        "credits_reported",
        "duration_ms",
    }
    assert set(board["series"][0]) == {"hour", "credits", "accepted"}


def test_the_fold_never_reads_the_whole_log() -> None:
    """``affects`` is the four entry types the fold reads, and nothing wider.

    The kernel wakes a fold only for the types it declares, so this set IS the
    guarantee that a report entry does not cost a pass over every line.
    """
    assert projection.WORKSTREAMS_TYPES == frozenset(
        {WORK_ENTRY_TYPE, "session/opened"}
        | {"turn/completed", "subagent/completed", "subagent/failed", "background/completed"}
    )
    assert projection._FOLDS["workstreams"].affects == projection.WORKSTREAMS_TYPES


# --------------------------------------------------------------------------- #
# the join: a task's cost is its bound worker's spend
# --------------------------------------------------------------------------- #


def test_a_task_is_costed_by_its_bound_worker_session() -> None:
    """The charge is in the WORKER's unit, and reaches the task through the bind."""
    value = _fold(
        [
            _opened(CONDUCTOR),
            _work("goal", goal="two tasks", round=1),
            _work("create", item_id="it_1", title="first", round=1),
            _work("bind", item_id="it_1", worker_session_key=WORKER_A),
            _work("create", item_id="it_2", title="second", round=1),
            _work("bind", item_id="it_2", worker_session_key=WORKER_B),
            _opened(WORKER_A),
            _turn(3.0, duration_ms=2_000),
            _opened(WORKER_B),
            _turn(1.5, duration_ms=500),
        ]
    )
    board = _board(value, "two tasks")
    first = _task(board, "first")
    assert (first["credits"], first["credits_reported"]) == (3.0, True)
    assert first["duration_ms"] == 2_000
    second = _task(board, "second")
    assert (second["credits"], second["credits_reported"]) == (1.5, True)
    assert (board["credits"], board["credits_reported"]) == (4.5, True)


def test_a_bind_folded_after_the_charge_still_costs_the_task() -> None:
    """The join is at RENDER, so a bind recorded late is not a lost cost.

    A board rebuilt from a worker's own baseline has the worker's charges in hand
    before the conductor entry that explains them, and a step-time join would
    drop exactly that board's spend.
    """
    value = _fold(
        [
            _opened(WORKER_A),
            _turn(7.0),
            _opened(CONDUCTOR),
            _work("goal", goal="late bind", round=1),
            _work("create", item_id="it_1", title="worked first", round=1),
            _work("bind", item_id="it_1", worker_session_key=WORKER_A),
        ]
    )
    board = _board(value, "late bind")
    assert _task(board, "worked first")["credits"] == 7.0
    assert board["credits"] == 7.0


def test_one_worker_on_two_items_is_counted_once_in_the_board_total() -> None:
    """A session spent its credits ONCE however many items it served.

    Each task row reports that session's spend, because that is what the task
    cost to run; the board adds up DISTINCT sessions, so the total is a bill
    rather than a bill times the number of items.
    """
    value = _fold(
        [
            _opened(CONDUCTOR),
            _work("goal", goal="one worker twice", round=1),
            _work("create", item_id="it_1", title="first", round=1),
            _work("bind", item_id="it_1", worker_session_key=WORKER_A),
            _work("create", item_id="it_2", title="second", round=1),
            _work("bind", item_id="it_2", worker_session_key=WORKER_A),
            _opened(WORKER_A),
            _turn(4.0),
        ]
    )
    board = _board(value, "one worker twice")
    assert _task(board, "first")["credits"] == 4.0
    assert _task(board, "second")["credits"] == 4.0
    assert board["credits"] == 4.0


# --------------------------------------------------------------------------- #
# the nesting link: which task a nested board hangs under
# --------------------------------------------------------------------------- #


def _nested() -> list[Entry]:
    """A board whose task bound a worker that runs a board of its own.

    The sub-board's slot IS ``WORKER_A``, which is the identity the parent bound, so
    the two boards are one tree with ``it_1`` as the joint.
    """
    return [
        _opened(CONDUCTOR),
        _work("goal", goal="the epic", round=4),
        _work("create", item_id="it_1", title="the task", round=4),
        _work("bind", item_id="it_1", worker_session_key=WORKER_A),
        _opened(WORKER_A),
        _turn(2.0),
        _work("goal", slot=WORKER_A, goal="the sub board", round=1),
        _work("create", slot=WORKER_A, item_id="it_s1", title="a subtask", round=1),
        _work("bind", slot=WORKER_A, item_id="it_s1", worker_session_key=WORKER_B),
        _opened(WORKER_B),
        _turn(1.0),
    ]


def test_a_nested_board_names_the_task_it_hangs_under() -> None:
    """The SUBTASK link, which is what makes the board list a tree.

    A page drawing epic/story/task/subtask has to know that the sub-board belongs
    under one task of another board, and the only record of that is the bind the
    parent wrote. Reading it back as ``parent`` is what saves the page from
    re-deriving a session identity out of a board id.
    """
    value = _fold(_nested())
    # The parent names its board by the SAME alias that board's own `id` carries, so
    # the tree can find it; a raw id here would be the conductor's session key.
    parent = _board(value, "the sub board")["parent"]
    assert parent == {
        "board": _board(value, "the epic")["id"],
        "item_id": "it_1",
        "title": "the task",
    }
    assert CONDUCTOR not in parent["board"]


def test_a_board_nobody_bound_has_no_parent() -> None:
    """``None``, so a root is a root rather than a board with a missing link."""
    value = _fold(_nested())
    assert _board(value, "the epic")["parent"] is None


def test_the_parent_link_survives_a_bind_folded_after_the_sub_board() -> None:
    """The link is read at RENDER, so the fold order of the bind cannot lose it.

    The sub-board's own entries can reach this fold before its parent's bind does --
    a board rebuilt from a worker's baseline, a conductor unit read second -- and a
    link recorded in the step would be absent for exactly those boards.
    """
    entries = [
        _opened(WORKER_A),
        _work("goal", slot=WORKER_A, goal="the sub board", round=1),
        _work("create", slot=WORKER_A, item_id="it_s1", title="a subtask", round=1),
        _opened(CONDUCTOR),
        _work("goal", goal="the epic", round=4),
        _work("create", item_id="it_1", title="the task", round=4),
        _work("bind", item_id="it_1", worker_session_key=WORKER_A),
    ]
    value = _fold(entries)
    # The parent names its board by the SAME alias that board's own `id` carries, so
    # the tree can find it; a raw id here would be the conductor's session key.
    parent = _board(value, "the sub board")["parent"]
    assert parent == {
        "board": _board(value, "the epic")["id"],
        "item_id": "it_1",
        "title": "the task",
    }
    assert CONDUCTOR not in parent["board"]


def test_a_board_whose_own_task_bound_its_own_slot_is_not_its_own_subtask() -> None:
    """A self-bind is not a nesting. Drawn as a root, because it has no parent
    anywhere else, and a node that is its own parent is a cycle the tree cannot
    draw at all."""
    value = _fold(
        [
            _opened(CONDUCTOR),
            _work("goal", goal="binds itself", round=1),
            _work("create", item_id="it_1", title="its own worker", round=1),
            _work("bind", item_id="it_1", worker_session_key=CONDUCTOR),
        ]
    )
    assert _board(value, "binds itself")["parent"] is None


def test_a_worker_bound_twice_names_one_parent() -> None:
    """First bind wins, in board then item order, so the tree has one shape.

    A worker serving two tasks could hang under either; picking the first keeps the
    answer the same on every read, which a reader comparing two renders depends on.
    """
    entries = [
        _opened(CONDUCTOR),
        _work("goal", goal="the epic", round=1),
        _work("create", item_id="it_1", title="first", round=1),
        _work("bind", item_id="it_1", worker_session_key=WORKER_A),
        _work("create", item_id="it_2", title="second", round=1),
        _work("bind", item_id="it_2", worker_session_key=WORKER_A),
        _opened(WORKER_A),
        _work("goal", slot=WORKER_A, goal="the sub board", round=1),
        _work("create", slot=WORKER_A, item_id="it_s1", title="a subtask", round=1),
    ]
    parent = _board(_fold(entries), "the sub board")["parent"]
    assert parent is not None
    assert parent["item_id"] == "it_1"


def test_a_task_carries_the_session_a_roll_up_must_bill_once() -> None:
    """``spender`` on the row, which is what a subtree total needs.

    Each task row reports its worker's WHOLE spend, so a tree adding its rows up
    would bill one session once per task it served -- the error this fold already
    refuses to make in a board total. The alias is how a reader reproduces that rule:
    4.0 across both rows is one spender's 4.0, not 8.0.
    """
    value = _fold(
        [
            _opened(CONDUCTOR),
            _work("goal", goal="one worker twice", round=1),
            _work("create", item_id="it_1", title="first", round=1),
            _work("bind", item_id="it_1", worker_session_key=WORKER_A),
            _work("create", item_id="it_2", title="second", round=1),
            _work("bind", item_id="it_2", worker_session_key=WORKER_A),
            _work("create", item_id="it_3", title="unbound", round=1),
            _opened(WORKER_A),
            _turn(4.0),
        ]
    )
    board = _board(value, "one worker twice")
    # ONE token for one worker across both rows, and it is not the session key: the
    # roll-up needs only that the two rows match each other.
    first, second = _task(board, "first")["spender"], _task(board, "second")["spender"]
    assert first == second
    assert WORKER_A not in {first, second}
    distinct = {row["spender"]: row["credits"] for row in board["tasks"] if row["credits_reported"]}
    assert sum(distinct.values()) == board["credits"] == 4.0
    # No bind, so nothing to bill it through -- and that is also why it has no cost.
    assert _task(board, "unbound")["spender"] is None
    assert _task(board, "unbound")["credits_reported"] is False


def test_the_subjects_own_board_leads_a_tie_on_last_activity() -> None:
    """The ordering question is answered from the SLOT, not from the rendered id.

    The id a row carries is a per-render alias by design, so it holds no slot. A
    tie-break comparing it against the subject's slot can never be true, and the
    order then falls back to insertion with the promise silently unkept: the
    crewmate's own workstream can sit below a sub-board that shares its stamp.
    """
    entries = [
        _opened(WORKER_A),
        _work("goal", slot=WORKER_A, goal="the sub board", round=1),
        _work("create", slot=WORKER_A, item_id="it_s1", title="a subtask", round=1),
        _opened(CONDUCTOR),
        _work("goal", goal="the epic", round=4),
        _work("create", item_id="it_1", title="the task", round=4),
        _work("bind", item_id="it_1", worker_session_key=WORKER_A),
    ]
    value = _fold(entries)
    stamps = {b["goal"]: b["last_activity_at"] for b in value["items"]}
    assert stamps["the epic"] == stamps["the sub board"], (
        "the two boards do not share a stamp, so this fixture cannot exercise the "
        "tie-break at all"
    )
    assert value["items"][0]["goal"] == "the epic", (
        "the subject's own board did not lead the tie, so the order fell back to "
        "insertion and the tie-break is not reading the slot"
    )
    # The ordering fact does not reach the page: the rendered keys are an exact set.
    assert "_own" not in value["items"][0]


def test_the_render_carries_no_third_party_session_key() -> None:
    """The payload is embedded in a page any dashboard caller can read.

    The conductor ledger's rule is that no reader but the conductor sees a session
    key, so this is checked over the WHOLE rendered value rather than on the task
    rows alone: a key that reappears on a board id or a parent link is just as
    readable, and a per-field assertion would not see it. Both of those ARE boards
    keyed by their conductor's slot, which is why they carry aliases too.

    The envelope's own ``slot`` is excluded deliberately, and it is the only
    exclusion: it names the SUBJECT this projection belongs to rather than a third
    party, every slot fold's render carries it, and no template reads it. Narrowing
    the subject's own identity is a change to the fold envelope shared by all of
    them, not to what this one render shapes.
    """
    value = _fold(
        [
            _opened(CONDUCTOR),
            _work("goal", goal="no keys on the page", round=1),
            _work("create", item_id="it_1", title="first", round=1),
            _work("bind", item_id="it_1", worker_session_key=WORKER_A),
            _work("create", item_id="it_2", title="second", round=1),
            _work("bind", item_id="it_2", worker_session_key=WORKER_B),
            _opened(WORKER_A),
            _turn(4.0),
        ]
    )
    board = _board(value, "no keys on the page")
    rendered = json.dumps({k: v for k, v in value.items() if k != "slot"})
    for key in (WORKER_A, WORKER_B, CONDUCTOR):
        assert key not in rendered, f"the rendered fold carries the session key {key!r}"
    assert "worker_session_key" not in rendered
    # Controls, so the check above cannot pass on an empty render: the aliases ARE
    # there, and the board carries one too rather than its conductor's slot.
    assert {row["spender"] for row in board["tasks"]} == {"1", "2"}
    assert board["id"] == "board-1"


@pytest.mark.parametrize(
    "worker_ran, why",
    [
        (False, "no worker bound at all"),
        (True, "a worker whose provider billed no credits"),
    ],
)
def test_an_unmeasured_cost_is_false_and_never_zero(worker_ran: bool, why: str) -> None:
    """``credits_reported: false`` is the only honest answer to an absent charge.

    ``0`` would read as "this task was free", which is a claim the fold cannot
    make: a provider that does not bill in credits and a task nobody has started
    both produce no charge.
    """
    entries = [
        _opened(CONDUCTOR),
        _work("goal", goal="unmeasured", round=1),
        _work("create", item_id="it_1", title="uncosted", round=1),
        _work("bind", item_id="it_1", worker_session_key=WORKER_A),
    ]
    if worker_ran:
        # The worker ran its turn; the closer carries a duration and no credits.
        entries += [_opened(WORKER_A), _turn(None)]
    value = _fold(entries)
    board = _board(value, "unmeasured")
    task = _task(board, "uncosted")
    assert task["credits_reported"] is False, why
    assert board["credits_reported"] is False, why


def test_a_charge_with_no_opener_is_stated_not_assigned() -> None:
    """A charge whose unit named no session belongs to no task.

    Counted under ``unattributed`` so the page can say the total is short, rather
    than folded into whichever board happened to be open.
    """
    value = _fold([_turn(9.0), _opened(CONDUCTOR), _work("goal", goal="g", round=1)])
    assert value["unattributed"] == {"credits": 9.0, "credits_reported": True}
    assert _board(value, "g")["credits_reported"] is False


# --------------------------------------------------------------------------- #
# what came of it, and what needs me
# --------------------------------------------------------------------------- #


def test_the_counts_answer_what_is_done_and_what_needs_me() -> None:
    """``accepted``/``open`` come from the item's state, ``needs_you`` from its status."""
    value = _fold(
        [
            _opened(CONDUCTOR),
            _work("goal", goal="counts", round=2),
            _work("create", item_id="it_1", title="landed", round=2),
            _work("close", item_id="it_1", state="accepted"),
            _work("create", item_id="it_2", title="asking", round=2),
            _work("report", actor="worker", item_id="it_2", status="question", summary="which?"),
            _work("create", item_id="it_3", title="stuck", round=2),
            _work("report", actor="worker", item_id="it_3", status="blocked", summary="no creds"),
            _work("create", item_id="it_4", title="running", round=2),
            _work("report", actor="worker", item_id="it_4", status="progress", summary="moving"),
        ]
    )
    board = _board(value, "counts")
    assert (board["total"], board["accepted"], board["needs_you"]) == (4, 1, 2)
    # ``open`` is the state, so the accepted item is out and the other three in.
    assert board["open"] == 3
    assert _task(board, "asking")["summary"] == "which?"


def test_an_item_moved_out_of_accepted_takes_its_hour_back() -> None:
    """The accepted-per-hour line is a DIFFERENCE, so a reversal is drawn.

    A counter only ever incremented would leave the chart claiming an acceptance
    the board does not hold.
    """
    value = _fold(
        [
            _opened(CONDUCTOR),
            _work("goal", goal="reversed", round=1),
            _work("create", item_id="it_1", title="once accepted", round=1),
            _work("close", item_id="it_1", state="accepted"),
            _work("close", item_id="it_1", state="rejected", time=NOON + HOUR),
        ]
    )
    board = _board(value, "reversed")
    assert board["accepted"] == 0
    assert sum(point["accepted"] for point in board["series"]) == 0


def test_the_series_buckets_credits_by_the_hour_they_fell_in() -> None:
    """One bar per UTC hour, oldest first, for the board and across boards."""
    value = _fold(
        [
            _opened(CONDUCTOR),
            _work("goal", goal="hourly", round=1),
            _work("create", item_id="it_1", title="costed", round=1),
            _work("bind", item_id="it_1", worker_session_key=WORKER_A),
            _opened(WORKER_A),
            _turn(1.0, time=NOON),
            _turn(2.0, time=NOON + HOUR),
        ]
    )
    board = _board(value, "hourly")
    assert [(p["hour"], p["credits"]) for p in board["series"] if p["credits"]] == [
        ("2026-01-03T12:00Z", 1.0),
        ("2026-01-03T13:00Z", 2.0),
    ]
    assert sum(point["credits"] for point in value["series"]) == 3.0


def test_two_boards_are_two_rows_newest_activity_first() -> None:
    """One row per board, which is one per workstream the slot reaches."""
    value = _fold(
        [
            _opened(CONDUCTOR),
            _work("goal", slot=CONDUCTOR, goal="older", round=1, generation="g1"),
            _work(
                "goal",
                slot=CONDUCTOR,
                goal="newer",
                round=1,
                generation="g2",
                time=NOON + HOUR,
            ),
        ]
    )
    assert [row["goal"] for row in value["items"]] == ["newer", "older"]


def test_a_slot_reused_for_a_second_board_keeps_them_apart() -> None:
    """The board key is ``(slot, generation)``.

    The log is append-only, so a slot whose first board was purged still carries
    both boards' entries; keying on the slot alone would merge a finished
    workstream's tasks into the current one.
    """
    value = _fold(
        [
            _opened(CONDUCTOR),
            _work("goal", goal="first board", round=1, generation="g1"),
            _work("create", item_id="it_1", title="old task", round=1, generation="g1"),
            _work("goal", goal="second board", round=1, generation="g2"),
            _work("create", item_id="it_2", title="new task", round=1, generation="g2"),
        ]
    )
    assert len(value["items"]) == 2
    assert [task["title"] for task in _board(value, "second board")["tasks"]] == ["new task"]


# --------------------------------------------------------------------------- #
# the caps, which are what make the fold's state bounded
# --------------------------------------------------------------------------- #


def test_tasks_past_the_cap_are_counted_rather_than_dropped_silently() -> None:
    """A board that ran more than the page shows SAYS so."""
    entries = [_opened(CONDUCTOR), _work("goal", goal="many", round=1)]
    over = projection.WORKSTREAMS_TASK_LIMIT + 3
    for index in range(over):
        entries.append(_work("create", item_id=f"it_{index}", title=f"task {index}", round=1))
    board = _board(_fold(entries), "many")
    assert len(board["tasks"]) == projection.WORKSTREAMS_TASK_LIMIT
    assert board["tasks_omitted"] == 3


def test_boards_past_the_cap_are_counted_at_the_top_level() -> None:
    entries = [_opened(CONDUCTOR)]
    over = projection.WORKSTREAMS_BOARD_LIMIT + 2
    for index in range(over):
        entries.append(_work("goal", goal=f"board {index}", round=1, generation=f"g{index}"))
    value = _fold(entries)
    assert len(value["items"]) == projection.WORKSTREAMS_BOARD_LIMIT
    assert value["omitted"] == 2


def test_the_series_window_keeps_the_newest_hours() -> None:
    """Hours past the window fall off the FRONT, so the chart stays current."""
    entries: list[Entry] = [
        _opened(CONDUCTOR),
        _work("goal", goal="long run", round=1),
        _work("create", item_id="it_1", title="costed", round=1),
        _work("bind", item_id="it_1", worker_session_key=WORKER_A),
        _opened(WORKER_A),
    ]
    over = projection.WORKSTREAMS_SERIES_LIMIT + 5
    for index in range(over):
        entries.append(_turn(1.0, time=NOON + index * HOUR))
    board = _board(_fold(entries), "long run")
    assert len(board["series"]) == projection.WORKSTREAMS_SERIES_LIMIT
    # The LAST hour written is present; the first is not.
    assert board["series"][-1]["hour"] == projection._workstreams_hour(NOON + (over - 1) * HOUR)
    assert board["series"][0]["hour"] != projection._workstreams_hour(NOON)


def test_the_row_counter_grows_with_the_state() -> None:
    """A slot fold's warm memo charges by rows, so the counter must see them.

    A constant here would under-charge the fold's own growth forever, which is
    the check ``_UNCOUNTED_SLOT_FOLDS`` exists to make at import.
    """
    fold = projection._FOLDS["workstreams"]
    assert fold.count_rows is not None
    empty = projection.initial("workstreams")
    small = projection.advance(
        empty,
        [_opened(CONDUCTOR), _work("goal", goal="g", round=1)],
    )
    bigger = projection.advance(
        small,
        [_work("create", item_id=f"it_{i}", title=f"t{i}", round=1) for i in range(5)],
    )
    assert fold.count_rows(bigger.state) > fold.count_rows(small.state)
    assert fold.count_rows(small.state) > fold.count_rows(empty.state)


def test_the_step_does_not_edit_the_state_it_was_given() -> None:
    """The kernel resumes from a savepoint, so a step that mutates in place
    corrupts the checkpoint it was read from."""
    before = projection.advance(
        projection.initial("workstreams"),
        [_opened(CONDUCTOR), _work("goal", goal="held", round=1)],
    )
    held = before.state["boards"][CONDUCTOR]
    held_items = held["items"]
    projection.advance(before, [_work("create", item_id="it_1", title="added", round=1)])
    assert held_items == {}, "the earlier state's board grew an item"


# --------------------------------------------------------------------------- #
# the unit walk: every board the fold SHOWS it must be able to COST
# --------------------------------------------------------------------------- #


class _Handle:
    """Just enough of a log handle for the walk: entries, read from one."""

    def __init__(self, entries: list[Entry]) -> None:
        self._entries = entries

    def iter_from(self, _seq: int, known: Any = None) -> "list[Entry]":
        return self._entries


def _chain(monkeypatch: pytest.MonkeyPatch, binds: dict[str, list[tuple[str, str]]]) -> None:
    """Wire a bind graph: *binds* maps a conductor slot to its (item, worker) binds.

    One unit per slot, named for it, which is what the walk's dedupe keys on.
    """
    logs = {
        f"u-{conductor}": _Handle(
            [_opened(conductor)]
            + [
                _work("bind", slot=conductor, item_id=item, worker_session_key=worker)
                for item, worker in rows
            ]
        )
        for conductor, rows in binds.items()
    }
    monkeypatch.setattr(projection, "open_session_log", lambda unit: logs.get(unit))
    monkeypatch.setattr(
        projection.session_ledger,
        "work_crew_log_units",
        lambda slot: (f"u-{slot}",) if f"u-{slot}" in logs else (),
    )


def test_a_board_three_conductors_down_is_costable_not_only_listed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The walk runs to CLOSURE, because visible and costable are one round apart.

    A board shows once its conductor's units are read, and costs once that
    conductor's own workers are read. Any fixed round count therefore has a last
    round whose boards it lists with no reachable spend -- and the bind that
    would have costed them is sitting in a log the walk already opened. Here the
    lead reaches ``sub-worker`` only on the third round.
    """
    _chain(
        monkeypatch,
        {
            CONDUCTOR: [("it_1", WORKER_A)],
            WORKER_A: [("it_2", "sub-lead")],
            "sub-lead": [("it_3", "sub-worker")],
            "sub-worker": [],
        },
    )
    units = projection._workstreams_units(CONDUCTOR)
    assert units == (
        f"u-{CONDUCTOR}",
        f"u-{WORKER_A}",
        "u-sub-lead",
        "u-sub-worker",
    ), "the walk stopped before the deepest board's own worker"


def test_the_walk_stops_at_the_unit_ceiling_not_at_a_depth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closure is bounded by units, so a long chain is a bounded read.

    The ceiling is the only bound the walk has, which is what makes removing the
    round count safe rather than an unbounded read.
    """
    depth = projection.WORKSTREAMS_UNIT_LIMIT + 20
    _chain(
        monkeypatch,
        {f"s{i}": [(f"it_{i}", f"s{i + 1}")] for i in range(depth)} | {f"s{depth}": []},
    )
    units = projection._workstreams_units("s0")
    assert len(units) == projection.WORKSTREAMS_UNIT_LIMIT


def test_a_bind_cycle_does_not_walk_forever(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two conductors binding each other is a cycle the dedupe already closes."""
    _chain(
        monkeypatch,
        {
            CONDUCTOR: [("it_1", WORKER_A)],
            WORKER_A: [("it_2", CONDUCTOR)],
        },
    )
    assert projection._workstreams_units(CONDUCTOR) == (
        f"u-{CONDUCTOR}",
        f"u-{WORKER_A}",
    )


# --------------------------------------------------------------------------- #
# the agentic value the lead writes beside the fold
# --------------------------------------------------------------------------- #


def _manifest(ftype: str = "array") -> TemplateManifest:
    """A manifest declaring ``for_you`` the way the report template does."""
    return TemplateManifest(
        id="project-report",
        version=1,
        title="Project report",
        description="what the crew did, what it cost, what needs me",
        source="builtin",
        fields={
            "for_you": FieldSpec("for_you", ftype, None, None, True),
            "workstreams": FieldSpec("workstreams", "object", "workstreams", None, False),
        },
    )


def test_for_you_is_written_through_the_existing_type_check() -> None:
    """An ARRAY of ``{text, workstream}``, accepted by the agentic check as is.

    No new validator: ``for_you`` is an agentic array like any other, so the
    lead's lines reach the page through the path every agentic value takes.
    """
    lines = [
        {"text": "the report page wants a look", "workstream": "dynamic dashboard"},
        {"text": "rule on the cost source", "workstream": "dynamic dashboard"},
    ]
    payload = agentic.check_write(
        agentic.Instance(manifest=_manifest(), instance_version=1), "for_you", lines
    )
    assert payload["field"] == "for_you"
    assert payload["type"] == "array"
    # WRAPPED under ``v``: the crew-log entry declares ``value`` as an object, so
    # every agentic type travels the same way. The lines go through unchanged.
    assert payload["value"] == {"v": lines}


def test_a_for_you_that_is_not_an_array_is_refused() -> None:
    """The refusal names the type, so the retry is the agent's to make."""
    with pytest.raises(agentic.WriteRefused) as caught:
        agentic.check_write(
            agentic.Instance(manifest=_manifest(), instance_version=1),
            "for_you",
            "one line, not a list",
        )
    assert caught.value.field == "for_you"
    assert "array" in str(caught.value)


def test_the_fold_backed_field_is_not_agentic() -> None:
    """The lead may write ``for_you`` and NOT the fold the page reads beside it.

    The boards and their costs are the log's own record; a field an agent could
    overwrite would make the report's numbers its own claim.
    """
    with pytest.raises(agentic.WriteRefused) as caught:
        agentic.check_write(
            agentic.Instance(manifest=_manifest(), instance_version=1),
            "workstreams",
            {"items": []},
        )
    assert caught.value.field == "workstreams"
    assert set(agentic.agentic_fields(_manifest())) == {"for_you"}
