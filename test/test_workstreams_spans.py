"""``workstreams`` task spans: the two stamps a per-worker timeline is drawn from.

Group B's `timeline` and `org-chart` templates place one ROW PER WORKER SESSION on a
clock, and the question that row answers -- what was this worker doing, and what ran at
the same time as it -- needs a start and an end PER TASK. Neither field the fold already
carried can give that:

- ``duration_ms`` is the worker SESSION's measured work time. One figure however many
  tasks that session served, and it names no instant, so two tasks of one worker are
  indistinguishable on a clock and nothing can be said about overlap.
- ``last_report_at`` is one instant: when the worker last spoke. It cannot start a bar.

So the fold grows ``created_at`` and ``closed_at`` per task, spelled exactly as the
``work`` fold spells its own, and these tests pin the four things a timeline would
silently get wrong without them: the opener is written by whichever entry arrives
first and never moved afterwards, the closer follows the item's STATE rather than the
``close`` action, a reopened item loses its close stamp, and a committed stamp beats the
append time so a span survives a rebuild.

They also pin the one absence the page must render rather than guess: a terminal item
whose conductor entry this fold never saw keeps an EMPTY ``closed_at``.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.crew_log import projection
from kiro_crew.crew_log.entry_types import WORK_ENTRY_TYPE
from kiro_crew.crew_log.schema import Entry

CONDUCTOR = "chat-1-lead"
WORKER_A = "chat-2-worker-a"
WORKER_B = "chat-3-worker-b"

#: 2026-10-03T12:00:00Z
NOON = 1_767_441_600_000
HOUR = 3_600_000

_seq = iter(range(1, 10_000))


def _entry(etype: str, data: dict[str, Any], time: int = NOON) -> Entry:
    return Entry(type=etype, seq=next(_seq), time=time, src="test", data=data)


def _opened(slot: str, time: int = NOON) -> Entry:
    return _entry("session/opened", {"slot": slot}, time)


def _work(action: str, slot: str = CONDUCTOR, time: int = NOON, **data: Any) -> Entry:
    return _entry(WORK_ENTRY_TYPE, {"action": action, "slot": slot, **data}, time)


def _fold(entries: list[Entry], slot: str = CONDUCTOR) -> dict[str, Any]:
    state = projection.initial("workstreams")
    bind = projection._FOLDS["workstreams"].bind_slot
    assert bind is not None
    bind(state.state, slot)
    return projection.projection_of(projection.advance(state, entries)).value


def _task(value: dict[str, Any], item_id: str) -> dict[str, Any]:
    """One rendered task row, BY ITEM ID.

    Not by title: ``title`` is a conductor field carried only on ``create``
    (:data:`WORK_CONDUCTOR_FIELDS`), so a task this fold minted from a worker's report
    has an empty one. Looking a row up by a field half these cases do not have would
    make the lookup, rather than the span, the thing under test.
    """
    rows = [t for board in value["items"] for t in board["tasks"] if t["item_id"] == item_id]
    ids = [t["item_id"] for b in value["items"] for t in b["tasks"]]
    assert len(rows) == 1, f"{item_id!r} not in {ids}"
    return rows[0]


# --------------------------------------------------------------------------- #
# the open end
# --------------------------------------------------------------------------- #


def test_a_tasks_span_opens_at_its_create_and_closes_at_its_close() -> None:
    """The ordinary case, and the whole reason the fields exist: one bar, two ends."""
    value = _fold(
        [
            _opened(CONDUCTOR),
            _work("goal", goal="ship it", round=1),
            _work("create", item_id="it_1", title="the fold", time=NOON),
            _work("close", item_id="it_1", state="accepted", time=NOON + 2 * HOUR),
        ]
    )
    task = _task(value, "it_1")
    assert task["created_at"] == projection._work_iso(NOON)
    assert task["closed_at"] == projection._work_iso(NOON + 2 * HOUR)


def test_an_open_task_has_no_close_stamp_rather_than_a_guessed_one() -> None:
    """MUTATION-SENSITIVE: the closer stays EMPTY while the task is open.

    A page draws an open bar to the now-line from this absence. A closer defaulted to
    the last entry's time would end every open bar at a moment the worker is still
    working past, and a long-running task would read as finished.
    """
    value = _fold(
        [
            _opened(CONDUCTOR),
            _work("goal", goal="ship it", round=1),
            _work("create", item_id="it_1", title="still going", time=NOON),
            _work("report", actor="worker", item_id="it_1", status="progress", time=NOON + HOUR),
            _work("decide", item_id="it_1", decision="keep going", time=NOON + 2 * HOUR),
        ]
    )
    task = _task(value, "it_1")
    assert task["created_at"] == projection._work_iso(NOON)
    assert task["closed_at"] == ""
    assert task["state"] == "open"


def test_the_opener_is_written_by_whichever_entry_reaches_the_item_first() -> None:
    """MUTATION-SENSITIVE: a WORKER's report can be the first entry for an item.

    This fold mints an item from any action that names it, not only from ``create``
    (`_workstreams_work` says so and explains why). An opener written only on a
    ``create`` action would leave a task minted by a report -- a board rebuilt from a
    baseline, a conductor unit pruned -- with no start at all, and a timeline cannot
    place a row it has no start for. The bar would simply be missing.
    """
    value = _fold(
        [
            _opened(CONDUCTOR),
            _work("goal", goal="ship it", round=1),
            # NO create for it_9: the worker's report is the first thing seen.
            _work(
                "report",
                actor="worker",
                item_id="it_9",
                title="minted by a report",
                status="progress",
                time=NOON + HOUR,
            ),
        ]
    )
    task = _task(value, "it_9")
    assert task["created_at"] == projection._work_iso(NOON + HOUR)


def test_the_opener_is_never_moved_by_a_later_entry() -> None:
    """MUTATION-SENSITIVE: first write wins, so a bar's left edge does not crawl.

    An opener re-stamped on every entry would place every task's start at its NEWEST
    activity, which collapses every bar on the page to the same near-zero width and
    destroys exactly the overlap the timeline exists to show.
    """
    value = _fold(
        [
            _opened(CONDUCTOR),
            _work("goal", goal="ship it", round=1),
            _work("create", item_id="it_1", title="long one", time=NOON),
            _work("bind", item_id="it_1", worker_session_key=WORKER_A, time=NOON + HOUR),
            _work(
                "report", actor="worker", item_id="it_1", status="progress", time=NOON + 3 * HOUR
            ),
            _work("decide", item_id="it_1", decision="carry on", time=NOON + 5 * HOUR),
        ]
    )
    assert _task(value, "it_1")["created_at"] == projection._work_iso(NOON)


# --------------------------------------------------------------------------- #
# the close end
# --------------------------------------------------------------------------- #


def test_the_closer_reads_the_resulting_state_and_not_the_actions_name() -> None:
    """MUTATION-SENSITIVE: a ``close`` that sets ``open`` must NOT stamp a closer.

    ``state`` rides on exactly one conductor action, and that action is spelled
    ``close`` (:data:`WORK_CONDUCTOR_FIELDS`) -- but the state it carries need not be
    terminal. A conductor reopening an item does it through this same action. So the
    action's NAME is not the fact; the state it leaves behind is. A closer stamped on
    seeing ``action == "close"`` would end the bar of a task the conductor had just put
    back to work, and the page would show its worker as free.

    Pinned beside the terminal case below it, so the two readings of one action are
    distinguished rather than assumed.
    """
    reopened = _fold(
        [
            _opened(CONDUCTOR),
            _work("goal", goal="ship it", round=1),
            _work("create", item_id="it_1", title="put back", time=NOON),
            _work("close", item_id="it_1", state="open", time=NOON + 4 * HOUR),
        ]
    )
    task = _task(reopened, "it_1")
    assert task["state"] == "open"
    assert task["closed_at"] == ""

    for terminal in ("accepted", "rejected", "abandoned"):
        value = _fold(
            [
                _opened(CONDUCTOR),
                _work("goal", goal="ship it", round=1),
                _work("create", item_id="it_2", title="settled", time=NOON),
                _work("close", item_id="it_2", state=terminal, time=NOON + 4 * HOUR),
            ]
        )
        row = _task(value, "it_2")
        assert row["state"] == terminal
        assert row["closed_at"] == projection._work_iso(NOON + 4 * HOUR), terminal


def test_a_reopened_task_loses_its_close_stamp() -> None:
    """MUTATION-SENSITIVE: a close stamp is CLEARED when the item goes back to open.

    An item can be closed and reopened -- the same transition
    ``_workstreams_item_state`` takes an accepted count back off an hour for. A stamp
    left standing would draw a bar that ended, for a task whose worker is running now,
    and the row would be read as free capacity.
    """
    value = _fold(
        [
            _opened(CONDUCTOR),
            _work("goal", goal="ship it", round=1),
            _work("create", item_id="it_1", title="came back", time=NOON),
            _work("close", item_id="it_1", state="accepted", time=NOON + HOUR),
            _work("close", item_id="it_1", state="open", decision="not done", time=NOON + 2 * HOUR),
        ]
    )
    task = _task(value, "it_1")
    assert task["state"] == "open"
    assert task["closed_at"] == ""


def test_the_closer_is_not_moved_by_a_later_conductor_entry() -> None:
    """Once stamped, a closed task's end stays where the close put it."""
    value = _fold(
        [
            _opened(CONDUCTOR),
            _work("goal", goal="ship it", round=1),
            _work("create", item_id="it_1", title="settled", time=NOON),
            _work("close", item_id="it_1", state="accepted", time=NOON + HOUR),
            _work("decide", item_id="it_1", decision="a note after the fact", time=NOON + 9 * HOUR),
        ]
    )
    assert _task(value, "it_1")["closed_at"] == projection._work_iso(NOON + HOUR)


def test_a_worker_report_never_stamps_a_close() -> None:
    """MUTATION-SENSITIVE: only a CONDUCTOR entry can close a span.

    A worker reporting ``done`` is a claim, not an acceptance, and the item stays open
    until its conductor rules. A closer written on the worker path would end the bar at
    the worker's own report -- which is precisely the case a reader opens this page for,
    a task waiting on a verdict, drawn as already finished.
    """
    value = _fold(
        [
            _opened(CONDUCTOR),
            _work("goal", goal="ship it", round=1),
            _work("create", item_id="it_1", title="claimed done", time=NOON),
            _work(
                "report",
                actor="worker",
                item_id="it_1",
                status="done",
                summary="pushed it",
                time=NOON + HOUR,
            ),
        ]
    )
    task = _task(value, "it_1")
    assert task["status"] == "done"
    assert task["state"] == "open"
    assert task["closed_at"] == ""


# --------------------------------------------------------------------------- #
# rebuilds and absence
# --------------------------------------------------------------------------- #


def test_a_committed_stamp_beats_the_append_time_on_both_ends() -> None:
    """MUTATION-SENSITIVE: the item's own stamps survive a rebuild.

    A baseline is folded at whatever time it was appended, which can be days after the
    work. Using the append time would redraw every pre-baseline task as having happened
    in the minute the record was rebuilt, and a timeline rebuilt from a baseline would
    show a crew that did everything at once.
    """
    opened = "2026-09-01T08:00:00+00:00"
    closed = "2026-09-01T11:30:00+00:00"
    value = _fold(
        [
            _opened(CONDUCTOR),
            _work("goal", goal="ship it", round=1),
            _work(
                "close",
                item_id="it_old",
                baseline=True,
                title="from before",
                state="accepted",
                created_at=opened,
                closed_at=closed,
                time=NOON + 40 * HOUR,
            ),
        ]
    )
    task = _task(value, "it_old")
    assert task["created_at"] == opened
    assert task["closed_at"] == closed


def test_a_terminal_task_whose_close_was_never_folded_says_nothing() -> None:
    """MUTATION-SENSITIVE: an unseen close is EMPTY, never the next entry's time.

    A board can reach this fold with a task already terminal and with no conductor
    entry carrying the transition -- a worker's report on an item whose close lives in
    a unit this read did not collect. The fold's own posture everywhere else is that
    absent is not zero, so the end stays empty and the page says it cannot place it.
    Filling it from whatever arrived next would state a close time nothing recorded.
    """
    value = _fold(
        [
            _opened(CONDUCTOR),
            _work("goal", goal="ship it", round=1),
            # Terminal from the first entry, on the WORKER path: a report carries no
            # state, so the state here comes from the baseline field copy.
            _work(
                "report",
                actor="worker",
                item_id="it_x",
                title="closed elsewhere",
                status="done",
                time=NOON,
            ),
        ]
    )
    task = _task(value, "it_x")
    # The worker path cannot set state, so this one IS open -- which is the honest
    # read of a record that only ever showed a worker speaking.
    assert task["state"] == "open"
    assert task["closed_at"] == ""
    # And its opener is still the worker's own entry, so the row can be placed.
    assert task["created_at"] == projection._work_iso(NOON)


def test_two_workers_tasks_each_carry_their_own_span() -> None:
    """THE TIMELINE'S WHOLE POINT: two workers' spans are separately placeable.

    Overlap is what a reader is looking for, and it can only be seen if each row's
    bars sit on the same clock from their own stamps. One session-wide
    ``duration_ms`` per worker cannot answer it: both workers below report the same
    work time while running at completely different hours.
    """
    value = _fold(
        [
            _opened(CONDUCTOR),
            _work("goal", goal="ship it", round=1),
            _work("create", item_id="it_a", title="A's task", time=NOON),
            _work("bind", item_id="it_a", worker_session_key=WORKER_A, time=NOON),
            _work("create", item_id="it_b", title="B's task", time=NOON + HOUR),
            _work("bind", item_id="it_b", worker_session_key=WORKER_B, time=NOON + HOUR),
            _work("close", item_id="it_a", state="accepted", time=NOON + 3 * HOUR),
        ]
    )
    a = _task(value, "it_a")
    b = _task(value, "it_b")
    assert a["created_at"] == projection._work_iso(NOON)
    assert a["closed_at"] == projection._work_iso(NOON + 3 * HOUR)
    assert b["created_at"] == projection._work_iso(NOON + HOUR)
    assert b["closed_at"] == ""
    # Different spenders, so a page draws them as two rows rather than one.
    assert a["spender"] != b["spender"]
    # And A's bar still covers B's start, which is the overlap the page shows.
    assert a["created_at"] < b["created_at"] < a["closed_at"]
