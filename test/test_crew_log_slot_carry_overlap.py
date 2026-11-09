"""A ledger record that overlaps a slot reset, and what the reset carries, read once.

Each test drives one interleaving of a ledger record with the successor's carry through
the real ledger writer, the live emitter and real crew logs on an isolated data home,
with the interleaving forced by a hook at the exact point it needs (no sleeps), and
states in its assertion message what a reader would see if the interleaving were lost.

The ledger's writer job decides where the entry goes, appends it and publishes the unit
order on the slot's writer lane, where the successor's carry is committed too
(``crew_log/late_writes.py``), so the hooks below run the carry before that job or
after it, never inside it.
"""

from __future__ import annotations

import itertools
import json

import pytest
from crew_log_drain import assert_drained, settle

from kiro_crew import crew_log as lg
from kiro_crew import session_ledger
from kiro_crew.crew_log import CrewLog, eager, emit, projection
from kiro_crew.crew_log import session_tree_projection as stp
from kiro_crew.crew_log import store, writer_groups
from kiro_crew.session_ledger import _store_name
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, wait_until

SLOT = "chat-late"
SLOT_SESSION_KEY = f"dashboard:{SLOT}"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    yield


@pytest.fixture
def carrying(monkeypatch):
    """The live emitter, the carry's fold inline, a session-key probe that maps nothing."""
    monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    emit.reset_caches()
    stp.reset_for_tests()
    monkeypatch.setattr(emit, "_submit_carry_fold", lambda job: job())
    emit.set_session_key_probe(lambda session_id: None)
    try:
        yield
    finally:
        emit.set_session_key_probe(None)
        settle()
        tree = stp.projection()
        stp.reset_for_tests()
        wait_until(
            lambda: not tree._write_scheduled,
            describe=lambda: "the session tree's debounced checkpoint is still sleeping",
        )


def _entries(unit_id: str) -> list[dict]:
    path = lg.crew_log_root(lg.KIND_SESSION) / _store_name(unit_id) / "log.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _start() -> None:
    """``u1`` opens for SLOT and records the slot's goal and one event."""
    emit.on_session_opened("u1", agent="kirocrew", slot=SLOT)
    assert_drained()
    session_ledger.record_update(SLOT, session_id="u1", goal="ship it", event="planned")
    assert_drained()


def _open_u2() -> None:
    """The slot resets: ``u2`` opens after ``u1`` and carries the slot's ledger."""
    emit.on_session_opened(
        "u2", agent="kirocrew", slot=SLOT, previous_sid="u1", session_key=SLOT_SESSION_KEY
    )
    assert_drained()
    assert any(
        e["type"] == "session/carried" and e["data"].get("fold") == "ledger" for e in _entries("u2")
    ), "the control: u2 carried the ledger"


def _events() -> list[str]:
    assert_drained()
    projection.forget_slot_folds()
    return [event["text"] for event in session_ledger.read_state(SLOT)["events"]]


# --------------------------------------------------------------------------- #
# No clock decides whether an update was carried
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("carrying")
def test_an_update_the_carry_already_read_is_not_filed_again_on_a_tied_stamp(monkeypatch):
    """The append lands, then the successor carries it before the caller is answered.

    Every stamp is the same millisecond. A check comparing the update's stamp with the
    carry's (``>=``) would call the update late and file it a second time under ``u2``,
    and the event would read twice.
    """
    monkeypatch.setattr(store, "now_ms", lambda: 1_700_000_000_000)  # flake-ok: fixed clock pin
    _start()
    real_flush = emit.flush
    fired: list[bool] = []

    def _flush_then_carry(*args, **kwargs):
        drained = real_flush(*args, **kwargs)
        if not fired:
            fired.append(True)
            assert _entries("u1")[-1]["data"].get("event") == "A", "the control: A landed"
            _open_u2()
        return drained

    monkeypatch.setattr(emit, "flush", _flush_then_carry)
    session_ledger.record_update(SLOT, session_id="u1", event="A")
    assert fired

    assert _events() == ["planned", "A"], "Outcome: the carried update is filed twice"


@pytest.mark.usefixtures("carrying")
def test_an_update_after_the_carry_is_kept_when_the_clock_steps_back(monkeypatch):
    """The carry lands before the append, and the clock steps back before the append.

    The update's stamp is below the carry's, so a stamp comparison would call it carried
    and leave it in ``u1``, whose state the carry replaces: the event would be lost.
    ``u1`` stays mapped, so it is not closed and only the lane's own answer moves it.
    """
    stamp = [1_700_000_000_000]
    monkeypatch.setattr(store, "now_ms", lambda: stamp[0])  # flake-ok: fixed clock pin
    # The map still names u1 (its key has not moved yet), so the carry does not close it
    # and the entry after A's ``seq_before`` is A's own.
    emit.set_session_key_probe(lambda session_id: SLOT_SESSION_KEY if session_id == "u1" else None)
    _start()
    real_alias = session_ledger.note_ledger_alias
    fired: list[bool] = []

    def _alias_then_carry_then_step_back(slot_key: str, alias: str) -> None:
        real_alias(slot_key, alias)
        if not fired:
            fired.append(True)
            _open_u2()
            stamp[0] -= 60_000

    monkeypatch.setattr(session_ledger, "note_ledger_alias", _alias_then_carry_then_step_back)
    _record, durable = session_ledger.record_update(SLOT, session_id="u1", event="A")
    assert fired

    assert durable, "the control: the caller was told the update was taken"
    assert _events() == ["planned", "A"], "Outcome: a post-carry update is lost"


# --------------------------------------------------------------------------- #
# No sampled seq stands in for this request's own entry
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("carrying")
def test_a_concurrent_records_entry_does_not_answer_for_this_ones(monkeypatch):
    """A is on its way, B appends and ``u2`` carries B, then A's append is made.

    A check reading the entry one past the seq A sampled before its append would read
    B's (stamped before the carry) and leave A in ``u1`` though it lands after the
    carry: A would be lost while A's caller is told it was taken.
    """
    ticks = itertools.count(1_700_000_000_000, 10)
    monkeypatch.setattr(store, "now_ms", lambda: next(ticks))  # flake-ok: strictly rising clock
    _start()
    real_alias = session_ledger.note_ledger_alias
    fired: list[bool] = []

    def _other_record_then_carry(slot_key: str, alias: str) -> None:
        real_alias(slot_key, alias)
        if not fired:
            fired.append(True)
            session_ledger.record_update(SLOT, session_id="u1", event="B")
            assert_drained()
            _open_u2()

    monkeypatch.setattr(session_ledger, "note_ledger_alias", _other_record_then_carry)
    _record, durable = session_ledger.record_update(SLOT, session_id="u1", event="A")
    assert fired

    assert durable, "the control: A's caller was told the update was taken"
    assert sorted(_events()) == ["A", "B", "planned"], "Outcome: A's post-carry update is lost"


# --------------------------------------------------------------------------- #
# The unit order is published on the slot's lane
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("carrying")
def test_the_order_is_published_where_the_carry_cannot_land_before_it(monkeypatch):
    """A lands in ``u1`` and ``u1`` is published while the writer holds the slot's lane.

    The successor's carry is committed on that lane too, so it cannot land between the
    check and the publication. Published after the carry instead, ``u1`` would follow its
    carrier in the order file, and an unpinned read would fold ``u1``'s history over the
    carry that already holds it.
    """
    _start()
    slot_lane = writer_groups.hold_key("u1")
    assert slot_lane == f"slot:{SLOT}", "the control: u1 is written on its slot's lane"
    real_order = session_ledger._note_unit_order
    seen: list[bool] = []

    def _publish(slot_key: str, session_id: str, **kwargs) -> None:
        if session_id == "u1" and not seen:
            # Whether a job in the successor's bucket, where its carry is committed,
            # could be claimed right now.
            holds = writer_groups.PROCESS_HOLDS
            seen.append(holds.taken("u2", slot_lane, writer_groups.hold_key))
        real_order(slot_key, session_id, **kwargs)

    monkeypatch.setattr(session_ledger, "_note_unit_order", _publish)
    session_ledger.record_update(SLOT, session_id="u1", event="A")
    _open_u2()

    assert seen == [True], "Outcome: u1 is published off the slot's lane, where u2 can carry first"
    assert session_ledger._recorded_unit_order(SLOT)[-1] == "u2", "u2 is the unit recorded last"
    assert _events() == ["planned", "A"], "Outcome: u1 replays over its carry"


@pytest.mark.usefixtures("carrying")
def test_a_carry_an_earlier_process_wrote_is_found_from_the_close_it_wrote():
    """``u2`` carried and closed ``u1`` in a process that has since gone.

    Nothing in this process saw that carry, so only ``u1``'s ``superseded`` close says the
    slot moved past it. Filed under ``u1``, the update would sit in a unit whose state
    the carry replaces, and retention may collect it: the event would be lost.
    """
    _start()
    _open_u2()
    assert store.closed_for_good(store.unit_dir_for(lg.KIND_SESSION, "u1")), "the control"
    # A new gateway process: caches gone, the files are all that is left.
    emit.reset_caches()

    _record, durable = session_ledger.record_update(SLOT, session_id="u1", event="A")

    assert durable, "the control: the caller was told the update was taken"
    assert _entries("u2")[-1]["data"].get("event") == "A", "filed under the slot's newest unit"
    assert _events() == ["planned", "A"], "Outcome: an update after the carry is lost"


# --------------------------------------------------------------------------- #
# A resumed unit's join outlives the tail window
# --------------------------------------------------------------------------- #

CHAT = "chat-x"
CHAT_KEY = "dashboard:chat-x"


def _open(unit_id: str, slot: str, *, previous: str = "", key: str = CHAT_KEY, **kwargs) -> None:
    emit.on_session_opened(
        unit_id, agent="kirocrew", slot=slot, previous_sid=previous, session_key=key, **kwargs
    )
    assert_drained()


@pytest.mark.usefixtures("carrying")
def test_a_resumed_units_join_holds_past_the_tail_window():
    """``test_an_older_unit_resumed_under_the_chats_key_joins_it_from_its_newest_opening``
    with more than ``_TAIL_WINDOW`` bytes appended after the resume.

    The resume's opening is the only record of the join, and it falls out of the newest
    segment's tail window. Read from that window alone, the sweep would collect ``a1``
    and the tab would lose the chat's goal.
    """
    import time

    from kiro_crew.crew_log.store_segment_reads import _TAIL_WINDOW

    a1, a2 = "window-a1", "window-a2"
    _open(a1, CHAT)
    session_ledger.record_update(CHAT, session_id=a1, goal="g-chat", event="e1")
    assert_drained()
    _open(a2, CHAT, previous=a1)
    session_ledger.record_update(CHAT, session_id=a2, next_step="n2", event="e2")
    assert_drained()
    assert store.closed_for_good(store.unit_dir_for(lg.KIND_SESSION, a1))
    tab, w1 = "workflow-window", "window-w1"
    _open(w1, tab, key="")
    emit.reset_caches()
    _open(w1, tab, resumed=True)
    log_file = store.unit_dir_for(lg.KIND_SESSION, w1) / "log.jsonl"
    resumed_at = log_file.stat().st_size
    for n in range(500):
        if log_file.stat().st_size - resumed_at > 2 * _TAIL_WINDOW:
            break
        session_ledger.record_update(CHAT, session_id=w1, event=f"f{n}", next_step="x" * 1500)
        assert_drained()
    assert log_file.stat().st_size - resumed_at > 2 * _TAIL_WINDOW, "the control: past the window"
    assert store.unit_opened_session_key(lg.KIND_SESSION, w1) == "", "the control: legacy"
    assert eager.drain(timeout=FALLBACK_TIMEOUT_SECS), "the eager folder never settled"

    store.sweep_expired(0, now=time.time() + 60)  # flake-ok: a moment after every close; ages only

    assert CrewLog.exists(lg.KIND_SESSION, a1), "Outcome: the chat's predecessor is collected"
    projection.forget_slot_folds()
    assert session_ledger.read_state(CHAT, w1)["goal"] == "g-chat"


# --------------------------------------------------------------------------- #
# A carried session/spawned is not a child of the unit it was carried into
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("carrying")
def test_a_reset_adds_no_child_to_the_new_units_session_tree_count(monkeypatch):
    """``c1`` created two children; the slot resets to ``c2``, which creates none.

    The carry writes ``c1``'s two ``session/spawned`` entries into ``c2`` unchanged, the
    ``otel_session_tree`` fold counts them as ``c2``'s children, and the export emits two
    child adds for ``c2`` on first sight, though ``c1`` already exported them.
    """
    from kiro_crew.crew_log.projection_otel_session_tree import (
        OTEL_SESSION_TREE_FOLD_NAME,
        _otel_session_tree_render,
    )
    from kiro_crew.metrics import crew_log_export as export

    c1, c2 = "tree-c1", "tree-c2"
    emit.on_session_opened(c1, agent="kirocrew", slot="creator", session_key="dashboard:creator")
    assert_drained()
    emit.on_session_spawned(c1, creator_slot="creator", child_slot="kid-1")
    emit.on_session_spawned(c1, creator_slot="creator", child_slot="kid-2")
    session_ledger.record_update("creator", session_id=c1, goal="g", event="e1")
    assert_drained()
    emit.on_session_opened(
        c2, agent="kirocrew", slot="creator", previous_sid=c1, session_key="dashboard:creator"
    )
    assert_drained()
    carried = [e for e in _entries(c2) if e["type"] == "session/spawned"]
    assert len(carried) == 2, "the control: the carry wrote c1's spawns into c2"

    folded = projection.fold_session(c2, (OTEL_SESSION_TREE_FOLD_NAME,))
    render = _otel_session_tree_render(folded.checkpoints[OTEL_SESSION_TREE_FOLD_NAME].state)
    # Installed before every opening, so the export takes c2 as new to this process.
    monkeypatch.setattr(export, "_install_ms", 1)  # flake-ok: an instant, not a wait
    monkeypatch.setattr(export, "_evicted_max_opened_ts", 1)
    child_adds = [
        metric
        for metric in export._otel_session_tree_diff(None, render)
        if metric.name == export.SESSION_TREE_CHILD_METRIC
    ]

    assert render["children"] == 0, "Outcome: c2 counts c1's carried children as its own"
    assert child_adds == [], "Outcome: the export adds c1's children again under c2"
