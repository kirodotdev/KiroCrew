"""Interface tests for :class:`kiro_crew.crew_log.turn_tracker.TurnTracker`.

The tracker is in-process state with no I/O, so every test builds its own instance --
no data home, no writer, no emitter -- and asserts what the interface answers: the
ordinals it mints, whether a call settles, which turn is live, what debt is owed, and
what a caller's own cache keeps after a trim. A fake clock stands in for the tool-call
timer, so nothing here sleeps or reads wall time.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict

import pytest

from kiro_crew.crew_log.turn_tracker import (
    MAX_LIVE_TURNS,
    MAX_OPEN_CALLS,
    MAX_SESSIONS,
    SettledCall,
    SweptCall,
    TurnTracker,
)

SID = "acp-sess-tracker"
OTHER = "acp-sess-other"


class _Clock:
    """A monotonic clock that only moves when told to."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


def _tracker(clock: _Clock | None = None, **kw) -> TurnTracker:
    return TurnTracker(clock=clock or _Clock(), **kw)


# --- defaults --------------------------------------------------------------


def test_the_defaults_are_the_documented_bounds():
    """The bounds a production tracker runs on, stated once."""
    assert MAX_LIVE_TURNS == 4096
    assert MAX_OPEN_CALLS == 512
    assert MAX_SESSIONS == 128


# --- the live record and its ordinals --------------------------------------


def test_a_turn_numbers_its_model_calls_and_its_tool_calls_independently():
    tracker = _tracker()
    tracker.begin(SID, 3)
    assert tracker.current_step(SID, 3) == 0, "no model call was announced yet"
    assert tracker.next_step(SID, 3) == 1
    assert [tracker.next_call_index(SID, 3) for _ in range(3)] == [1, 2, 3]
    assert tracker.current_step(SID, 3) == 1, "reading a step must not mint one"
    assert tracker.next_step(SID, 3) == 2
    assert tracker.next_call_index(SID, 3) == 4, "a new step does not restart call order"


def test_a_second_start_of_the_same_turn_keeps_its_numbering():
    """Resetting the counters on a repeated start is the corruption the record prevents."""
    tracker = _tracker()
    tracker.begin(SID, 1)
    tracker.next_call_index(SID, 1)
    tracker.next_call_index(SID, 1)
    tracker.begin(SID, 1)
    assert tracker.next_call_index(SID, 1) == 3


def test_live_turn_names_the_newest_running_turn_and_zero_when_none_runs():
    tracker = _tracker()
    assert tracker.live_turn(SID) == 0
    assert tracker.live_turn("") == 0
    tracker.begin(SID, 4)
    tracker.begin(SID, 9)  # a nested turn pins its own record beside its parent's
    assert tracker.live_turn(SID) == 9
    tracker.closing(SID, 9)()
    assert tracker.live_turn(SID) == 4
    tracker.closing(SID, 4)()
    assert tracker.live_turn(SID) == 0


def test_an_empty_session_id_pins_nothing():
    tracker = _tracker()
    tracker.begin("", 1)
    tracker.release_unclosed("")
    assert tracker.live_turn("") == 0
    assert tracker.current_step("", 1) == 0


def test_the_release_a_terminal_owes_ends_the_turn_and_restarts_nothing_else():
    tracker = _tracker()
    tracker.begin(SID, 1)
    tracker.next_call_index(SID, 1)
    release = tracker.closing(SID, 1)
    assert tracker.live_turn(SID) == 1, "handing a terminal over must not end the turn yet"
    release()
    assert tracker.live_turn(SID) == 0
    # The ordinal is free again: a later turn of the same number starts its own count.
    assert tracker.next_call_index(SID, 1) == 1


def test_a_fresh_claim_releases_only_turns_no_terminal_is_coming_for():
    """A claim ends what nothing else will; a turn whose closer is queued keeps its pin.

    Taking back a closer-owed pin lets the handle -- and its lease -- go while the file
    still shows that turn open, so a successor repairs a turn whose real completion is
    still on its way to disk.
    """
    tracker = _tracker()
    tracker.begin(SID, 1)
    tracker.begin(SID, 2)
    tracker.closing(SID, 2)  # turn 2's terminal is handed over, not yet landed
    tracker.release_unclosed(SID)
    assert tracker.live_turn(SID) == 2, "the closer-owed turn lost its pin"
    tracker.release_unclosed(SID)
    assert tracker.live_turn(SID) == 2


# --- the leak alarm --------------------------------------------------------


def test_a_live_turn_is_never_evicted_even_past_the_ceiling():
    """Past the cap every record is still a running turn, so the overage is accepted.

    Shedding a LIVE record drops its step and call_index, and its next event re-mints
    them from zero, so two entries claim one ordinal -- in a file that is never
    rewritten that duplicate is read as fact.
    """
    tracker = _tracker(max_live_turns=4)
    tracker.begin(SID, 1)
    tracker.next_call_index(SID, 1)
    tracker.next_call_index(SID, 1)
    for n in range(7):
        tracker.begin(f"{OTHER}-{n}", 1)
    assert tracker.live_turn(SID) == 1, "the oldest live turn was evicted"
    assert all(tracker.live_turn(f"{OTHER}-{n}") == 1 for n in range(7))
    assert tracker.next_call_index(SID, 1) == 3, "a live turn's numbering restarted"


def test_the_ceiling_sheds_closer_owed_records_oldest_first(caplog):
    tracker = _tracker(max_live_turns=3)
    for n in range(3):
        tracker.begin(f"{OTHER}-{n}", 1)
        tracker.closing(f"{OTHER}-{n}", 1)  # terminals handed over, never released
    with caplog.at_level(logging.ERROR, logger="kiro_crew.crew_log.turn_tracker"):
        tracker.begin(SID, 1)
    assert tracker.live_turn(f"{OTHER}-0") == 0, "the oldest closed-but-undrained record stayed"
    assert tracker.live_turn(f"{OTHER}-1") == 1
    assert tracker.live_turn(SID) == 1
    assert any("closed-but-undrained" in r.getMessage() for r in caplog.records)


def test_an_accepted_overage_is_reported_once_and_again_after_it_clears(caplog):
    tracker = _tracker(max_live_turns=2)
    with caplog.at_level(logging.ERROR, logger="kiro_crew.crew_log.turn_tracker"):
        for n in range(6):
            tracker.begin(f"{OTHER}-{n}", 1)
            tracker.next_call_index(f"{OTHER}-{n}", 1)
        first = [r for r in caplog.records if "accepting the overage" in r.getMessage()]
        assert len(first) == 1, "the overage was named per event, not once"
        for n in range(6):
            tracker.closing(f"{OTHER}-{n}", 1)()
        for n in range(4):
            tracker.begin(f"{SID}-{n}", 1)
    again = [r for r in caplog.records if "accepting the overage" in r.getMessage()]
    assert len(again) == 2, "a recurrence after the map fell back to the cap was not named"


def test_the_tracker_reports_through_the_logger_it_is_given(caplog):
    named = logging.getLogger("kiro_crew.crew_log.emit")
    tracker = _tracker(max_live_turns=1, logger=named)
    with caplog.at_level(logging.ERROR, logger="kiro_crew.crew_log.emit"):
        tracker.begin(SID, 1)
        tracker.begin(OTHER, 1)
    assert [r.name for r in caplog.records] == ["kiro_crew.crew_log.emit"]


# --- tool calls ------------------------------------------------------------


def test_a_call_settles_once_with_its_openers_identity_and_elapsed(clock):
    tracker = _tracker(clock)
    tracker.begin(SID, 1)
    tracker.call_opened(SID, "tc-1", name="fs_read", server="srv", call_index=2, step=1, turn=1)
    clock.now += 1.25
    settled = tracker.call_settled(SID, "tc-1", 1)
    assert settled == SettledCall(1250, "fs_read", "srv", 2, 1)
    assert tracker.call_settled(SID, "tc-1", 1) is None, "a duplicate frame settled twice"


def test_a_call_never_opened_still_settles_once_without_an_elapsed():
    tracker = _tracker()
    assert tracker.call_settled(SID, "unseen", 1) == SettledCall(-1, "", "", 0, 0)
    assert tracker.call_settled(SID, "unseen", 1) is None


def test_the_sweep_closes_only_this_turns_open_calls_and_settles_them(clock):
    """A call left open by an earlier turn is not this turn's to close."""
    tracker = _tracker(clock)
    tracker.begin(SID, 1)
    tracker.call_opened(SID, "old", name="a", server="s", call_index=1, step=0, turn=1)
    tracker.begin(SID, 2)
    tracker.call_opened(SID, "new", name="b", server="s", call_index=1, step=3, turn=2)
    tracker.call_opened(OTHER, "new", name="c", server="s", call_index=1, step=0, turn=2)
    clock.now += 0.5
    swept = tracker.sweep_open_calls(SID, 2)
    assert swept == [SweptCall("new", 500, "b", "s", 1, 3)]
    assert tracker.call_settled(SID, "new", 2) is None, "the sweep was not the call's closer"
    assert tracker.sweep_open_calls(SID, 2) == []
    assert [c.call_id for c in tracker.sweep_open_calls(SID, 1)] == ["old"]


def test_a_settled_call_id_settles_again_in_a_later_turn():
    """Markers go with their turn, so a reused id in a later turn is not a duplicate."""
    tracker = _tracker()
    tracker.begin(SID, 1)
    assert tracker.call_settled(SID, "reused", 1) is not None
    tracker.closing(SID, 1)()
    tracker.begin(SID, 2)
    assert tracker.call_settled(SID, "reused", 2) is not None


def test_the_settled_markers_trim_their_oldest_inside_one_live_turn():
    """A marker accumulates per COMPLETED call, so under the never-evict rule a long turn
    would grow the map for as long as it runs. The youngest markers are the ones a
    duplicate frame arrives beside, so the oldest go first.
    """
    tracker = _tracker(max_open_calls=4)
    tracker.begin(SID, 1)
    for n in range(6):
        assert tracker.call_settled(SID, f"c{n}", 1) is not None
    assert tracker.call_settled(SID, "c5", 1) is None, "the youngest marker was trimmed"
    assert tracker.call_settled(SID, "c0", 1) is not None, "the oldest marker was kept"


def test_the_open_calls_are_bounded_but_never_lose_a_live_turns_call():
    tracker = _tracker(max_open_calls=2)
    tracker.begin(SID, 1)
    for n in range(4):
        tracker.call_opened(SID, f"live-{n}", name="x", server="", call_index=n, step=0, turn=1)
    for n in range(3):
        tracker.call_opened(OTHER, f"gone-{n}", name="x", server="", call_index=n, step=0, turn=1)
    assert len(tracker.sweep_open_calls(SID, 1)) == 4, "a live turn's open call was trimmed"
    assert len(tracker.sweep_open_calls(OTHER, 1)) < 3, "the unpinned calls were not trimmed"


def test_a_closed_session_keeps_only_what_a_live_turn_still_needs():
    tracker = _tracker()
    # Turn 1 is gone; turn 2 is still running when the session closes under it.
    tracker.call_opened(SID, "gone-open", name="x", server="", call_index=1, step=0, turn=1)
    assert tracker.call_settled(SID, "gone-settled", 1) is not None
    tracker.begin(SID, 2)
    tracker.call_opened(SID, "live-open", name="x", server="", call_index=1, step=0, turn=2)
    assert tracker.call_settled(SID, "live-settled", 2) is not None
    tracker.next_attempt(SID, 2)
    tracker.forget_session(SID)
    assert tracker.sweep_open_calls(SID, 1) == [], "a gone turn's open call survived the close"
    assert tracker.call_settled(SID, "gone-settled", 1) is not None, "a gone marker survived"
    assert [c.call_id for c in tracker.sweep_open_calls(SID, 2)] == ["live-open"]
    assert tracker.call_settled(SID, "live-settled", 2) is None, "a live turn lost its marker"
    assert not tracker.knows_attempts(SID)


# --- attempts --------------------------------------------------------------


def test_reruns_of_one_ordinal_count_from_one():
    tracker = _tracker()
    assert not tracker.knows_attempts(SID)
    assert [tracker.next_attempt(SID, 7) for _ in range(3)] == [1, 2, 3]
    assert tracker.next_attempt(SID, 8) == 1
    assert tracker.knows_attempts(SID)


def test_a_seed_raises_counts_and_never_lowers_one():
    tracker = _tracker()
    tracker.next_attempt(SID, 1)
    tracker.next_attempt(SID, 1)
    tracker.seed_attempts(SID, {1: 1, 7: 2})
    assert tracker.next_attempt(SID, 1) == 3, "a seed lowered a count memory held"
    assert tracker.next_attempt(SID, 7) == 3
    tracker.seed_attempts(OTHER, {})
    assert not tracker.knows_attempts(OTHER), "an empty seed recorded a session"


def test_the_attempt_map_is_bounded_without_evicting_a_live_session():
    tracker = _tracker(max_sessions=2)
    tracker.begin(SID, 1)
    tracker.next_attempt(SID, 1)
    for n in range(4):
        tracker.next_attempt(f"{OTHER}-{n}", 1)
    assert tracker.knows_attempts(SID), "a live session's counts were evicted"
    assert not tracker.knows_attempts(f"{OTHER}-0"), "the map was not bounded"


# --- the supersede repair's debt -------------------------------------------


def test_a_stand_down_records_the_debt_only_while_a_turn_runs():
    tracker = _tracker()
    assert tracker.stand_down(SID, "chat-7") == 0
    assert tracker.repair_debt(SID) == ""
    tracker.begin(SID, 3)
    assert tracker.stand_down(SID, "chat-7") == 3
    assert tracker.repair_debt(SID) == "chat-7"
    assert tracker.take_repair_debt(SID) == "chat-7"
    assert tracker.take_repair_debt(SID) == ""


def test_a_landed_terminal_pays_the_debt():
    tracker = _tracker()
    tracker.begin(SID, 1)
    tracker.stand_down(SID, "chat-7")
    tracker.closing(SID, 1)()
    assert tracker.repair_debt(SID) == "", "a landed terminal left the debt standing"


def test_a_nested_turn_landing_first_does_not_pay_a_siblings_debt():
    """The debt is keyed by SESSION: it is paid only once no live turn of it remains.

    Paying it on the first terminal to land would let a LATER live turn's own terminal
    drop with nothing left to re-queue the repair.
    """
    tracker = _tracker()
    tracker.begin(SID, 1)
    tracker.begin(SID, 2)
    tracker.stand_down(SID, "chat-7")
    tracker.closing(SID, 2)()
    assert tracker.repair_debt(SID) == "chat-7", "a nested turn paid its sibling's debt"
    tracker.closing(SID, 1)()
    assert tracker.repair_debt(SID) == ""


def test_the_stand_down_reads_liveness_and_records_the_debt_in_one_lock_hold():
    """The liveness read and the debt write are ONE critical section.

    A ceiling rejection that read the debt between a separate liveness read and a later
    debt write would find it empty, release its terminal's pin and re-queue nothing --
    and the debt written afterwards would have no consumer, leaving the predecessor's
    tail open for the life of the file. Kept as a lock-count pin: the window is two
    statements wide and no interface call can open it on demand.
    """
    tracker = _tracker()
    tracker.begin(SID, 1)
    real = tracker._lock
    taken = {"n": 0}

    class _Counting:
        def __enter__(self):
            taken["n"] += 1
            real.acquire()
            return self

        def __exit__(self, *exc):
            real.release()

    tracker._lock = _Counting()  # type: ignore[assignment]
    try:
        assert tracker.stand_down(SID, "chat-7") == 1
    finally:
        tracker._lock = real
    assert taken["n"] == 1, f"the stand-down took the lock {taken['n']} times, not once"
    assert tracker.repair_debt(SID) == "chat-7"


# --- trimming a caller's cache ---------------------------------------------


def test_a_trim_never_drops_a_live_sessions_entry_and_may_overshoot():
    tracker = _tracker()
    tracker.begin(SID, 1)
    store: OrderedDict[str, int] = OrderedDict((k, 0) for k in (SID, "a", "b", "c"))
    tracker.trim(store, 2, lambda key: key, "test cache")
    assert list(store) == [SID, "c"], "the trim dropped the live entry or the wrong ones"
    tracker.begin("c", 1)
    store["d"] = 0
    store["e"] = 0
    tracker.trim(store, 1, lambda key: key, "test cache")
    assert list(store) == [SID, "c"], "every remaining entry is live, so the cap overshoots"


def test_pop_unless_live_keeps_a_live_sessions_entry():
    tracker = _tracker()
    tracker.begin(SID, 1)
    store = {SID: "handle", OTHER: "handle"}
    tracker.pop_unless_live(store, SID)
    tracker.pop_unless_live(store, OTHER)
    assert store == {SID: "handle"}


def test_concurrent_turns_never_share_an_ordinal():
    """Minting is atomic: threads numbering one turn get distinct, gapless ordinals."""
    tracker = _tracker()
    tracker.begin(SID, 1)
    minted: list[int] = []
    guard = threading.Lock()
    start = threading.Barrier(4)

    def _mint() -> None:
        start.wait(timeout=10)
        for _ in range(250):
            n = tracker.next_call_index(SID, 1)
            with guard:
                minted.append(n)

    threads = [threading.Thread(target=_mint) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not any(thread.is_alive() for thread in threads)
    assert sorted(minted) == list(range(1, 1001))
