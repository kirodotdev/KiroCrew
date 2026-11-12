"""The slot carry: a slot's newest unit carries every slot fold, then retention may collect.

Two halves, each pinned against real crew logs on an isolated data home.

The FOLD half: a ``session/carried`` entry for the fold and the slot being folded
REPLACES the fold's state, so a slot fold over the units after a carry reaches the same
value whether or not the units before it are still on disk. An entry carried for another
slot or another fold, or one that does not decode to the fold's shape, changes nothing.

The WRITER half: a unit created for a slot that already had units gets one carry per
slot fold with state, and each older unit nothing still holds gets
``session/closed {superseded}``, which the sweep collects on age. A still-mapped unit, an
unreadable map, a missing probe and a live turn each keep the older unit as it was.

Every new unit a test expects carried is opened with the session key its turns run under,
as both of the gateway's openers state it; an opening that states none is not carried.
"""

from __future__ import annotations

import functools
import json
import time

import pytest
from crew_log_drain import assert_drained, settle

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog, eager, emit, projection
from kiro_crew.crew_log import session_tree_projection as stp
from kiro_crew.crew_log import store
from kiro_crew.session_ledger import _store_name
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, wait_until

SLOT = "chat-carry"
#: The session key SLOT's own dashboard turns run under; its ledger spelling is SLOT.
SLOT_SESSION_KEY = f"dashboard:{SLOT}"
OTHER_SLOT = "chat-other"
#: A caller spelling of SLOT whose ledger reads join the units headed with it.
CALLER_KEY = "cron:42"
#: A channel conversation's raw key, and the slot its filename fold names.
CHANNEL_KEY = "slack:1785370133.085469"
CHANNEL_SLOT = "slack_1785370133.085469"


def _ledger(goal: str, phase: str, event: str) -> dict:
    """One ``ledger/recorded`` payload, the shape the ledger's own writer appends."""
    return {
        "slot": SLOT,
        "goal": goal,
        "phase": phase,
        "event": event,
        "event_kind": "phase",
    }


def _unit(unit_id: str, slot: str = SLOT, *, previous: str = "") -> CrewLog:
    """A session unit whose header names *slot*, with its opening entry.

    *previous* names the unit the slot was writing before, which orders the slot's units
    by succession.
    """
    log = CrewLog.create(lg.KIND_SESSION, unit_id, owner="default", agent="kirocrew", slot=slot)
    opened: dict = {
        "agent": "kirocrew",
        "slot": slot,
        "model": "",
        "cwd": "",
        "owner": "default",
        "resumed": False,
    }
    if previous:
        opened["previous"] = {"sid": previous}
    log.append("session/opened", opened, src="gateway")
    return log


def _carry(log: CrewLog, fold: str, slot: str, state: dict) -> None:
    log.append(
        "session/carried",
        {
            "fold": fold,
            "slot": slot,
            "version": projection.fold_state_version(fold),
            "state": json.dumps(state),
        },
        src="gateway",
    )


def _entries(unit_id: str) -> list[dict]:
    path = lg.crew_log_root(lg.KIND_SESSION) / _store_name(unit_id) / "log.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _newest_lifecycle(unit_id: str) -> dict:
    lifecycle = [e for e in _entries(unit_id) if e["type"] in ("session/opened", "session/closed")]
    return lifecycle[-1]


def _sweep(now: float) -> "tuple[int, int]":
    """``store.sweep_expired(0, now=now)`` once the eager folder has folded every wake.

    The folder seals a closed unit's savepoints on its own thread, and a savepoint is
    written under a lease on its unit (``checkpoint._hold``), so a sweep that lands while
    one is written finds the unit owned and keeps it for the next pass. The writer is
    drained before this, so no wake arrives after the folder settles. Not held paused:
    the removal pauses the folder itself.
    """
    assert eager.drain(timeout=FALLBACK_TIMEOUT_SECS), "the eager folder never settled"
    return store.sweep_expired(0, now=now)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, _floor_monkeypatch):
    """Through the floor's own ``MonkeyPatch``, so a test's ``monkeypatch.undo()`` keeps it."""
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    yield


# --------------------------------------------------------------------------- #
# The fold half
# --------------------------------------------------------------------------- #


def test_a_carry_makes_the_slot_fold_independent_of_the_units_before_it():
    """The property retention rests on: the older unit stops changing the answer."""
    first = _unit("u-first")
    first.append("ledger/recorded", _ledger("ship it", "design", "planned"), src="gateway")
    carried = projection.fold_slot_checkpoint("ledger", ("u-first",), slot=SLOT).state
    second = _unit("u-second")
    _carry(second, "ledger", SLOT, carried)
    second.append("ledger/recorded", _ledger("ship it", "build", "started"), src="gateway")
    del first, second

    both = projection.fold_slot_checkpoint("ledger", ("u-first", "u-second"), slot=SLOT)
    alone = projection.fold_slot_checkpoint("ledger", ("u-second",), slot=SLOT)

    assert both.state == alone.state
    assert projection.projection_of(alone).value["goal"] == "ship it"
    assert projection.projection_of(alone).value["phase"] == "build"


def test_without_the_carry_the_older_unit_is_the_only_record_of_its_state():
    """The control for the test above: the same units with no carry lose the goal."""
    first = _unit("u-first")
    first.append("ledger/recorded", _ledger("ship it", "design", "planned"), src="gateway")
    second = _unit("u-second")
    second.append("ledger/recorded", {"slot": SLOT, "event": "noted"}, src="gateway")
    del first, second

    both = projection.fold_slot_checkpoint("ledger", ("u-first", "u-second"), slot=SLOT)
    alone = projection.fold_slot_checkpoint("ledger", ("u-second",), slot=SLOT)

    assert projection.projection_of(both).value["goal"] == "ship it"
    assert projection.projection_of(alone).value["goal"] != "ship it"


def test_the_warm_cell_applies_a_carry_the_way_the_cold_fold_does():
    first = _unit("u-first")
    first.append("ledger/recorded", _ledger("ship it", "design", "planned"), src="gateway")
    units = ("u-first",)
    projection.fold_slot_warm("ledger", units, slot=SLOT)
    carried = projection.fold_slot_checkpoint("ledger", units, slot=SLOT).state
    second = _unit("u-second")
    _carry(second, "ledger", SLOT, {**carried, "goal": "carried goal"})
    del first, second

    warm = projection.fold_slot_warm("ledger", ("u-first", "u-second"), slot=SLOT)
    cold = projection.fold_slot_checkpoint("ledger", ("u-first", "u-second"), slot=SLOT)

    assert warm.state == cold.state
    assert warm.state["goal"] == "carried goal"


def _goal_and_events(value: dict) -> tuple:
    return value.get("goal"), [event["text"] for event in value.get("events", [])]


def test_a_savepoint_written_mid_fold_on_a_carry_holds_the_carried_state(monkeypatch):
    """A cold fold writes savepoints part way (``SAVE_DURING_FOLD_ENTRIES``).

    One landing on the carry is stamped with the carry's position, so it must hold the
    carried state. One holding the state from before the carry would make every read
    resumed from it answer ``u-first``'s events where the carry put ``u-other``'s.
    """
    from kiro_crew.crew_log import checkpoint as savepoints
    from kiro_crew.crew_log import projection_slot_memo as memo_mod
    from kiro_crew.crew_log import slot_state_codec

    other = _unit("u-other")
    other.append("ledger/recorded", _ledger("carried goal", "build", "carried"), src="gateway")
    carried = projection.fold_slot_checkpoint("ledger", ("u-other",), slot=SLOT).state
    first = _unit("u-first")
    for i in range(10):
        first.append("ledger/recorded", _ledger("first goal", "design", f"e{i}"), src="gateway")
    second = _unit("u-second", previous="u-first")
    _carry(second, "ledger", SLOT, carried)
    second.append("ledger/recorded", _ledger("carried goal", "build", "after"), src="gateway")
    del other, first, second
    units = ("u-first", "u-second")
    # Every entry the stream yields from u-first (seq 1 up), then u-second's opening and
    # its carry, so the cadence lands on the carry.
    yielded = [e for e in _entries("u-first") if isinstance(e.get("seq"), int) and e["seq"] >= 1]
    monkeypatch.setattr(memo_mod, "SAVE_DURING_FOLD_ENTRIES", len(yielded) + 2)
    path = savepoints.slot_checkpoint_path(lg.KIND_SESSION, "u-second", "ledger")

    projection.forget_slot_folds()
    try:
        first_read = projection.fold_slot_warm("ledger", units, slot=SLOT)
        saved = json.loads(path.read_text(encoding="utf-8"))
        projection.forget_slot_folds()
        resumed = projection.fold_slot_warm("ledger", units, slot=SLOT)
    finally:
        projection.forget_slot_folds()
    cold = projection.fold_slot("ledger", units, slot=SLOT).value

    assert saved["witness"]["seq"] == 2, "the savepoint sits on u-second's carry"
    held = json.loads(slot_state_codec.decode_state(saved["state"]) or "null")
    assert held == json.loads(json.dumps(carried)), "it holds the carried state"
    assert _goal_and_events(cold) == ("carried goal", ["carried", "after"])
    assert _goal_and_events(projection.projection_of(first_read).value) == _goal_and_events(cold)
    assert _goal_and_events(projection.projection_of(resumed).value) == _goal_and_events(cold)


def test_a_carry_for_another_slot_or_another_fold_changes_nothing():
    """A unit of another slot joined into this fold carries THAT slot's state."""
    first = _unit("u-first")
    first.append("ledger/recorded", _ledger("ship it", "design", "planned"), src="gateway")
    stranger = _unit("u-stranger", slot=OTHER_SLOT)
    empty = projection.fold_slot_checkpoint("ledger", (), slot=OTHER_SLOT).state
    _carry(stranger, "ledger", OTHER_SLOT, empty)
    _carry(stranger, "goal", SLOT, projection.fold_slot_checkpoint("goal", (), slot=SLOT).state)
    del first, stranger

    joined = projection.fold_slot_checkpoint("ledger", ("u-first", "u-stranger"), slot=SLOT)

    assert projection.projection_of(joined).value["goal"] == "ship it"


@pytest.mark.parametrize(
    "data",
    [
        {"fold": "ledger", "slot": SLOT, "version": 1, "state": "not json"},
        {"fold": "ledger", "slot": SLOT, "version": 1, "state": json.dumps({"goal": "x"})},
        {"fold": "ledger", "slot": SLOT, "version": 1, "state": json.dumps(["a", "list"])},
        {"fold": "tools", "slot": SLOT, "version": 1, "state": json.dumps({})},
    ],
    ids=["unreadable", "wrong-shape", "not-an-object", "not-a-carried-fold"],
)
def test_a_carry_that_is_not_this_folds_state_is_ignored(data):
    from kiro_crew.crew_log.schema import Entry

    entry = Entry.from_dict(
        {"type": "session/carried", "seq": 3, "time": 1, "src": "gateway", "data": data}
    )
    assert entry is not None
    assert projection.carried_state(entry, data["fold"], SLOT) is None


def test_no_slot_means_no_carry_applies():
    """A session fold, or a caller that names no slot, never has its state replaced."""
    from kiro_crew.crew_log.schema import Entry

    state = projection.fold_slot_checkpoint("ledger", (), slot=SLOT).state
    data = {"fold": "ledger", "slot": SLOT, "version": 1, "state": json.dumps(state)}
    entry = Entry.from_dict(
        {"type": "session/carried", "seq": 3, "time": 1, "src": "gateway", "data": data}
    )
    assert entry is not None
    assert projection.carried_state(entry, "ledger", SLOT) == state
    assert projection.carried_state(entry, "ledger", "") is None


def test_only_a_carried_fold_is_woken_by_a_carry():
    """Every slot fold and tree fold is carried, and of the session folds only ``status``.

    ``usage`` and ``tools`` are read over a slot too, and lose the turns of collected
    units by decision (crew-log-core.md, "A slot's units are carried").
    """
    assert projection.CARRIED_FOLD_NAMES == frozenset(
        projection.SLOT_PROJECTION_NAMES
        + projection.INTERNAL_SLOT_PROJECTION_NAMES
        + projection.TREE_PROJECTION_NAMES
        + projection.CARRIED_SESSION_FOLD_NAMES
    )
    assert projection.CARRIED_SESSION_FOLD_NAMES == ("status",)
    assert projection.WORKTREE_FOLD_NAME in projection.CARRIED_FOLD_NAMES
    for name in projection.CARRIED_FOLD_NAMES:
        assert projection._FOLDS[name].touched_by_type("session/carried")
    for name in ("tools", "approvals", "usage"):
        assert not projection._FOLDS[name].touched_by_type("session/carried")


def test_a_tree_carry_publishes_the_work_order_the_tree_is_read_in():
    """The work tree reads the work order, so a carry of the tree alone publishes it too.

    ``u3`` holds the carry and ``u4`` opens after it. Left out of the order, ``u3`` would
    be read before ``u1`` and ``u2``, which recorded earlier, and their entries would be
    laid over the state it carries.
    """
    from kiro_crew import session_ledger
    from kiro_crew.crew_log import emit_session

    previous = ""
    for unit_id in ("u1", "u2", "u3", "u4"):
        _unit(unit_id, previous=previous)
        previous = unit_id
    session_ledger.note_work_unit_recorded(SLOT, "u1")
    session_ledger.note_work_unit_recorded(SLOT, "u2")

    emit_session._note_carried_order(SLOT, "u3", frozenset({projection.WORKTREE_FOLD_NAME}))

    assert session_ledger.work_crew_log_units(SLOT) == ("u1", "u2", "u3", "u4")
    units, _ = projection.tree_fold_units(SLOT, projection.WORKTREE_FOLD_NAME)
    assert units == ("u1", "u2", "u3", "u4")


def test_several_folds_folded_together_match_each_fold_on_its_own():
    first = _unit("u-first")
    first.append("ledger/recorded", _ledger("ship it", "design", "planned"), src="gateway")
    del first
    names = ("ledger", "goal", "worker")

    together = projection.fold_slot_checkpoints(names, ("u-first",), slot=SLOT)

    for name in names:
        alone = projection.fold_slot_checkpoint(name, ("u-first",), slot=SLOT)
        assert together[name] == alone


# --------------------------------------------------------------------------- #
# The writer half
# --------------------------------------------------------------------------- #


@pytest.fixture
def carrying(monkeypatch):
    """The live emitter with a session-key probe whose map is the yielded dict.

    The carry's fold runs inline instead of on the maintenance pool, so a drain covers
    the whole chain: the opening, the hop behind the predecessor, the carry and the
    closes. A value of ``RuntimeError`` in the map makes the probe raise for that id.
    """
    monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    emit.reset_caches()
    stp.reset_for_tests()
    monkeypatch.setattr(emit, "_submit_carry_fold", lambda job: job())
    mapped: dict[str, object] = {}

    def _probe(session_id: str) -> "str | None":
        held = mapped.get(session_id)
        if held is RuntimeError:
            raise RuntimeError("the map cannot be read")
        return held if isinstance(held, str) else None

    emit.set_session_key_probe(_probe)
    try:
        yield mapped
    finally:
        emit.set_session_key_probe(None)
        settle()
        tree = stp.projection()
        stp.reset_for_tests()
        wait_until(
            lambda: not tree._write_scheduled,
            describe=lambda: "the session tree's debounced checkpoint is still sleeping",
        )


def _two_units_of_one_slot() -> dict:
    """``u1`` with ledger state, then ``u2`` created for the same slot naming it."""
    emit.on_session_opened("u1", agent="kirocrew", slot=SLOT)
    emit.on_ledger_recorded("u1", _ledger("ship it", "design", "planned"))
    assert_drained()
    before = projection.read_slot_projection(SLOT, "ledger").value
    emit.on_session_opened(
        "u2", agent="kirocrew", slot=SLOT, previous_sid="u1", session_key=SLOT_SESSION_KEY
    )
    assert_drained()
    return before


@pytest.mark.usefixtures("carrying")
def test_a_new_unit_carries_its_slot_and_the_unit_it_replaced_is_closed_superseded():
    before = _two_units_of_one_slot()

    carried = [e["data"] for e in _entries("u2") if e["type"] == "session/carried"]
    assert "ledger" in {data["fold"] for data in carried}
    assert all(data["slot"] == SLOT for data in carried)
    close = _newest_lifecycle("u1")
    assert close["type"] == "session/closed"
    assert close["data"] == {"reason": "superseded", "successor": "u2"}
    assert _newest_lifecycle("u2")["type"] == "session/opened"
    assert projection.read_slot_projection(SLOT, "ledger").value["goal"] == before["goal"]


@pytest.mark.usefixtures("carrying")
def test_retention_then_collects_the_replaced_unit_and_the_slot_keeps_its_state():
    """The user's scenario end to end: the folds read the same before and after."""
    before = _two_units_of_one_slot()

    assert _sweep(time.time() + 60) == (1, 0)

    assert not CrewLog.exists(lg.KIND_SESSION, "u1")
    assert CrewLog.exists(lg.KIND_SESSION, "u2")
    after = projection.fold_slot_checkpoint("ledger", ("u2",), slot=SLOT)
    assert projection.projection_of(after).value["goal"] == before["goal"]
    assert projection.projection_of(after).value["phase"] == before["phase"]


@pytest.mark.usefixtures("carrying")
def test_the_ledger_reads_its_carry_last_so_no_event_is_read_twice():
    """The carry publishes its unit in the ledger's order file, through the ledger's writer.

    ``record_update`` names the unit that recorded in that file, and ``u2`` and ``u3``
    record nothing. A read with no live session (a nudge snapshot, the dashboard's ledger
    view) folds the slot's newest unit last even when the file does not name it
    (``session_ledger_control._causal_units``), but only while it is the newest. Once the
    slot opens ``u3``, an unpublished ``u2`` would fold FIRST and lay ``u1``'s whole
    history over the state ``u2`` carries, in every read and in ``u3``'s own carry, so
    every event would read twice even after ``u1`` and ``u2`` are collected. Each read
    starts cold, as a fresh process reads it.
    """
    from kiro_crew import session_ledger

    emit.on_session_opened("u1", agent="kirocrew", slot=SLOT)
    assert_drained()
    session_ledger.record_update(SLOT, session_id="u1", goal="ship it", event="planned")
    session_ledger.record_update(SLOT, session_id="u1", next_step="write tests", event="started")
    assert_drained()
    before = session_ledger.read_state(SLOT)
    emit.on_session_opened(
        "u2", agent="kirocrew", slot=SLOT, previous_sid="u1", session_key=SLOT_SESSION_KEY
    )
    assert_drained()
    projection.forget_slot_folds()

    carried = session_ledger.read_state(SLOT)
    order = session_ledger.crew_log_units(SLOT)
    emit.on_session_opened(
        "u3", agent="kirocrew", slot=SLOT, previous_sid="u2", session_key=SLOT_SESSION_KEY
    )
    assert_drained()
    projection.forget_slot_folds()
    moved = session_ledger.read_state(SLOT)
    moved_order = session_ledger.crew_log_units(SLOT)
    assert _sweep(time.time() + 60) == (2, 0)
    projection.forget_slot_folds()
    after = session_ledger.read_state(SLOT)

    assert [event["text"] for event in before["events"]] == ["planned", "started"]
    assert carried["events"] == before["events"]
    assert moved["events"] == before["events"]
    assert after["events"] == before["events"]
    assert after["goal"] == "ship it"
    assert order == ("u1", "u2")
    assert moved_order == ("u1", "u2", "u3")


def _ledger_view(state: dict) -> tuple:
    return state["goal"], state["next"], [event["text"] for event in state["events"]]


@pytest.mark.usefixtures("carrying")
def test_a_record_under_the_callers_own_spelling_keeps_reading_across_the_carry():
    """A reader keyed ``cron:<id>`` joins the units headed with that key, so the carry does.

    ``ua`` is headed with the caller's own key and holds its goal; ``u1`` is the unit the
    slot heads, recorded into under the same key. The read under that key folds ``ua``
    first, so a carry in ``u2`` folded over the slot's units alone would replace the goal
    with an empty one while ``ua`` is still on disk. ``ua`` is never closed, its header
    names another slot, and the read keeps it before and after ``u1`` is collected.
    Nothing reads before the carry, so the write alone records the join, and ``u2``'s
    opening states the slot's own key, so that record is the only thing that joins it.
    """
    from kiro_crew import session_ledger

    emit.on_session_opened("ua", agent="kirocrew", slot=CALLER_KEY)
    assert_drained()
    session_ledger.record_update(
        CALLER_KEY, session_id="ua", goal="written under the alias", event="alias-event"
    )
    emit.on_session_opened("u1", agent="kirocrew", slot=SLOT)
    assert_drained()
    session_ledger.record_update(
        CALLER_KEY, session_id="u1", next_step="written canonically", event="canon-event"
    )
    assert_drained()
    assert session_ledger._recorded_aliases(SLOT) == (CALLER_KEY,)
    emit.on_session_opened(
        "u2", agent="kirocrew", slot=SLOT, previous_sid="u1", session_key=SLOT_SESSION_KEY
    )
    assert_drained()
    projection.forget_slot_folds()

    carried = _ledger_view(session_ledger.read_state(CALLER_KEY, "u2"))
    canonical = _ledger_view(session_ledger.read_state(SLOT))
    assert _newest_lifecycle("u1")["data"] == {"reason": "superseded", "successor": "u2"}
    assert _sweep(time.time() + 60) == (1, 0)
    projection.forget_slot_folds()
    after = _ledger_view(session_ledger.read_state(CALLER_KEY, "u2"))

    joined = ("written under the alias", "written canonically", ["alias-event", "canon-event"])
    assert carried == joined
    assert after == joined
    assert canonical == joined
    assert not CrewLog.exists(lg.KIND_SESSION, "u1")
    assert _newest_lifecycle("ua")["type"] == "session/opened"


@pytest.mark.usefixtures("carrying")
def test_a_joined_spellings_units_carry_in_succession_after_a_backward_clock_step(monkeypatch):
    """One spelling's units fold in their own succession, not their header clock's order.

    ``jb`` follows ``ja`` under the caller's spelling but the clock stepped back, so its
    header is stamped earlier and the header listing puts it first. The carry into ``j2``
    must still end on what ``jb`` recorded, or it replays ``ja`` after ``jb`` and brings
    back the older goal.
    """
    from kiro_crew import session_ledger

    stamp = [1_700_000_000_000]
    monkeypatch.setattr(store, "now_ms", lambda: stamp[0])  # flake-ok: fixed clock pin
    emit.on_session_opened("ja", agent="kirocrew", slot=CALLER_KEY)
    assert_drained()
    session_ledger.record_update(CALLER_KEY, session_id="ja", goal="older goal", event="ja-event")
    stamp[0] = 1_650_000_000_000
    emit.on_session_opened("jb", agent="kirocrew", slot=CALLER_KEY, previous_sid="ja")
    assert_drained()
    session_ledger.record_update(CALLER_KEY, session_id="jb", goal="newer goal", event="jb-event")
    stamp[0] = 1_750_000_000_000
    emit.on_session_opened("j1", agent="kirocrew", slot=SLOT)
    assert_drained()
    session_ledger.record_update(
        CALLER_KEY, session_id="j1", next_step="canonical", event="j1-event"
    )
    assert_drained()
    assert store.session_units_for_slot(CALLER_KEY) == ("jb", "ja"), "the control"
    emit.on_session_opened(
        "j2", agent="kirocrew", slot=SLOT, previous_sid="j1", session_key=SLOT_SESSION_KEY
    )
    assert_drained()
    projection.forget_slot_folds()

    carried = session_ledger.read_state(CALLER_KEY, "j2")
    assert carried["goal"] == "newer goal"
    assert [event["text"] for event in carried["events"]] == ["ja-event", "jb-event", "j1-event"]


@pytest.mark.usefixtures("carrying")
def test_a_spelling_only_read_under_keeps_reading_across_the_carry():
    """The join is recorded by a live read too, so a spelling nobody writes under is kept.

    ``u1`` is recorded into under the slot's own spelling, and the caller keyed
    ``cron:<id>`` only reads. Its read joins ``ua``, which holds the goal, and nothing
    else would tell the carry that it does: ``u2``'s opening states the slot's own key.
    """
    from kiro_crew import session_ledger

    emit.on_session_opened("ua", agent="kirocrew", slot=CALLER_KEY)
    assert_drained()
    session_ledger.record_update(
        CALLER_KEY, session_id="ua", goal="written under the alias", event="alias-event"
    )
    emit.on_session_opened("u1", agent="kirocrew", slot=SLOT)
    assert_drained()
    session_ledger.record_update(SLOT, session_id="u1", next_step="written canonically")
    assert_drained()
    assert session_ledger._recorded_aliases(SLOT) == ()
    projection.forget_slot_folds()
    before = _ledger_view(session_ledger.read_state(CALLER_KEY, "u1"))
    assert session_ledger._recorded_aliases(SLOT) == (CALLER_KEY,)
    emit.on_session_opened(
        "u2", agent="kirocrew", slot=SLOT, previous_sid="u1", session_key=SLOT_SESSION_KEY
    )
    assert_drained()
    assert _sweep(time.time() + 60) == (1, 0)
    projection.forget_slot_folds()

    assert before == ("written under the alias", "written canonically", ["alias-event"])
    assert _ledger_view(session_ledger.read_state(CALLER_KEY, "u2")) == before


@pytest.mark.usefixtures("carrying")
def test_a_channel_keys_raw_spelling_keeps_reading_across_the_carry_with_no_live_session():
    """A nudge read of ``slack:<ts>`` joins the units an older build headed with that key.

    Nothing was recorded under the raw key into the units the slot heads now, so the only
    record that the read joins them is ``ua``'s header, whose filename fold is the slot.
    """
    from kiro_crew import session_ledger

    emit.on_session_opened("ua", agent="kirocrew", slot=CHANNEL_KEY)
    assert_drained()
    session_ledger.record_update(CHANNEL_KEY, session_id="ua", goal="old goal", event="old-event")
    emit.on_session_opened("u1", agent="kirocrew", slot=CHANNEL_SLOT)
    assert_drained()
    session_ledger.record_update(
        CHANNEL_SLOT, session_id="u1", next_step="current next", event="new-event"
    )
    assert_drained()
    projection.forget_slot_folds()
    before = _ledger_view(session_ledger.read_state(CHANNEL_KEY))
    # The opening states the tab's own key, so only ``ua``'s header can join the raw key.
    emit.on_session_opened(
        "u2",
        agent="kirocrew",
        slot=CHANNEL_SLOT,
        previous_sid="u1",
        session_key=f"dashboard:{CHANNEL_SLOT}",
    )
    assert_drained()
    projection.forget_slot_folds()

    carried = _ledger_view(session_ledger.read_state(CHANNEL_KEY))
    assert _sweep(time.time() + 60) == (1, 0)
    projection.forget_slot_folds()
    after = _ledger_view(session_ledger.read_state(CHANNEL_KEY))

    assert before == ("old goal", "current next", ["old-event", "new-event"])
    assert carried == before
    assert after == before
    assert session_ledger._recorded_aliases(CHANNEL_SLOT) == ()
    assert not CrewLog.exists(lg.KIND_SESSION, "u1")
    assert CrewLog.exists(lg.KIND_SESSION, "ua")


@pytest.mark.usefixtures("carrying")
@pytest.mark.parametrize(
    ("caller", "slot"),
    [("cron:42", "cron-42"), ("taskrunner:run:7", "taskrunner_run_7")],
    ids=["cron-tab", "filename-fold"],
)
def test_a_spelling_an_older_build_wrote_under_keeps_reading_across_the_first_carry(caller, slot):
    """A caller key the gateway names the slot from is found on the unit headers.

    An older build wrote these records but never the slot's ``aliases`` file, so the file
    is removed before the slot opens its next unit, and nothing writes or reads under the
    caller's spelling in between: the first carry after an upgrade. The alias unit is
    headed with the caller's own key, and the slot is the one the gateway names for that
    key, the ``cron-<id>`` tab for a cron key and the filename fold for any other, so the
    carry joins it with nothing recorded and the caller's read keeps its records.

    Unit ids are per case: two cases can be handed the same temporary data home path, and
    the store's header memo would then give the second one the first one's header for a
    unit of the same id.
    """
    from kiro_crew import session_ledger

    alias_unit, older, newest = f"{slot}-ua", f"{slot}-u1", f"{slot}-u2"
    emit.on_session_opened(alias_unit, agent="kirocrew", slot=caller)
    assert_drained()
    session_ledger.record_update(
        caller, session_id=alias_unit, goal="written under the alias", event="alias-event"
    )
    emit.on_session_opened(older, agent="kirocrew", slot=slot)
    assert_drained()
    session_ledger.record_update(
        caller, session_id=older, next_step="written canonically", event="canon-event"
    )
    assert_drained()
    (session_ledger.control_dir(slot) / session_ledger._ALIAS_FILE).unlink()
    assert session_ledger._recorded_aliases(slot) == ()
    # The opening states the tab's own key, so only the alias unit's header can join the
    # caller's spelling.
    emit.on_session_opened(
        newest, agent="kirocrew", slot=slot, previous_sid=older, session_key=f"dashboard:{slot}"
    )
    assert_drained()
    projection.forget_slot_folds()

    carried = _ledger_view(session_ledger.read_state(caller, newest))
    canonical = _ledger_view(session_ledger.read_state(slot))
    assert _newest_lifecycle(older)["data"] == {"reason": "superseded", "successor": newest}
    assert _sweep(time.time() + 60) == (1, 0)
    projection.forget_slot_folds()
    after = _ledger_view(session_ledger.read_state(caller, newest))

    joined = ("written under the alias", "written canonically", ["alias-event", "canon-event"])
    assert carried == joined
    assert after == joined
    assert canonical == joined
    assert not CrewLog.exists(lg.KIND_SESSION, older)
    assert _newest_lifecycle(alias_unit)["type"] == "session/opened"


@pytest.mark.usefixtures("carrying")
@pytest.mark.parametrize(
    ("caller", "session_key", "slot"),
    [
        ("chat-x", "dashboard:chat-x", "workflow-run2"),
        (CHANNEL_KEY, CHANNEL_KEY, "workflow-run1"),
        (CALLER_KEY, CALLER_KEY, "chat-real-slot"),
    ],
    ids=["workflow-fallback-of-a-dashboard-chat", "other-name-channel", "other-name-cron"],
)
def test_the_key_the_slots_turns_run_under_keeps_reading_across_the_first_carry(
    caller, session_key, slot
):
    """The new unit's opening names the key its turns run under, and the carry joins it.

    The shipped shape is a workflow's fallback tab: ``workflow-<run>``, linked to the gone
    originating chat's ``dashboard:chat-x``, whose own tab headed its units ``chat-x``.
    Its turns run under that key, so a ledger caller in it is keyed ``chat-x`` and its
    read folds the ``chat-x`` units first. No header names the slot from that key, and an
    older build never wrote the slot's ``aliases`` file, so the file is removed before the
    slot opens its next unit and nothing reads or writes under the caller's spelling in
    between: the first carry after an upgrade. The opening states ``dashboard:chat-x``
    as the dashboard's turn admission does, so the carry joins the ``chat-x`` units and
    the caller's read keeps its records after the carry and after the older unit is
    collected. A channel key and a cron key on tabs named some other way are the same.

    Unit ids are per case, for the store header memo reason the test above gives.
    """
    from kiro_crew import session_ledger

    tag = slot.replace(".", "-")
    alias_unit, older, newest = f"{tag}-ua", f"{tag}-u1", f"{tag}-u2"
    emit.on_session_opened(alias_unit, agent="kirocrew", slot=caller)
    assert_drained()
    session_ledger.record_update(
        caller, session_id=alias_unit, goal="written under the alias", event="alias-event"
    )
    emit.on_session_opened(older, agent="kirocrew", slot=slot)
    assert_drained()
    session_ledger.record_update(
        caller, session_id=older, next_step="written canonically", event="canon-event"
    )
    assert_drained()
    (session_ledger.control_dir(slot) / session_ledger._ALIAS_FILE).unlink()
    assert session_ledger._recorded_aliases(slot) == ()
    assert session_ledger.ledger_aliases(slot) == ()
    emit.on_session_opened(
        newest, agent="kirocrew", slot=slot, previous_sid=older, session_key=session_key
    )
    assert_drained()
    projection.forget_slot_folds()

    [opened] = [e["data"] for e in _entries(newest) if e["type"] == "session/opened"]
    carried = _ledger_view(session_ledger.read_state(caller, newest))
    canonical = _ledger_view(session_ledger.read_state(slot))
    assert _newest_lifecycle(older)["data"] == {"reason": "superseded", "successor": newest}
    assert _sweep(time.time() + 60) == (1, 0)
    projection.forget_slot_folds()
    after = _ledger_view(session_ledger.read_state(caller, newest))

    joined = ("written under the alias", "written canonically", ["alias-event", "canon-event"])
    assert opened["session_key"] == session_key
    folds = {e["data"]["fold"] for e in _entries(newest) if e["type"] == "session/carried"}
    assert "ledger" in folds
    assert carried == joined
    assert after == joined
    assert canonical == joined
    assert not CrewLog.exists(lg.KIND_SESSION, older)
    assert _newest_lifecycle(alias_unit)["type"] == "session/opened"


@pytest.mark.usefixtures("carrying")
def test_an_opening_that_states_no_session_key_carries_nothing_and_closes_nothing():
    """Without the key its turns run under, a carry could not know a reader's join.

    So a new unit whose opening states none carries no fold and closes no older unit,
    which the sweep then keeps, as it kept every unit before the carry existed.
    """
    emit.on_session_opened("u1", agent="kirocrew", slot=SLOT)
    emit.on_ledger_recorded("u1", _ledger("ship it", "design", "planned"))
    assert_drained()
    emit.on_session_opened("u2", agent="kirocrew", slot=SLOT, previous_sid="u1")
    assert_drained()

    [opened] = [e["data"] for e in _entries("u2") if e["type"] == "session/opened"]
    assert "session_key" not in opened
    assert not [e for e in _entries("u2") if e["type"] == "session/carried"]
    assert _newest_lifecycle("u1")["type"] == "session/opened"
    assert _sweep(time.time() + 60) == (0, 0)
    assert CrewLog.exists(lg.KIND_SESSION, "u1")


@pytest.mark.usefixtures("carrying")
def test_spellings_that_cannot_be_read_carry_nothing_and_close_nothing(monkeypatch):
    """Without them the ledger's carry would drop what their units hold, so nothing moves."""
    from kiro_crew import session_ledger

    def _unreadable(slot_key: str) -> tuple:
        raise OSError("the spellings cannot be read")

    monkeypatch.setattr(session_ledger, "_recorded_aliases", _unreadable)
    _two_units_of_one_slot()

    assert not [e for e in _entries("u2") if e["type"] == "session/carried"]
    assert _newest_lifecycle("u1")["type"] == "session/opened"


@pytest.mark.usefixtures("carrying")
def test_a_gone_row_reads_the_same_once_the_older_unit_is_collected():
    """``status`` is carried, because a gone row decides ``lost`` or ``closed`` on it.

    ``u1``'s last turn was cancelled, then ``u2`` opened for the slot and its tab was
    closed before ``u2`` ran a turn. The row reads ``lost`` from ``u1``'s turn and must
    still read it once ``u1`` is collected: ``closed`` tells a conductor the worker
    finished, so it stops looking for it.
    """
    from kiro_crew.dashboard.session_control_parts.session_status import _gone_slot_lifecycles

    emit.on_session_opened("u1", agent="kirocrew", slot=SLOT)
    emit.on_turn_started("u1", 1)
    emit.on_turn_completed("u1", 1, stop_reason="cancelled", model="model-a", provider="p-a")
    assert_drained()
    emit.on_session_opened(
        "u2", agent="kirocrew", slot=SLOT, previous_sid="u1", session_key=SLOT_SESSION_KEY
    )
    assert_drained()
    emit.on_session_closed("u2", "removed")
    assert_drained()
    projection.forget_slot_folds()
    before = projection.read_slot_projection(SLOT, "status").value
    assert _gone_slot_lifecycles([SLOT]) == {SLOT: {"status": "lost"}}

    assert _sweep(time.time() + 60) == (1, 0)
    projection.forget_slot_folds()

    assert not CrewLog.exists(lg.KIND_SESSION, "u1")
    assert _gone_slot_lifecycles([SLOT]) == {SLOT: {"status": "lost"}}
    after = projection.read_slot_projection(SLOT, "status").value
    for key in (
        "lifecycle",
        "close_reason",
        "turn_open",
        "turns_completed",
        "last_stop_reason",
        "model",
        "provider",
    ):
        assert after[key] == before[key], key
    assert before["turns_completed"] == 1


@pytest.mark.usefixtures("carrying")
def test_a_turn_the_new_unit_runs_while_the_carry_folds_is_in_the_carry(monkeypatch):
    """The carry continues each state over what the new unit appended after the fold read.

    The fold runs on the maintenance pool while the new unit keeps writing, so its first
    turn can land between the fold's read and the carry, which then lands after it and
    replaces the slot's state. Here ``u1``'s turn ended cleanly and ``u2``'s turn, run in
    exactly that window, was cancelled. A carry of the state the fold read would drop
    ``u2``'s turn: one turn, ``end_turn``, and a gone row reading ``closed`` for a worker
    whose last turn was cancelled, which tells a conductor to stop looking for it.

    The fold is run here, off the writer as the pool runs it, and ``u2``'s turn is on
    disk before the fold hands the carry to the writer.
    """
    from kiro_crew.dashboard.session_control_parts.session_status import _gone_slot_lifecycles

    emit.on_session_opened("u1", agent="kirocrew", slot=SLOT)
    emit.on_turn_started("u1", 1)
    emit.on_turn_completed("u1", 1, stop_reason="end_turn", model="model-a", provider="p-a")
    assert_drained()
    folds: list = []
    monkeypatch.setattr(emit, "_submit_carry_fold", folds.append)
    emit.on_session_opened(
        "u2", agent="kirocrew", slot=SLOT, previous_sid="u1", session_key=SLOT_SESSION_KEY
    )
    assert_drained()
    assert len(folds) == 1
    read_states = projection.carried_fold_states

    def _read_then_u2_runs_a_turn(slot: str, newest: str):
        states = read_states(slot, newest)
        emit.on_turn_started("u2", 1)
        emit.on_turn_completed("u2", 1, stop_reason="cancelled", model="model-b", provider="p-b")
        assert_drained()
        return states

    monkeypatch.setattr(projection, "carried_fold_states", _read_then_u2_runs_a_turn)
    folds[0]()
    assert_drained()

    types = [e["type"] for e in _entries("u2")]
    assert types.index("turn/completed") < types.index("session/carried"), types
    assert _newest_lifecycle("u1")["data"] == {"reason": "superseded", "successor": "u2"}
    emit.on_session_closed("u2", "removed")
    assert_drained()
    projection.forget_slot_folds()
    before = projection.read_slot_projection(SLOT, "status").value
    gone_before = _gone_slot_lifecycles([SLOT])
    assert _sweep(time.time() + 60) == (1, 0)
    projection.forget_slot_folds()
    after = projection.read_slot_projection(SLOT, "status").value

    for value in (before, after):
        assert value["turns_completed"] == 2
        assert value["last_stop_reason"] == "cancelled"
        assert value["model"] == "model-b"
    assert gone_before == {SLOT: {"status": "lost"}}
    assert _gone_slot_lifecycles([SLOT]) == {SLOT: {"status": "lost"}}


def _open_next(unit_id: str, previous: str) -> None:
    emit.on_session_opened(
        unit_id, agent="kirocrew", slot=SLOT, previous_sid=previous, session_key=SLOT_SESSION_KEY
    )
    assert_drained()


def _assert_only_u3_carries_the_slot() -> None:
    """``u3`` carries the slot and closes ``u1`` and ``u2``; every event reads once, then too."""
    from kiro_crew import session_ledger

    assert not [e for e in _entries("u2") if e["type"] == "session/carried"]
    assert [e for e in _entries("u3") if e["type"] == "session/carried"]
    for unit_id in ("u1", "u2"):
        assert _newest_lifecycle(unit_id)["data"] == {"reason": "superseded", "successor": "u3"}
    assert _newest_lifecycle("u3")["type"] == "session/opened"
    assert session_ledger.crew_log_units(SLOT)[-1] == "u3"
    projection.forget_slot_folds()
    before = session_ledger.read_state(SLOT)
    assert _sweep(time.time() + 60) == (2, 0)
    projection.forget_slot_folds()
    after = session_ledger.read_state(SLOT)
    for state in (before, after):
        assert [event["text"] for event in state["events"]] == ["planned", "started", "reviewed"]
        assert state["goal"] == "ship it"


@pytest.mark.usefixtures("carrying")
def test_a_carry_whose_slot_moved_on_before_it_folded_stands_down(monkeypatch):
    """A slot that moves twice before the first carry folds is carried by its newest unit.

    ``u2``'s fold is held back while the slot moves on to ``u3`` and ``u3`` records. Folded
    then, ``u2``'s carry would fold ``u3``'s event BEFORE ``u2``'s, close ``u3`` and be
    published last, and ``u3``'s own carry, folding ``u2``'s, would read that event twice.
    So ``u2`` folds nothing and writes nothing, and ``u3`` carries the slot.
    """
    from kiro_crew import session_ledger

    emit.on_session_opened("u1", agent="kirocrew", slot=SLOT)
    assert_drained()
    session_ledger.record_update(SLOT, session_id="u1", goal="ship it", event="planned")
    assert_drained()
    folds: list = []
    monkeypatch.setattr(emit, "_submit_carry_fold", folds.append)
    _open_next("u2", "u1")
    session_ledger.record_update(SLOT, session_id="u2", event="started")
    _open_next("u3", "u2")
    session_ledger.record_update(SLOT, session_id="u3", event="reviewed")
    assert_drained()
    assert len(folds) == 2
    read_states = projection.carried_fold_states
    folded: list[str] = []

    def _noting(slot: str, newest: str, *args, **kwargs):
        folded.append(newest)
        return read_states(slot, newest, *args, **kwargs)

    monkeypatch.setattr(projection, "carried_fold_states", _noting)
    for fold in folds:
        fold()
        assert_drained()

    _assert_only_u3_carries_the_slot()
    assert folded == ["u3"]


@pytest.mark.usefixtures("carrying")
def test_a_carry_whose_slot_moved_on_while_it_folded_writes_nothing(monkeypatch):
    """The writer asks again, since a later unit can open while the fold runs off it.

    ``u3`` opens, records and carries between ``u2``'s fold and its write. Written then,
    ``u2``'s carry would land after ``u3``'s and be published last in the ledger's order
    file, so a read would fold the state ``u2``'s fold saw last and lose ``u3``'s event.
    """
    from kiro_crew import session_ledger

    emit.on_session_opened("u1", agent="kirocrew", slot=SLOT)
    assert_drained()
    session_ledger.record_update(SLOT, session_id="u1", goal="ship it", event="planned")
    assert_drained()
    folds: list = []
    monkeypatch.setattr(emit, "_submit_carry_fold", folds.append)
    _open_next("u2", "u1")
    session_ledger.record_update(SLOT, session_id="u2", event="started")
    assert_drained()
    assert len(folds) == 1
    read_states = projection.carried_fold_states

    def _read_then_the_slot_moves_on(slot: str, newest: str, *args, **kwargs):
        states = read_states(slot, newest, *args, **kwargs)
        if newest == "u2":
            _open_next("u3", "u2")
            session_ledger.record_update(SLOT, session_id="u3", event="reviewed")
            assert_drained()
            assert len(folds) == 2
            folds[1]()
            assert_drained()
        return states

    monkeypatch.setattr(projection, "carried_fold_states", _read_then_the_slot_moves_on)
    folds[0]()
    assert_drained()

    _assert_only_u3_carries_the_slot()


def test_every_older_unit_is_closed_not_only_the_named_predecessor(carrying):
    """A slot whose earlier units predate the carry has them all closed by the next one.

    The probe is off while ``u1`` and ``u2`` open -- the units a build before this one
    left behind -- and on when ``u3`` names only ``u2``.
    """
    probe = emit._session_key_probe[0]
    emit.set_session_key_probe(None)
    emit.on_session_opened("u1", agent="kirocrew", slot=SLOT)
    emit.on_session_opened("u2", agent="kirocrew", slot=SLOT)
    assert_drained()
    emit.set_session_key_probe(probe)
    emit.on_session_opened(
        "u3", agent="kirocrew", slot=SLOT, previous_sid="u2", session_key=SLOT_SESSION_KEY
    )
    assert_drained()

    for unit_id in ("u1", "u2"):
        assert _newest_lifecycle(unit_id)["data"] == {"reason": "superseded", "successor": "u3"}
    assert _newest_lifecycle("u3")["type"] == "session/opened"


def test_a_unit_a_key_still_maps_is_not_closed(carrying):
    carrying["u1"] = "dashboard:imported-copy"
    _two_units_of_one_slot()

    assert _newest_lifecycle("u1")["type"] == "session/opened"
    assert _sweep(time.time() + 60) == (0, 0)
    assert CrewLog.exists(lg.KIND_SESSION, "u1")


def test_an_unreadable_map_withholds_the_close(carrying):
    carrying["u1"] = RuntimeError
    _two_units_of_one_slot()

    assert _newest_lifecycle("u1")["type"] == "session/opened"


@pytest.mark.usefixtures("carrying")
def test_without_a_probe_nothing_is_carried_and_nothing_is_closed():
    emit.set_session_key_probe(None)
    _two_units_of_one_slot()

    assert not [e for e in _entries("u2") if e["type"] == "session/carried"]
    assert _newest_lifecycle("u1")["type"] == "session/opened"


@pytest.mark.usefixtures("carrying")
def test_a_live_turn_on_the_older_unit_stands_the_carry_down():
    emit.on_session_opened("u1", agent="kirocrew", slot=SLOT)
    emit.on_ledger_recorded("u1", _ledger("ship it", "design", "planned"))
    emit.on_turn_started("u1", 1)
    assert_drained()
    assert emit.live_turn("u1")
    emit.on_session_opened(
        "u2", agent="kirocrew", slot=SLOT, previous_sid="u1", session_key=SLOT_SESSION_KEY
    )
    assert_drained()

    assert not [e for e in _entries("u2") if e["type"] == "session/carried"]
    assert _newest_lifecycle("u1")["type"] == "session/opened"


@pytest.mark.usefixtures("carrying")
def test_a_turn_the_older_unit_ends_while_the_carry_folds_is_not_lost(monkeypatch):
    """An older unit whose turn ends between the fold's read and the carry keeps that end.

    ``u1``'s turn is still running when ``u2`` opens, so the fold reads it open, and it
    ends before the writer runs the carry, so the live-turn check there finds nothing. A
    carry of the state the fold read would replace ``u1``'s ``turn/completed`` with the
    open turn, and the superseded close after it lets retention take the only record of
    that end. The turn ends once whichever way the carry runs: inside the fold's read
    when there is one, else right after the fold job returns.
    """
    emit.on_session_opened("u1", agent="kirocrew", slot=SLOT)
    emit.on_turn_started("u1", 1)
    assert_drained()
    assert emit.live_turn("u1")
    folds: list = []
    monkeypatch.setattr(emit, "_submit_carry_fold", folds.append)
    emit.on_session_opened(
        "u2", agent="kirocrew", slot=SLOT, previous_sid="u1", session_key=SLOT_SESSION_KEY
    )
    assert_drained()
    assert len(folds) == 1
    read_states = projection.carried_fold_states
    ended: list[bool] = []

    def _end_u1s_turn() -> None:
        if not ended:
            ended.append(True)
            emit.on_turn_completed("u1", 1, stop_reason="end_turn", model="model-a", provider="p-a")
            assert_drained()

    def _read_then_u1s_turn_ends(slot: str, newest: str, *args, **kwargs):
        states = read_states(slot, newest, *args, **kwargs)
        _end_u1s_turn()
        return states

    monkeypatch.setattr(projection, "carried_fold_states", _read_then_u1s_turn_ends)
    folds[0]()
    _end_u1s_turn()
    assert_drained()

    assert not emit.live_turn("u1")
    projection.forget_slot_folds()
    before = projection.read_slot_projection(SLOT, "status").value
    # Any moment after every close: a constant rather than a clock read.
    _sweep(4_000_000_000.0)
    projection.forget_slot_folds()
    after = projection.read_slot_projection(SLOT, "status").value

    for value in (before, after):
        assert value["turns_completed"] == 1, value
        assert value["last_stop_reason"] == "end_turn", value
        assert value["model"] == "model-a", value


@pytest.mark.usefixtures("carrying")
def test_a_later_carry_after_a_backward_clock_step_reads_each_turn_once(monkeypatch):
    """The carried folds read the slot's units in succession, not in header-clock order.

    The clock steps back between ``u1`` and ``u2``, so ``u2``'s header is stamped before
    ``u1``'s. ``u2`` carries ``u1``, then ``u3`` opens before retention collects either.
    Read in header order, ``u3``'s carry would fold ``u2``, whose carry already holds
    ``u1``'s turn, then ``u1`` again, and keep that turn twice once both are collected.
    """
    stamp = [1_700_000_000_000]
    # Through the store facade, which writes it into store_handle's binding, the one the
    # header writer reads.
    monkeypatch.setattr(store, "now_ms", lambda: stamp[0])  # flake-ok: fixed clock pin
    emit.on_session_opened("u1", agent="kirocrew", slot=SLOT)
    emit.on_turn_started("u1", 1)
    emit.on_turn_completed("u1", 1, stop_reason="end_turn", model="model-a", provider="p-a")
    assert_drained()
    stamp[0] = 1_650_000_000_000
    _open_next("u2", "u1")
    stamp[0] = 1_750_000_000_000
    _open_next("u3", "u2")
    assert store.session_units_for_slot(SLOT) == ("u2", "u1", "u3"), "the control"
    for unit_id in ("u1", "u2"):
        assert _newest_lifecycle(unit_id)["type"] == "session/closed"

    # Any moment after every close: a constant rather than a clock read.
    assert _sweep(4_000_000_000.0) == (2, 0)
    projection.forget_slot_folds()
    after = projection.read_slot_projection(SLOT, "status").value

    assert after["turns_completed"] == 1, after
    assert after["last_stop_reason"] == "end_turn", after


#: The carried folds whose owner keeps a recorded order of the units written into, which
#: places a unit the order does not name by succession (``_causal_units``).
_RECORDED_ORDER_FOLDS = frozenset({"ledger", "work", "panel", "workstreams", "worktree"})


def test_every_carried_fold_reads_its_units_in_succession_not_the_header_clock(monkeypatch):
    """Each carried fold's carry is folded over the list its reader folds, in succession.

    The header clock lists ``new`` first, as a backward clock step does. A carry folded in
    that order would read ``old`` after a carry that already holds it. ``ledger`` needs a
    unit's opening to name its join and ``worker`` reads the session tree, so each is
    pinned by its own test (``test_crew_log_workers_fold.py`` for ``worker``). The fold
    names are read in the body, not at collection, so a tree without this fold set still
    collects the rest of the file.
    """
    monkeypatch.setattr(store, "session_units_for_slot", lambda slot, **_kw: ("new", "old"))
    monkeypatch.setattr(projection, "units_in_succession", lambda slot: ("old", "new"))
    monkeypatch.setattr(projection, "slot_succession", lambda slot: ("old", "new"))

    names = sorted(projection.CARRIED_FOLD_NAMES - {"ledger", "worker"})
    assert names, "no carried fold to check"
    for name in names:
        assert projection._carried_units(SLOT, name, "new") == ("old", "new"), name
        if name not in _RECORDED_ORDER_FOLDS:
            assert projection._slot_units_for_fold(SLOT, name) == ("old", "new"), name


@pytest.mark.parametrize(
    ("recorded", "units", "expected"),
    [
        # Nothing recorded: the slot's succession.
        ((), ("u2", "u1", "u3"), ("u1", "u2", "u3")),
        # A carrying unit the order does not name stays after the units its carry holds,
        # once the slot opens a later unit too.
        (("u1",), ("u2", "u1", "u3"), ("u1", "u2", "u3")),
        (("u1", "u3"), ("u2", "u1", "u3", "u4"), ("u1", "u2", "u3", "u4")),
        # An older unit evicted from the bounded order still folds first.
        (("u2", "u3"), ("u3", "u1", "u2"), ("u1", "u2", "u3")),
        # A joined spelling's unit, in no succession of this slot, folds first.
        (("u1",), ("ua", "u2", "u1"), ("ua", "u1", "u2")),
        # The recorded order stands where it and the succession disagree.
        (("u2", "u1"), ("u1", "u2"), ("u2", "u1")),
    ],
)
def test_a_unit_the_recorded_order_does_not_name_is_placed_by_succession(
    monkeypatch, recorded, units, expected
):
    from kiro_crew.session_ledger_control import _causal_units

    monkeypatch.setattr(projection, "slot_succession", lambda slot: ("u1", "u2", "u3", "u4"))

    assert _causal_units(SLOT, units, recorded) == expected


@pytest.mark.usefixtures("carrying")
def test_a_carry_its_reader_would_not_fold_last_stands_down(monkeypatch):
    """A carry is folded in its reader's order, never with the new unit moved to the end.

    ``u1`` records after ``u2`` did, before ``u2``'s fold runs, so the ledger's order file
    lists ``u2`` first and every reader folds ``u1`` after it. A carry folded with ``u2``
    moved last would hold ``u1``'s record, and each of those reads would lay it over the
    carry a second time. So nothing is carried and ``u1`` stays.
    """
    from kiro_crew import session_ledger

    emit.on_session_opened("u1", agent="kirocrew", slot=SLOT)
    assert_drained()
    session_ledger.record_update(SLOT, session_id="u1", goal="ship it", event="planned")
    assert_drained()
    folds: list = []
    monkeypatch.setattr(emit, "_submit_carry_fold", folds.append)
    _open_next("u2", "u1")
    session_ledger.record_update(SLOT, session_id="u2", event="started")
    assert_drained()
    session_ledger.record_update(SLOT, session_id="u1", event="late")
    assert_drained()
    assert session_ledger.crew_log_units(SLOT) == ("u2", "u1"), "the control"
    folds[0]()
    assert_drained()

    assert not [e for e in _entries("u2") if e["type"] == "session/carried"]
    assert _newest_lifecycle("u1")["type"] == "session/opened"
    projection.forget_slot_folds()
    events = [event["text"] for event in session_ledger.read_state(SLOT)["events"]]
    assert sorted(events) == ["late", "planned", "started"]


@pytest.mark.usefixtures("carrying")
def test_an_entry_an_older_unit_gains_while_the_carry_folds_folds_the_carry_again(monkeypatch):
    """A record that lands in an older unit between the fold's read and the write is carried.

    No turn runs on ``u1``, so no live-turn check sees anything, but the record moves its
    tail. Written then, the carry would hold the state read before the record, replace it
    in every read, and close ``u1`` for retention to take the only copy. The writer sees
    the tail moved, and the carry folds again before it writes.
    """
    from kiro_crew import session_ledger

    emit.on_session_opened("u1", agent="kirocrew", slot=SLOT)
    assert_drained()
    session_ledger.record_update(SLOT, session_id="u1", goal="ship it", event="planned")
    assert_drained()
    folds: list = []
    monkeypatch.setattr(emit, "_submit_carry_fold", folds.append)
    _open_next("u2", "u1")
    assert len(folds) == 1
    read_states = projection.carried_fold_states
    recorded: list[bool] = []

    def _read_then_u1_records(slot: str, newest: str, *args, **kwargs):
        states = read_states(slot, newest, *args, **kwargs)
        if not recorded:
            recorded.append(True)
            session_ledger.record_update(SLOT, session_id="u1", event="late")
            assert_drained()
        return states

    monkeypatch.setattr(projection, "carried_fold_states", _read_then_u1_records)
    folds[0]()
    assert_drained()
    assert not [e for e in _entries("u2") if e["type"] == "session/carried"]
    assert _newest_lifecycle("u1")["type"] == "session/opened"
    assert len(folds) == 2, "the carry folds again"
    folds[1]()
    assert_drained()

    assert [e for e in _entries("u2") if e["type"] == "session/carried"]
    assert _newest_lifecycle("u1")["data"] == {"reason": "superseded", "successor": "u2"}
    # Any moment after the close: a constant rather than a clock read.
    assert _sweep(4_000_000_000.0) == (1, 0)
    projection.forget_slot_folds()
    after = session_ledger.read_state(SLOT)
    assert [event["text"] for event in after["events"]] == ["planned", "late"]


@pytest.mark.usefixtures("carrying")
def test_a_carry_whose_older_unit_keeps_changing_stands_down(monkeypatch):
    """The refold is bounded: past it the carry writes nothing and closes nothing."""
    from kiro_crew import session_ledger

    emit.on_session_opened("u1", agent="kirocrew", slot=SLOT)
    assert_drained()
    session_ledger.record_update(SLOT, session_id="u1", goal="ship it", event="planned")
    assert_drained()
    folds: list = []
    monkeypatch.setattr(emit, "_submit_carry_fold", folds.append)
    _open_next("u2", "u1")
    read_states = projection.carried_fold_states

    def _read_then_u1_records(slot: str, newest: str, *args, **kwargs):
        states = read_states(slot, newest, *args, **kwargs)
        session_ledger.record_update(SLOT, session_id="u1", event=f"late-{len(folds)}")
        assert_drained()
        return states

    monkeypatch.setattr(projection, "carried_fold_states", _read_then_u1_records)
    ran = 0
    while ran < len(folds):
        assert ran <= emit._CARRY_REFOLDS, "a carry folds again a bounded number of times"
        folds[ran]()
        assert_drained()
        ran += 1

    assert ran == emit._CARRY_REFOLDS + 1
    assert not [e for e in _entries("u2") if e["type"] == "session/carried"]
    assert _newest_lifecycle("u1")["type"] == "session/opened"


@pytest.mark.usefixtures("carrying")
def test_a_record_a_joined_spelling_gains_while_the_carry_folds_is_carried(monkeypatch):
    """The tail check covers every unit the folds read, a joined spelling's units too.

    ``ua`` is headed with the caller's own key, which the ledger carry joins. A record it
    gains while the carry folds is folded before the carry by a read under that key, so a
    carry of the state read before it would hide the record from that read for good.
    """
    from kiro_crew import session_ledger

    emit.on_session_opened("ua", agent="kirocrew", slot=CALLER_KEY)
    assert_drained()
    session_ledger.record_update(
        CALLER_KEY, session_id="ua", goal="written under the alias", event="alias-event"
    )
    emit.on_session_opened("u1", agent="kirocrew", slot=SLOT)
    assert_drained()
    session_ledger.record_update(
        CALLER_KEY, session_id="u1", next_step="written canonically", event="canon-event"
    )
    assert_drained()
    folds: list = []
    monkeypatch.setattr(emit, "_submit_carry_fold", folds.append)
    _open_next("u2", "u1")
    read_states = projection.carried_fold_states
    recorded: list[bool] = []

    def _read_then_ua_records(slot: str, newest: str, *args, **kwargs):
        states = read_states(slot, newest, *args, **kwargs)
        if not recorded:
            recorded.append(True)
            session_ledger.record_update(CALLER_KEY, session_id="ua", event="late-alias-event")
            assert_drained()
        return states

    monkeypatch.setattr(projection, "carried_fold_states", _read_then_ua_records)
    folds[0]()
    assert_drained()
    assert len(folds) == 2, "the carry folds again"
    folds[1]()
    assert_drained()
    projection.forget_slot_folds()

    events = _ledger_view(session_ledger.read_state(CALLER_KEY, "u2"))[2]
    assert sorted(events) == ["alias-event", "canon-event", "late-alias-event"]


@pytest.mark.usefixtures("carrying")
def test_an_older_unit_that_gains_an_entry_after_the_carry_read_it_is_not_closed():
    """A job already queued in the older unit's bucket can append before its close runs."""
    from kiro_crew.crew_log.writer import WriteJob

    probe = emit._session_key_probe[0]
    emit.set_session_key_probe(None)
    _two_units_of_one_slot()
    emit.set_session_key_probe(probe)
    read = emit._unit_tail("u1")
    emit.on_ledger_recorded("u1", _ledger("ship it", "build", "late"))
    assert_drained()

    def _close(tail: tuple) -> None:
        emit._hand_over(
            "u1",
            WriteJob.follow_on(
                functools.partial(emit._close_superseded, "u1", SLOT, "u2", tail),
                "a test's superseded close",
            ),
        )
        assert_drained()

    _close(read)
    assert _newest_lifecycle("u1")["type"] == "session/opened"
    _close(emit._unit_tail("u1"))
    assert _newest_lifecycle("u1")["data"] == {"reason": "superseded", "successor": "u2"}


@pytest.mark.usefixtures("carrying")
def test_a_slots_first_unit_writes_no_carry():
    """Unless its ledger readers join another spelling's units (test_crew_log_retention_joins.py)."""
    emit.on_session_opened("u1", agent="kirocrew", slot=SLOT)
    emit.on_ledger_recorded("u1", _ledger("ship it", "design", "planned"))
    assert_drained()

    assert not [e for e in _entries("u1") if e["type"] == "session/carried"]


def _record_then_carry_then_late_write(
    monkeypatch: pytest.MonkeyPatch, *, gap: bool
) -> "tuple[dict, dict]":
    """``u1`` records twice, ``u2`` carries, then a record filed under ``u1`` lands late.

    Returns (state before the late write, state after it), each read cold and unpinned.
    With *gap* the carry lands INSIDE the late record's own call, after its unit was
    chosen and before its append, by opening ``u2`` from the alias note that precedes it.
    """
    from kiro_crew import session_ledger

    emit.on_session_opened("u1", agent="kirocrew", slot=SLOT)
    assert_drained()
    session_ledger.record_update(SLOT, session_id="u1", goal="ship it", event="planned")
    session_ledger.record_update(SLOT, session_id="u1", next_step="write tests", event="started")
    assert_drained()

    def _open_u2() -> None:
        emit.on_session_opened(
            "u2", agent="kirocrew", slot=SLOT, previous_sid="u1", session_key=SLOT_SESSION_KEY
        )
        assert_drained()

    before: dict = {}
    if gap:
        real = session_ledger.note_ledger_alias
        opened: list[bool] = []

        def _alias_then_carry(slot_key: str, alias: str) -> None:
            real(slot_key, alias)
            if not opened:
                opened.append(True)
                _open_u2()

        monkeypatch.setattr(session_ledger, "note_ledger_alias", _alias_then_carry)
        session_ledger.record_update(SLOT, session_id="u1", event="late")
        assert opened
    else:
        _open_u2()
        projection.forget_slot_folds()
        before = session_ledger.read_state(SLOT)
        session_ledger.record_update(SLOT, session_id="u1", event="late")
    assert_drained()
    projection.forget_slot_folds()
    return before, session_ledger.read_state(SLOT)


@pytest.mark.usefixtures("carrying")
def test_a_late_record_under_the_carried_predecessor_reads_once_not_twice(monkeypatch):
    """A record filed under ``u1`` after ``u2`` carried the slot must not replay ``u1``.

    ``/api/session-ledger/record`` resolves the slot's unit, then appends on a worker
    thread, so a reset can land between: the late record still names ``u1``. Appended
    there, and published as ``u1`` recording last, it folds AFTER ``u2``'s carry and lays
    ``u1``'s whole history over the state ``u2`` carries: every event reads twice.
    """
    before, after = _record_then_carry_then_late_write(monkeypatch, gap=False)

    assert [e["text"] for e in before["events"]] == ["planned", "started"]
    assert [e["text"] for e in after["events"]] == ["planned", "started", "late"]
    assert after["goal"] == "ship it"


@pytest.mark.usefixtures("carrying")
def test_a_record_whose_carry_lands_before_its_append_reads_once_not_twice(monkeypatch):
    """The same, with the carry landing between the unit being chosen and the append."""
    _, after = _record_then_carry_then_late_write(monkeypatch, gap=True)

    assert [e["text"] for e in after["events"]] == ["planned", "started", "late"]


# --------------------------------------------------------------------------- #
# The gateway's probe
# --------------------------------------------------------------------------- #


def test_the_gateway_registers_the_probe_after_readiness_and_before_its_workers():
    """The first session a dashboard worker or a cron opens can ask the map."""
    import inspect

    from kiro_crew.slack.gateway import GatewayOrchestrator

    source = inspect.getsource(GatewayOrchestrator.run)
    ready = source.index('print(f"KIROCREW_READY:')
    register = source.index("self._register_session_key_probe()")
    workers = source.index("self._start_dashboard_workers_after_memory_ready()")
    assert ready < register < workers


def test_the_gateway_probe_reads_the_session_map_and_cannot_say_without_one():
    from types import SimpleNamespace

    from kiro_crew.slack.gateway import GatewayOrchestrator

    held = {"acp-held": "dashboard:chat-1"}
    fake = SimpleNamespace(sessions=SimpleNamespace(find_key_by_sid=held.get))
    GatewayOrchestrator._register_session_key_probe(fake)  # type: ignore[arg-type]
    try:
        assert emit._session_key_holds("acp-held") is True
        assert emit._session_key_holds("acp-free") is False
        fake.sessions = None
        assert emit._session_key_holds("acp-free") is None
    finally:
        emit.set_session_key_probe(None)
    assert emit._session_key_holds("acp-free") is None
