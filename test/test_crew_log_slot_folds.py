"""The slot-level crew-log folds on the projection kernel.

A slot's fold runs over the CONCATENATION of every unit it ran under, and a crew
log's ``seq`` restarts at 1 in each unit file -- so the kernel, which holds one
watermark per cell and drops an event at or below it, is handed an ORDINAL instead:
the entry's own seq plus the heights of the units before it.

The load-bearing test here is :func:`test_a_warm_read_equals_a_cold_fold`, which is
what makes the whole path safe to keep warm: whatever the incremental route does, the
value it serves is the one a fold from empty over the same units reaches. The rest pin
the cases that must NOT be continued -- a longer unit list, a recreated newest unit, an
older unit that grew -- because each of those is a wrong answer rather than a slow one.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import eager as crew_log_eager
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log import store as crew_log_store

SLOT = "chat-slotfold"
FIRST = "acp-first"
SECOND = "acp-second"
CREW = "crew-77"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Own data home, crew log on, and no warm fold carried between tests.

    The eager folder is silenced, for the reason
    ``test_work_fold_warm_read.py``'s fixture gives: this file counts ``CrewLog.iter_from``
    calls on the CLASS, so a background thread folding this same log has its reads land in
    the counter. Committing a ``work/recorded`` wakes exactly that thread, and whether its
    pass falls inside the counted window is a matter of how loaded the runner is -- which
    is a measurement of the machine, not of the read. The lazy contract measured here is
    unchanged either way: it is what the read path honours whenever no wake arrived.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    monkeypatch.setattr(crew_log_eager, "note_commit", lambda *a, **k: None)
    crew_log_emit.reset_caches()
    crew_log_eager.stop_for_tests()
    crew_log.forget_slot_folds()
    yield
    crew_log_emit.reset_caches()
    crew_log_eager.stop_for_tests()
    crew_log.forget_slot_folds()


def _unit(unit_id: str, *, slot: str = SLOT) -> None:
    """Create one session crew log, then drop the handle so it holds no lease."""
    CrewLog.create(lg.KIND_SESSION, unit_id, owner="owner", agent="kirocrew", slot=slot)


def _ledger(unit_id: str, **fields: Any) -> None:
    payload: dict[str, Any] = {"slot": SLOT}
    payload.update(fields)
    crew_log_emit.on_ledger_recorded(unit_id, payload)
    crew_log_emit.flush(timeout=5.0)


def _radar(unit_id: str, *, number: int, event: str, kind: str = "investigate") -> None:
    crew_log_emit.on_radar_recorded(
        unit_id,
        {
            "crew_id": CREW,
            "owner": "kirodotdev",
            "repo": "KiroCrew",  # brand-ok: the repo half of the slug, an identifier
            "number": number,
            "event": event,
            "event_kind": kind,
        },
    )
    crew_log_emit.flush(timeout=5.0)


def _work(unit_id: str, *, item_id: str, event: str) -> None:
    crew_log_emit.on_work_recorded(
        unit_id,
        {
            "slot": SLOT,
            "actor": "conductor",
            "by": "owner",
            "action": "create",
            "item_id": item_id,
            "title": event,
            "acceptance": {"kind": "human_approval"},
        },
        timeout=5.0,
    )
    crew_log_emit.flush(timeout=5.0)


def _normalized(value: dict[str, Any]) -> str:
    """One fold value as canonical JSON, so two of them compare byte for byte."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _cold(name: str, units: tuple[str, ...]) -> str:
    """*name* folded from EMPTY over *units* -- the answer the warm path must match."""
    return _normalized(crew_log.fold_slot(name, units, slot=SLOT).value)


def _warm(name: str, units: tuple[str, ...]) -> str:
    return _normalized(
        crew_log.projection_of(crew_log.fold_slot_warm(name, units, slot=SLOT)).value
    )


# --------------------------------------------------------------------------- #
# warm equals cold
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", ["ledger", "radar", "work"])
def test_a_warm_read_equals_a_cold_fold(name):
    """After N appends across two units, the continued value IS the from-empty value.

    Read BETWEEN the appends, which is the only way the warm path is exercised at all:
    a first read folds cold whatever happens, so a test that read once at the end would
    pass with the incremental route never taken.

    ``moved`` is the guard against the way this test can pass while proving nothing. Two
    empty folds compare equal, so an append the emitter REFUSES -- a payload missing a
    declared field -- would leave every comparison trivially true.
    """
    _unit(FIRST)
    _unit(SECOND)
    units = (FIRST, SECOND)
    appended = {
        "ledger": lambda unit, n: _ledger(unit, goal=f"goal {n}", event=f"e{n}", event_kind="note"),
        "radar": lambda unit, n: _radar(unit, number=n, event=f"e{n}"),
        "work": lambda unit, n: _work(unit, item_id=f"it_{n:08x}", event=f"e{n}"),
    }[name]
    moved = {
        "ledger": lambda value: bool(value["events"]),
        "radar": lambda value: bool(value["events"]),
        "work": lambda value: bool(value["conductor"]["entries"]),
    }[name]

    for step in range(1, 4):
        appended(FIRST, step)
        assert _warm(name, units) == _cold(name, units)
    for step in range(4, 7):
        appended(SECOND, step)
        assert _warm(name, units) == _cold(name, units)

    assert _warm(name, units) == _cold(name, units)
    assert moved(json.loads(_warm(name, units))), "the appends landed and the fold moved"


def test_a_warm_read_of_an_unchanged_slot_reads_no_unit_again(monkeypatch):
    """Nothing appended means nothing re-read -- the whole point of keeping it warm.

    This is what the memo's heights earn. They record what the pass FOLDED, so a slot
    that has not moved compares equal and the read answers from the cell; heights taken
    from the pre-read sample would sit one short of the cell after any append that
    landed mid-fold, and every later read would open the newest unit for a tail that is
    already folded.
    """
    _unit(FIRST)
    _ledger(FIRST, goal="g", event="opened", event_kind="progress")
    units = (FIRST,)
    crew_log.fold_slot_warm("ledger", units, slot=SLOT)

    reads: list[int] = []
    real = CrewLog.iter_from

    def counted(self, start, **kwargs):
        reads.append(start)
        return real(self, start, **kwargs)

    monkeypatch.setattr(CrewLog, "iter_from", counted)
    crew_log.fold_slot_warm("ledger", units, slot=SLOT)

    assert reads == []


def test_an_unchanged_slot_reads_no_unit_even_after_an_append_raced_the_fold():
    """The memo's height is what the pass folded, so a raced read still settles warm."""
    _unit(FIRST)
    _ledger(FIRST, goal="g", event="one", event_kind="progress")

    real_mark = crew_log._unit_mark
    calls = {"n": 0}

    def _one_short(unit_id: str):
        calls["n"] += 1
        mark = real_mark(unit_id)
        if calls["n"] == 1:
            # Only the HEIGHT is lowered: a real sample of a growing file reports the
            # file's own identity and fingerprint, and a stub that dropped either would
            # be standing in for a different situation than a raced append.
            return replace(mark, last_seq=mark.last_seq - 1)
        return mark

    crew_log._unit_mark = _one_short  # type: ignore[assignment]
    try:
        crew_log.fold_slot_warm("ledger", (FIRST,), slot=SLOT)
    finally:
        crew_log._unit_mark = real_mark  # type: ignore[assignment]

    held = crew_log._slot_memos[(str(crew_log.data_home()), SLOT, "ledger")]
    assert held.marks[-1].last_seq == real_mark(FIRST).last_seq
    assert held.reached == real_mark(FIRST).last_seq


def test_a_warm_read_after_one_append_reads_only_the_tail(monkeypatch):
    """The continuation starts ABOVE what it already folded, not at the file's start."""
    _unit(FIRST)
    _ledger(FIRST, goal="g", event="one", event_kind="progress")
    units = (FIRST,)
    before = crew_log.fold_slot_warm("ledger", units, slot=SLOT)
    _ledger(FIRST, event="two", event_kind="progress")

    reads: list[int] = []
    real = CrewLog.iter_from

    def counted(self, start, **kwargs):
        reads.append(start)
        return real(self, start, **kwargs)

    monkeypatch.setattr(CrewLog, "iter_from", counted)
    crew_log.fold_slot_warm("ledger", units, slot=SLOT)

    assert reads == [before.last_seq + 1]


def test_a_repeated_slot_projection_read_of_the_work_fold_reads_no_unit_again(monkeypatch):
    """The work fold had no cache of its own and folded cold on every read.

    It is the one consumer that kept no incremental path, so it gains one purely by
    ``read_slot_projection`` going through the shared fold -- which is what this counts.

    The residual reads are NOT the fold's. The work fold's units are discovered by
    scanning for the entries that name this board (``_work_units``), which happens before
    the fold is asked for anything; that scan is a separate cost and is measured on its
    own here, so the assertion says what it means: the FOLD reads nothing the second
    time.
    """
    _unit(FIRST)
    _work(FIRST, item_id="it_00000001", event="first item")
    first = crew_log.read_slot_projection(SLOT, "work")
    assert first.value["conductor"]["entries"] == 1

    reads: list[int] = []
    real = CrewLog.iter_from

    def counted(self, start, **kwargs):
        reads.append(start)
        return real(self, start, **kwargs)

    monkeypatch.setattr(CrewLog, "iter_from", counted)
    again = crew_log.read_slot_projection(SLOT, "work")
    whole_read = list(reads)
    reads.clear()
    crew_log._slot_units_for_fold(SLOT, "work")
    discovery = list(reads)

    assert whole_read == discovery, "every remaining read is unit discovery, not the fold"
    assert _normalized(again.value) == _normalized(first.value)


def test_a_repeated_ledger_slot_projection_read_reads_nothing_at_all():
    """The ledger fold's units come from the recorded order, so a repeat reads NO file.

    The work fold cannot reach this because it discovers its units by scanning; the
    ledger's owner keeps an order log, which is what makes a warm read free here.
    """
    _unit(FIRST)
    _ledger(FIRST, goal="g", event="a", event_kind="progress")
    units = crew_log._slot_units_for_fold(SLOT, "ledger")
    crew_log.fold_slot_warm("ledger", units, slot=SLOT)

    reads: list[int] = []
    real = CrewLog.iter_from

    def counted(self, start, **kwargs):
        reads.append(start)
        return real(self, start, **kwargs)

    CrewLog.iter_from = counted  # type: ignore[assignment]
    try:
        crew_log.fold_slot_warm("ledger", units, slot=SLOT)
    finally:
        CrewLog.iter_from = real  # type: ignore[assignment]

    assert reads == []


def test_the_checkpoint_reports_the_newest_units_own_seq_not_an_ordinal():
    """The writer advances this checkpoint over an entry whose seq comes from its FILE.

    An ordinal -- the seq plus the earlier units' heights -- would sit far above that
    entry, and ``advance`` would refuse it as already folded, so the record a write
    answers with would come back empty.
    """
    _unit(FIRST)
    _unit(SECOND)
    _ledger(FIRST, goal="first", event="a", event_kind="progress")
    _ledger(SECOND, event="b", event_kind="progress")
    units = (FIRST, SECOND)

    warm = crew_log.fold_slot_warm("ledger", units, slot=SLOT)
    cold = crew_log.fold_slot_checkpoint("ledger", units, slot=SLOT)

    assert warm.last_seq == cold.last_seq
    handle = CrewLog.open(lg.KIND_SESSION, SECOND)
    try:
        assert warm.last_seq == handle.last_seq
    finally:
        del handle


# --------------------------------------------------------------------------- #
# what must NOT be continued
# --------------------------------------------------------------------------- #


def test_a_longer_unit_list_folds_cold():
    """A slot that started another session folds over the new list from empty."""
    _unit(FIRST)
    _ledger(FIRST, goal="first", event="a", event_kind="progress")
    assert _warm("ledger", (FIRST,)) == _cold("ledger", (FIRST,))

    _unit(SECOND)
    _ledger(SECOND, goal="second", event="b", event_kind="progress")

    assert _warm("ledger", (FIRST, SECOND)) == _cold("ledger", (FIRST, SECOND))
    assert json.loads(_warm("ledger", (FIRST, SECOND)))["goal"] == "second"


def test_a_newest_unit_recreated_with_a_lower_seq_folds_cold():
    """A removed and recreated log describes DIFFERENT bytes, whatever its seq says."""
    _unit(FIRST)
    _unit(SECOND)
    for step in range(3):
        _ledger(SECOND, goal=f"g{step}", event=f"e{step}", event_kind="progress")
    units = (FIRST, SECOND)
    assert _warm("ledger", units) == _cold("ledger", units)

    for path in crew_log.segment_paths(lg.KIND_SESSION, SECOND):
        path.unlink()
    crew_log_emit.reset_caches()
    _unit(SECOND)
    _ledger(SECOND, goal="after the recreate", event="fresh", event_kind="progress")

    warm = json.loads(_warm("ledger", units))
    assert warm == json.loads(_cold("ledger", units))
    assert warm["goal"] == "after the recreate"
    assert [event["text"] for event in warm["events"]] == ["fresh"]


def test_an_older_unit_that_grew_folds_cold():
    """An earlier unit is NOT closed to writes, and its new entry has to land.

    A forced reset tears a session down while a turn is still running, and that turn
    goes on appending through the handle it already holds. Continuing only the newest
    unit would leave those entries permanently outside the record.
    """
    _unit(FIRST)
    _unit(SECOND)
    _ledger(FIRST, goal="first", event="a", event_kind="progress")
    _ledger(SECOND, event="b", event_kind="progress")
    units = (FIRST, SECOND)
    assert _warm("ledger", units) == _cold("ledger", units)

    _ledger(FIRST, event="late from the older unit", event_kind="progress")

    warm = json.loads(_warm("ledger", units))
    assert warm == json.loads(_cold("ledger", units))
    assert "late from the older unit" in [event["text"] for event in warm["events"]]


def _rewrite_in_place(unit_id: str, old: str, new: str) -> None:
    """Replace *old* with *new* inside an entry already written, leaving every seq alone.

    Damage, or a history rewritten under a reader: the bytes a fold has already consumed
    say something different now, and nothing about how far the file goes has moved.
    """
    for path in crew_log.segment_paths(lg.KIND_SESSION, unit_id):
        text = path.read_text()
        if old in text:
            path.write_text(text.replace(old, new))
            return
    raise AssertionError(f"{old!r} was not in {unit_id}'s log to rewrite")


def _cut_back(unit_id: str, old: str, new: str) -> None:
    """What one of the store's recovery paths leaves behind, including its accounting.

    Those paths CUT the file back and then write over the seqs they freed, and the unit's
    cut counter is raised on either side of that. So a stand-in for them has to move that
    counter too: a rewrite without it stands for a hand-edit instead, which is a
    different event with different evidence.

    The NEWEST unit is settled by the counter, which is why this matters here and not
    for an earlier unit -- an earlier unit is held to its whole mark, and a rewrite
    there fails the comparison on its own.
    """
    _rewrite_in_place(unit_id, old, new)
    _a_completed_cut(lg.crew_log_path(lg.KIND_SESSION, unit_id).parent)


def test_an_earlier_unit_rewritten_in_place_folds_cold():
    """An already-folded entry can CHANGE without its seq moving, and must not be served.

    An identity and a height say how far a log goes, never what it says. An entry edited
    in place keeps its seq, so a reuse test reading those two alone compares equal and
    answers from a cell folded out of the old bytes -- and
    ``rebuild_from_projection`` writes that cell straight back over the record's header,
    items and events, which is how one stale read becomes the stored answer.

    The newest unit grows here too, which is the one shape a continuation may carry. That
    is what makes the earlier unit's fingerprint load-bearing rather than incidental:
    without it, this growth alone qualifies for the tail-only route and the rewritten
    entry is never read a second time.
    """
    _unit(FIRST)
    _unit(SECOND)
    _ledger(FIRST, goal="the original goal", event="a", event_kind="progress")
    _ledger(SECOND, event="b", event_kind="progress")
    units = (FIRST, SECOND)
    assert _warm("ledger", units) == _cold("ledger", units)

    _rewrite_in_place(FIRST, "the original goal", "the goal as rewritten underneath")
    _ledger(SECOND, event="c", event_kind="progress")

    warm = json.loads(_warm("ledger", units))
    assert warm == json.loads(_cold("ledger", units))
    assert warm["goal"] == "the goal as rewritten underneath"


def test_the_newest_unit_rewritten_under_a_grown_file_folds_cold():
    """Growth is not proof of an append, and the newest unit is admitted FOR growing.

    The store rewrites a committed prefix on two recovery paths -- an unreachable chunk
    group is truncated away and closers are appended with seqs continuing from the cut,
    and a failed append is truncated back after its bytes reached the disk -- and no
    reader holds the append lock while either runs. What both leave behind is a file that
    has grown since the last read and reuses seqs the fold already consumed, which every
    stat and every seq comparison reads as a plain append. Continuing on that evidence
    resumes above the rewritten entry and serves it from the old bytes for as long as the
    cell lives, and `rebuild_from_projection` writes that answer back over the record.

    What says so is the unit's CUT COUNTER, which the store raises before each of those
    truncations -- so the reader's evidence is a few bytes read rather than a hash of
    every record it ever folded, and a warm read costs the same on a log of ten entries
    and a log of a hundred thousand.

    An event text is what this asserts on, because events accumulate: a rewritten
    ``goal`` would be overwritten by any later entry carrying one, and the test would
    pass without the fold ever re-reading a thing.
    """
    _unit(FIRST)
    _ledger(FIRST, goal="g", event="ORIGINAL-ONE", event_kind="progress")
    _ledger(FIRST, goal="g", event="ORIGINAL-TWO", event_kind="progress")
    units = (FIRST,)
    assert _warm("ledger", units) == _cold("ledger", units)

    _cut_back(FIRST, "ORIGINAL-TWO", "REWRITTEN-UNDER-A-GROWN-FILE")
    crew_log_emit.reset_caches()
    _ledger(FIRST, goal="g", event="ORIGINAL-THREE", event_kind="progress")

    warm = json.loads(_warm("ledger", units))
    assert warm == json.loads(_cold("ledger", units))
    assert [event["text"] for event in warm["events"]] == [
        "ORIGINAL-ONE",
        "REWRITTEN-UNDER-A-GROWN-FILE",
        "ORIGINAL-THREE",
    ]


def test_a_slot_with_no_units_folds_to_the_empty_record():
    """The zero-unit fold answers, because a caller asks it before any log exists."""
    assert _warm("ledger", ()) == _cold("ledger", ())


def test_a_unit_with_no_log_is_skipped_rather_than_refused():
    """A slot whose oldest unit was collected by retention still folds the ones it has."""
    _unit(SECOND)
    _ledger(SECOND, goal="kept", event="a", event_kind="progress")
    units = ("acp-collected", SECOND)

    assert _warm("ledger", units) == _cold("ledger", units)
    assert json.loads(_warm("ledger", units))["goal"] == "kept"


def _held_cell_bytes() -> int:
    """What every warm cell is charged together, the way the eviction loop counts it."""
    return sum(memo.weight for memo in crew_log._slot_memos.values())


def _ledger_cell() -> int:
    """What FIRST's one ledger cell is charged, read off a real stored memo.

    The charge is ``(rows + 1) x`` the fold's row cost, and the row count is the fold's
    own counter's to give, so the figure is read rather than recomputed here.
    """
    crew_log.fold_slot_warm("ledger", (FIRST,), slot="chat-weigh")
    weight = next(m.weight for k, m in crew_log._slot_memos.items() if k[1] == "chat-weigh")
    crew_log.forget_slot_folds("chat-weigh")
    return weight


def test_the_warm_store_is_bounded_by_resident_bytes(monkeypatch):
    """Many slots cannot grow the store without limit; the oldest is evicted.

    By BYTES, through the operator's own variable: the ceiling here is eight ``ledger``
    cells, so a ninth slot evicts the first.
    """
    _unit(FIRST)
    _ledger(FIRST, goal="g", event="a", event_kind="progress")
    cell = _ledger_cell()
    monkeypatch.setenv(crew_log.SLOT_FOLD_CACHE_BYTES_ENV, str(cell * 8))
    for index in range(16):
        crew_log.fold_slot_warm("ledger", (FIRST,), slot=f"chat-{index}")

    assert _held_cell_bytes() <= cell * 8
    assert len(crew_log._slot_memos) == 8, (
        f"a budget of eight ledger cells holds {len(crew_log._slot_memos)}; the charge is "
        "not what each cell is charged"
    )


class _GuardedOnly(dict):
    """A warm store that refuses any access while the guard is NOT held.

    Reads and writes run on worker threads, so the store's get, set and eviction must
    all happen under ``_slot_memo_guard``: an eviction is two steps -- pick the oldest
    key, pop it -- and two threads taking them unguarded can pop the same key or resize
    the dict under the other's iterator. This dict makes an unguarded access a failure
    the test sees rather than a race a slot sees.
    """

    def _held(self):
        assert crew_log._slot_memo_guard.locked(), "warm store touched without the guard"

    def get(self, key, default=None):
        self._held()
        return super().get(key, default)

    def __setitem__(self, key, value):
        self._held()
        super().__setitem__(key, value)

    def pop(self, key, *default):
        self._held()
        return super().pop(key, *default)

    def __iter__(self):
        self._held()
        return super().__iter__()


def test_every_warm_store_access_holds_the_guard(monkeypatch):
    monkeypatch.setattr(crew_log, "_slot_memos", _GuardedOnly())
    _unit(FIRST)
    _ledger(FIRST, goal="g", event="a", event_kind="progress")

    # The write path's remember, then the read path's get.
    assert crew_log.fold_slot_warm("ledger", (FIRST,), slot=SLOT).state["goal"] == "g"
    assert crew_log.fold_slot_warm("ledger", (FIRST,), slot=SLOT).state["goal"] == "g"
    cell = next(m.weight for k, m in crew_log._slot_memos.items() if k[1] == SLOT)
    # Fill past the budget so the eviction loop runs under the guard too.
    monkeypatch.setenv(crew_log.SLOT_FOLD_CACHE_BYTES_ENV, str(cell * 4))
    for index in range(8):
        crew_log.fold_slot_warm("ledger", (FIRST,), slot=f"chat-guard-{index}")

    assert len(crew_log._slot_memos) == 4
    crew_log.forget_slot_folds(SLOT)


def test_forgetting_one_slots_fold_leaves_another_slots_warm():
    """The drop is scoped, so one test's isolation does not cost every slot its cell."""
    _unit(FIRST)
    _ledger(FIRST, goal="g", event="a", event_kind="progress")
    crew_log.fold_slot_warm("ledger", (FIRST,), slot="chat-a")
    crew_log.fold_slot_warm("ledger", (FIRST,), slot="chat-b")

    crew_log.forget_slot_folds("chat-a")

    held = {key[1] for key in crew_log._slot_memos}
    assert "chat-a" not in held
    assert "chat-b" in held


# --------------------------------------------------------------------------- #
# The push revision
# --------------------------------------------------------------------------- #


def _revision(units: "tuple[str, ...]", name: str = "ledger") -> int:
    """The revision the read path reports for *name* over *units*."""
    return crew_log.fold_slot_warm_revised(name, units, slot=SLOT)[1]


def test_a_fold_that_advanced_reports_a_higher_revision():
    """The base case: something new folded, so the number a client orders by moved."""
    _unit(FIRST)
    _ledger(FIRST, goal="g", event="a", event_kind="progress")
    first = _revision((FIRST,))

    _ledger(FIRST, event="b", event_kind="progress")
    second = _revision((FIRST,))

    assert first > 0, "a slot read has to vouch for a revision, or nothing can be pushed"
    assert second > first, (
        f"the revision stood at {first} and is {second} after an entry folded; a client "
        "ordering frames by it would discard the new value"
    )


def test_a_fold_that_found_nothing_new_keeps_its_revision():
    """MUTATION-SENSITIVE: an idle board produces no new number, so it produces no frame.

    Minting on every read instead would push the same value to every client on every
    dashboard poll -- which is the cost eager folding exists to remove, re-added on the
    socket instead of the read.
    """
    _unit(FIRST)
    _ledger(FIRST, goal="g", event="a", event_kind="progress")
    held = _revision((FIRST,))

    assert _revision((FIRST,)) == held
    assert _revision((FIRST,)) == held, (
        "a second read of an unmoved board minted a new revision; every poll would then "
        "look like a change"
    )


def test_the_revision_rises_across_a_new_unit_even_though_the_seq_falls():
    """THE CASE THE WHOLE CONTRACT EXISTS FOR: seq is not orderable, revision is.

    A slot fold's ``last_seq`` is the NEWEST unit's own, and a fresh unit starts at 1 --
    so adding a unit to a board can move the folded VALUE while moving the seq DOWN. A
    client ordering frames by seq discards the newer value; the revision is what it orders
    by instead.
    """
    _unit(FIRST)
    for step in range(5):
        _ledger(FIRST, goal=f"g{step}", event=f"e{step}", event_kind="progress")
    before, first_revision = crew_log.fold_slot_warm_revised("ledger", (FIRST,), slot=SLOT)

    _unit(SECOND)
    _ledger(SECOND, event="from the new unit", event_kind="progress")
    after, second_revision = crew_log.fold_slot_warm_revised("ledger", (FIRST, SECOND), slot=SLOT)

    assert after.last_seq < before.last_seq, (
        f"the fixture did not reproduce the case: seq went {before.last_seq} -> "
        f"{after.last_seq}, so this proves nothing about ordering by seq"
    )
    assert "from the new unit" in [
        event["text"] for event in after.state["events"]
    ], "the new unit's entry is not in the value, so there is no change to order"
    assert second_revision > first_revision, (
        f"the revision went {first_revision} -> {second_revision} while the value changed; "
        "the frame carrying it would be discarded as stale"
    )


def test_the_revision_rises_across_a_unit_recreated_under_the_same_id():
    """A recreated log folds cold, and the value after it must still be orderable.

    Its seq climbs back from 1, so the number can land at or below where it already was.
    The revision is minted by the folding process and never read off a file, which is what
    makes it rise here.
    """
    _unit(FIRST)
    _unit(SECOND)
    for step in range(4):
        _ledger(SECOND, goal=f"g{step}", event=f"e{step}", event_kind="progress")
    units = (FIRST, SECOND)
    before = _revision(units)

    for path in crew_log.segment_paths(lg.KIND_SESSION, SECOND):
        path.unlink()
    crew_log_emit.reset_caches()
    _unit(SECOND)
    _ledger(SECOND, goal="after the recreate", event="fresh", event_kind="progress")

    checkpoint, after = crew_log.fold_slot_warm_revised("ledger", units, slot=SLOT)
    assert checkpoint.state["goal"] == "after the recreate", "the cold refold did not happen"
    assert after > before, (
        f"the revision went {before} -> {after} across a recreated unit; a client would "
        "keep the retired file's value"
    )


def test_the_revision_rises_across_a_rewritten_prefix():
    """A prefix rewritten under a grown file folds cold, and that value must order too.

    The store rewrites a committed prefix on its recovery paths, which every stat and seq
    comparison reads as a plain append -- so the fold is redone and the value changes while
    the seq only grows by the one appended entry. The revision rises because a cold fold
    mints one.
    """
    _unit(FIRST)
    _ledger(FIRST, goal="g", event="ORIGINAL-ONE", event_kind="progress")
    _ledger(FIRST, goal="g", event="ORIGINAL-TWO", event_kind="progress")
    units = (FIRST,)
    before = _revision(units)

    _cut_back(FIRST, "ORIGINAL-TWO", "REWRITTEN-UNDER-A-GROWN-FILE")
    crew_log_emit.reset_caches()
    _ledger(FIRST, goal="g", event="ORIGINAL-THREE", event_kind="progress")

    checkpoint, after = crew_log.fold_slot_warm_revised("ledger", units, slot=SLOT)
    assert [event["text"] for event in checkpoint.state["events"]] == [
        "ORIGINAL-ONE",
        "REWRITTEN-UNDER-A-GROWN-FILE",
        "ORIGINAL-THREE",
    ]
    assert after > before, (
        f"the revision went {before} -> {after} across a rewritten prefix; the client "
        "would hold the pre-rewrite value"
    )


def test_the_revision_never_repeats_across_two_folds_and_two_slots():
    """One counter for every key, so a number identifies one value of one board.

    A per-key counter would have to live in the memo table, and that table is EVICTED --
    an evicted key's next cell would restart low and a client holding the earlier number
    would discard every value after it.
    """
    _unit(FIRST)
    _ledger(FIRST, goal="g", event="a", event_kind="progress")
    _radar(FIRST, number=1, event="looked")

    seen = [
        crew_log.fold_slot_warm_revised("ledger", (FIRST,), slot=SLOT)[1],
        crew_log.fold_slot_warm_revised("radar", (FIRST,), slot=SLOT)[1],
        crew_log.fold_slot_warm_revised("ledger", (FIRST,), slot="chat-other")[1],
        crew_log.fold_slot_warm_revised("radar", (FIRST,), slot="chat-other")[1],
    ]

    assert len(set(seen)) == len(seen), f"two cells share a revision: {seen}"
    assert seen == sorted(seen), f"the counter went backwards across keys: {seen}"


def test_an_evicted_cell_refolds_to_a_higher_revision_not_a_lower_one(monkeypatch):
    """MUTATION-SENSITIVE: eviction must not reset the number a client has already seen.

    This is what the process-wide counter buys. Hold the revision for a board, evict its
    cell through the byte budget, then read it again: the refold is cold and its revision
    has to be ABOVE the one the client holds, or the client discards the value it just
    asked for and the board freezes for the life of the tab.
    """
    _unit(FIRST)
    _ledger(FIRST, goal="g", event="a", event_kind="progress")
    held = _revision((FIRST,))

    # A budget of one ledger cell, so every further board evicts the one before it.
    monkeypatch.setenv(crew_log.SLOT_FOLD_CACHE_BYTES_ENV, str(_ledger_cell()))
    for index in range(4):
        crew_log.fold_slot_warm("ledger", (FIRST,), slot=f"chat-evict-{index}")
    assert (
        str(crew_log.data_home()),
        SLOT,
        "ledger",
    ) not in crew_log._slot_memos, (
        "the fixture did not evict the cell under test, so this measured nothing"
    )
    after = _revision((FIRST,))

    assert after > held, (
        f"a cold refold after eviction reported revision {after} against the {held} a "
        "client already holds; every later frame for this board would be discarded"
    )


def _a_completed_cut(directory) -> None:
    """Record a cut of *directory*'s log that has FINISHED, as the store records one.

    The store brackets every cut with a raise on either side of the bytes
    (``_open_cut`` / ``_close_cut``), so a stand-in that raised the counter once would
    leave it odd -- reported as a cut still in flight, which is a different reading with
    a different answer. Both halves, in the store's own order, so what this stands for is
    a completed mutation.
    """
    crew_log_store._open_cut(directory)
    crew_log_store._close_cut(directory)
