"""The slot fold's ON-DISK savepoint: resume after a restart, or fold cold.

The slot fold is kept warm in a process-wide table, and a gateway restart and an
eviction from the table's byte ceiling each empty it. Without a savepoint the next
read folds a slot's whole crew log from its first record, which is 70 s on a 644 MB
log; with one the state is read back beside the newest unit's log and only the tail
above it is folded.

The two load-bearing tests are
:func:`test_a_resumed_read_parses_nothing_below_the_savepoint`, which is the whole
point (a read with a cleared memo must not walk the log), and
:func:`test_a_resumed_read_equals_a_cold_fold_on_every_refused_shape`, which is the
rule the speed must never cost: every shape ``_continuable`` refuses in memory is a
shape the savepoint refuses on disk, and each one falls back to the cold fold rather
than to a different record.

Most tests lower ``MIN_ADVANCE_ENTRIES``, because the threshold is 256 entries and
appending that many through the emitter dominates the file's runtime.
:func:`test_a_slot_below_the_write_threshold_leaves_no_savepoint` pins the real
number.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import checkpoint as savepoints
from kiro_crew.crew_log import eager as crew_log_eager
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log import store as crew_log_store
from kiro_crew.projection import DirectoryCheckpointStore
from kiro_crew.session_ledger import _MAX_ARTIFACTS

SLOT = "chat-savepoint"
OTHER_SLOT = "chat-elsewhere"
FIRST = "acp-sp-first"
SECOND = "acp-sp-second"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, _floor_monkeypatch):
    """Own data home, crew log on, no warm cell carried in, eager folder silenced.

    The eager folder is silenced for the reason ``test_crew_log_slot_folds.py`` gives:
    this file counts ``CrewLog.iter_from`` on the CLASS, so a background thread folding
    the same log would land its reads in the counter and make the measurement a property
    of how loaded the runner is.

    Through ``_floor_monkeypatch``, the isolation floor's own instance, rather than the
    shared ``monkeypatch``: the two are undone independently, so a test in this file
    that calls ``monkeypatch.undo()`` to drop one of its own patches cannot also drop
    this fixture's ``KIROCREW_HOME`` pin and send the rest of the test at the operator's
    real data home.
    """
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _floor_monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    _floor_monkeypatch.setattr(crew_log_eager, "note_commit", lambda *a, **k: None)
    crew_log_emit.reset_caches()
    crew_log_eager.stop_for_tests()
    crew_log.forget_slot_folds()
    yield
    crew_log_emit.reset_caches()
    crew_log_eager.stop_for_tests()
    crew_log.forget_slot_folds()


@pytest.fixture
def _cheap_writes(monkeypatch):
    """Earn a savepoint write after two entries instead of 256.

    The threshold is a property of the files and is pinned on its own; what every test
    below is about is what happens once a file EXISTS, and paying 256 emitter round
    trips to reach one would make this file the slowest in the suite for nothing.
    """
    monkeypatch.setattr(savepoints, "MIN_ADVANCE_ENTRIES", 2)


def _unit(unit_id: str, *, slot: str = SLOT) -> None:
    """Create one session crew log, then drop the handle so it holds no lease."""
    CrewLog.create(lg.KIND_SESSION, unit_id, owner="owner", agent="kirocrew", slot=slot)


def _ledger(unit_id: str, *, slot: str = SLOT, **fields: Any) -> None:
    payload: dict[str, Any] = {"slot": slot}
    payload.update(fields)
    crew_log_emit.on_ledger_recorded(unit_id, payload)
    crew_log_emit.flush(timeout=5.0)


def _steps(unit_id: str, first: int, last: int, *, slot: str = SLOT) -> None:
    for step in range(first, last + 1):
        _ledger(unit_id, slot=slot, next=f"step {step}", event=f"e{step}", event_kind="progress")


def _normalized(value: dict[str, Any]) -> str:
    """One fold value as canonical JSON, so two of them compare byte for byte."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _cold(units: tuple[str, ...], *, slot: str = SLOT) -> str:
    """The ledger folded from EMPTY over *units* -- the answer every route must match.

    ``fold_slot`` takes neither the memo nor the savepoint, so it is the from-scratch
    answer by construction rather than by a reset this test would have to remember.
    """
    return _normalized(crew_log.fold_slot("ledger", units, slot=slot).value)


def _read(units: tuple[str, ...], *, slot: str = SLOT) -> str:
    """The ledger through the warm route -- the one that resumes."""
    return _normalized(
        crew_log.projection_of(crew_log.fold_slot_warm("ledger", units, slot=slot)).value
    )


def _restart(units: tuple[str, ...], *, slot: str = SLOT) -> tuple[str, int]:
    """One read with the warm table cleared. ``(value, entries parsed)``.

    Clearing the table is what a gateway restart and a byte-ceiling eviction both leave
    behind, and it is the only part of a restart this read can see: the savepoint is on
    disk either way.

    The count comes from ``CrewLog.iter_from``, the one call every unit's stream goes
    through, because the number that says a replay was avoided is the ENTRIES PARSED.
    Seconds would say this host was fast.
    """
    crew_log.forget_slot_folds()
    parsed = {"entries": 0}
    real = CrewLog.iter_from

    def counted(self, *args, **kwargs):
        for entry in real(self, *args, **kwargs):
            parsed["entries"] += 1
            yield entry

    CrewLog.iter_from = counted  # type: ignore[method-assign]
    try:
        value = _read(units, slot=slot)
    finally:
        CrewLog.iter_from = real  # type: ignore[method-assign]
    return value, parsed["entries"]


def _savepoint_path(unit_id: str):
    return savepoints.slot_checkpoint_path(lg.KIND_SESSION, unit_id, "ledger")


def _stored_state(payload: dict[str, Any]) -> dict[str, Any]:
    """The fold state inside a savepoint payload, whatever framing it carries.

    The state rides as a JSON STRING rather than as an object, because the kernel store
    serializes with ``sort_keys=True`` and these folds age out ``next(iter(...))``. So a
    test reading or editing a stored state goes through the same encoding the writer
    used, and a test asserting key ORDER has an order to assert.

    The plain-object shape is accepted too, and that is not politeness: a helper that
    raised on it would make every test here fail with a ``KeyError`` about this function
    the moment the encoding went away, and a test whose failure names its own helper is
    evidence about the helper rather than about the code under test.
    """
    state = payload["state"]
    encoded = state.get(savepoints._SLOT_STATE_JSON) if isinstance(state, dict) else None
    return json.loads(encoded) if isinstance(encoded, str) else state


# --------------------------------------------------------------------------- #
# the replay is gone
# --------------------------------------------------------------------------- #


def test_a_resumed_read_parses_nothing_below_the_savepoint(_cheap_writes):
    """A read with the warm table cleared folds the tail, not the log.

    The count is against the log's own length, so the assertion fails on the behaviour
    this file exists to change: with no savepoint the read walks all six entries.
    """
    _unit(FIRST)
    _steps(FIRST, 1, 6)
    assert _read((FIRST,)) == _cold((FIRST,))
    assert _savepoint_path(FIRST).is_file(), "the read that folded cold wrote a savepoint"

    value, parsed = _restart((FIRST,))

    assert parsed == 0, "the whole log is below the savepoint, so nothing is re-parsed"
    assert value == _cold((FIRST,))


def test_a_resumed_read_parses_only_the_entries_appended_since(_cheap_writes):
    """The tail is what it costs, and the tail is what it reads."""
    _unit(FIRST)
    _steps(FIRST, 1, 6)
    _read((FIRST,))
    _steps(FIRST, 7, 9)

    value, parsed = _restart((FIRST,))

    assert parsed == 3, "only the three entries above the savepoint"
    assert value == _cold((FIRST,))


def test_a_resumed_read_joins_the_units_a_slot_ran_under(_cheap_writes):
    """A slot owns one session id at a time, so the record spans its units.

    The savepoint's state was folded over BOTH units and its ordinal base is derived
    from the earlier one's height, so a resume that got the base wrong would fold the
    tail onto ordinals the cell has already consumed and silently drop it.
    """
    _unit(FIRST)
    _steps(FIRST, 1, 4)
    _unit(SECOND)
    _steps(SECOND, 5, 8)
    units = (FIRST, SECOND)
    assert _read(units) == _cold(units)
    _steps(SECOND, 9, 11)

    value, parsed = _restart(units)

    assert parsed == 3, "the first unit is below the savepoint and is not re-read"
    assert value == _cold(units)
    assert json.loads(value)["next"] == "step 11"


def test_the_savepoint_moves_forward_as_the_log_grows(_cheap_writes):
    """A live gateway's cell brings its own savepoint forward.

    Without this the savepoint would sit wherever the process first folded, and the
    restart it exists for would replay every entry appended since -- which on a
    long-running gateway is the whole gap. A warm continuation may write because the
    cell CARRIES its prefix digest and ``_prefix_holds`` re-checks it, so custody of
    the already-folded bytes survives from the pass that folded them.
    """
    _unit(FIRST)
    _steps(FIRST, 1, 4)
    _read((FIRST,))
    first_written = _savepoint_path(FIRST).read_text(encoding="utf-8")

    _steps(FIRST, 5, 12)
    _read((FIRST,))  # a WARM continuation, not a cold fold
    moved = _savepoint_path(FIRST).read_text(encoding="utf-8")

    assert json.loads(moved)["watermark"] > json.loads(first_written)["watermark"]
    value, parsed = _restart((FIRST,))
    assert parsed == 0
    assert value == _cold((FIRST,))


def _reuse_the_tail_seqs(unit_id: str, keep_through_seq: int, fresh: int) -> None:
    """Drop the records above *keep_through_seq* and re-append *fresh* at the same seqs.

    The store does this to itself: its recovery truncates an unreachable chunk group
    away and appends closers with the seqs continuing from the cut, and a reader holds
    no append lock while that runs. To every stat and to every seq comparison the file
    has simply GROWN, and to a digest that stops at an earlier boundary it has not
    changed at all -- which is the whole reason a cell's evidence has to cover the bytes
    its own pass folded rather than the bytes its savepoint was written at.

    THE CUT IS COUNTED, because the store counts it: every one of those paths raises the
    unit's cut counter before it truncates. A stand-in that rewrote the file and left the
    counter alone would stand for a hand-edit instead, which is a different event with
    different evidence -- and the real repair is exercised end to end against the counter
    in ``test_crew_log_cut_counter.py``.
    """
    path = lg.store.crew_log_path(lg.KIND_SESSION, unit_id)
    _a_completed_cut(path.parent)
    kept: list[bytes] = []
    for line in path.read_bytes().split(b"\n"):
        if not line:
            continue
        seq = re.search(rb'"seq":(\d+)', line)
        if seq is None or int(seq.group(1)) <= keep_through_seq:
            kept.append(line)
    for index in range(fresh):
        seq = keep_through_seq + 1 + index
        kept.append(
            b'{"type":"ledger/recorded","seq":%d,"time":1700000000000,"src":"gateway",'
            b'"data":{"slot":"%s","next":"reused %d","event":"reused %d",'
            b'"event_kind":"progress"}}' % (seq, SLOT.encode(), seq, seq)
        )
    path.write_bytes(b"\n".join(kept) + b"\n")


def test_a_reused_tail_seq_after_a_resume_still_reads_as_the_cold_fold(_cheap_writes):
    """The bytes a RESUMED pass folded are covered by the cell's digest, or it folds cold.

    The hole this closes: a resumed pass stands on a savepoint written at seq S and
    folds the tail above it, so a digest taken from that savepoint ends at S while the
    state describes S+N. Carrying it leaves the next continuation asking only about the
    first S records -- and the store's own tail repair rewrites exactly the stretch above
    them, reusing the seqs. The prefix then compares equal, the file is taller, growth is
    admitted, and the cell keeps state folded from bytes the file has replaced.

    So the newest unit is rewritten after a resume, at the same seqs and then past them,
    and the read that follows must reach the from-empty answer.
    """
    _unit(FIRST)
    _steps(FIRST, 1, 6)
    assert _read((FIRST,)) == _cold((FIRST,))
    assert _savepoint_path(FIRST).is_file()
    _steps(FIRST, 7, 9)

    resumed, parsed = _restart((FIRST,))
    assert parsed == 3, "the resumed pass folded the tail"
    assert resumed == _cold((FIRST,))

    # The repair: seqs 7-9 go away and four records take their place, 7-10. The file is
    # TALLER than the cell, so a continuation is admitted on height alone.
    _reuse_the_tail_seqs(FIRST, keep_through_seq=6, fresh=4)
    value = _read((FIRST,))

    assert value == _cold((FIRST,))
    assert "reused 10" in value, "the rewritten tail is what the record now carries"
    assert "e9" not in value, "and the entries it replaced are gone from it"


def _ordered(value: dict[str, Any]) -> str:
    """One fold value as JSON with its key ORDER preserved.

    ``_normalized`` sorts, which is what makes two records comparable regardless of how
    they were built -- and exactly what would hide the defect below. These folds order
    their maps by INSERTION and age out the first one, so for an artifact map the order
    IS part of the value and has to be compared as one.
    """
    return json.dumps(value, separators=(",", ":"), default=str)


def test_a_resumed_artifact_map_keeps_the_order_it_was_recorded_in(_cheap_writes):
    """Insertion order survives the savepoint round trip, so the right key ages out.

    The kernel store serializes its payload with ``sort_keys=True``, so a state handed
    over as an object comes back alphabetical. ``_ledger_step`` evicts
    ``next(iter(artifacts))`` to age out the OLDEST pointer, so on a map whose keys are
    not already in alphabetical order a resumed state drops whichever key sorts first --
    and here that is the NEWEST artifact, recorded last.

    The keys are deliberately reverse-alphabetical, so sorted order and insertion order
    disagree at every position, and the comparison is order-sensitive.
    """
    oldest = f"k{_MAX_ARTIFACTS:03d}"
    _unit(FIRST)
    _steps(FIRST, 1, 6)
    # Recorded newest-last and in reverse alphabetical order, one per entry so each takes
    # its own insertion position. So sorted order and insertion order disagree at every
    # position, and the key that sorts FIRST is the one recorded LAST.
    for index in range(_MAX_ARTIFACTS):
        _ledger(FIRST, artifacts={f"k{_MAX_ARTIFACTS - index:03d}": f"v{index}"})

    # The setup guard, read off the fold's own value rather than off the file, so it
    # holds whatever framing the savepoint uses.
    recorded = list(crew_log.fold_slot("ledger", (FIRST,), slot=SLOT).value["artifacts"])
    assert (
        recorded == sorted(recorded, reverse=True) != sorted(recorded)
    ), "the recorded order is reverse alphabetical, so a sort is visible"

    crew_log.forget_slot_folds()
    _read((FIRST,))
    assert _savepoint_path(FIRST).is_file()

    # One more artifact, past the cap, so the map has to evict -- the eviction is what
    # reads the order. The first read after the restart is the one that resumes.
    crew_log.forget_slot_folds()
    _ledger(FIRST, artifacts={"k999": "pushes one out"})
    resumed = _ordered(
        crew_log.projection_of(crew_log.fold_slot_warm("ledger", (FIRST,), slot=SLOT)).value
    )
    crew_log.forget_slot_folds()
    cold = _ordered(crew_log.fold_slot("ledger", (FIRST,), slot=SLOT).value)

    artifacts = json.loads(resumed)["artifacts"]
    assert len(artifacts) == _MAX_ARTIFACTS
    assert "k999" in artifacts, "the newest artifact is kept"
    assert oldest not in artifacts, "the OLDEST artifact is the one dropped"
    assert resumed == cold, "and the whole record, key order included, is the cold one"

    stored = _stored_state(json.loads(_savepoint_path(FIRST).read_text(encoding="utf-8")))
    assert list(stored["artifacts"]) != sorted(
        stored["artifacts"]
    ), "the order on disk is the recorded one too, not the sorted one"


def test_a_repair_between_admission_and_the_pass_falls_back_to_a_cold_fold(_cheap_writes):
    """The window between admitting a savepoint and capturing the pass's digest.

    Every check on the resuming side can pass while the state is wrong, and that is why
    this needs a test of its own rather than reasoning. ``load_slot`` admits the
    savepoint against the file as it is THEN. The store's supersede repair replaces
    orphan chunk records at seqs the savepoint already consumed. The tail starts above
    those seqs, so the fold never reads the replacement; the pass's own digest is
    captured AFTER the repair, so it matches the repaired file and ``_vouched`` accepts
    it. The state came from bytes the repair removed, and nothing on that path disagrees.

    The repair is forced INSIDE the window by wrapping ``load_slot`` -- which is where
    the window opens -- so this reproduces the ordering rather than a rewrite that
    happens to land before or after it.
    """
    _unit(FIRST)
    _steps(FIRST, 1, 6)
    assert _read((FIRST,)) == _cold((FIRST,))
    _steps(FIRST, 7, 9)
    crew_log.forget_slot_folds()

    real = savepoints.load_slot
    fired: list[int] = []

    def repair_inside_the_window(*args: Any, **kwargs: Any):
        admitted = real(*args, **kwargs)
        if admitted is not None and not fired:
            fired.append(1)
            # Records at seqs 4-6 go away and four take their place. The savepoint
            # stands at 6 and the tail starts at 7, so the fold reads none of this.
            _reuse_the_tail_seqs(FIRST, keep_through_seq=3, fresh=6)
        return admitted

    crew_log_checkpoint_load = savepoints.load_slot
    try:
        savepoints.load_slot = repair_inside_the_window  # type: ignore[assignment]
        value = _read((FIRST,))
    finally:
        savepoints.load_slot = crew_log_checkpoint_load  # type: ignore[assignment]

    assert fired, "the repair ran inside the window"
    crew_log.forget_slot_folds()
    assert value == _cold((FIRST,))
    assert "reused 4" in value, "the record carries what the repair wrote"
    assert "e5" not in value, "and not the entries it replaced"


def test_two_restarts_in_a_row_each_resume_from_disk(_cheap_writes):
    """A resumed read leaves a savepoint the NEXT restart can use.

    The second half of the same defect, and the one that is pure cost rather than a
    wrong answer: a resumed pass that carried its savepoint's own witness would write one
    describing the boundary it RESUMED from rather than the one it reached, and the next
    read refuses exactly that. So every other restart would cold-fold, and the savepoint
    would look present and healthy on disk the whole time.
    """
    _unit(FIRST)
    _steps(FIRST, 1, 6)
    _read((FIRST,))
    _steps(FIRST, 7, 12)

    first_value, first_parsed = _restart((FIRST,))
    second_value, second_parsed = _restart((FIRST,))

    assert first_parsed == 6, "restart one folds the tail above the savepoint"
    assert second_parsed == 0, "restart two finds the savepoint restart one wrote"
    assert first_value == second_value == _cold((FIRST,))
    witness = json.loads(_savepoint_path(FIRST).read_text(encoding="utf-8"))["witness"]
    assert witness["seq"] == 12, "the witness names the boundary this pass reached"


def test_a_slot_below_the_write_threshold_leaves_no_savepoint():
    """The real 256-entry threshold, with nothing lowered.

    A short slot folds cheaply from the start, so a file for it would be a write spent
    on a replay worth nothing -- and the read is correct either way.
    """
    _unit(FIRST)
    _steps(FIRST, 1, 4)

    assert _read((FIRST,)) == _cold((FIRST,))
    assert not _savepoint_path(FIRST).exists()
    value, parsed = _restart((FIRST,))
    assert parsed == 4, "no savepoint, so the cold fold -- the behaviour before the fix"
    assert value == _cold((FIRST,))


# --------------------------------------------------------------------------- #
# resumed equals cold on every shape a continuation refuses
# --------------------------------------------------------------------------- #


# Every helper below edits a crew log IN BYTES, and that is a platform requirement
# rather than a style. The store appends with `newline="\n"`, so a crew log is LF-only
# on every platform; `read_text` / `write_text` go through universal newlines, which on
# Windows translates every LF back out as CRLF and rewrites the file one byte per line
# longer than it was. For a case that only needs the digest or the height to move, that
# is survivable; for the one case below that asserts nothing but the identity moved, it
# is the difference between forcing the shape and corrupting the fixture. Bytes have no
# such mode.


def _grow_an_earlier_unit(units: tuple[str, ...]) -> None:
    _steps(units[0], 90, 91)


def _rewrite_the_prefix(units: tuple[str, ...]) -> None:
    """Change an already-folded record, leaving its seq and the height alone.

    This is the one shape no stat can see: the store rewrites a committed prefix on its
    truncation recovery paths, and a reader holds no append lock while one runs, so the
    file grows and reuses seqs the fold already consumed. Nothing in an identity or a
    height tells it from a pure append.

    The CUT IS COUNTED, as those paths count theirs, so what this stands for is the
    store's own mutation rather than a hand-edit. The counter is the evidence that
    catches it, and on a unit that states none the digest is.
    """
    path = lg.store.crew_log_path(lg.KIND_SESSION, units[-1])
    lines = path.read_bytes().split(b"\n")
    for index, line in enumerate(lines):
        if b'"event":"e' in line:
            lines[index] = line.replace(b'"event":"e', b'"event":"rewritten-e')
            break
    else:  # pragma: no cover - the fixture always appends an event to the newest unit
        raise AssertionError("no folded entry to rewrite")
    _a_completed_cut(path.parent)
    path.write_bytes(b"\n".join(lines))


def _truncate_below_the_savepoint(units: tuple[str, ...]) -> None:
    """Cut the newest unit back, so the log is SHORTER than the savepoint stands at."""
    path = lg.store.crew_log_path(lg.KIND_SESSION, units[-1])
    lines = path.read_bytes().split(b"\n")
    path.write_bytes(b"\n".join(lines[:3]) + b"\n")


def _recreate_an_earlier_unit(units: tuple[str, ...]) -> None:
    """Give the oldest unit a new creation identity, and change NOTHING else.

    A seq comparison alone cannot tell a recreated log from a grown one once the new
    file has climbed back past the remembered height, which is why ``origin`` is in the
    identity block. ``origin`` is the header's ``createdAt`` beside the file's device
    and inode, so a same-length digit edit to that stamp is a fresh identity on the same
    inode -- and restoring the mtime afterwards leaves the mark's ``size`` and
    ``mtime_ns`` equal to what the savepoint recorded. So ``origin`` is the only field
    that moves, which makes this case isolate the one signal it is named for.

    Rewriting the stamp rather than removing and rebuilding the directory: a tree
    removal needs every handle in it closed, and the unit's own ``.lease`` is open on
    Windows, where ``rmtree`` then raises ``PermissionError`` instead of forcing the
    shape under test.
    """
    path = lg.store.crew_log_path(lg.KIND_SESSION, units[0])
    before = path.stat()
    header, separator, rest = path.read_bytes().partition(b"\n")
    stamp = re.search(rb'"createdAt":(\d+)', header)
    assert stamp is not None, "the header carries the creation stamp this case edits"
    digits = stamp.group(1)
    # Same LENGTH, different value: a longer or shorter number would move the file's
    # size, and this case has to leave every other mark field alone.
    fresh = digits[:-1] + (b"0" if digits[-1:] != b"0" else b"1")
    path.write_bytes(
        header.replace(b'"createdAt":' + digits, b'"createdAt":' + fresh) + separator + rest
    )
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert path.stat().st_size == before.st_size, "only the identity moved"


def _corrupt_the_savepoint(units: tuple[str, ...]) -> None:
    _savepoint_path(units[-1]).write_text("{not json", encoding="utf-8")


def _nest_a_malformed_state(units: tuple[str, ...]) -> None:
    """A state whose TOP level is the fold's shape and whose interior is not.

    The admission conditions read a state's top level, so this payload is accepted and
    then raises inside the fold -- the one failure that has to be answered by discarding
    the file, because the alternative is a read that 500s on every later call.
    """
    path = _savepoint_path(units[-1])
    payload = json.loads(path.read_text(encoding="utf-8"))
    state = _stored_state(payload)
    state["events"] = [{"ts": 1, "kind": [], "text": None}, 7]
    payload["state"] = {savepoints._SLOT_STATE_JSON: json.dumps(state)}
    path.write_text(json.dumps(payload), encoding="utf-8")


def _relabel_the_savepoint_slot(units: tuple[str, ...]) -> None:
    path = _savepoint_path(units[-1])
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["identity"]["slot"] = OTHER_SLOT
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize(
    "damage,expect_parsed",
    [
        (_grow_an_earlier_unit, "all"),
        (_rewrite_the_prefix, "all"),
        (_truncate_below_the_savepoint, "all"),
        (_recreate_an_earlier_unit, "all"),
        (_corrupt_the_savepoint, "all"),
        (_nest_a_malformed_state, "all"),
        (_relabel_the_savepoint_slot, "all"),
    ],
    ids=[
        "an-earlier-unit-grew",
        "the-prefix-was-rewritten",
        "the-log-is-shorter-than-the-savepoint",
        "an-earlier-unit-was-recreated",
        "the-file-is-not-readable",
        "the-state-cannot-be-folded",
        "the-file-names-another-slot",
    ],
)
def test_a_resumed_read_equals_a_cold_fold_on_every_refused_shape(
    _cheap_writes, damage, expect_parsed
):
    """Each shape a continuation refuses falls back to the COLD answer, not another one.

    ``_cold`` is recomputed after the damage, so the comparison is against what the
    files say NOW rather than against a value captured before. That is the rule the
    savepoint must not cost: a payload that cannot be trusted buys a slower read and
    never a different record.

    ``parsed`` is the guard against passing for the wrong reason. Two routes that both
    folded cold agree trivially, so each case also asserts the read really did walk the
    log -- without it, a resume that served stale state would still have to differ from
    ``_cold`` to fail, and a damage that happened to leave the record unchanged would
    hide it.
    """
    _unit(FIRST)
    _steps(FIRST, 1, 4)
    _unit(SECOND)
    _steps(SECOND, 5, 8)
    units = (FIRST, SECOND)
    assert _read(units) == _cold(units)
    assert _savepoint_path(SECOND).is_file(), "there is a savepoint for the damage to defeat"

    damage(units)

    value, parsed = _restart(units)

    assert value == _cold(units)
    assert parsed > 0, "the savepoint was refused and the log was folded"


def test_a_refused_savepoint_is_not_left_to_be_refused_forever(_cheap_writes):
    """A payload that could not be FOLDED is removed, so one read pays for it.

    A file that fails admission is cheap to reject again, but one that fails inside the
    fold costs a half-folded registry and an exception on every later read. It is
    discarded instead, and the next read's own write replaces it.
    """
    _unit(FIRST)
    _steps(FIRST, 1, 6)
    _read((FIRST,))
    _nest_a_malformed_state((FIRST,))

    value, parsed = _restart((FIRST,))

    assert value == _cold((FIRST,))
    assert parsed == 6, "the cold fold"
    replaced = _stored_state(json.loads(_savepoint_path(FIRST).read_text(encoding="utf-8")))
    assert replaced["events"], "the unusable payload was replaced rather than left on disk"


def test_a_savepoint_is_not_read_across_slots(_cheap_writes):
    """Two slots never share a cell, and never share a file.

    The identity block carries the slot key, so a unit that is newest for two slots
    costs them a cold fold each rather than letting one serve the other's state.
    """
    _unit(FIRST)
    _steps(FIRST, 1, 6)
    _read((FIRST,))

    value, parsed = _restart((FIRST,), slot=OTHER_SLOT)

    assert value == _cold((FIRST,), slot=OTHER_SLOT)
    assert parsed == 6


# --------------------------------------------------------------------------- #
# what the file is, and is not
# --------------------------------------------------------------------------- #


def test_the_slot_savepoint_does_not_share_a_file_with_a_session_fold(_cheap_writes):
    """A slot fold and a session fold of the same name live in separate directories.

    ``tools`` is read both ways, and the kernel store derives a file name from the fold
    name alone -- so one directory would have the two overwrite each other, and each
    would then be refused by the other's identity block for the life of the unit.
    """
    _unit(FIRST)
    _steps(FIRST, 1, 6)
    _read((FIRST,))

    slot_file = savepoints.slot_checkpoint_path(lg.KIND_SESSION, FIRST, "ledger")
    session_dir = savepoints.checkpoint_dir(lg.KIND_SESSION, FIRST)

    assert slot_file.is_file()
    assert slot_file.parent != session_dir
    assert slot_file.parent.name == savepoints.SLOT_CHECKPOINT_DIR


def test_a_cell_too_big_to_keep_warm_is_not_serialized_to_be_refused(_cheap_writes, monkeypatch):
    """A cell over the warm ceiling is skipped BEFORE the store measures it.

    The store decides a payload's size by serializing it, so a ``radar`` cell at its
    declared caps would build a 2.4 GiB string to find out it does not fit -- and because
    such a cell is not kept warm, every read of that slot would arrive here and do it
    again. The skip is the memory, not the refusal.
    """
    _unit(FIRST)
    _steps(FIRST, 1, 6)
    monkeypatch.setattr(crew_log, "slot_fold_cache_bytes", lambda: 1)
    offered: list[str] = []
    real = DirectoryCheckpointStore.save

    def counted(self, store, savepoint):
        offered.append(savepoint.key)
        return real(self, store, savepoint)

    monkeypatch.setattr(DirectoryCheckpointStore, "save", counted)

    assert _read((FIRST,)) == _cold((FIRST,))

    assert not _savepoint_path(FIRST).exists()
    assert offered == [], "the store was never handed a payload to measure"


def test_one_fold_name_read_both_ways_keeps_two_files(_cheap_writes):
    """``tools`` is a slot fold AND a session fold, and each keeps its own savepoint.

    This is the case the separate directory is FOR, and it is the one a shared one would
    break silently: the kernel store names a file from the fold name alone, so the second
    writer would overwrite the first, the first's next load would be refused by the
    other's identity block, and that fold would cold-fold for the life of the unit while
    every value stayed correct.
    """
    _unit(FIRST)
    for step in range(1, 7):
        crew_log_emit.on_tool_called(FIRST, 1, name=f"t{step}", call_id=f"c{step}")
    crew_log_emit.flush(timeout=5.0)

    crew_log.fold_slot_warm("tools", (FIRST,), slot=SLOT)
    crew_log.fold_session(FIRST, ("tools",))

    slot_payload = json.loads(
        savepoints.slot_checkpoint_path(lg.KIND_SESSION, FIRST, "tools").read_text(encoding="utf-8")
    )
    session_payload = json.loads(
        savepoints.checkpoint_path(lg.KIND_SESSION, FIRST, "tools").read_text(encoding="utf-8")
    )

    assert slot_payload["identity"]["slot"] == SLOT
    assert "slot" not in session_payload["identity"], "the session block names no slot"
    assert slot_payload["identity"]["unit"] == session_payload["identity"]["unit"] == FIRST


def test_the_savepoint_names_the_bytes_its_state_came_from(_cheap_writes):
    """The witness is the evidence a later read re-checks, so it must be there.

    An empty witness is a payload nothing can check, which ``prefix_admit`` refuses --
    so a write that forgot one would cost this slot every savepoint it ever wrote,
    silently, and every test above would still pass by folding cold.

    A unit the store seeded a cut counter on carries the COUNT, which is every unit
    created by this build. The digest spelling is what a log from before the counter
    gets, and ``test_crew_log_cut_counter.py`` holds that case.
    """
    _unit(FIRST)
    _steps(FIRST, 1, 6)
    _read((FIRST,))

    payload = json.loads(_savepoint_path(FIRST).read_text(encoding="utf-8"))

    assert payload["identity"]["slot"] == SLOT
    assert payload["identity"]["units"] == [FIRST]
    assert payload["identity"]["marks"] == []
    assert payload["witness"]["seq"] > 0
    assert payload["witness"]["prefix_cuts"] == 0, "no cut yet, and 0 is a real reading"
    assert "prefix_sha" not in payload["witness"], "one spelling, never both"
    assert payload["watermark"] == payload["witness"]["seq"], "one unit, so base zero"


def test_the_savepoint_records_every_earlier_unit_s_whole_mark(_cheap_writes):
    """Height alone describes a log by how FAR it goes, never by what it says.

    So the identity carries each earlier unit's fingerprint too, which is what makes an
    in-place rewrite of an already-folded entry a cold fold rather than an equal
    comparison.

    EVERY field of the mark, cut count included. The block exists to be admitted on the
    shapes a warm continuation is admitted on, and that comparison is whole-mark, so a
    field left out here would let a savepoint stand on an earlier unit the warm path
    refuses.
    """
    _unit(FIRST)
    _steps(FIRST, 1, 4)
    _unit(SECOND)
    _steps(SECOND, 5, 8)
    _read((FIRST, SECOND))

    payload = json.loads(_savepoint_path(SECOND).read_text(encoding="utf-8"))
    origin, height, size, mtime_ns, cuts = payload["identity"]["marks"][0]

    assert payload["identity"]["units"] == [FIRST, SECOND]
    assert isinstance(origin, str) and origin
    assert height == 4
    assert isinstance(size, int) and size > 0
    assert isinstance(mtime_ns, int) and mtime_ns > 0
    assert cuts == 0, "the earlier unit's own cut count, and 0 is a real reading"
    assert payload["watermark"] == height + payload["witness"]["seq"], "base plus the unit seq"


def test_a_savepoint_whose_two_positions_disagree_is_refused(_cheap_writes):
    """The ordinal and the witness seq are tied by the base, and the tie is checked.

    A payload claiming an ordinal that is not the newest unit's base plus the seq its
    witness certifies would resume the cell past entries the tail then folds again, or
    short of ones it never folds. Nothing in the file can say which of the two numbers
    is right, so it is not a savepoint of this fold.
    """
    _unit(FIRST)
    _steps(FIRST, 1, 6)
    _read((FIRST,))
    path = _savepoint_path(FIRST)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["watermark"] = payload["watermark"] + 1
    path.write_text(json.dumps(payload), encoding="utf-8")

    value, parsed = _restart((FIRST,))

    assert value == _cold((FIRST,))
    assert parsed == 6


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
