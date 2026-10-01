"""Per-fold savepoint versions, and the eager folding mode built on them.

Three properties, each the reason one piece of the change exists.

A fold's stored shape is ITS OWN, so retiring one fold's savepoints costs a cold
fold to that fold and nothing to the others
(:func:`test_bumping_one_folds_version_retires_only_that_folds_savepoint`). Under the
single global number every bump retired all six, so a counting fix in one fold made
every long session refold every fold it had.

An EAGER fold may never be one that every entry moves
(:func:`test_an_eager_fold_must_declare_what_affects_it`): eager folding runs off the
append path, and a fold with ``affects=None`` would wake it for every message body in
the log -- which is the cost the mode exists to avoid paying on a read.

And an eager fold ADVANCES with no reader
(:func:`test_an_emitted_entry_advances_an_eager_fold_with_no_reader`), which is the
whole claim: a dashboard reads the current value in O(1) because the value was already
folded when the entry landed, not because the read got faster.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import unittest.mock
from dataclasses import replace

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import checkpoint as savepoints
from kiro_crew.crew_log import eager, emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log.entry_types import RADAR_ENTRY_TYPE

SESSION = "s-eager"
GATEWAY = "gateway"

#: The board key the work entries name. A ``work/recorded`` entry carries the slot it
#: belongs to, and the fold joins on THAT rather than on the unit's header, so the two
#: must agree for the eager fold to land where a reader would look.
WORK_SLOT = "dashboard:9"

#: Enough entries above the savepoint floor that ``save`` is owed one.
_EARNING_TURNS = savepoints.MIN_ADVANCE_ENTRIES // 2 + 4


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Every test writes into its own data home, never the live one."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    eager.stop_for_tests()
    crew_log.forget_slot_folds()
    emit.reset_caches()
    yield
    eager.stop_for_tests()
    crew_log.forget_slot_folds()
    # Drop the leased CrewLog handle the emitter cached in ``emit._open`` for the
    # emitter-backed cases: its store lease holds a descriptor released only when the
    # handle is dropped, and with ``tmp_path_retention_policy = failed`` pytest removes a
    # passing case's ``tmp_path`` at teardown. On Windows a live descriptor makes that
    # removal fail silently (errors ignored), leaving host state -- a cached handle
    # pointing at a removed home -- for the rest of the worker. Clearing the cache here
    # mirrors the setup half above, the same pairing the rest of the suite uses.
    emit.reset_caches()


def _log(unit_id: str = SESSION, **fields) -> CrewLog:
    fields.setdefault("owner", "raymond")
    fields.setdefault("agent", "kirocrew")
    return CrewLog.create(lg.KIND_SESSION, unit_id, **fields)


def _grow(handle: CrewLog, turns: int) -> None:
    """*turns* whole turns, which is what both ``usage`` and ``status`` move on."""
    for turn in range(1, turns + 1):
        handle.append("turn/started", {"turn": turn, "actor": "user", "depth": 0}, src=GATEWAY)
        handle.append(
            "turn/completed",
            {
                "turn": turn,
                "stop_reason": "end_turn",
                "depth": 0,
                "duration_ms": 900,
                "model": "opus",
                "provider": "kiro",
                "credits": 0.5,
                "tokens": {"input": 100, "output": 20, "cache_read": 5, "cache_write": 1},
            },
            src=GATEWAY,
        )


def _saved(handle: CrewLog, names: tuple[str, ...]) -> None:
    """Fold *names* far enough that every one of them has a savepoint on disk."""
    crew_log.fold_session(handle.id, names)
    for name in names:
        path = savepoints.checkpoint_path(handle.kind, handle.id, name)
        assert path.exists(), f"the fixture must earn a savepoint for {name}"


# --------------------------------------------------------------------------- #
# 1. The version is the fold's own
# --------------------------------------------------------------------------- #


def test_every_fold_declares_its_own_state_version():
    """The version lives on the fold, so there is one number per fold and no other."""
    versions = {name: fold.state_version for name, fold in crew_log._FOLDS.items()}
    assert set(versions) == set(crew_log.FOLD_NAMES)
    assert all(isinstance(value, int) and value >= 1 for value in versions.values())


def test_the_append_paths_wake_filter_covers_every_eager_folds_types():
    """MUTATION-SENSITIVE: the cheap filter and the registry name the same types.

    ``note_commit`` runs on every append, and the bulk of a log is entries no eager fold
    names, so the filter that rejects them is a frozenset of literals -- it loads no
    registry and imports nothing. The cost of that is a second place the types are
    written, and the failure it invites is silent: a fold marked eager whose type is
    missing from the filter looks exactly like a fold working lazily.

    This is what makes that impossible. The types are DERIVED here from the registry, and
    the filter has to equal them plus ``session/closed``, which is a wake no fold declares
    because it drops the slot instead of advancing it.
    """
    derived = {
        entry_type
        for name in crew_log.EAGER_FOLD_NAMES
        for entry_type in (crew_log._FOLDS[name].affects or ())
    }
    assert derived, "no eager fold declares an affects set, so this measured nothing"
    assert eager._WAKE_TYPES == derived | {"session/closed"}, (
        f"the append path's filter is {sorted(eager._WAKE_TYPES)} while the registry names "
        f"{sorted(derived)}: a type in one and not the other is a fold that silently "
        "stays lazy, or a wake nothing folds"
    )


def test_a_savepoint_file_records_the_folds_own_version():
    """The number in the file is that fold's, which is what makes the bump per fold."""
    handle = _log()
    _grow(handle, _EARNING_TURNS)
    _saved(handle, ("usage",))

    payload = json.loads(savepoints.checkpoint_path(handle.kind, handle.id, "usage").read_text())
    assert payload["state_version"] == crew_log._FOLDS["usage"].state_version


def test_bumping_one_folds_version_retires_only_that_folds_savepoint(monkeypatch):
    """A version bump is per fold: the bumped one folds cold, its neighbour resumes.

    The property the whole per-fold change exists for. Both folds have a savepoint on
    disk written by this build; ``status`` is then rebuilt at a higher version and
    ``usage`` is left alone, so the reload must refuse exactly one of the two files.
    """
    handle = _log()
    _grow(handle, _EARNING_TURNS)
    wanted = ("usage", "status")
    _saved(handle, wanted)

    both = savepoints.load(handle, wanted)
    assert both is not None and set(both.checkpoints) == set(wanted)

    status = crew_log._FOLDS["status"]
    monkeypatch.setitem(
        crew_log._FOLDS, "status", replace(status, state_version=status.state_version + 1)
    )

    resumed = savepoints.load(handle, wanted)
    assert resumed is not None
    assert "usage" in resumed.checkpoints, "usage's savepoint is at its own version, so it stands"
    assert "status" not in resumed.checkpoints, "status moved version, so its savepoint retired"


# --------------------------------------------------------------------------- #
# 2. An eager fold must be selective
# --------------------------------------------------------------------------- #


def test_an_eager_fold_must_declare_what_affects_it():
    """``mode="eager"`` with ``affects=None`` is refused where the fold is built.

    An all-eating fold woken off the append path would fold on every message body in
    the log, which is the cost the mode exists to avoid.
    """
    with pytest.raises(ValueError, match="eager"):
        crew_log._Fold(
            "throwaway",
            dict,
            lambda state, entry: None,
            lambda state: {},
            mode="eager",
        )


def test_an_eager_fold_with_an_affects_set_is_accepted():
    """The control: the same fold with a declared ``affects`` set is built."""
    fold = crew_log._Fold(
        "throwaway",
        dict,
        lambda state, entry: None,
        lambda state: {},
        affects=frozenset({"turn/started"}),
        mode="eager",
    )
    assert fold.mode == "eager"


def test_eager_folds_are_slot_keyed_and_the_rest_stay_lazy():
    """Eager folding runs through the slot warm path, so an eager fold must be one."""
    eager = [name for name, fold in crew_log._FOLDS.items() if fold.mode == "eager"]
    assert eager, "the change is pointless with nothing eager"
    assert set(eager) <= set(crew_log.SLOT_PROJECTION_NAMES)
    assert crew_log._FOLDS["timeline"].mode == "lazy"


# --------------------------------------------------------------------------- #
# 3. The fold advances off the append path
# --------------------------------------------------------------------------- #


def _slot_log(unit_id: str, slot: str) -> CrewLog:
    """A session log whose HEADER names *slot*, which is what the slot read joins on."""
    return CrewLog.create(lg.KIND_SESSION, unit_id, owner="raymond", agent="kirocrew", slot=slot)


def _work_entry(handle: CrewLog, item_id: str, slot: str = WORK_SLOT) -> int:
    """One ``work/recorded`` entry naming *slot*'s board, returning its seq."""
    entry = handle.append(
        "work/recorded",
        {
            "slot": slot,
            "actor": "conductor",
            "by": slot,
            "action": "create",
            "item_id": item_id,
            "title": "a work item",
        },
        src=GATEWAY,
    )
    return int(entry.seq)


def _memo_keys() -> set[tuple[str, str]]:
    """The (slot, fold) pairs currently held warm, whatever data home they are under."""
    return {(key[1], key[2]) for key in crew_log._slot_memos}


def test_an_emitted_entry_advances_an_eager_fold_with_no_reader():
    """A committed ``work/recorded`` leaves ``work`` folded before anyone reads it.

    The whole claim of the mode. Nothing here calls a read path: the entry lands, the
    eager worker is told, and the warm memo for (slot, ``work``) exists and stands at
    that entry's seq -- so the dashboard's next read is a memo lookup.
    """
    slot = WORK_SLOT
    handle = _slot_log("s-work-1", slot)
    seq = _work_entry(handle, "it-1")
    assert _memo_keys() == set(), "no reader has run, so nothing is warm yet"

    eager.note_commit("s-work-1", "work/recorded", seq)
    assert eager.drain(timeout=10.0)

    assert (slot, "work") in _memo_keys()
    folded = crew_log.read_slot_projection(slot, "work")
    assert folded.seq == seq
    assert folded.value["items"], "the eager fold carries the item it was woken for"


def test_an_unrelated_entry_does_not_wake_the_work_fold():
    """``message/sent`` is not in ``work``'s affects set, so nothing folds.

    The other half of the cost claim: a log is mostly message bodies, and an eager
    worker that woke for them would do on every append what the read was doing once.
    """
    slot = WORK_SLOT
    handle = _slot_log("s-work-2", slot)
    _work_entry(handle, "it-1")

    eager.note_commit("s-work-2", "message/sent", 99)
    assert eager.drain(timeout=10.0)

    assert _memo_keys() == set(), "no eager fold names message/sent"


def test_a_full_queue_drops_and_counts_instead_of_blocking(monkeypatch):
    """Overflow is dropped and counted; the append path is never made to wait.

    The consumer is stubbed out rather than merely stopped. ``note_commit`` starts the
    worker itself, so a test that only stopped it would have its own first wake start a
    thread that drains while the test fills -- and the drop count would then depend on
    which of the two won, which is a stopwatch race rather than a statement about the
    bound. With ``_run`` replaced the queue has no consumer at all, so the overflow is
    exactly the number of wakes past the bound.
    """
    eager.stop_for_tests()
    monkeypatch.setattr(eager, "_run", lambda: None)
    before = eager._dropped
    for index in range(eager.QUEUE_LIMIT + 8):
        eager.note_commit("s-missing", "work/recorded", index + 1)

    assert eager._dropped - before == 8


def test_a_dropped_wake_is_recovered_by_the_next_read():
    """A dropped wake costs currency, never correctness: the read still folds it.

    What makes dropping safe. The queue overflowing means some entry never woke its
    fold, so the memo sits behind the file -- and the read path's own continuation
    carries it forward, which is the lazy behaviour that was there before.
    """
    slot = WORK_SLOT
    handle = _slot_log("s-work-3", slot)
    seq = _work_entry(handle, "it-1")
    # The wake is never delivered at all, which is the worst case overflow produces.
    folded = crew_log.read_slot_projection(slot, "work")
    assert folded.seq == seq
    assert folded.value["items"]


# --------------------------------------------------------------------------- #
# 6. The memory the warm memos hold
# --------------------------------------------------------------------------- #


def test_a_closed_session_drops_the_slots_eager_memos():
    """``session/closed`` ends the reason to hold the slot warm -- for the EAGER folds.

    Two things this pins beyond the drop itself.

    THE LAZY CELLS STAY. A closer is not always the end of a board: a slot reset, or one
    worker unit of a live crew, closes while the board goes on. Dropping every
    ``(home, slot, *)`` memo would take that crew's ``ledger`` and ``radar`` with it --
    cells no wake ever warmed, whose next read then pays a cold fold nothing asked for.

    AND THE COUNT CEILING IS NOT WHAT DOES IT. Eviction by count fires only when the table
    is full; below the cap it removes nothing, ever, and a closed board's cell is never
    re-stored so it never moves towards the back either. On any gateway running fewer than
    ``SLOT_FOLD_CACHE_SLOTS`` board folds -- the ordinary case -- that cell would sit there
    for the life of the process. The assertion below records that the table was nowhere
    near the cap when the drop happened, so the ceiling cannot be the cause.
    """
    slot = WORK_SLOT
    handle = _slot_log("s-work-6", slot)
    seq = _work_entry(handle, "it-1")
    eager.note_commit("s-work-6", "work/recorded", seq)
    assert eager.drain(timeout=10.0)
    assert (slot, "work") in _memo_keys()
    # A lazy fold of the SAME slot, warmed by a read rather than by a wake.
    crew_log.fold_slot_warm("ledger", ("s-work-6",), slot=slot)
    assert (slot, "ledger") in _memo_keys(), "the lazy cell this test is about is not warm"

    assert len(_memo_keys()) < crew_log.SLOT_FOLD_CACHE_SLOTS, (
        f"{len(_memo_keys())} cells held against a ceiling of "
        f"{crew_log.SLOT_FOLD_CACHE_SLOTS}: this case has to sit below the cap, or the "
        "ceiling could be what drops the cell rather than the closer"
    )

    closed = handle.append("session/closed", {"reason": "done"}, src=GATEWAY)
    eager.note_commit("s-work-6", "session/closed", int(closed.seq))
    assert eager.drain(timeout=10.0)

    assert (slot, "work") not in _memo_keys()
    assert (slot, "ledger") in _memo_keys(), (
        f"the closer took a lazy fold with it; warm = {sorted(_memo_keys())}. It drops what "
        "eager folding warmed, not every fold the slot has"
    )


def test_the_count_ceiling_evicts_back_to_a_cold_fold(monkeypatch):
    """Past the retained-cell ceiling the least recently stored memo goes.

    The memos are an optimization with no answer of their own, so the ceiling is spent on
    the slots being written now and an evicted one pays one refold. Measured bound rather
    than an argued one: the largest cell any slot fold can hold is ``radar`` at its item
    cap, 995,342 bytes, so a full table of those is 60.8 MiB -- which is why a count
    ceiling is the whole bound and a byte ceiling on top of it never fires.
    """
    monkeypatch.setattr(crew_log, "SLOT_FOLD_CACHE_SLOTS", 1)
    slots = ["dashboard:20", "dashboard:21", "dashboard:22"]
    for index, slot in enumerate(slots):
        handle = _slot_log(f"s-budget-{index}", slot)
        seq = _work_entry(handle, f"it-{index}", slot=slot)
        eager.note_commit(f"s-budget-{index}", "work/recorded", seq, board=slot)
        assert eager.drain(timeout=10.0)

    assert len(_memo_keys()) == 1, "a one-cell ceiling keeps only the newest memo"
    assert (slots[-1], "work") in _memo_keys()
    # Correctness is untouched: the evicted slot still folds its own record.
    assert crew_log.read_slot_projection(slots[0], "work").value["items"]


def test_an_advanced_cell_moves_to_the_back_and_outlives_an_older_neighbour(monkeypatch):
    """MUTATION-SENSITIVE: a stored cell moves to the back, so the NEXT eviction skips it.

    The ceiling alone does not say this. At one cell every store evicts the other, so a
    table that appended without moving a re-stored cell would pass that test and still
    evict the cell most recently advanced -- which is the eviction order this change
    declares and the eager worker depends on: a board being written now is stored on every
    wake and must not be the one that goes.

    Three boards at a ceiling of two, and the discriminating step is the middle one: board
    A is advanced by a fresh entry AFTER B is folded, so with the move A sits behind B and
    C's arrival evicts B. Without it A is still at the front and C's arrival evicts A.
    """
    monkeypatch.setattr(crew_log, "SLOT_FOLD_CACHE_SLOTS", 2)
    boards = {}
    for index, slot in enumerate(("dashboard:30", "dashboard:31")):
        unit = f"s-lru-{index}"
        handle = _slot_log(unit, slot)
        _work_entry(handle, "it-0", slot=slot)
        boards[slot] = (unit, handle)
        crew_log.fold_slot_warm("work", (unit,), slot=slot)

    first, second = tuple(boards)
    # A store, not merely a read: the cell is behind the file, so the pass carries it
    # forward and records a new memo. A read that found it already current stores nothing.
    unit, handle = boards[first]
    _work_entry(handle, "it-1", slot=first)
    crew_log.fold_slot_warm("work", (unit,), slot=first)

    third = "dashboard:32"
    third_unit = "s-lru-2"
    third_handle = _slot_log(third_unit, third)
    _work_entry(third_handle, "it-0", slot=third)
    crew_log.fold_slot_warm("work", (third_unit,), slot=third)

    held = _memo_keys()
    assert len(held) == 2, f"a two-cell ceiling should hold two memos, holds {sorted(held)}"
    assert (third, "work") in held, "the newest fold is not held at all"
    assert (first, "work") in held, (
        f"the cell advanced most recently was evicted; held = {sorted(held)}. A store has to "
        "move its cell to the back, or the board being written now is the one that goes"
    )
    assert (second, "work") not in held, (
        f"the untouched older cell survived; held = {sorted(held)} -- eviction is not "
        "following the order this change claims"
    )


class _CountingLog:
    """A crew log handle that counts the entries a caller PARSES through it.

    The number every cost claim here is made in. A warm read still opens each unit and
    reads its TAIL for the newest seq, which is what tells it the file has not moved --
    O(the slot's unit count), and the cold read pays it too. What the cold read pays on
    top, and the warm read must not, is one parse per line of every unit. That is the cost
    eager folding removes, so that is the number measured, rather than a wall-clock time
    that would report the machine instead of the change.
    """

    def __init__(self, inner, counter: list[int]) -> None:
        self._inner = inner
        self._counter = counter

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    def iter_from(self, *args, **kwargs):
        for entry in self._inner.iter_from(*args, **kwargs):
            self._counter[0] += 1
            yield entry


@pytest.fixture
def parsed(monkeypatch):
    """Entries parsed through any crew log handle the read path opens."""
    counter = [0]
    inner = crew_log.open_session_log

    def _counting(unit_id: str):
        handle = inner(unit_id)
        return None if handle is None else _CountingLog(handle, counter)

    monkeypatch.setattr(crew_log, "open_session_log", _counting)
    return counter


def test_a_fold_after_an_eager_fold_parses_no_entries(parsed):
    """The whole point, as a number: the eager fold already paid the parse.

    Measured at :func:`fold_slot_warm`, with the unit list handed in, because that is
    exactly the span eager folding changes. The read route's other half -- discovering
    WHICH units name this board -- parses the conductor's log on every read by its own
    design (``test_work_fold_warm_read.py`` says so and measures it separately), and it
    is unaffected by this change either way. Folding it into this number would report
    someone else's cost as this one's.
    """
    slot = WORK_SLOT
    handle = _slot_log("s-measured", slot)
    for index in range(40):
        handle.append(
            "message/sent",
            {"turn": 1, "text": f"filler {index}", "chars": 10},
            src=GATEWAY,
        )
    seq = _work_entry(handle, "it-1")
    units = ("s-measured",)

    eager.note_commit("s-measured", "work/recorded", seq)
    assert eager.drain(timeout=10.0)
    assert parsed[0] > 0, "the eager fold itself parsed the log, which is where the cost went"

    parsed[0] = 0
    warm = crew_log.fold_slot_warm("work", units, slot=slot)
    warm_cost = parsed[0]

    crew_log.forget_slot_folds()
    parsed[0] = 0
    cold = crew_log.fold_slot_warm("work", units, slot=slot)
    cold_cost = parsed[0]

    assert warm.state == cold.state and warm.last_seq == cold.last_seq
    assert warm_cost == 0, f"a fold after an eager fold parsed {warm_cost} entries"
    assert cold_cost >= 41, f"the cold fold parses the whole unit, measured {cold_cost}"


def test_a_binding_recorded_after_a_warm_read_still_reaches_the_workers_unit():
    """The cached unit list is re-resolved when a conductor unit grows.

    What keeps the ``work`` unit-list cache from being a correctness bug. A worker's
    report lives in the WORKER's log, and the only record that the worker belongs to
    this board is a ``bind`` entry in the conductor's log -- so a list cached before the
    bind would leave that worker's units out of the fold for as long as it was held. The
    guard is the conductor unit's own stat fingerprint, which an append moves.
    """
    conductor_slot = WORK_SLOT
    worker_slot = "dashboard:31"
    conductor = _slot_log("s-conductor", conductor_slot)
    _work_entry(conductor, "it-1")
    # Warm the unit list while the board has no workers at all.
    assert crew_log.read_slot_projection(conductor_slot, "work").value["items"]

    worker = _slot_log("s-worker", worker_slot)
    conductor.append(
        "work/recorded",
        {
            "slot": conductor_slot,
            "actor": "conductor",
            "by": conductor_slot,
            "action": "bind",
            "item_id": "it-1",
            "worker_session_key": worker_slot,
        },
        src=GATEWAY,
    )
    worker.append(
        "work/recorded",
        {
            "slot": conductor_slot,
            "actor": "worker",
            "by": worker_slot,
            "action": "report",
            "item_id": "it-1",
            "status": "done",
            "summary": "the worker reported",
        },
        src=GATEWAY,
    )

    folded = crew_log.read_slot_projection(conductor_slot, "work")
    item = folded.value["items"][0]
    assert item["status"] == "done", "the newly bound worker's report reached the fold"


# --------------------------------------------------------------------------- #
# The production wiring
# --------------------------------------------------------------------------- #


def test_the_real_append_path_wakes_the_eager_fold(monkeypatch):
    """End to end through ``emit``: no test calls ``note_commit`` here.

    The tests above drive the worker directly, which says nothing about whether anything
    in the product tells it. This goes through the emitter's own append job -- the one
    every ordinary entry type takes -- so the hook inside it is what is under test, and
    the entry is written by the writer thread rather than by this one.
    """
    # The suite runs with the emitter switched OFF (``KIROCREW_CREW_LOG=0``), so a test
    # of the append path has to switch it on for itself; with it off, ``_handle``
    # answers None and nothing is appended at all.
    monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    emit.reset_caches()
    slot = WORK_SLOT
    emit.on_session_opened(
        "s-emitted", agent="kirocrew", slot=slot, model="opus", cwd="/w", owner="raymond"
    )
    assert emit.flush(timeout=10.0)

    emit._write(
        "s-emitted",
        "work/recorded",
        {
            "slot": slot,
            "actor": "conductor",
            "by": slot,
            "action": "create",
            "item_id": "it-1",
            "title": "emitted",
        },
        src="gateway",
    )
    assert emit.flush(timeout=10.0)
    assert eager.drain(timeout=10.0)

    assert (slot, "work") in _memo_keys()
    assert crew_log.read_slot_projection(slot, "work").value["items"]


def test_drain_waits_for_a_wake_it_has_not_folded_yet():
    """``drain`` answers about FOLDING, not about the queue being empty.

    The counters are taken when a wake is ENQUEUED rather than when the worker takes one,
    because a take and its count cannot be made atomic against a reader without holding a
    lock across a blocking get. With the consumer stubbed out the queue is drained by
    nobody, so a ``drain`` that answered on emptiness alone would say True here.
    """
    eager.stop_for_tests()
    slot = WORK_SLOT
    handle = _slot_log("s-pending", slot)
    seq = _work_entry(handle, "it-1")
    eager.note_commit("s-pending", "work/recorded", seq)
    # Take the only wake off the queue without folding it -- the exact window a waiter
    # comparing "queue empty" against a taken-count would misread.
    assert eager._queue is not None
    eager._queue.get_nowait()

    assert eager.drain(timeout=0.3) is False
    assert _memo_keys() == set()


def test_a_burst_of_entries_costs_the_worker_one_pass(parsed):
    """What the WORKER pays, measured, because the append path's share is not the whole cost.

    "One ``put_nowait``" is the calling thread's share. The worker then folds, and its fold
    goes through the read path -- including the unit discovery that parses the conductor's
    log. So a burst's real cost is the question, and the answer is the batch: every wake
    already queued is coalesced into one, so a turn that writes several entries pays one
    pass rather than one per entry.

    Measured against a long conductor log, so the per-pass cost is the thing that would
    multiply if batching failed.
    """
    slot = WORK_SLOT
    handle = _slot_log("s-burst", slot)
    for index in range(60):
        handle.append(
            "message/sent",
            {"turn": 1, "text": f"filler {index}", "chars": 10},
            src=GATEWAY,
        )
    first = _work_entry(handle, "it-0")
    eager.note_commit("s-burst", "work/recorded", first, board=slot)
    assert eager.drain(timeout=10.0)
    one_pass = parsed[0]
    assert one_pass > 60, f"the fold must have read the long log; measured {one_pass}"

    # Eight more entries, all queued before the worker is let near them, which is the
    # shape a turn's burst has.
    eager.stop_for_tests()
    seqs = [_work_entry(handle, f"it-{n}") for n in range(1, 9)]
    parsed[0] = 0
    for seq in seqs:
        eager.note_commit("s-burst", "work/recorded", seq, board=slot)
    assert eager.drain(timeout=20.0)
    burst = parsed[0]

    assert len(crew_log.read_slot_projection(slot, "work").value["items"]) == 9
    # One batch, so one pass -- not eight. The bound is generous because the worker may
    # legitimately wake twice if it reaches the queue between two puts.
    assert burst <= one_pass * 3, (
        f"eight queued entries cost {burst} parsed entries against {one_pass} for one; "
        "the batch is not being coalesced"
    )


def test_a_worker_report_advances_the_conductors_board_not_its_own():
    """The board comes from the ENTRY, never from the unit's header.

    A worker's log header names the worker's own slot, while its ``work/recorded`` names
    the conductor's board and reaches that fold only by being joined into it. Folding by
    the header advances a board nobody reads and leaves the conductor's -- the one the
    dashboard reads -- exactly as stale as it was.
    """
    conductor_slot = WORK_SLOT
    worker_slot = "dashboard:51"
    conductor = _slot_log("s-c-board", conductor_slot)
    worker = _slot_log("s-w-board", worker_slot)
    _work_entry(conductor, "it-1")
    conductor.append(
        "work/recorded",
        {
            "slot": conductor_slot,
            "actor": "conductor",
            "by": conductor_slot,
            "action": "bind",
            "item_id": "it-1",
            "worker_session_key": worker_slot,
        },
        src=GATEWAY,
    )
    report = worker.append(
        "work/recorded",
        {
            "slot": conductor_slot,
            "actor": "worker",
            "by": worker_slot,
            "action": "report",
            "item_id": "it-1",
            "status": "done",
            "summary": "the worker reported",
        },
        src=GATEWAY,
    )

    eager.note_commit("s-w-board", "work/recorded", int(report.seq), board=conductor_slot)
    assert eager.drain(timeout=10.0)

    warm = _memo_keys()
    assert (conductor_slot, "work") in warm, "the conductor's board is the one that advanced"
    assert (worker_slot, "work") not in warm, "the worker's own board is not this entry's"
    assert (
        crew_log.read_slot_projection(conductor_slot, "work").value["items"][0]["status"] == "done"
    )


def test_the_real_emitter_sends_a_worker_report_to_the_conductors_board(monkeypatch):
    """End to end, through the emitter, with the board DIFFERENT from the unit's header.

    The sibling end-to-end test writes a conductor's own entry, where the header slot and
    the board are the same string -- so it cannot tell whether the emitter passes the board
    or the folder guesses it. This one writes a WORKER's report: the unit's header names
    the worker's slot and the payload names the conductor's, and only the conductor's board
    may advance.
    """
    monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    emit.reset_caches()
    conductor_slot = WORK_SLOT
    worker_slot = "dashboard:61"
    conductor = _slot_log("s-e2e-c", conductor_slot)
    _work_entry(conductor, "it-1")
    conductor.append(
        "work/recorded",
        {
            "slot": conductor_slot,
            "actor": "conductor",
            "by": conductor_slot,
            "action": "bind",
            "item_id": "it-1",
            "worker_session_key": worker_slot,
        },
        src=GATEWAY,
    )
    emit.on_session_opened(
        "s-e2e-w", agent="kirocrew", slot=worker_slot, model="opus", cwd="/w", owner="raymond"
    )
    assert emit.flush(timeout=10.0)

    emit._write(
        "s-e2e-w",
        "work/recorded",
        {
            "slot": conductor_slot,
            "actor": "worker",
            "by": worker_slot,
            "action": "report",
            "item_id": "it-1",
            "status": "done",
            "summary": "reported through the emitter",
        },
        src="gateway",
    )
    assert emit.flush(timeout=10.0)
    assert eager.drain(timeout=10.0)

    warm = _memo_keys()
    assert (conductor_slot, "work") in warm, "the emitter did not pass the entry's board"
    assert (worker_slot, "work") not in warm, "the worker's own board is not this entry's"


def test_one_unit_writing_two_boards_folds_both():
    """A batch is keyed by (unit, board), so two boards from one unit are both folded.

    A worker bound to two conductors reports to both from its own log. Keying the batch on
    the unit alone would collapse the two wakes onto one entry and fold whichever won,
    leaving the other board stale with nothing raised.
    """
    eager.stop_for_tests()
    first, second = WORK_SLOT, "dashboard:62"
    worker = _slot_log("s-two-boards", "dashboard:63")
    seqs = []
    for board in (first, second):
        entry = worker.append(
            "work/recorded",
            {
                "slot": board,
                "actor": "worker",
                "by": "dashboard:63",
                "action": "report",
                "item_id": "it-1",
                "status": "progress",
                "summary": f"reporting to {board}",
            },
            src=GATEWAY,
        )
        seqs.append((board, int(entry.seq)))
    for board, seq in seqs:
        eager.note_commit("s-two-boards", "work/recorded", seq, board=board)
    assert eager.drain(timeout=20.0)

    warm = _memo_keys()
    assert (first, "work") in warm and (
        second,
        "work",
    ) in warm, f"one unit's two boards did not both fold; warm = {sorted(warm)}"


def test_two_concurrent_folds_never_pair_one_passs_seq_with_anothers_state():
    """MUTATION-SENSITIVE: the (state, seq) a pass returns describes one moment.

    Two passes on one slot share the cell -- the continuation drives ``memo.registry`` and
    the checkpoint then reads that live cell beside the CALLER's own ``reached``. So a pass
    that read a shorter tail can return the other's newer state under its own older seq,
    and seq is what a reader truncates against.

    The interleaving is FORCED rather than raced for, because a race that only sometimes
    happens is a test that only sometimes measures. Pass A is held inside its stream by an
    event; while it is held, a further entry is appended and pass B runs to completion on
    the shared cell; only then is A released. Without serialization A returns B's state
    under A's seq, deterministically.
    """
    slot = WORK_SLOT
    handle = _slot_log("s-race", slot)
    _work_entry(handle, "it-0", slot=slot)
    units = ("s-race",)
    # Warm the cell, so both passes take the CONTINUATION path -- the one that shares.
    crew_log.fold_slot_warm("work", units, slot=slot)
    _work_entry(handle, "it-1", slot=slot)

    entered = threading.Event()
    b_done = threading.Event()
    held = threading.Event()
    inner_vouched = crew_log._vouched

    #: How long pass A waits for pass B while holding its window open. Reached only when
    #: the fix is PRESENT: B is then queued behind A's lock and cannot signal, so A waits
    #: this out and releases. Without the fix B runs at once and A wakes immediately, so
    #: the wait costs nothing in the direction that must fail.
    _B_WAIT = 3.0

    def gated_vouched(unit_id, before):
        # The window the finding is about: pass A's stream has ENDED, so its own ``reached``
        # is fixed, but it has not yet read the shared cell to pair with it. Hold A here and
        # let another pass drive that cell past A's position.
        if not held.is_set():
            held.set()
            entered.set()
            b_done.wait(timeout=_B_WAIT)
        return inner_vouched(unit_id, before)

    a_result: list[tuple[int, int]] = []
    b_result: list[tuple[int, int]] = []
    errors: list[BaseException] = []

    def fold(into: list, done: "threading.Event | None" = None) -> None:
        try:
            cp = crew_log.fold_slot_warm("work", units, slot=slot)
            into.append((cp.last_seq, len(cp.state["order"])))
        except BaseException as exc:  # pragma: no cover - reported, not swallowed
            errors.append(exc)
        finally:
            if done is not None:
                done.set()

    with unittest.mock.patch.object(crew_log, "_vouched", gated_vouched):
        pass_a = threading.Thread(target=fold, args=(a_result,))
        pass_a.start()
        assert entered.wait(timeout=10.0), "pass A never reached its window"
        # A's stream is done and its seq is fixed at it-1. A third entry lands and pass B
        # folds it, driving the very cell A is about to read.
        _work_entry(handle, "it-2", slot=slot)
        pass_b = threading.Thread(target=fold, args=(b_result, b_done))
        pass_b.start()
        pass_a.join(timeout=30.0)
        pass_b.join(timeout=30.0)

    assert not errors, f"a fold raised: {errors!r}"
    assert a_result and b_result, f"both passes must return; A={a_result!r} B={b_result!r}"
    a_seq, a_items = a_result[0]
    b_seq, b_items = b_result[0]
    assert b_items == 3, f"pass B should have folded all three items, got {b_items}"
    # THE PROPERTY: a pass's state may be older than another's, never newer than its own
    # seq allows. A sampled a log stopping at it-1, so two items is the most it can carry.
    assert a_items <= 2, (
        f"pass A returned {a_items} items at seq {a_seq} while B reached seq {b_seq} with "
        f"{b_items}: A is wearing B's state under its own older number"
    )


def test_the_recorded_worst_case_cell_size_is_derived_not_asserted():
    """The byte figure the removed ceiling was decided on, re-derived here.

    ``crew-log-projection.md`` says the largest cell any slot fold can hold is ``radar``
    at its item cap, and that 64 of those is 60.8 MiB -- which is the whole argument for
    bounding the table by COUNT and adding no byte ceiling. A number in prose drifts; this
    recomputes it, so a fold whose state grows past what that decision assumed fails here
    instead of quietly invalidating the spec.

    A band rather than an exact byte, because the decision rests on the order of
    magnitude: the point is that a full table stays near the 64 MiB a byte ceiling would
    have spent, not that it is 63,701,888 bytes.
    """
    slot = "dashboard:worst-cell"
    handle = _slot_log("s-worst", slot)
    label = "n" * crew_log.TEXT_LIMIT
    appended = 0
    for number in range(crew_log.RADAR_ITEM_LIMIT + 4):
        handle.append(
            RADAR_ENTRY_TYPE,
            {
                "crew_id": label[:64],
                "repo": label[:64],
                "owner": label[:64],
                "number": number + 1,
                "event": label[:200],
                "event_kind": "investigate",
                "phase": "investigating",
                "why": label[:200],
                "next": label[:200],
                "branch": label[:100],
                "worktree": label[:200],
                "base_sha": "a" * 40,
                "outcome": label[:120],
                "decision": label[:120],
            },
            src=GATEWAY,
        )
        appended += 1
    assert appended > crew_log.RADAR_ITEM_LIMIT, "the fixture must push past the item cap"

    folded = crew_log.fold_slot_warm("radar", ("s-worst",), slot=slot)
    cell = len(json.dumps(folded.state, ensure_ascii=False).encode("utf-8"))
    table = cell * crew_log.SLOT_FOLD_CACHE_SLOTS
    recorded = 60.8 * 1024 * 1024
    assert 0.65 * recorded <= table <= 1.35 * recorded, (
        f"a full table of worst-case cells measures {table:,} bytes "
        f"({table / 1024 / 1024:.1f} MiB); the spec records 60.8 MiB and the decision to "
        "bound by count alone rests on it -- re-measure and update both"
    )


def test_a_finished_fold_keeps_no_lock_for_the_slot_it_folded():
    """MUTATION-SENSITIVE: the lock table holds passes IN FLIGHT, not slots ever folded.

    The per-key lock closes the pairing race, and it has to outlive a pass that is
    WAITING for it -- hand the next caller a different object and the race is open again.
    Both together mean the table is keyed on the passes running right now: one entry while
    a fold is inside it, and nothing at all once the last holder leaves.

    The alternative is a table keyed on every (home, slot, fold) the process has ever
    folded, which nothing bounds -- ``SLOT_FOLD_CACHE_SLOTS`` caps the memos beside it and
    says nothing about this.
    """
    inner_vouched = crew_log._vouched
    inside: list[int] = []

    def watching(unit_id, before):
        # Mid-pass: this key's lock is held, so it must still be in the table.
        inside.append(len(crew_log._slot_fold_locks))
        return inner_vouched(unit_id, before)

    with unittest.mock.patch.object(crew_log, "_vouched", watching):
        for index in range(3):
            unit = f"s-lock-{index}"
            slot = f"dashboard:lock-{index}"
            handle = _slot_log(unit, slot)
            _work_entry(handle, "it-0", slot=slot)
            crew_log.fold_slot_warm("work", (unit,), slot=slot)

    assert inside, "no pass reached the window, so this measured nothing"
    assert max(inside) == 1, (
        f"a pass saw {max(inside)} lock entries while holding one key's lock; the table "
        "should hold the pass in flight and nothing else"
    )
    assert crew_log._slot_fold_locks == {}, (
        f"three finished folds left {len(crew_log._slot_fold_locks)} lock(s) behind: "
        f"{sorted(crew_log._slot_fold_locks)} -- the table grows with slots seen"
    )


def test_a_worker_that_will_not_stop_fails_the_case_instead_of_outliving_it():
    """MUTATION-SENSITIVE: a stop that cannot finish is reported, never assumed.

    The worker is a process-wide daemon and a case's data home is pinned by an env var
    that its ``monkeypatch`` undoes on teardown. So a batch still folding after
    :func:`eager.stop_for_tests` returns is a write landing in whatever home the
    environment names once that pin is gone -- and the case that leaked the batch is never
    the case that fails.

    A join that expires therefore RAISES, which is what ``drain_breadcrumb_writes`` does
    in ``test/conftest.py`` for the same shape of leak: the stop is the only moment at
    which the leak is still attributable.
    """
    monkeypatch = pytest.MonkeyPatch()
    inside = threading.Event()
    release = threading.Event()

    def wedged(batch, closers=None):
        inside.set()
        release.wait(timeout=30.0)

    monkeypatch.setattr(eager, "_fold_batch", wedged)
    monkeypatch.setattr(eager, "_STOP_JOIN_SECONDS", 0.2)
    try:
        eager.note_commit(SESSION, "work/recorded", 1, board=WORK_SLOT)
        assert inside.wait(timeout=10.0), "the worker never entered the wedged batch"
        with pytest.raises(TimeoutError, match="crew-log-eager-fold"):
            eager.stop_for_tests()
        # The expiry KEEPS the handle. A later stop is the only thing that can ever join
        # this thread, so dropping it here would leave it running and unjoinable for the
        # life of the process -- and the counters it is still using are left alone with it.
        stranded = eager._worker
        assert (
            stranded is not None and stranded.is_alive()
        ), "the timed-out stop dropped its handle on a live worker; nothing can join it"
    finally:
        release.set()
        monkeypatch.undo()
        # The wedged batch is free now, so the retained handle is joinable and this case
        # leaves no thread behind for the next one.
        eager.stop_for_tests()
        assert eager._worker is None, "the second stop did not join the retained worker"


def test_an_entry_after_the_closer_leaves_its_fold_warm(monkeypatch):
    """MUTATION-SENSITIVE: one batch applies the closer and a LATER entry in that order.

    A ``session/closed`` is not the end of a unit's file -- an append after it is accepted
    (a reset closes a session and leaves its id mapped). So a batch can hold a closer AND
    an entry committed after it, and the two mean opposite things about the same memo:
    the closer says drop it, the entry says advance it.

    Applied by commit order the answer is both, in that order, and the fold ends warm at
    the newer entry. Applied by neither, the closer wins the key, the later entry's wake is
    discarded, and the drop erases what nothing then refolds -- so the next dashboard read
    pays the whole history for an entry this process had already seen.
    """
    slot = WORK_SLOT
    handle = _slot_log("s-after-close", slot)
    handle.append("session/closed", {"reason": "reset"}, src=GATEWAY)
    closed_seq = handle.last_seq
    after = _work_entry(handle, "it-after", slot=slot)
    assert after > closed_seq, "the fixture must append AFTER the closer"

    # The worker exists but consumes nothing, so both wakes are still queued and this test
    # drives the coalesce-and-fold pass itself -- which is where the ordering lives.
    eager.stop_for_tests()
    monkeypatch.setattr(eager, "_run", lambda: None)
    eager.note_commit("s-after-close", "session/closed", closed_seq, board=slot)
    eager.note_commit("s-after-close", "work/recorded", after, board=slot)
    crew_log.forget_slot_folds()

    queued = eager._queue
    assert queued is not None, "the wakes were never queued"
    batch, closers, _taken = eager._coalesce(queued.get_nowait())
    eager._fold_batch(batch, closers)

    warm = {(key[1], key[2]) for key in crew_log._slot_memos}
    assert (slot, "work") in warm, (
        f"the entry after the closer left no warm fold; warm = {sorted(warm)}. The closer "
        "took the key and the later entry was discarded"
    )
    held = crew_log._slot_memos[(str(crew_log.data_home()), slot, "work")]
    reached = crew_log._slot_checkpoint("work", held)
    assert reached.last_seq == after, (
        f"the warm fold stands at seq {reached.last_seq}, not at the post-closer entry "
        f"{after}: the drop was applied after the advance"
    )


def test_a_closer_in_the_same_batch_as_an_earlier_entry_still_drops_the_slot(monkeypatch):
    """MUTATION-SENSITIVE: the closer is matched to its UNIT, not to a board it never names.

    The emitter passes a board only for the types that carry one: a ``work/recorded`` names
    the board in its own field, a ``session/closed`` has no such field and its wake carries
    none. So inside one batch the two wakes for a single unit arrive under different keys,
    and a closer looked up by (unit, board) is not found for the work wake it must suppress.

    What that costs is the whole point of the drop: the slot is forgotten and then folded
    again from the entry the closer already outranks, so a closed session is left warm.
    """
    slot = WORK_SLOT
    handle = _slot_log("s-close-batch", slot)
    seq = _work_entry(handle, "it-1", slot=slot)
    closed = handle.append("session/closed", {"reason": "done"}, src=GATEWAY)
    assert int(closed.seq) > seq

    eager.stop_for_tests()
    monkeypatch.setattr(eager, "_run", lambda: None)
    # Exactly what the emitter sends: the board for the work entry, nothing for the closer.
    eager.note_commit("s-close-batch", "work/recorded", seq, board=slot)
    eager.note_commit("s-close-batch", "session/closed", int(closed.seq))
    crew_log.forget_slot_folds()

    queued = eager._queue
    assert queued is not None, "the wakes were never queued"
    batch, closers, _taken = eager._coalesce(queued.get_nowait())
    eager._fold_batch(batch, closers)

    warm = {(key[1], key[2]) for key in crew_log._slot_memos}
    assert (slot, "work") not in warm, (
        f"a closed session was left warm; warm = {sorted(warm)}. The closer's wake carries "
        "no board, so matching it by board misses the entry it outranks"
    )


def test_a_workers_last_report_still_reaches_the_conductors_board_when_the_worker_closes(
    monkeypatch,
):
    """MUTATION-SENSITIVE: closing a unit drops ITS board, never a board it only writes to.

    A worker's final act is usually two entries: the report that names the CONDUCTOR's
    board, then its own ``session/closed``. Both land in one batch, and they are about
    different cells -- the closer about the worker's own slot, the report about the
    conductor's.

    So a wake is outranked by a closer on two conditions together, and each is load-bearing.
    The closer must be the wake's OWN unit's, because a seq numbers one unit's file and
    another unit's is not comparable to it. And the wake's target slot must be that unit's
    own board, because closing the worker says nothing about what the conductor's board
    should show. Drop either condition and the conductor's board loses a report it was
    handed -- the dashboard then refolds the whole history to find it.
    """
    conductor_slot = WORK_SLOT
    worker_slot = "dashboard:52"
    conductor = _slot_log("s-c-close", conductor_slot)
    worker = _slot_log("s-w-close", worker_slot)
    _work_entry(conductor, "it-1", slot=conductor_slot)
    conductor.append(
        "work/recorded",
        {
            "slot": conductor_slot,
            "actor": "conductor",
            "by": conductor_slot,
            "action": "bind",
            "item_id": "it-1",
            "worker_session_key": worker_slot,
        },
        src=GATEWAY,
    )
    report = worker.append(
        "work/recorded",
        {
            "slot": conductor_slot,
            "actor": "worker",
            "by": worker_slot,
            "action": "report",
            "item_id": "it-1",
            "status": "done",
            "summary": "the worker reported before closing",
        },
        src=GATEWAY,
    )
    closed = worker.append("session/closed", {"reason": "done"}, src=GATEWAY)
    assert int(closed.seq) > int(report.seq), "the closer must follow the report"

    eager.stop_for_tests()
    monkeypatch.setattr(eager, "_run", lambda: None)
    # Exactly what the emitter sends: the conductor's board on the report, none on the closer.
    eager.note_commit("s-w-close", "work/recorded", int(report.seq), board=conductor_slot)
    eager.note_commit("s-w-close", "session/closed", int(closed.seq))
    crew_log.forget_slot_folds()

    queued = eager._queue
    assert queued is not None, "the wakes were never queued"
    batch, closers, _taken = eager._coalesce(queued.get_nowait())
    eager._fold_batch(batch, closers)

    warm = {(key[1], key[2]) for key in crew_log._slot_memos}
    assert (conductor_slot, "work") in warm, (
        f"the conductor's board lost the report; warm = {sorted(warm)}. The worker's closer "
        "suppressed a wake aimed at a board that unit does not own"
    )
    assert (
        worker_slot,
        "work",
    ) not in warm, f"the closed worker's own board is still warm; warm = {sorted(warm)}"


def test_a_wake_after_retirement_starts_no_worker_until_the_next_test_resumes():
    """MUTATION-SENSITIVE: the gap between tests is fenced, the test itself is not.

    A retirement stops the worker, but the append writer is a thread of its own: an append
    can land after the stop returns, and ``note_commit`` would answer it by starting a
    replacement daemon -- which resolves the data home when it folds, by then whatever the
    environment names rather than the home the caller pinned. Draining the writer first
    narrows that window and cannot close it, because a test whose subject is a writer that
    cannot write owes entries forever.

    So retirement sets a fence and a wake inside it is dropped. What makes that safe rather
    than a silent switch-off is the other side: ``resume_for_tests`` lifts it before a test
    runs, so every test still meets a live worker and the real append path is still
    exercised.
    """
    eager.retire_for_tests()
    eager.note_commit(SESSION, "work/recorded", 1, board=WORK_SLOT)
    assert eager._worker is None, (
        "a wake inside the fence started a worker; its fold would resolve a data home no "
        "caller pinned"
    )
    assert eager._queue is None, "a wake inside the fence was queued for a stopped worker"

    eager.resume_for_tests()
    eager.note_commit(SESSION, "work/recorded", 1, board=WORK_SLOT)
    assert (
        eager._worker is not None
    ), "the fence outlived the resume, so this process would fold nothing eagerly again"


def test_a_late_wake_during_the_stop_starts_no_worker():
    """MUTATION-SENSITIVE: the fence covers the STOP window, not just after it.

    ``retire_for_tests`` drains the writer, then stops the worker, then fences. The bug
    the fence must close is not only a wake AFTER retirement returns -- it is a wake
    DURING the stop. ``flush`` waits a bounded 5s and discards its result, so the emit
    writer can still be live when ``stop_for_tests`` runs; a wake it lands between the old
    worker's ``join`` returning and the reset taking ``_lock`` would pass ``note_commit``
    and reach ``_ensure_worker``, which clears ``_stopping`` and starts a replacement
    daemon that the reset then orphans -- and that orphan resolves the data home once the
    caller's pin lifts.

    Setting the fence BEFORE the stop closes it: a wake landing anywhere inside the stop
    window is dropped and starts nothing. This drives that exact moment by injecting a
    ``note_commit`` from inside ``stop_for_tests`` -- the mid-stop late append -- and
    asserts no worker survives. Reverting the fence-before-stop ordering (setting
    ``_retired`` only after ``stop_for_tests`` returns) leaves the injected wake unfenced
    and this test catches the orphaned worker.
    """
    real_stop = eager.stop_for_tests

    def _stop_then_a_late_wake() -> None:
        # Run the real stop (join + reset _worker/_queue to None), THEN stand in for the
        # still-live emit writer landing an append in the window the stop just opened:
        # ``flush``'s 5s wait is bounded and its result discarded, so the writer can wake
        # after the reset. With the fence set BEFORE this call, the wake is dropped and
        # starts nothing. With the fence set only after ``stop_for_tests`` returns, the
        # wake reaches ``_ensure_worker`` here and starts a daemon nothing joins -- the
        # orphan that survives retirement and resolves an unpinned data home.
        real_stop()
        eager.note_commit(SESSION, "work/recorded", 1, board=WORK_SLOT)

    with unittest.mock.patch.object(eager, "stop_for_tests", _stop_then_a_late_wake):
        eager.retire_for_tests()

    assert eager._worker is None, (
        "a wake landing during the stop started a replacement worker; its fold would "
        "resolve a data home no caller pinned"
    )
    assert eager._retired.is_set(), "retirement returned without the fence in force"

    eager.resume_for_tests()
    eager.stop_for_tests()


def test_worker_creation_under_the_fence_hands_back_a_queue_and_starts_nothing():
    """MUTATION-SENSITIVE: the fence is re-read where the worker is CREATED, not only at the gate.

    ``note_commit``'s gate drops every wake that arrives after the latch goes up. It
    cannot cover the one already past it: a wake that read the latch as clear a few
    bytecodes earlier is still on its way into ``_enqueue``, and ``_enqueue`` asks for the
    queue -- which is where a worker gets started -- before it re-checks anything. So
    ``_ensure_worker`` reads the latch again under ``_lock``, the same lock retirement's
    reset takes, and refuses to start a replacement the reset would then orphan.

    Driven at the seam rather than through a thread race, because that race cannot be
    scheduled from a test: the fence is set and ``_ensure_worker`` is called directly, the
    way ``_enqueue`` calls it. BOTH halves are the contract. It must still hand back a
    queue, because the caller's ``put_nowait`` has to land somewhere and the queue is
    reaped by the next stop, and it must leave the worker stopped. Dropping the re-read
    starts a daemon right here, which this catches.
    """
    eager.retire_for_tests()
    assert eager._worker is None, "retirement left a worker running"

    pending = eager._ensure_worker()

    assert pending is not None, "a wake already past the gate was given no queue to land in"
    assert eager._worker is None, (
        "worker creation ignored the fence and started a daemon the reset would orphan; its "
        "fold would resolve a data home no caller pinned"
    )

    eager.resume_for_tests()
    eager.stop_for_tests()


def test_a_late_wake_after_the_reset_leaves_the_progress_counters_consistent():
    """MUTATION-SENSITIVE: a wake counted after retirement's reset must not skew parity.

    A wake that passed ``note_commit``'s fence check in the few bytecodes before
    ``retire_for_tests`` set the latch is still on its way into ``_enqueue``. If its
    ``_queued += 1`` lands AFTER the reset zeroes the counters, ``_queued`` sits at 1 with
    ``_settled`` at 0 and no worker to settle it (``_ensure_worker`` refuses to restart
    under the fence), so ``drain`` reads ``_settled >= _queued`` as forever false -- the
    counter corruption the second fenced finding named.

    ``_enqueue`` re-checks the fence inside ``_progress`` -- the same lock the reset takes
    -- so a wake arriving after the reset is dropped rather than counted. This drives that
    exact ordering: retire (which sets the fence and resets the counters to zero), then a
    late ``_enqueue`` past the entry gate, and asserts the counters stay consistent and
    ``drain`` reaches parity. Removing the in-``_progress`` fence re-check leaves
    ``_queued`` at 1 and ``drain`` never reaches parity, which this catches.
    """
    eager.retire_for_tests()
    assert eager._queued == 0 and eager._settled == 0, "retirement did not zero the counters"

    # A wake that got past note_commit's gate just before the latch, now reaching the
    # count under the fence. Drive _enqueue directly: note_commit's own gate would drop it
    # earlier, but the window this closes is the one INSIDE _enqueue, after the gate.
    eager._enqueue(eager._Wake(SESSION, "work/recorded", 1, WORK_SLOT))

    assert (
        eager._queued == 0
    ), "a wake counted after the reset skewed _queued; drain would never reach parity"
    assert eager.drain(timeout=0.0) is True, "the progress counters are not at parity"

    eager.resume_for_tests()


def test_a_stale_enqueue_onto_a_replaced_queue_does_not_skew_the_next_test(monkeypatch):
    """MUTATION-SENSITIVE: a wake holding a queue the reset replaced must not count.

    This is the window the ``_retired`` flag alone CANNOT close. A wake enters
    ``_enqueue`` and captures ``pending`` from ``_ensure_worker``, then pauses. Between
    that capture and its resume a whole test cycle turns over: teardown's
    ``stop_for_tests`` replaces ``_queue`` (a fresh queue for the next case) and the next
    ``resume_for_tests`` CLEARS ``_retired``. So when the wake resumes and re-checks the
    fence, ``_retired.is_set()`` reads False -- the flag has been cleared again -- and a
    fence-only guard would wave it through to ``pending.put_nowait`` + ``_queued += 1`` on
    the ORPHANED queue, incrementing the NEW test's counter for a wake no live worker will
    ever settle. ``drain`` then reads ``_settled >= _queued`` as forever false.

    The identity check ``pending is not _queue`` closes it: it asks whether the queue this
    call holds is still the live one, which stays true across the flag's set/clear cycle.
    Here the module is in a live, non-retired state (fresh queue, fence clear, exactly as
    it is when the paused wake resumes), and ``_ensure_worker`` is made to hand back a
    FOREIGN queue -- standing in for the queue the wake captured before it was replaced.
    With the identity check the stale wake is dropped and ``_queued`` stays 0; removing it
    leaves ``_queued`` at 1 against a queue no worker drains, which this catches via
    ``drain`` never reaching parity.
    """
    eager.resume_for_tests()
    eager.stop_for_tests()  # zero the counters and null the queue, as between cases
    assert eager._queued == 0 and eager._settled == 0, "reset did not zero the counters"
    assert not eager._retired.is_set(), "the fence is up; this test is about the fence being DOWN"

    # The live queue for the current case. _ensure_worker would normally return this; we
    # make it hand back a different object to model a wake that captured `pending` before
    # the queue was replaced under it.
    live = queue.Queue(maxsize=eager.QUEUE_LIMIT)
    orphan = queue.Queue(maxsize=eager.QUEUE_LIMIT)
    with eager._lock:
        eager._queue = live
    monkeypatch.setattr(eager, "_ensure_worker", lambda: orphan)

    eager._enqueue(eager._Wake(SESSION, "work/recorded", 1, WORK_SLOT))

    assert eager._queued == 0, (
        "a wake landing on a replaced (orphaned) queue counted against the live counter; "
        "the next test's drain would never reach parity"
    )
    assert orphan.qsize() == 0, "the stale wake was put on the orphaned queue instead of dropped"

    with eager._lock:
        eager._queue = None
    eager.resume_for_tests()


def test_a_stop_wakes_the_parked_worker_instead_of_waiting_out_its_poll(monkeypatch):
    """MUTATION-SENSITIVE: the stop wakes the worker; it does not wait for the poll to lapse.

    An idle worker is parked in ``queue.get(timeout=_POLL_SECONDS)``, and a
    ``threading.Event`` cannot interrupt a blocking ``get``. So setting the stop flag and
    joining does not stop the worker -- it waits for the current poll to time out, and the
    teardown of every case that started a worker pays that interval.

    The stop therefore puts a sentinel on the queue: the parked ``get`` returns at once, the
    loop sees the flag and leaves. Measured against a poll interval made LONG on purpose, so
    the margin is the whole interval rather than a few milliseconds -- a loaded runner cannot
    turn that into a false pass.
    """
    parked = 30.0
    monkeypatch.setattr(eager, "_POLL_SECONDS", parked)
    eager.resume_for_tests()

    # Start the worker and let it settle into its parked get.
    eager.note_commit(SESSION, "work/recorded", 1, board=WORK_SLOT)
    assert eager.drain(timeout=10.0), "the worker never folded its first wake"
    time.sleep(0.3)
    worker = eager._worker
    assert worker is not None and worker.is_alive(), "no worker to park"

    started = time.monotonic()
    eager.stop_for_tests()
    elapsed = time.monotonic() - started

    assert eager._worker is None, "the stop did not complete"
    assert elapsed < parked / 4, (
        f"the stop took {elapsed:.1f}s against a {parked:.0f}s poll interval: it waited for "
        "the parked get to lapse instead of waking it"
    )
